"""Company mode: prepare a team server (data, first superadmin, departments, providers, how it runs)."""
import getpass
import json
import os
import plistlib
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from setup_wizard import providers
from setup_wizard.contracts import Answer, Deploy, WizardError
from setup_wizard.files import PRIVATE_MODE, atomic_write
from setup_wizard.prompts import Option
from setup_wizard.steps import Action, Context, Plan, Step
from team_memory.cli import DEFAULT_HOST, DEFAULT_PORT, bootstrap_admin, local_url
from team_memory.contracts import DomainError
from team_memory.registry import Registry
from team_memory.replication import (
    POSTGRES_NOTE,
    SQLITE_BACKEND,
    ReplicationSettings,
    render_config,
    storage_backend,
)
from team_memory.settings import SettingsStore, load_cipher

COMPANY = frozenset(("company",))
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,64}")
HEALTH_TIMEOUT_SECONDS = 3
SERVICE_NAME = "tam-team"
LAUNCHD_LABEL = "dev.totalmemory.tam-team"
KEEP = "keep"
REPLICA_CREDENTIALS = "replica-credentials.env"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def previous(ctx: Context):
    return ctx.previous.company if ctx.previous and ctx.previous.company else None


class ExistingServer(Answer):
    """What an existing data directory already holds, read without writing to it."""

    exists: bool = False
    admins: list[str] = Field(default_factory=list)
    teams: dict[str, str] = Field(default_factory=dict)
    organization: dict[str, str] = Field(default_factory=dict)
    settings: list[str] = Field(default_factory=list)


def inspect(root: Path) -> ExistingServer:
    database = root / "identity.db"
    if not database.is_file():
        return ExistingServer(exists=root.exists())
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            admins = [row[0] for row in db.execute(
                "SELECT id FROM users WHERE org_role='superadmin' AND active=1")] if "users" in tables and \
                "org_role" in {r[1] for r in db.execute("PRAGMA table_info(users)")} else []
            teams = dict(db.execute("SELECT id,name FROM teams")) if "teams" in tables else {}
            organization = dict(db.execute("SELECT key,value FROM organization")) if "organization" in tables else {}
            settings = [row[0] for row in db.execute("SELECT key FROM settings")] if "settings" in tables else []
    except sqlite3.DatabaseError as exc:
        raise WizardError(f"{database} cannot be read ({exc}); is it a team server data directory?") from exc
    return ExistingServer(exists=True, admins=admins, teams=teams, organization=organization, settings=settings)


class DataDirAnswer(Answer):
    path: str
    existing: ExistingServer

    def summary(self) -> list[tuple[str, str]]:
        state = "existing server" if self.existing.admins else "existing directory" if self.existing.exists else "new"
        return [("Data directory", f"{self.path} ({state})")]


def _dir_problem(value: str) -> str | None:
    path = Path(value).expanduser()
    if path.exists() and not path.is_dir():
        return "That path is a file; choose a directory"
    return None


def ask_data_dir(ctx: Context) -> DataDirAnswer:
    before = previous(ctx)
    default = before.data_dir if before else str(Path(ctx.host.environ.get("TAM_TEAM_DIR") or ctx.host.home / ".tam-server"))
    personal = {ctx.memory_dir.expanduser().resolve()}
    if ctx.base and ctx.base.personal:
        personal.add(Path(ctx.base.personal.memory_dir).expanduser().resolve())

    def problem(value: str) -> str | None:
        issue = _dir_problem(value)
        path = Path(value).expanduser().resolve()
        clash = next((p for p in personal if path == p or p in path.parents or path in p.parents), None)
        if issue is None and clash is not None:
            return f"That overlaps the personal memory in {clash}; choose a separate directory"
        return issue

    raw = ctx.prompter.text("data_dir", "Where should the server keep its data?", default, problem)
    path = Path(raw).expanduser().resolve()
    existing = inspect(path)
    if existing.admins:
        ctx.prompter.say(f"  This directory already holds a server (administrator: {', '.join(existing.admins)}). "
                         "Setup keeps its users and memory.")
    return DataDirAnswer(path=str(path), existing=existing)


class NetworkAnswer(Answer):
    host: str
    port: int
    public_url: str

    def summary(self) -> list[tuple[str, str]]:
        return [("Listen on", f"{self.host}:{self.port}"), ("Public URL", self.public_url)]


