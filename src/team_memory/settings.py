import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

import settings_catalog as catalog
from settings_catalog import (  # noqa: F401 — re-exported
    COMMON_FIELDS,
    EMBED_PROVIDERS,
    LLM_PROVIDERS,
    PROVIDER_DEFAULTS,
    PROVIDER_KEYS,
    PROVIDERS,
    TEAM_CATALOG,
    ProviderSpec,
    SettingSpec,
    mask,
)
from team_memory.contracts import DTO, DomainError
from team_memory.database import serializable
from team_memory.registry import AUDIT_ACTOR, Registry

LOGGER = logging.getLogger(__name__)
MASTER_KEY_ENV = "TAM_TEAM_MASTER_KEY"
MASTER_KEY_FILE = "master.key"
CATALOG = TEAM_CATALOG
SPECS = {spec.key: spec for spec in CATALOG}




class SettingView(DTO):
    key: str
    group: str
    label: str
    kind: str
    choices: tuple[str, ...]
    help: str
    source: Literal["web", "env", "default"]
    value: str | None = None
    is_set: bool
    hint: str | None = None
    readable: bool = True


def load_master_key(root: Path, environ: Mapping[str, str] | None = None, *, create: bool = True) -> bytes:
    """The Fernet master key: TAM_TEAM_MASTER_KEY, else <root>/master.key (created when ``create``).

    FileNotFoundError when the file is missing and ``create`` is False.
    """
    return catalog.load_master_key(root, MASTER_KEY_ENV, environ, create=create)


def load_cipher(root: Path, environ: Mapping[str, str] | None = None, *, create: bool = True) -> Fernet:
    return Fernet(load_master_key(root, environ, create=create))


def validate_value(spec: SettingSpec, value: str) -> str:
    try:
        return catalog.validate_value(spec, value)
    except catalog.SettingError as exc:
        raise DomainError(str(exc)) from None


class SettingsStore:
    def __init__(self, registry: Registry, cipher: Fernet, environ: Mapping[str, str] | None = None):
        self.registry, self.cipher = registry, cipher
        self.plane = registry.plane
        self.environ = os.environ if environ is None else environ

    def view(self) -> list[SettingView]:
        stored = self._stored()
        result = []
        for spec in CATALOG:
            base = spec.model_dump()
            if spec.key in stored:
                raw, secret = stored[spec.key]
                plain = self._decrypt(raw) if secret else raw
                result.append(SettingView(**base, source="web", is_set=True, readable=plain is not None,
                                          value=None if secret else plain,
                                          hint=mask(plain) if secret and plain is not None else None))
            elif self.environ.get(spec.key):
                env_value = self.environ[spec.key]
                secret = spec.kind == "secret"
                result.append(SettingView(**base, source="env", is_set=True, value=None if secret else env_value,
                                          hint=mask(env_value) if secret else None))
            else:
                result.append(SettingView(**base, source="default", is_set=False))
        return result

    @serializable
    def update(self, changes: Mapping[str, str | None]) -> list[str]:
        unknown = sorted(set(changes) - set(SPECS))
        if unknown:
            raise DomainError("Unknown setting: " + ", ".join(unknown))
        prepared = {key: None if value is None else validate_value(SPECS[key], value) for key, value in changes.items()}
        with self.registry.connect() as db:
            for key, value in prepared.items():
                if value is None:
                    db.execute("DELETE FROM settings WHERE key=?", (key,))
                    Registry._event(db, "setting_cleared", key)
                    continue
                secret = SPECS[key].kind == "secret"
                stored = self.cipher.encrypt(value.encode()).decode() if secret else value
                db.execute("INSERT INTO settings(key,value,secret,updated_by) VALUES (?,?,?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret,"
                           "updated_by=excluded.updated_by,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                           (key, stored, int(secret), AUDIT_ACTOR.get()))
                Registry._event(db, "setting_updated", key, "secret" if secret else "value")
        return sorted(prepared)

    def overrides(self) -> dict[str, str]:
        result = {}
        for key, (raw, secret) in self._stored().items():
            plain = self._decrypt(raw) if secret else raw
            if plain is None:
                LOGGER.error(json.dumps({"event": "setting_undecryptable", "key": key}))
                continue
            result[key] = plain
        return result

    def effective(self) -> dict[str, str]:
        merged = {key: value for key, value in self.environ.items() if key in SPECS and value}
        merged.update(self.overrides())
        return merged

    def active_provider(self, target: str) -> str:
        value = self.effective().get(PROVIDER_KEYS[target], PROVIDER_DEFAULTS[target])
        return value if value == "auto" or any(p.target == target and p.id == value for p in PROVIDERS) \
            else PROVIDER_DEFAULTS[target]

    @serializable
    def record_check(self, target: str, provider: str, ok: bool, detail: str) -> None:
        with self.registry.connect() as db:
            db.execute("INSERT INTO provider_checks(target,provider,ok,detail) VALUES (?,?,?,?) "
                       "ON CONFLICT(target,provider) DO UPDATE SET ok=excluded.ok,detail=excluded.detail,"
                       "at=strftime('%Y-%m-%dT%H:%M:%fZ','now')", (target, provider, int(ok), detail))

    @serializable
    def checks(self) -> dict[tuple[str, str], dict]:
        with self.registry.connect() as db:
            rows = db.execute("SELECT target,provider,ok,detail,at FROM provider_checks").fetchall()
        return {(row["target"], row["provider"]): {"ok": bool(row["ok"]), "detail": row["detail"], "at": row["at"]}
                for row in rows}

    @serializable
    def _stored(self) -> dict[str, tuple[str, bool]]:
        with self.registry.connect() as db:
            rows = db.execute("SELECT key,value,secret FROM settings").fetchall()
        return {row["key"]: (row["value"], bool(row["secret"])) for row in rows if row["key"] in SPECS}

    def _decrypt(self, token: str) -> str | None:
        try:
            return self.cipher.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

