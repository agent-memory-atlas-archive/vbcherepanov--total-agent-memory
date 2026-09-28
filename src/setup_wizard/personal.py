"""Personal mode: connect this machine's AI clients to a local memory server."""
import importlib.util
import json
import os
import queue
import subprocess
import tempfile
import threading
import time

import choose_embed
from local_settings import SPECS, LocalSettings
from settings_catalog import validate_value
from setup_wizard import clients, extras, providers
from setup_wizard.contracts import Answer, WizardError
from setup_wizard.prompts import Option
from setup_wizard.steps import Action, Context, Plan, Step

PERSONAL = frozenset(("personal",))
VERIFY_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 10
STDERR_TAIL_CHARS = 1500
LEGACY_PROTOCOL = "2025-06-18"


class Preset:
    def __init__(self, id: str, backend: str, label: str, note: str):
        self.id, self.backend, self.label, self.note = id, backend, label, note

    @property
    def model(self) -> str:
        return choose_embed.BACKEND_MODEL[self.backend]

    def option(self) -> Option:
        return Option(self.id, self.label, f"{self.note}; {self.model}, {choose_embed.BACKEND_DIM[self.backend]}-dim")


PRESETS = (
    Preset("multilingual", "fastembed", "Multilingual, fast", "default, 50+ languages, ~220 MB download"),
    Preset("multilingual-large", "e5-large", "Multilingual, higher quality", "~2.2 GB download, slower on CPU"),
    Preset("multilingual-m3", "bge-m3", "Multilingual, long documents", "needs the [rerank] extra (torch)"),
)
PRESET_BY_ID = {preset.id: preset for preset in PRESETS}
EMBED_KEYS = ("V9_EMBED_BACKEND", "MEMORY_TEXT_EMBED_MODEL")
MANAGED_ENV = frozenset(("TAM_MEMORY_DIR", *EMBED_KEYS, *providers.LLM_KEYS))


def available_presets() -> list[Preset]:
    torch_ready = importlib.util.find_spec("sentence_transformers") is not None
    return [p for p in PRESETS if p.backend != "bge-m3" or torch_ready]


def previous(ctx: Context):
    return ctx.previous.personal if ctx.previous and ctx.previous.personal else None


def registered_env(ctx: Context, client_ids: list[str] | None = None) -> dict[str, str]:
    """Env of the memory entry already registered with one of the clients (first readable wins)."""
    for client in clients.CLIENTS:
        if client_ids is not None and client.id not in client_ids:
            continue
        try:
            entry = clients.current_entry(client, ctx.host)
        except clients.ConfigError:
            continue
        if entry and isinstance(entry.get("env"), dict):
            return {str(k): str(v) for k, v in entry["env"].items()}
    return {}


class ClientsAnswer(Answer):
    clients: list[str]

    def summary(self) -> list[tuple[str, str]]:
        names = [clients.BY_ID[c].label for c in self.clients]
        return [("Connect clients", ", ".join(names) if names else "none (configure them yourself later)")]


def ask_clients(ctx: Context) -> ClientsAnswer:
    offered = [c for c in clients.CLIENTS if clients.available(c, ctx.install_root)]
    detected = [c.id for c in offered if c.detected(ctx.host)]
    options = [Option(c.id, c.label, "found" if c.id in detected else "not found") for c in offered]
    before = previous(ctx)
    defaults = before.clients if before else detected
    if not detected:
        ctx.prompter.say("  No MCP clients were found in their usual places. Pick the ones you use anyway.")
    chosen = ctx.prompter.choose_many("clients", "Which AI clients should use this memory?", options, defaults)
    return ClientsAnswer(clients=chosen)


def contribute_clients(ctx: Context, plan: Plan, answer: ClientsAnswer) -> None:
    changes: list[clients.Change] = []
    host = ctx.host

    def prepare() -> None:
        plan.env["TAM_MEMORY_DIR"] = str(ctx.memory_dir)
        for client_id in answer.clients:
            client = clients.BY_ID[client_id]
            current = clients.current_entry(client, host) or {}
            kept = {k: str(v) for k, v in (current.get("env") or {}).items() if k not in MANAGED_ENV}
            entry = clients.server_entry({**kept, **plan.env})
            changes.append(clients.plan(client, entry, host, private=bool(plan.secret_env)))

    def commit() -> list[str]:
        lines = []
        for change in changes:
            clients.write(change)
            lines.append(f"Registered with {change.client.label}: {change.path}")
            plan.next_steps.append(change.client.restart)
        return lines

    plan.actions.append(Action("Register the MCP server", commit, prepare))
    plan.record["clients"] = answer.clients


