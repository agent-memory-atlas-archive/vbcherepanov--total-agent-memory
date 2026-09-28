"""`tam-aml` — serve the AML adapter, purge run data, print the frozen settings."""

from __future__ import annotations

import argparse
import json
import os
import sys

from aml_adapter.config import AdapterConfig, ConfigError
from aml_adapter.logs import configure_logging
from aml_adapter.metrics import Metrics
from aml_adapter.pool import WorkerPool
from aml_adapter.registry import Registry
from aml_adapter.service import AmlService, settings_factory

CROSS_RERANK_ENV = "MEMORY_CROSS_RERANK"


def tam_worker_env() -> dict[str, str]:
    """Environment the worker processes add on top of the inherited one.

    Cross-encoder re-ranking in TAM's `auto` mode starts only once the model
    has loaded, so the first queries of every new worker would be ranked
    differently from later ones. The adapter resolves `auto` to `on`
    (deterministic); set MEMORY_CROSS_RERANK=off to disable re-ranking.
    """
    mode = os.environ.get(CROSS_RERANK_ENV, "auto").strip().lower() or "auto"
    return {CROSS_RERANK_ENV: "on" if mode == "auto" else mode}


def build_service(config: AdapterConfig) -> AmlService:
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = Registry(config.data_dir)
    pool = WorkerPool(registry.users_dir, maximum=config.workers, timeout=config.operation_timeout_seconds,
                      wait_seconds=config.worker_wait_seconds, settings_for=settings_factory(config),
                      tam_env=tam_worker_env())
    return AmlService(config, registry, pool, Metrics())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tam-aml", description="AML Add/Search adapter for total-agent-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the HTTP adapter (configuration from AML_* env vars)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    purge = commands.add_parser("purge", help="delete run data offline (server must be stopped)")
    scope = purge.add_mutually_exclusive_group(required=True)
    scope.add_argument("--expired", action="store_true", help="users older than AML_RETENTION_DAYS")
    scope.add_argument("--all", action="store_true", help="every user; requires --yes")
    purge.add_argument("--yes", action="store_true")
    commands.add_parser("settings", help="print result-shaping settings and their hash")
    args = parser.parse_args(argv)
    configure_logging()
    try:
        config = AdapterConfig.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
    if args.command == "settings":
        print(json.dumps({"settings": config.public_settings(), "sha256": config.fingerprint(),
                          "worker_env": tam_worker_env()}, indent=2, sort_keys=True, default=str))
        return 0
    if args.command == "purge":
        return _purge(config, everything=args.all, confirmed=args.yes)
    return _serve(config, host=args.host or config.host, port=args.port or config.port)


def _serve(config: AdapterConfig, *, host: str, port: int) -> int:
    import uvicorn

    from aml_adapter.app import create_app
    from team_memory.lifecycle import ServerLease
    from version import VERSION

    with ServerLease(config.data_dir):
        service = build_service(config)
        uvicorn.run(create_app(config, service, VERSION), host=host, port=port, access_log=False,
                    log_config=None, timeout_graceful_shutdown=30)
    return 0


def _purge(config: AdapterConfig, *, everything: bool, confirmed: bool) -> int:
    from team_memory.contracts import Conflict
    from team_memory.lifecycle import ServerLease

    if everything and not confirmed:
        print("refusing to delete every user without --yes", file=sys.stderr)
        return 2
    try:
        with ServerLease(config.data_dir):
            service = build_service(config)
            try:
                deleted = service.purge_all() if everything else service.purge_expired()
            finally:
                service.pool.close()
                service.registry.close()
    except Conflict as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"deleted_users": len(deleted), "journal": str(config.data_dir / "deletion-journal.jsonl")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