def _port_problem(value: str) -> str | None:
    return None if value.isdigit() and 1 <= int(value) <= 65535 else "Enter a port from 1 to 65535"


def _host_problem(value: str) -> str | None:
    return None if re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", value) else "Enter an IP address or host name"


def _url_problem(value: str) -> str | None:
    try:
        Registry._organization_value("public_url", value)
    except DomainError as exc:
        return str(exc)
    return None


def port_in_use(host: str, port: int) -> bool:
    try:
        with closing(socket.create_connection(("127.0.0.1" if host in ("0.0.0.0", "::", "") else host, port),
                                              timeout=0.5)):
            return True
    except OSError:
        return False


def ask_network(ctx: Context) -> NetworkAnswer:
    before = previous(ctx)
    ctx.prompter.say("  127.0.0.1 keeps the server private to this machine (put an HTTPS reverse proxy in front); "
                     "0.0.0.0 listens on every network interface.")
    host = ctx.prompter.text("host", "Bind address", before.host if before else DEFAULT_HOST, _host_problem)
    port = int(ctx.prompter.text("port", "Port", str(before.port if before else DEFAULT_PORT), _port_problem))
    if port_in_use(host, port):
        ctx.prompter.say(f"  Something already answers on port {port}. That is fine if it is this server; "
                         "otherwise pick another port.")
    public = before.public_url if before and before.port == port else local_url(host, port)
    url = ctx.prompter.text("public_url", "Public URL people will use (for links and client snippets)", public,
                            _url_problem)
    return NetworkAnswer(host=host, port=port, public_url=url.rstrip("/"))


class DeployAnswer(Answer):
    kind: Deploy

    def summary(self) -> list[tuple[str, str]]:
        return [("Runs as", {"service": "a background service (systemd or launchd)", "compose": "Docker Compose",
                             "manual": "tam-team serve, started by you"}[self.kind])]


def deploy_options(system: str) -> list[Option]:
    options = []
    if system == "Linux":
        options.append(Option("service", "System service", "systemd unit, starts on boot"))
    elif system == "Darwin":
        options.append(Option("service", "Background service", "launchd agent, starts at login"))
    options += [Option("compose", "Docker Compose", "container from docker-compose.team.yml; finish setup in the browser"),
                Option("manual", "Run it yourself", "print the tam-team serve command")]
    return options


def ask_deploy(ctx: Context) -> DeployAnswer:
    options = deploy_options(ctx.host.system)
    before = previous(ctx)
    ids = [o.id for o in options]
    default = before.deploy if before and before.deploy in ids else ids[0]
    return DeployAnswer(kind=ctx.prompter.choose("deploy", "How should the server run?", options, default))


def _data(ctx: Context) -> DataDirAnswer:
    answer = ctx.answer("company.data_dir", DataDirAnswer)
    if answer is None:
        raise WizardError("The data directory step did not run")
    return answer


def on_this_machine(ctx: Context) -> bool:
    answer = ctx.answer("company.deploy", DeployAnswer)
    return answer is not None and answer.kind != "compose"


class OrganizationAnswer(Answer):
    name: str

    def summary(self) -> list[tuple[str, str]]:
        return [("Company", self.name)]


def _name_problem(value: str) -> str | None:
    return None if len(value.strip()) <= 128 else "Use at most 128 characters"


def ask_organization(ctx: Context) -> OrganizationAnswer:
    current = _data(ctx).existing.organization.get("name")
    before = previous(ctx)
    default = current or (before.company_name if before else None)
    return OrganizationAnswer(name=ctx.prompter.text("company_name", "Company name", default, _name_problem).strip())


class AdminAnswer(Answer):
    user_id: str | None = None
    name: str | None = None
    existing: list[str] = Field(default_factory=list)

    def summary(self) -> list[tuple[str, str]]:
        if self.existing:
            return [("Administrator", ", ".join(self.existing) + " (already exists)")]
        return [("Administrator", f"{self.name} ({self.user_id})")]


def _id_problem(value: str) -> str | None:
    return None if IDENTIFIER.fullmatch(value) else "Use 1-64 letters, digits, - or _"