class EmbedAnswer(Answer):
    preset: str
    changed_with_data: bool = False

    def summary(self) -> list[tuple[str, str]]:
        preset = PRESET_BY_ID[self.preset]
        return [("Embeddings", f"{preset.label} ({preset.model})")]


def ask_embed(ctx: Context) -> EmbedAnswer:
    presets = available_presets()
    before = previous(ctx)
    default = before.embed_preset if before and before.embed_preset in PRESET_BY_ID else "multilingual"
    chosen = ctx.prompter.choose("embed", "Which languages and embedding model?", [p.option() for p in presets], default)
    if chosen not in {p.id for p in presets}:
        raise WizardError(f"Embedding preset {chosen} needs sentence-transformers: pip install 'total-agent-memory[rerank]'")
    changed = (ctx.memory_dir / "memory.db").exists() and chosen != default
    if changed:
        ctx.prompter.say("  Existing memories were embedded with another model. After setup, run the "
                         "memory_rebuild_embeddings tool once so search covers them again.")
    return EmbedAnswer(preset=chosen, changed_with_data=changed)


def contribute_embed(ctx: Context, plan: Plan, answer: EmbedAnswer) -> None:
    preset = PRESET_BY_ID[answer.preset]
    plan.env.update({"V9_EMBED_BACKEND": preset.backend, "MEMORY_TEXT_EMBED_MODEL": preset.model})
    plan.record["embed_preset"] = preset.id
    if answer.changed_with_data:
        plan.next_steps.append("Ask your agent to run memory_rebuild_embeddings once: stored memories use the old model.")


def ask_llm(ctx: Context) -> providers.ProviderAnswer:
    before = previous(ctx)
    default = before.llm_provider if before else providers.NO_LLM
    provider = ctx.prompter.choose("llm", "Should memory use a language model for summaries and enrichment?",
                                   providers.LLM_OPTIONS, default)
    existing = {**registered_env(ctx), **_saved_settings(ctx)} if before and before.llm_provider == provider else {}
    spec_secret = next((k for k in providers.LLM_SECRET_KEYS if existing.get(k)), None)
    answer = providers.ask_fields(ctx.prompter, "llm", provider, before.llm_settings if before else {},
                                  existing.get(spec_secret) if spec_secret else None)
    if provider != providers.NO_LLM and ctx.prompter.confirm("llm_test", "Test the connection now?", ctx.prompter.interactive):
        result = providers.test_connection(answer)
        ctx.prompter.say(("  OK: " if result.ok else "  Not reachable: ") + result.detail +
                         ("" if result.ok else " (setup continues; fix it later with `tam setup --reconfigure`)"))
    return answer


def _saved_settings(ctx: Context) -> dict[str, str]:
    return LocalSettings(ctx.memory_dir, ctx.host.environ).overrides()


def contribute_llm(ctx: Context, plan: Plan, answer: providers.ProviderAnswer) -> None:
    """LLM choices go to the encrypted settings file the dashboard edits, never into client configs.

    Every non-secret LLM setting is written or cleared, so an earlier dashboard value cannot
    silently outrank this answer; other providers' keys are left alone (OpenAI's also serves embeddings).
    """
    values = answer.settings()
    changes: dict[str, str | None] = {key: values.get(key) for key in providers.LLM_KEYS
                                      if SPECS[key].kind != "secret"}
    if answer.secret_name and answer.secret is not None:
        changes[answer.secret_name] = answer.secret.get_secret_value()
    store = LocalSettings(ctx.memory_dir, ctx.host.environ)

    def prepare() -> None:
        for key, value in changes.items():
            if value is not None:
                validate_value(SPECS[key], value)

    def commit() -> list[str]:
        store.update(changes)
        return [f"Saved language model settings in {store.path}" +
                (" (API key encrypted)" if answer.secret_name and answer.secret is not None else "")]

    plan.actions.append(Action("Save language model settings", commit, prepare))
    plan.record.update({"llm_provider": answer.provider, "llm_settings": dict(answer.values),
                        "llm_key_set": answer.secret is not None})


class ExtrasAnswer(Answer):
    hooks: bool = False
    skills: bool = False

    def summary(self) -> list[tuple[str, str]]:
        return [("Claude Code hooks", "install" if self.hooks else "skip"),
                ("Agent skills", "install" if self.skills else "skip")]


def _chosen(ctx: Context) -> list[str]:
    answer = ctx.answer("personal.clients", ClientsAnswer)
    return answer.clients if answer else []


