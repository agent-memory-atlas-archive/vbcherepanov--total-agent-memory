"""Personal-install settings changed from the local dashboard.

Values live in ``<memory dir>/settings.json`` (0600). Secrets in it are Fernet tokens
under ``TAM_MASTER_KEY`` or ``<memory dir>/master.key`` (0600, created on the first
saved secret), so no key sits in plain text in the file or in any MCP client config.

Precedence matches the team server: a value saved here overrides the process
environment, which overrides the built-in default. `apply` runs before the server
reads its configuration, so saved values take effect for every new session.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict

from settings_catalog import (
    LOCAL_CATALOG,
    SettingError,
    load_master_key,
    mask,
    validate_value,
)

LOGGER = logging.getLogger(__name__)
MASTER_KEY_ENV = "TAM_MASTER_KEY"
SETTINGS_FILE = "settings.json"
FORMAT_VERSION = 1
SPECS = {spec.key: spec for spec in LOCAL_CATALOG}


class SettingView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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


class LocalSettings:
    def __init__(self, root: Path, environ: Mapping[str, str] | None = None):
        self.root = Path(root)
        self.path = self.root / SETTINGS_FILE
        self.environ = os.environ if environ is None else environ

    def view(self) -> list[SettingView]:
        stored = self._read()
        result = []
        for spec in LOCAL_CATALOG:
            base = spec.model_dump()
            if spec.key in stored["secrets"]:
                plain = self._decrypt(stored["secrets"][spec.key])
                result.append(SettingView(**base, source="web", is_set=True, readable=plain is not None,
                                          hint=mask(plain) if plain is not None else None))
            elif spec.key in stored["values"]:
                result.append(SettingView(**base, source="web", is_set=True, value=stored["values"][spec.key]))
            elif self.environ.get(spec.key):
                env_value = self.environ[spec.key]
                secret = spec.kind == "secret"
                result.append(SettingView(**base, source="env", is_set=True, value=None if secret else env_value,
                                          hint=mask(env_value) if secret else None))
            else:
                result.append(SettingView(**base, source="default", is_set=False))
        return result

    def update(self, changes: Mapping[str, str | None]) -> list[str]:
        """Save or clear (None) settings atomically; every value is validated before anything is written."""
        unknown = sorted(set(changes) - set(SPECS))
        if unknown:
            raise SettingError("Unknown setting: " + ", ".join(unknown))
        prepared = {key: None if value is None else validate_value(SPECS[key], value) for key, value in changes.items()}
        stored = self._read()
        self.root.mkdir(parents=True, exist_ok=True)
        cipher = Fernet(load_master_key(self.root, MASTER_KEY_ENV, self.environ)) if any(
            value is not None and SPECS[key].kind == "secret" for key, value in prepared.items()) else None
        for key, value in prepared.items():
            stored["values"].pop(key, None)
            stored["secrets"].pop(key, None)
            if value is None:
                continue
            if SPECS[key].kind == "secret":
                stored["secrets"][key] = cipher.encrypt(value.encode()).decode()
            else:
                stored["values"][key] = value
        self._write(stored)
        LOGGER.info(json.dumps({"event": "local_settings_updated", "keys": sorted(prepared)}))
        return sorted(prepared)

    def overrides(self) -> dict[str, str]:
        """Every saved value in plain text; undecryptable secrets are logged and skipped."""
        stored = self._read()
        result = dict(stored["values"])
        for key, token in stored["secrets"].items():
            plain = self._decrypt(token)
            if plain is None:
                LOGGER.error(json.dumps({"event": "setting_undecryptable", "key": key}))
                continue
            result[key] = plain
        return result

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": FORMAT_VERSION, "values": {}, "secrets": {}}
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise SettingError(f"{self.path}: unsupported settings format")
        return {"version": FORMAT_VERSION,
                "values": {k: v for k, v in data.get("values", {}).items() if k in SPECS and isinstance(v, str)},
                "secrets": {k: v for k, v in data.get("secrets", {}).items() if k in SPECS and isinstance(v, str)}}

    def _write(self, stored: dict) -> None:
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".settings-", suffix=".json")
        try:
            os.chmod(temp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(stored, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def _decrypt(self, token: str) -> str | None:
        """None when the master key is gone or is not the one the value was encrypted with."""
        try:
            cipher = Fernet(load_master_key(self.root, MASTER_KEY_ENV, self.environ, create=False))
        except FileNotFoundError:
            return None
        try:
            return cipher.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def apply(environ: MutableMapping[str, str], root: Path) -> list[str]:
    """Overlay saved settings onto `environ`; returns the keys applied. A broken file never blocks startup."""
    try:
        overrides = LocalSettings(root, environ).overrides()
    except (SettingError, OSError, ValueError) as error:
        LOGGER.error(json.dumps({"event": "local_settings_unreadable", "error": str(error)}))
        return []
    environ.update(overrides)
    return sorted(overrides)
