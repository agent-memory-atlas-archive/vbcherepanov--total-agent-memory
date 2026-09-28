"""Existing installs upgrade as "Just me": record personal mode silently, never prompt, never touch memory data."""
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from setup_wizard import clients
from setup_wizard.contracts import PersonalRecord, SetupRecord
from setup_wizard.files import atomic_write
from setup_wizard.locations import locate_memory_dir, record_path
from version import VERSION

LOGGER = logging.getLogger(__name__)
PRESET_BY_BACKEND = {"fastembed": "multilingual", "minilm": "multilingual", "e5-large": "multilingual-large",
                     "bge-m3": "multilingual-m3"}
LLM_VALUE_KEYS = ("MEMORY_LLM_MODEL", "MEMORY_LLM_API_BASE", "OLLAMA_URL", "MEMORY_LLM_TIMEOUT_SEC")
LLM_SECRET_KEYS = ("MEMORY_LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
DATABASE = "memory.db"


def registered(host: clients.Host) -> dict[str, dict]:
    """Clients that already have the memory server; unreadable configs are logged and skipped."""
    found = {}
    for client in clients.CLIENTS:
        try:
            entry = clients.current_entry(client, host)
        except clients.ConfigError as exc:
            LOGGER.warning(json.dumps({"event": "setup_adopt_config_unreadable", "client": client.id,
                                       "detail": str(exc)}))
            continue
        if entry is not None:
            found[client.id] = entry
    return found


def inferred(memory_dir: Path, entries: dict[str, dict], host: clients.Host) -> PersonalRecord:
    env = next((e["env"] for e in entries.values() if isinstance(e.get("env"), dict)), {})
    enabled = str(env.get("MEMORY_LLM_ENABLED", "auto")).lower()
    provider = "none" if enabled == "false" else str(env.get("MEMORY_LLM_PROVIDER") or "ollama")
    return PersonalRecord(
        memory_dir=str(Path(env["TAM_MEMORY_DIR"]).expanduser() if env.get("TAM_MEMORY_DIR") else memory_dir),
        clients=list(entries), embed_preset=PRESET_BY_BACKEND.get(str(env.get("V9_EMBED_BACKEND", "")), "multilingual"),
        llm_provider=provider, llm_settings={k: str(env[k]) for k in LLM_VALUE_KEYS if env.get(k)},
        llm_key_set=any(env.get(k) for k in LLM_SECRET_KEYS),
        hooks=(host.home / ".claude" / "hooks" / "session-start.sh").is_file(),
        skills=(host.home / ".claude" / "skills" / "memory-protocol").is_dir())


def legacy_hint(host: clients.Host) -> str | None:
    """One line for installs whose older installer wrote a config file the client does not read."""
    found = clients.legacy_registrations(host)
    if not found:
        return None
    where = "; ".join(f"{client}: {path}" for client, path in found)
    return (f"total-agent-memory: an older installer registered the server where the client does not look ({where}). "
            "Run `tam setup --reconfigure` to register it in the right place.")


def plaintext_key_hint(entries: dict[str, dict]) -> str | None:
    """One line when a client config still carries an LLM API key in plain text."""
    holders = sorted(client for client, entry in entries.items()
                     if isinstance(entry.get("env"), dict) and any(entry["env"].get(k) for k in LLM_SECRET_KEYS))
    if not holders:
        return None
    return (f"total-agent-memory: {', '.join(holders)} keep an API key in plain text in the client config. "
            "Run `tam setup --reconfigure` to move it into the encrypted settings file.")


def adopt_existing(environ: Mapping[str, str], host: clients.Host | None = None) -> SetupRecord | None:
    """Write the personal-mode record for an install that predates the wizard; None when nothing to do."""
    memory_dir = locate_memory_dir(environ)
    path = record_path(environ, memory_dir)
    if path.exists():
        return None
    host = host or clients.Host.current()
    entries = registered(host)
    hint = legacy_hint(host)
    if not (memory_dir / DATABASE).is_file() and not entries and hint is None:
        return None
    for line in (hint, plaintext_key_hint(entries)):
        if line is not None:
            LOGGER.warning(line)
    record = SetupRecord(mode="personal", source="upgrade", completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
                         tam_version=VERSION, personal=inferred(memory_dir, entries, host))
    try:
        atomic_write(path, record.model_dump_json(indent=2) + "\n")
    except OSError as exc:
        LOGGER.warning(json.dumps({"event": "setup_adopt_failed", "record": str(path), "detail": str(exc)}))
        return None
    LOGGER.info(json.dumps({"event": "setup_adopted_existing_install", "record": str(path),
                            "clients": record.personal.clients}))
    return record


def record_registration(environ: Mapping[str, str], host: clients.Host, memory_dir: Path,
                        client_ids: list[str]) -> Path:
    """Installers record personal mode (or add the clients to it) so `tam` does not start the wizard afterwards."""
    path = record_path(environ, memory_dir)
    existing = SetupRecord.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if existing is not None and existing.personal is not None:
        merged = list(dict.fromkeys([*existing.personal.clients, *client_ids]))
        record = existing.model_copy(update={"personal": existing.personal.model_copy(update={"clients": merged})})
    else:
        personal = inferred(memory_dir, registered(host), host)
        personal = personal.model_copy(update={"memory_dir": str(memory_dir),
                                               "clients": list(dict.fromkeys([*personal.clients, *client_ids]))})
        record = SetupRecord(mode=existing.mode if existing else "personal", source="installer", completed_at=now,
                             tam_version=VERSION, personal=personal, company=existing.company if existing else None)
    atomic_write(path, record.model_dump_json(indent=2) + "\n")
    return path