def extras_apply(ctx: Context) -> bool:
    root = ctx.install_root
    return ("claude-code" in _chosen(ctx) and extras.hooks_available(root, ctx.host)) or \
        bool(extras.skill_copies(root, ctx.host, _chosen(ctx)))


def ask_extras(ctx: Context) -> ExtrasAnswer:
    before = previous(ctx)
    root = ctx.install_root
    hooks = skills = False
    if "claude-code" in _chosen(ctx) and extras.hooks_available(root, ctx.host):
        ctx.prompter.say("  Hooks let Claude Code save and recall memory automatically at session start, "
                         "on edits and at the end.")
        hooks = ctx.prompter.confirm("hooks", "Install the Claude Code hooks?", before.hooks if before else True)
    if extras.skill_copies(root, ctx.host, _chosen(ctx)):
        skills = ctx.prompter.confirm("skills", "Install the memory-protocol and onboarding skills?",
                                      before.skills if before else True)
    return ExtrasAnswer(hooks=hooks, skills=skills)


def contribute_extras(ctx: Context, plan: Plan, answer: ExtrasAnswer) -> None:
    root, host = ctx.install_root, ctx.host
    if answer.hooks:
        prepared: list[extras.HookPlan] = []
        plan.actions.append(Action("Install Claude Code hooks", lambda: _install_hooks(prepared[0]),
                                   lambda: prepared.append(extras.plan_hooks(root, host))))
    if answer.skills:
        copies = extras.skill_copies(root, host, _chosen(ctx))
        plan.actions.append(Action("Install agent skills", lambda: [f"Skill installed: {path}" for path in
                                                                    extras.apply_skills(copies)]))
    plan.record.update({"hooks": answer.hooks, "skills": answer.skills})


def _install_hooks(hook_plan: extras.HookPlan) -> list[str]:
    copied, skipped = extras.apply_hooks(hook_plan)
    events = ", ".join(sorted(set(hook_plan.added))) or "already registered"
    return [f"Hooks: {copied} script(s) copied, {skipped} kept as they were; events: {events}"]


def verify(ctx: Context, plan: Plan) -> tuple[bool, str]:
    """Start the registered server on a throwaway memory directory and list its tools over stdio."""
    entry = clients.server_entry({})
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {},
                          "clientInfo": {"name": "tam-setup", "version": "1"}}},
              {"jsonrpc": "2.0", "method": "notifications/initialized"},
              {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    with tempfile.TemporaryDirectory(prefix="tam-setup-verify-") as scratch, \
            tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as log:
        env = {**os.environ, **plan.env, "HOME": str(ctx.host.home), "TAM_MEMORY_DIR": scratch, "CLAUDE_MEMORY_DIR": scratch,
               "MCP_TRANSPORT": "stdio", "MEMORY_ASYNC_ENRICHMENT": "false", "MEMORY_LLM_ENABLED": "false"}
        started = time.monotonic()
        try:
            process = subprocess.Popen([entry.command, *entry.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, text=True, encoding="utf-8", env=env)
        except OSError as exc:
            return False, f"could not start {entry.command}: {exc.strerror}"
        try:
            tools = _list_tools(process, frames, started + VERIFY_TIMEOUT_SECONDS)
        finally:
            _stop(process)
        if isinstance(tools, int):
            return True, f"the server started and offered {tools} tools in {time.monotonic() - started:.1f} s"
        log.seek(0)
        tail = log.read().strip()[-STDERR_TAIL_CHARS:]
        return False, tools + (f"; last output:\n{tail}" if tail else "")


def _list_tools(process: subprocess.Popen, frames: list[dict], deadline: float) -> int | str:
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    process.stdin.write("".join(json.dumps(frame) + "\n" for frame in frames))
    process.stdin.flush()
    while True:
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            return f"the server did not answer within {VERIFY_TIMEOUT_SECONDS} s"
        if line is None:
            return f"the server exited with code {process.wait()} before listing its tools"
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2:
            if "result" in message:
                return len(message["result"].get("tools", []))
            return "the server refused tools/list: " + json.dumps(message.get("error"))


def _stop(process: subprocess.Popen) -> None:
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)


STEPS = [
    Step("personal.clients", "Connect your AI clients", PERSONAL, ask_clients, contribute_clients),
    Step("personal.embed", "Language and embeddings", PERSONAL, ask_embed, contribute_embed),
    Step("personal.llm", "Language model (optional)", PERSONAL, ask_llm, contribute_llm),
    Step("personal.extras", "Hooks and skills", PERSONAL, ask_extras, contribute_extras, extras_apply),
]