def ask_admin(ctx: Context) -> AdminAnswer:
    existing = _data(ctx).existing.admins
    if existing:
        ctx.prompter.say(f"  Administrator already set up: {', '.join(existing)}. New invite codes: "
                         "tam-team --root <data dir> invite <user-id>.")
        return AdminAnswer(existing=existing)
    default_id = slug(getpass.getuser()) or None
    user_id = ctx.prompter.text("admin_id", "Administrator user ID (for signing in)", default_id, _id_problem)
    name = ctx.prompter.text("admin_name", "Administrator full name", None, _name_problem).strip()
    return AdminAnswer(user_id=user_id, name=name)


class DepartmentsAnswer(Answer):
    departments: dict[str, str] = Field(default_factory=dict)
    existing: dict[str, str] = Field(default_factory=dict)

    def summary(self) -> list[tuple[str, str]]:
        new = ", ".join(f"{name} ({team_id})" for team_id, name in self.departments.items()) or "none"
        rows = [("New departments", new)]
        if self.existing:
            rows.append(("Existing departments", ", ".join(self.existing.values())))
        return rows


def ask_departments(ctx: Context) -> DepartmentsAnswer:
    existing = _data(ctx).existing.teams
    prompter = ctx.prompter
    if not prompter.interactive:
        preset = getattr(prompter, "answers", {}).get("departments") or []
        return DepartmentsAnswer(departments={i: n for i, n in preset if i not in existing}, existing=existing)
    prompter.say("  Departments share memory among their members. Add them now or later in the dashboard;")
    prompter.say("  press Enter on an empty name when you are done.")
    departments: dict[str, str] = {}
    while True:
        name = prompter.text(f"department.{len(departments)}", "Department name", None,
                             _name_problem, required=False)
        if not name:
            return DepartmentsAnswer(departments=departments, existing=existing)
        team_id = prompter.text(f"department.{len(departments)}.id", "  ID", slug(name) or None, _id_problem)
        if team_id in existing or team_id in departments:
            prompter.say(f"  {team_id} already exists; skipped.")
            continue
        departments[team_id] = name.strip()


class ProvidersAnswer(Answer):
    llm: providers.ProviderAnswer | None = None
    embed: providers.ProviderAnswer | None = None

    def summary(self) -> list[tuple[str, str]]:
        rows = []
        for answer, label in ((self.llm, "Language model"), (self.embed, "Embeddings")):
            rows += answer.summary() if answer else [(label, "unchanged")]
        return rows


def ask_providers(ctx: Context) -> ProvidersAnswer:
    existing = _data(ctx).existing
    stored = set(existing.settings)
    keep = [Option(KEEP, "Keep current settings", "leave what is configured")] if existing.exists and stored else []
    ctx.prompter.say("  Values are stored encrypted in the server's settings (as in the dashboard's Providers page).")
    llm_choice = ctx.prompter.choose("llm", "Language model for enrichment and summaries", [*keep, *providers.LLM_OPTIONS],
                                     KEEP if keep else "ollama")
    llm = None if llm_choice == KEEP else providers.ask_fields(
        ctx.prompter, "llm", llm_choice, {}, None, secret_is_set=any(k in stored for k in providers.LLM_SECRET_KEYS))
    embed_choice = ctx.prompter.choose("embed_provider", "Embedding provider", [*keep, *providers.EMBED_OPTIONS],
                                       KEEP if keep else "fastembed")
    embed = None if embed_choice == KEEP else providers.ask_fields(ctx.prompter, "embed", embed_choice, {}, None)
    return ProvidersAnswer(llm=llm, embed=embed)


BACKUP_OFF = Option("off", "No continuous backup", "snapshots only, with tam-team backup")
BACKUP_S3 = Option("s3", "S3 bucket", "AWS S3 or an S3-compatible store (MinIO, RustFS, Backblaze B2, Cloudflare R2)")
BACKUP_FILE = Option("file", "Directory", "a NAS share or another disk mounted on this machine")
DEFAULT_BACKUP_RETENTION = "168h"


