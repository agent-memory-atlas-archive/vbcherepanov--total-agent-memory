"""`tam setup`: interactive first-run wizard, or `--non-interactive` with flags for installers and containers."""
import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from setup_wizard import clients
from setup_wizard.contracts import UsageError, WizardError
from setup_wizard.locations import install_root, locate_memory_dir, record_path
from setup_wizard.prompts import AnswerPrompter, Prompter, TerminalPrompter
from setup_wizard.steps import Context
from setup_wizard.wizard import FAILED, Wizard, describe, load_record

SKIP_ENV = "TAM_NO_SETUP"
CI_VARS = ("CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "TF_BUILD", "JENKINS_URL",
           "TEAMCITY_VERSION")
FALSE_VALUES = ("", "0", "false", "no")
USAGE_ERROR = 64
FLAGS = {
    "mode": "--mode", "clients": "--clients", "embed": "--embed-preset", "llm": "--llm",
    "llm.MEMORY_LLM_MODEL": "--llm-model", "llm.MEMORY_LLM_API_BASE": "--llm-base-url", "llm.OLLAMA_URL": "--ollama-url",
    "llm.key": "--llm-api-key-env", "llm_test": "--test-llm", "hooks": "--hooks", "skills": "--skills",
    "data_dir": "--data-dir", "host": "--host", "port": "--port", "public_url": "--public-url", "deploy": "--deploy",
    "company_name": "--company-name", "admin_id": "--admin-id", "admin_name": "--admin-name",
    "departments": "--department", "embed_provider": "--embed-provider", "embed.MEMORY_EMBED_MODEL": "--embed-model",
    "embed.MEMORY_EMBED_API_BASE": "--embed-base-url", "embed.MEMORY_EMBED_DIMENSIONS": "--embed-dimensions",
    "embed.key": "--embed-api-key-env", "backup": "--backup", "backup_url": "--backup-url",
    "backup_endpoint": "--backup-endpoint", "backup_region": "--backup-region", "backup_retention": "--backup-retention",
}