class BackupAnswer(Answer):
    kind: Literal["off", "s3", "file"] = "off"
    url: str | None = None
    endpoint: str | None = None
    region: str | None = None
    retention: str = DEFAULT_BACKUP_RETENTION

    def settings(self) -> ReplicationSettings | None:
        if self.kind == "off" or self.url is None:
            return None
        values = {"url": self.url, "endpoint": self.endpoint, "retention": self.retention}
        if self.region:
            values["region"] = self.region
        return ReplicationSettings(**values)

    def summary(self) -> list[tuple[str, str]]:
        if self.kind == "off":
            return [("Continuous backup", "off (snapshots with tam-team backup)")]
        rows = [("Continuous backup", f"Litestream to {self.url}")]
        if self.endpoint:
            rows.append(("Backup endpoint", self.endpoint))
        return rows + [("Backup history", f"{self.retention} of point-in-time restore")]


def _replica_problem(**values: str | None) -> str | None:
    try:
        ReplicationSettings(**{k: v for k, v in values.items() if v is not None})
    except ValidationError as exc:
        return exc.errors()[0]["msg"].removeprefix("Value error, ")
    return None


def ask_backup(ctx: Context) -> BackupAnswer:
    if storage_backend(Path(_data(ctx).path)) != SQLITE_BACKEND:
        ctx.prompter.say("  " + POSTGRES_NOTE + ".")
        return BackupAnswer()
    before = previous(ctx)
    replica = before.backup_replica if before else None
    deploy = ctx.answer("company.deploy", DeployAnswer)
    options = [BACKUP_OFF, BACKUP_S3] if deploy and deploy.kind == "compose" else [BACKUP_OFF, BACKUP_S3, BACKUP_FILE]
    ids = [o.id for o in options]
    previous_kind = "s3" if replica and replica.startswith("s3://") else "file" if replica else "off"
    ctx.prompter.say("  Litestream streams every change to a bucket or directory, so a lost server can be rebuilt, "
                     "to any moment of the kept history, on another machine.")
    kind = ctx.prompter.choose("backup", "Continuous backup", options, previous_kind if previous_kind in ids else "off")
    if kind == "off":
        return BackupAnswer()
    data_dir = Path(_data(ctx).path)
    if kind == "s3":
        ctx.prompter.say("  Credentials are not stored by setup: Litestream reads AWS_ACCESS_KEY_ID and "
                         "AWS_SECRET_ACCESS_KEY from its environment.")
        url = ctx.prompter.text("backup_url", "Bucket and path (s3://bucket/path)",
                                replica if previous_kind == "s3" else None, lambda v: _replica_problem(url=v))
        endpoint = ctx.prompter.text("backup_endpoint", "S3 endpoint for non-AWS stores (empty for AWS)", None,
                                     lambda v: _replica_problem(url=url, endpoint=v), required=False)
        region = ctx.prompter.text("backup_region", "Region", "us-east-1",
                                   lambda v: _replica_problem(url=url, region=v))
    else:
        def directory_problem(value: str) -> str | None:
            path = Path(value).expanduser()
            if not path.is_absolute():
                return "Enter an absolute path"
            resolved = path.resolve()
            if resolved == data_dir or data_dir in resolved.parents or resolved in data_dir.parents:
                return "Keep the replica outside the server's data directory"
            return _replica_problem(url=resolved.as_uri())

        default = Path(replica.removeprefix("file://")).as_posix() if previous_kind == "file" else None
        folder = ctx.prompter.text("backup_url", "Replica directory (absolute path)", default, directory_problem)
        url, endpoint, region = Path(folder).expanduser().resolve().as_uri(), None, None
    retention = ctx.prompter.text("backup_retention", "Keep point-in-time history for (e.g. 168h = 7 days)",
                                  DEFAULT_BACKUP_RETENTION, lambda v: _replica_problem(url=url, retention=v))
    return BackupAnswer(kind=kind, url=url.rstrip("/"), endpoint=endpoint.rstrip("/") if endpoint else None,
                        region=region, retention=retention)


def team_command() -> tuple[list[str], dict[str, str]]:
    script = Path(sys.executable).parent / ("tam-team.exe" if os.name == "nt" else "tam-team")
    if script.is_file():
        return [str(script)], {}
    src = Path(__file__).resolve().parents[1]
    return [sys.executable, "-m", "team_memory.cli"], {"PYTHONPATH": str(src)}


def serve_args(root: Path, network: NetworkAnswer) -> list[str]:
    command, _env = team_command()
    return [*command, "--root", str(root), "serve", "--host", network.host, "--port", str(network.port)]


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r'[\s"\\]', value) else value


def systemd_unit(root: Path, network: NetworkAnswer) -> str:
    _command, env = team_command()
    environment = {"TAM_TEAM_DIR": str(root), **env}
    return "\n".join([
        "[Unit]", "Description=total-agent-memory team server", "After=network-online.target",
        "Wants=network-online.target", "", "[Service]", "Type=simple", f"User={getpass.getuser()}",
        "ExecStart=" + " ".join(_systemd_quote(a) for a in serve_args(root, network)),
        *(f"Environment={_systemd_quote(k + '=' + v)}" for k, v in environment.items()),
        "Restart=on-failure", "RestartSec=5", "NoNewPrivileges=true", "PrivateTmp=true", "",
        "[Install]", "WantedBy=multi-user.target", ""])


def launchd_plist(root: Path, network: NetworkAnswer) -> str:
    _command, env = team_command()
    log = str(root / "logs" / "server.log")
    return plistlib.dumps({"Label": LAUNCHD_LABEL, "ProgramArguments": serve_args(root, network),
                           "EnvironmentVariables": {"TAM_TEAM_DIR": str(root), **env}, "RunAtLoad": True,
                           "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log}).decode()


def compose_env(network: NetworkAnswer, replication: ReplicationSettings | None = None) -> str:
    publish = "127.0.0.1" if network.host in ("127.0.0.1", "localhost") else "0.0.0.0"
    lines = ["# docker compose --env-file <this file> -f docker-compose.team.yml up -d",
             f"TAM_TEAM_PUBLISH_HOST={publish}", f"TAM_TEAM_PORT={network.port}"]
    if replication is not None:
        lines += ["# Continuous backup: add --profile litestream and set the bucket credentials below.",
                  *(f"{key}={value}" for key, value in replication.environment().items()),
                  "AWS_ACCESS_KEY_ID=", "AWS_SECRET_ACCESS_KEY="]
    return "\n".join([*lines, ""])


def compose_file() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "docker-compose.team.yml"
    return candidate if candidate.is_file() else None


class Workdir:
    """Writes go to a sibling staging directory when the data directory is new, then one rename publishes it."""

    def __init__(self, root: Path):
        self.root = root
        self.staging: Path | None = None

    @property
    def path(self) -> Path:
        return self.staging or self.root

    def open(self) -> None:
        if not self.root.exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self.staging = Path(tempfile.mkdtemp(prefix=".tam-setup-", dir=self.root.parent))
            self.staging.chmod(0o700)

    def publish(self) -> None:
        if self.staging is not None:
            if self.root.exists():
                raise WizardError(f"{self.root} appeared while setup was running; nothing was changed")
            self.staging.rename(self.root)
            self.staging = None

    def discard(self) -> None:
        if self.staging is not None:
            shutil.rmtree(self.staging, ignore_errors=False)
            self.staging = None


def contribute_data_dir(ctx: Context, plan: Plan, answer: DataDirAnswer) -> None:
    workdir = Workdir(Path(answer.path))
    plan.internal["workdir"] = workdir

    def commit() -> list[str]:
        workdir.open()
        return []

    plan.actions.append(Action("Prepare the data directory", commit))
    plan.on_success.append(workdir.publish)
    plan.on_failure.append(workdir.discard)
    plan.record["data_dir"] = answer.path


def _workdir(plan: Plan) -> Workdir:
    workdir = plan.internal.get("workdir")
    if not isinstance(workdir, Workdir):
        raise WizardError("The data directory was not prepared")
    return workdir


def _registry(plan: Plan) -> Registry:
    return Registry(_workdir(plan).path)


def contribute_network(ctx: Context, plan: Plan, answer: NetworkAnswer) -> None:
    plan.record.update({"host": answer.host, "port": answer.port, "public_url": answer.public_url})


def contribute_organization(ctx: Context, plan: Plan, answer: OrganizationAnswer) -> None:
    network = ctx.answer("company.network", NetworkAnswer)

    def commit() -> list[str]:
        values = {"name": answer.name, "setup_state": "complete"}
        if network:
            values["public_url"] = network.public_url
        _registry(plan).set_organization(values)
        return [f"Company name saved: {answer.name}"]

    plan.actions.append(Action("Save the company profile", commit))
    plan.record["company_name"] = answer.name


def contribute_admin(ctx: Context, plan: Plan, answer: AdminAnswer) -> None:
    if answer.existing:
        plan.record["admin_id"] = answer.existing[0]
        return

    def commit() -> list[str]:
        invite = bootstrap_admin(_registry(plan), answer.user_id, answer.name)
        plan.result["invite"] = invite.model_dump()
        return [f"Superadmin created: {answer.name} ({answer.user_id})"]

    plan.actions.append(Action("Create the first superadmin", commit))
    plan.record["admin_id"] = answer.user_id


def contribute_departments(ctx: Context, plan: Plan, answer: DepartmentsAnswer) -> None:
    def commit() -> list[str]:
        registry = _registry(plan)
        for team_id, name in answer.departments.items():
            registry.add_team(team_id, name)
        return [f"Department created: {name} ({team_id})" for team_id, name in answer.departments.items()]

    if answer.departments:
        plan.actions.append(Action("Create departments", commit))
    plan.record["departments"] = sorted({*answer.existing, *answer.departments})


def contribute_providers(ctx: Context, plan: Plan, answer: ProvidersAnswer) -> None:
    values: dict[str, str] = {}
    for provider in (answer.llm, answer.embed):
        if provider is not None:
            values.update(provider.settings())

    def commit() -> list[str]:
        root = _workdir(plan).path
        changed = SettingsStore(_registry(plan), load_cipher(root, ctx.host.environ), ctx.host.environ).update(values)
        return [f"Provider settings saved (encrypted keys): {', '.join(changed)}"]

    if values:
        plan.actions.append(Action("Save provider settings", commit))
    plan.record["llm_provider"] = answer.llm.provider if answer.llm else None
    plan.record["embed_provider"] = answer.embed.provider if answer.embed else None


def contribute_deploy(ctx: Context, plan: Plan, answer: DeployAnswer) -> None:
    network = ctx.answer("company.network", NetworkAnswer)
    data = _data(ctx)
    root = Path(data.path)
    plan.record["deploy"] = answer.kind
    if network is None:
        raise WizardError("The network step did not run")

    def artifact(name: str, text: str, mode: int = 0o644) -> Path:
        target = _workdir(plan).path / "deploy" / name
        atomic_write(target, text, mode)
        return root / "deploy" / name

    if answer.kind == "service" and ctx.host.system == "Linux":
        def commit() -> list[str]:
            unit = artifact(SERVICE_NAME + ".service", systemd_unit(root, network))
            plan.next_steps.append(f"Install the service: sudo cp {unit} /etc/systemd/system/ && sudo systemctl "
                                   f"daemon-reload && sudo systemctl enable --now {SERVICE_NAME}")
            return [f"systemd unit written: {unit}"]
    elif answer.kind == "service":
        def commit() -> list[str]:
            agent = artifact(LAUNCHD_LABEL + ".plist", launchd_plist(root, network))
            (_workdir(plan).path / "logs").mkdir(mode=0o700, exist_ok=True)
            plan.next_steps.append(f"Install the service: cp {agent} ~/Library/LaunchAgents/ && launchctl bootstrap "
                                   f"gui/$(id -u) ~/Library/LaunchAgents/{agent.name}")
            return [f"launchd agent written: {agent}"]
    elif answer.kind == "compose":
        backup = ctx.answer("company.backup", BackupAnswer)

        def commit() -> list[str]:
            env_file = artifact("compose.env", compose_env(network, backup.settings() if backup else None),
                                PRIVATE_MODE)
            source = compose_file()
            compose = str(source) if source else "docker-compose.team.yml (from the total-agent-memory repository)"
            profile = " --profile litestream" if backup and backup.kind != "off" else ""
            plan.next_steps += [f"Start it: docker compose --env-file {env_file} -f {compose}{profile} up -d",
                                ("Finish setup in the browser: the server prints a one-time setup code in its log "
                                 f"(docker compose -f {compose} logs team-memory), then open "
                                 f"{network.public_url}/dashboard/")]
            return [f"Compose settings written: {env_file}"]
    else:
        def commit() -> list[str]:
            plan.next_steps.append("Start the server: " + " ".join(_shell_quote(a) for a in serve_args(root, network)))
            return []
    plan.actions.append(Action("Prepare how the server runs", commit))


def litestream_unit(root: Path) -> str:
    config = str(root / "deploy" / "litestream.yml")
    return "\n".join([
        "[Unit]", "Description=Litestream continuous backup of the total-agent-memory team server",
        "After=network-online.target", "Wants=network-online.target", "", "[Service]", "Type=simple",
        f"User={getpass.getuser()}",
        "EnvironmentFile=" + _systemd_quote(str(root / "deploy" / REPLICA_CREDENTIALS)),
        "ExecStart=litestream replicate -config " + _systemd_quote(config),
        "Restart=on-failure", "RestartSec=5", "NoNewPrivileges=true", "PrivateTmp=true", "",
        "[Install]", "WantedBy=multi-user.target", ""])


def contribute_backup(ctx: Context, plan: Plan, answer: BackupAnswer) -> None:
    settings = answer.settings()
    plan.record["backup_replica"] = settings.url if settings else None
    deploy = ctx.answer("company.deploy", DeployAnswer)
    if settings is None or deploy is None:
        return
    root = Path(_data(ctx).path)
    if deploy.kind == "compose":
        plan.next_steps.append(f"Continuous backup: fill AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in "
                               f"{root / 'deploy' / 'compose.env'}; the litestream container replicates to "
                               f"{settings.url}. Check it: docs/TEAM_BACKUP.md")
        return

    def commit() -> list[str]:
        deploy_dir = _workdir(plan).path / "deploy"
        atomic_write(deploy_dir / "litestream.yml", render_config(root, settings), PRIVATE_MODE)
        atomic_write(deploy_dir / "replication.env",
                     "".join(f"{key}={value}\n" for key, value in settings.environment().items()), PRIVATE_MODE)
        (_workdir(plan).path / "workspaces").mkdir(mode=0o700, exist_ok=True)
        config = root / "deploy" / "litestream.yml"
        credentials = root / "deploy" / REPLICA_CREDENTIALS
        lines = [f"Litestream config written: {config}"]
        if settings.scheme == "s3":
            plan.next_steps.append(f"Continuous backup: put AWS_ACCESS_KEY_ID=... and AWS_SECRET_ACCESS_KEY=... in "
                                   f"{credentials} (chmod 600); setup never stores them")
        if deploy.kind == "service" and ctx.host.system == "Linux":
            unit = deploy_dir / (SERVICE_NAME + "-litestream.service")
            atomic_write(unit, litestream_unit(root))
            installed = root / "deploy" / unit.name
            plan.next_steps.append(f"Install Litestream (https://litestream.io/install/) and its service: sudo cp "
                                   f"{installed} /etc/systemd/system/ && sudo systemctl daemon-reload && "
                                   f"sudo systemctl enable --now {unit.stem}")
            lines.append(f"systemd unit written: {installed}")
        else:
            plan.next_steps.append(f"Run Litestream next to the server (https://litestream.io/install/): "
                                   f"litestream replicate -config {_shell_quote(str(config))}")
        plan.next_steps.append(f"Check replication: set -a; . {_shell_quote(str(root / 'deploy' / 'replication.env'))}; "
                               f"tam-team --root {_shell_quote(str(root))} replication status")
        return lines

    plan.actions.append(Action("Prepare continuous backup", commit))


def _shell_quote(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", value) else "'" + value.replace("'", "'\"'\"'") + "'"


def health(url: str, transport: httpx.BaseTransport | None = None) -> str | None:
    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
            response = client.get(url.rstrip("/") + "/healthz")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return str(json.loads(response.text).get("version", "unknown"))
    except (json.JSONDecodeError, AttributeError):
        return None


STEPS = [
    Step("company.data_dir", "Data directory", COMPANY, ask_data_dir, contribute_data_dir),
    Step("company.network", "Address and port", COMPANY, ask_network, contribute_network),
    Step("company.deploy", "How the server runs", COMPANY, ask_deploy, contribute_deploy),
    Step("company.organization", "Company", COMPANY, ask_organization, contribute_organization, on_this_machine),
    Step("company.admin", "First superadmin", COMPANY, ask_admin, contribute_admin, on_this_machine),
    Step("company.departments", "Departments", COMPANY, ask_departments, contribute_departments, on_this_machine),
    Step("company.providers", "Model providers", COMPANY, ask_providers, contribute_providers, on_this_machine),
    Step("company.backup", "Continuous backup", COMPANY, ask_backup, contribute_backup),
]