def should_autorun(environ: Mapping[str, str], stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> bool:
    """First launch of plain `tam` by a person at a terminal; never for MCP clients, pipes or CI."""
    if not (stdin.isatty() and stdout.isatty()):
        return False
    if any(environ.get(name, "").strip().lower() not in FALSE_VALUES for name in CI_VARS):
        return False
    if environ.get("MCP_TRANSPORT") or environ.get(SKIP_ENV, "").strip().lower() not in FALSE_VALUES:
        return False
    return not record_path(environ).exists()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tam setup", description="Set up total-agent-memory for yourself or for a company.")
    p.add_argument("--reconfigure", action="store_true", help="change an existing setup; current values are defaults")
    p.add_argument("--non-interactive", action="store_true", help="take every answer from flags (installers, Docker)")
    p.add_argument("--json", action="store_true", help="with --non-interactive: print the result as JSON on stdout")
    p.add_argument("--skip-verify", action="store_true", help="do not start the server to check the result")
    p.add_argument("--memory-dir", type=Path, help="personal memory directory (default: TAM_MEMORY_DIR or ~/.tam)")
    p.add_argument("--mode", choices=("personal", "company"))
    personal = p.add_argument_group("personal mode")
    personal.add_argument("--clients", help="comma-separated client ids, 'detected' or 'none'; ids: "
                          + ", ".join(c.id for c in clients.CLIENTS))
    personal.add_argument("--embed-preset", help="multilingual, multilingual-large or multilingual-m3")
    personal.add_argument("--hooks", action=argparse.BooleanOptionalAction, default=None)
    personal.add_argument("--skills", action=argparse.BooleanOptionalAction, default=None)
    personal.add_argument("--test-llm", action="store_true", help="probe the language model provider during setup")
    both = p.add_argument_group("providers (both modes)")
    both.add_argument("--llm", help="none, ollama, openai, anthropic or openai-compatible")
    both.add_argument("--llm-model")
    both.add_argument("--llm-base-url")
    both.add_argument("--ollama-url")
    both.add_argument("--llm-api-key-env", metavar="VAR", help="name of the environment variable holding the key")
    company = p.add_argument_group("company mode")
    company.add_argument("--data-dir")
    company.add_argument("--host")
    company.add_argument("--port")
    company.add_argument("--public-url")
    company.add_argument("--deploy", help="service, compose or manual")
    company.add_argument("--company-name")
    company.add_argument("--admin-id")
    company.add_argument("--admin-name")
    company.add_argument("--department", action="append", default=[], metavar="ID=NAME")
    company.add_argument("--embed-provider", help="fastembed, openai, cohere or dashscope")
    company.add_argument("--embed-model")
    company.add_argument("--embed-base-url")
    company.add_argument("--embed-dimensions")
    company.add_argument("--embed-api-key-env", metavar="VAR")
    company.add_argument("--backup", help="continuous backup with Litestream: off, s3 or file")
    company.add_argument("--backup-url", help="s3://bucket/path, or an absolute directory with --backup file")
    company.add_argument("--backup-endpoint", help="S3 endpoint for non-AWS stores, e.g. https://s3.example.com")
    company.add_argument("--backup-region")
    company.add_argument("--backup-retention", help="point-in-time history to keep, e.g. 168h")
    return p


VALUE_FLAGS = ("clients", "embed_preset", "hooks", "skills", "llm", "llm_model", "llm_base_url", "ollama_url",
               "llm_api_key_env", "data_dir", "host", "port", "public_url", "deploy", "company_name", "admin_id",
               "admin_name", "embed_provider", "embed_model", "embed_base_url", "embed_dimensions", "embed_api_key_env",
               "backup", "backup_url", "backup_endpoint", "backup_region", "backup_retention")


def _secret_from_env(variable: str | None, environ: Mapping[str, str], flag: str) -> str | None:
    if not variable:
        return None
    value = environ.get(variable, "").strip()
    if not value:
        raise UsageError(f"{flag}: environment variable {variable} is empty or unset")
    return value


def answers_from(args: argparse.Namespace, environ: Mapping[str, str], host: clients.Host) -> dict[str, object]:
    answers: dict[str, object] = {
        "mode": args.mode, "embed": args.embed_preset, "llm": args.llm, "llm.MEMORY_LLM_MODEL": args.llm_model,
        "llm.MEMORY_LLM_API_BASE": args.llm_base_url, "llm.OLLAMA_URL": args.ollama_url,
        "llm.key": _secret_from_env(args.llm_api_key_env, environ, "--llm-api-key-env"),
        "llm_test": args.test_llm or None, "hooks": args.hooks, "skills": args.skills, "data_dir": args.data_dir,
        "host": args.host, "port": args.port, "public_url": args.public_url, "deploy": args.deploy,
        "company_name": args.company_name, "admin_id": args.admin_id, "admin_name": args.admin_name,
        "embed_provider": args.embed_provider, "embed.MEMORY_EMBED_MODEL": args.embed_model,
        "embed.MEMORY_EMBED_API_BASE": args.embed_base_url, "embed.MEMORY_EMBED_DIMENSIONS": args.embed_dimensions,
        "embed.key": _secret_from_env(args.embed_api_key_env, environ, "--embed-api-key-env"), "apply": True,
        "backup": args.backup, "backup_url": args.backup_url, "backup_endpoint": args.backup_endpoint,
        "backup_region": args.backup_region, "backup_retention": args.backup_retention,
    }
    if args.clients is not None:
        value = args.clients.strip().lower()
        answers["clients"] = ([c.id for c in clients.CLIENTS if c.detected(host)] if value == "detected" else
                              [] if value == "none" else [c.strip() for c in value.split(",") if c.strip()])
    departments = []
    for item in args.department:
        team_id, separator, name = item.partition("=")
        if not separator or not team_id.strip() or not name.strip():
            raise UsageError(f"--department {item!r}: expected ID=NAME")
        departments.append((team_id.strip(), name.strip()))
    answers["departments"] = departments
    return {key: value for key, value in answers.items() if value is not None}


def main(argv: Sequence[str] | None = None, first_run: bool = False, environ: Mapping[str, str] | None = None,
         prompter: Prompter | None = None, host: clients.Host | None = None, out: TextIO | None = None) -> int:
    env = os.environ if environ is None else environ
    out = out or sys.stdout
    args = parser().parse_args(list(argv or []))
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    host = host or clients.Host.current()
    memory_dir = (args.memory_dir.expanduser() if args.memory_dir else locate_memory_dir(env)).resolve()
    path = record_path(env, memory_dir)
    report = sys.stderr if args.json else out
    try:
        previous = load_record(path)
        if not args.non_interactive and any(getattr(args, name) not in (None, False) for name in VALUE_FLAGS) \
                and prompter is None:
            raise UsageError("answer flags need --non-interactive")
        adding = previous is not None and args.mode is not None and args.mode != previous.mode and \
            getattr(previous, args.mode) is None
        if previous and not args.reconfigure and not adding:
            report.write("total-agent-memory is already set up:\n")
            for line in describe(previous):
                report.write(f"  {line}\n")
            report.write("Run `tam setup --reconfigure` to change it.\n")
            if previous.personal and not previous.company:
                report.write("Add a company server next to it: `tam setup --mode company`.\n")
            return 0
        if prompter is None:
            prompter = (AnswerPrompter(answers_from(args, env, host), FLAGS, report) if args.non_interactive
                        else TerminalPrompter(out, environ=env))
        if first_run:
            prompter.say("Welcome to total-agent-memory. This one-time setup takes about a minute "
                         "(skip it with TAM_NO_SETUP=1).")
        ctx = Context(prompter=prompter, host=host, install_root=install_root(), memory_dir=memory_dir,
                      previous=previous if args.reconfigure else None, base=previous,
                      preset_mode=args.mode if not args.non_interactive else None)
        outcome = Wizard(ctx, path, verify=not args.skip_verify).run()
    except UsageError as exc:
        report.write(f"tam setup: {exc}\n")
        return USAGE_ERROR
    except WizardError as exc:
        report.write(f"tam setup: {exc}\nNothing was changed.\n")
        return FAILED
    if args.json:
        out.write(json.dumps(outcome.as_json(), indent=2) + "\n")
    return outcome.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
