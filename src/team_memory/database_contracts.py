"""Contracts for choosing, checking and migrating the team server's database.

The DSN is a secret end to end: it is parsed server-side into ``DatabaseDsn``, stored
Fernet-encrypted in ``<root>/database.json`` and shown to the browser only as
``DatabaseDsn.masked()``. Nothing here imports psycopg.
"""

import ipaddress
import re
from collections.abc import Mapping
from enum import StrEnum
from os import PathLike
from typing import Literal, Protocol, Self
from urllib.parse import quote, unquote
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    SecretStr,
    computed_field,
    model_validator,
)

from tam_db.contracts import ActiveDatabase, Backend, DatabaseSettings
from team_memory.contracts import DTO, Conflict, DomainError

DATABASE_URL_ENV = "TAM_TEAM_DATABASE_URL"
DATABASE_CONFIG_FILE = "database.json"
POSTGRES_MARKER_FILE = "postgres-installation.json"
DATABASE_CONFIG_FORMAT = 1
MIGRATION_DIR = "migration"
ARCHIVE_DIR = "archive"
ARCHIVE_PREFIX = "sqlite-"
ARCHIVE_PATH_PATTERN = r"^archive/sqlite-[0-9A-Za-z_-]{1,64}$"

MAX_DSN_CHARS = 1024
MAX_IDENTIFIER_BYTES = 63
MAX_PARAM_VALUE_CHARS = 512
MAX_MESSAGE_CHARS = 2000
DEFAULT_PORT = 5432
MAX_PORT = 65535
PASSWORD_MASK = "•" * 4
URI_SCHEMES = ("postgresql://", "postgres://")
CANONICAL_SCHEME = "postgresql://"
LOCAL_HOST_NAMES = frozenset({"localhost"})
HOST_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,252})$")
BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
MAX_ECHOED_PARAM_NAME = 32

CHECK_TIMEOUT_SECONDS = 5
MAINTENANCE_RETRY_AFTER_SECONDS = 30
PLAN_TTL_SECONDS = 900
PROGRESS_POLL_SECONDS = 1

DATABASE_API = "/dashboard/api/admin/database"
DATABASE_TEST_API = DATABASE_API + "/test"
DATABASE_PLAN_API = DATABASE_API + "/plan"
DATABASE_MIGRATE_API = DATABASE_API + "/migrate"
DATABASE_MIGRATION_API = DATABASE_API + "/migration"
DATABASE_MIGRATION_CANCEL_API = DATABASE_MIGRATION_API + "/cancel"
DATABASE_REPOINT_API = DATABASE_API + "/repoint"
DATABASE_ROLLBACK_API = DATABASE_API + "/rollback"
SETUP_DATABASE_API = "/dashboard/api/setup/database"
SETUP_DATABASE_TEST_API = SETUP_DATABASE_API + "/test"

MCP_PATH_PREFIXES = ("/mcp", "/api/call")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# The only mutating call allowed during maintenance. Sign-in and sign-out are NOT exempt:
# both write identity.db (new session, failed-login counters, admin_events, session
# deletion) after it was copied, so the write would be lost on PostgreSQL and a signed-out
# session would come back to life. Cancelling keeps SQLite authoritative, so its writes stay.
MAINTENANCE_EXEMPT_PATHS = frozenset({
    DATABASE_MIGRATION_CANCEL_API,
})


class OutputDTO(DTO):
    """A response/journal model with computed fields: re-reading its own JSON ignores them."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class InvalidDsn(DomainError):
    """The submitted DSN is unusable. Messages never contain the DSN or any part of its password."""


class StartupRefusal(StrEnum):
    FOREIGN_INSTALLATION = "foreign_installation"
    EMPTY_TARGET_WITH_SQLITE_DATA = "empty_target_with_sqlite_data"
    KEY_UNAVAILABLE = "key_unavailable"
    CONFIG_UNREADABLE = "config_unreadable"
    POSTGRES_DSN_MISSING = "postgres_dsn_missing"


class DatabaseStartupRefused(Conflict):
    """The split-brain guard refused to start the server (plan 4.3); never falls back silently."""

    def __init__(self, reason: StartupRefusal, message: str):
        super().__init__(message)
        self.reason = reason


class DsnOrigin(StrEnum):
    """Who supplied the DSN. WEB (dashboard, setup wizard) is untrusted input from a browser;
    ENV (TAM_TEAM_DATABASE_URL, CLI, stored config written by them) is the host operator."""

    WEB = "web"
    ENV = "env"


# sslrootcert=system (libpq >= 16) selects the OS trust store and names no file, so it is
# the only certificate parameter a browser may set.
WEB_SSLROOTCERT_VALUES = frozenset({"system"})
WEB_FORBIDDEN_FILE_PARAMS = ("sslcert", "sslkey")
# Connection keywords that stop libpq from filling a web DSN with the server process's
# ambient credentials: ~/.pgpass (passfile), ~/.postgresql/postgresql.crt (sslcertmode),
# Kerberos tickets (gssencmode) and trust/peer/cert/GSS authentication (require_auth: the
# server must ask for the password in the DSN). require_auth needs libpq >= 16 and
# sslcertmode libpq >= 17; the psycopg[binary] wheel bundles a newer libpq.
MIN_LIBPQ_VERSION = 170000
WEB_REQUIRE_AUTH = "password,md5,scram-sha-256"


class SslMode(StrEnum):
    DISABLE = "disable"
    ALLOW = "allow"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


WEAK_SSL_MODES = frozenset({SslMode.DISABLE, SslMode.ALLOW, SslMode.PREFER})
LIBPQ_DEFAULT_SSLMODE = SslMode.PREFER


class ChannelBinding(StrEnum):
    DISABLE = "disable"
    PREFER = "prefer"
    REQUIRE = "require"


class TargetSessionAttrs(StrEnum):
    ANY = "any"
    READ_WRITE = "read-write"
    READ_ONLY = "read-only"
    PRIMARY = "primary"
    STANDBY = "standby"
    PREFER_STANDBY = "prefer-standby"


# Query parameters accepted in the DSN, in canonical output order. Everything else is
# rejected; "options" in particular could set search_path or role and bypass isolation.
ALLOWED_PARAMS = (
    "sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout",
    "application_name", "target_session_attrs", "channel_binding",
)
FORBIDDEN_PARAM_MESSAGES = {
    "options": "the options parameter is not allowed: it can change search_path or role and bypass workspace isolation",
}


def _fail(message: str) -> InvalidDsn:
    return InvalidDsn(message)


def _decode(component: str, what: str) -> str:
    if BAD_PERCENT_ESCAPE.search(component):
        raise _fail(f"{what} contains a malformed percent-escape")
    try:
        return unquote(component, errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail(f"{what} is not valid UTF-8 after percent-decoding") from exc


def _check_text(value: str, what: str, max_bytes: int | None = None, max_chars: int | None = None) -> None:
    if not value:
        raise _fail(f"{what} must not be empty")
    if any(not ch.isprintable() for ch in value):
        raise _fail(f"{what} must not contain control characters")
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        raise _fail(f"{what} must be at most {max_bytes} bytes")
    if max_chars is not None and len(value) > max_chars:
        raise _fail(f"{what} must be at most {max_chars} characters")


def _is_socket_path(host: str) -> bool:
    return host.startswith("/")


def _check_host(host: str) -> None:
    if _is_socket_path(host):
        _check_text(host, "socket directory", max_chars=MAX_PARAM_VALUE_CHARS)
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    if not HOST_NAME_PATTERN.fullmatch(host) or ".." in host:
        raise _fail("host must be a DNS name, an IP address or a percent-encoded socket directory")


class DsnHost(DTO):
    host: str
    port: int = Field(default=DEFAULT_PORT, ge=1, le=MAX_PORT)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        _check_host(self.host)
        return self

    @property
    def is_local(self) -> bool:
        if _is_socket_path(self.host) or self.host.lower() in LOCAL_HOST_NAMES:
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def display(self) -> str:
        if _is_socket_path(self.host):
            return f"{self.host}:{self.port}"
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"

    def encoded(self) -> str:
        if _is_socket_path(self.host):
            return f"{quote(self.host, safe='')}:{self.port}"
        return self.display()


class DsnAudit(DTO):
    """The only DSN details that audit events and logs may carry."""

    host: str
    database: str
    sslmode: SslMode


class DatabaseDsn(DTO):
    """A validated ``postgresql://`` URI (plan 4.5). Build it with ``DatabaseDsn.parse``.

    ``repr``/``str`` are the mask; ``to_uri()`` is the only accessor that yields the
    password and must only feed the driver or the encrypted config.
    """

    user: str
    password: SecretStr | None = None
    hosts: tuple[DsnHost, ...] = Field(min_length=1)
    database: str
    sslmode: SslMode | None = None
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None
    connect_timeout: int | None = Field(default=None, ge=1, le=3600)
    application_name: str | None = None
    target_session_attrs: TargetSessionAttrs | None = None
    channel_binding: ChannelBinding | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        _check_text(self.user, "user name", max_bytes=MAX_IDENTIFIER_BYTES)
        _check_text(self.database, "database name", max_bytes=MAX_IDENTIFIER_BYTES)
        if self.password is not None:
            _check_text(self.password.get_secret_value(), "password", max_chars=MAX_DSN_CHARS)
        if self.application_name is not None:
            _check_text(self.application_name, "application_name", max_bytes=MAX_IDENTIFIER_BYTES)
        for name in ("sslrootcert", "sslcert", "sslkey"):
            value = getattr(self, name)
            if value is not None:
                _check_text(value, name, max_chars=MAX_PARAM_VALUE_CHARS)
        return self

    @classmethod
    def parse(cls, raw: str, origin: DsnOrigin = DsnOrigin.WEB) -> Self:
        """Parse and validate; ``origin`` defaults to the strict WEB rules (see ``validate_for``)."""
        return cls._parse(raw).validate_for(origin)

    def validate_for(self, origin: DsnOrigin) -> Self:
        """Extra rules for a DSN typed into the web UI.

        A browser must not make the server open local files or sockets: unix-socket hosts
        (peer/trust auth as the server's OS user), sslcert/sslkey and sslrootcert other than
        "system" (arbitrary server paths) are accepted only from TAM_TEAM_DATABASE_URL or
        the CLI (origin ENV).
        """
        if origin is DsnOrigin.ENV:
            return self
        if origin is not DsnOrigin.WEB:
            raise _fail("unknown DSN origin")
        if self.password is None:
            raise _fail("a DSN entered in the web UI must include a password; passwordless, certificate "
                        "and peer authentication are accepted only from TAM_TEAM_DATABASE_URL or the CLI")
        if any(_is_socket_path(host.host) for host in self.hosts):
            raise _fail("unix-socket hosts are accepted only from TAM_TEAM_DATABASE_URL or the CLI; "
                        "enter a TCP host name or address")
        for name in WEB_FORBIDDEN_FILE_PARAMS:
            if getattr(self, name) is not None:
                raise _fail(f"{name} names a file on the server and is accepted only from "
                            "TAM_TEAM_DATABASE_URL or the CLI")
        if self.sslrootcert is not None and self.sslrootcert not in WEB_SSLROOTCERT_VALUES:
            raise _fail("from the web UI sslrootcert may only be 'system' (the OS trust store); "
                        "certificate files are accepted only from TAM_TEAM_DATABASE_URL or the CLI")
        return self

    @classmethod
    def _parse(cls, raw: str) -> Self:
        if not isinstance(raw, str):
            raise _fail("the DSN must be a string")
        text = raw.strip()
        if not text:
            raise _fail("the DSN must not be empty")
        if len(text) > MAX_DSN_CHARS:
            raise _fail(f"the DSN must be at most {MAX_DSN_CHARS} characters")
        scheme = next((prefix for prefix in URI_SCHEMES if text[:len(prefix)].lower() == prefix), None)
        if scheme is None:
            if "://" not in text and "=" in text:
                raise _fail("key=value connection strings are not accepted; use a postgresql:// URI")
            raise _fail("only postgresql:// or postgres:// URIs are accepted")
        if any(ch.isspace() or not ch.isprintable() for ch in text):
            raise _fail("the DSN must not contain spaces or control characters; percent-encode them")
        rest = text[len(scheme):]
        if "#" in rest:
            raise _fail("the DSN must not contain a fragment (#); percent-encode it as %23")
        cut = min((index for index in (rest.find("/"), rest.find("?")) if index >= 0), default=len(rest))
        authority, remainder = rest[:cut], rest[cut:]
        userinfo, at, hostlist = authority.rpartition("@")
        if not at:
            raise _fail("the DSN must name a user: postgresql://user@host/database")
        user_part, colon, password_part = userinfo.partition(":")
        user = _decode(user_part, "user name")
        password = _decode(password_part, "password") if colon and password_part else None
        hosts = tuple(cls._parse_host(item) for item in hostlist.split(","))
        path, question, query = remainder.partition("?")
        if not path or path == "/":
            raise _fail("the DSN must name a database: postgresql://user@host/database")
        if "/" in path[1:]:
            raise _fail("the database name must not contain '/'; percent-encode it")
        database = _decode(path[1:], "database name")
        params = cls._parse_query(query) if question else {}
        timeout = params.pop("connect_timeout", None)
        if timeout is not None and not (timeout.isascii() and timeout.isdigit()):
            raise _fail("connect_timeout must be a whole number of seconds")
        try:
            sslmode = SslMode(params.pop("sslmode")) if "sslmode" in params else None
            attrs = TargetSessionAttrs(params.pop("target_session_attrs")) if "target_session_attrs" in params else None
            binding = ChannelBinding(params.pop("channel_binding")) if "channel_binding" in params else None
        except ValueError as exc:
            raise _fail("sslmode, target_session_attrs or channel_binding has an unsupported value; "
                        f"sslmode is one of {', '.join(mode.value for mode in SslMode)}") from exc
        if timeout is not None and not 1 <= int(timeout) <= 3600:
            raise _fail("connect_timeout must be between 1 and 3600 seconds")
        return cls(user=user, password=SecretStr(password) if password is not None else None, hosts=hosts,
                   database=database, sslmode=sslmode, target_session_attrs=attrs, channel_binding=binding,
                   connect_timeout=int(timeout) if timeout is not None else None, **params)

    @staticmethod
    def _parse_host(item: str) -> DsnHost:
        if not item:
            raise _fail("the DSN must name a host: postgresql://user@host/database")
        if item.startswith("["):
            host, bracket, tail = item[1:].partition("]")
            if not bracket or (tail and not tail.startswith(":")):
                raise _fail("an IPv6 host must be written as [address]:port")
            port_text = tail[1:] if tail else ""
            try:
                if ipaddress.ip_address(host).version != 6:
                    raise ValueError(host)
            except ValueError as exc:
                raise _fail("an IPv6 host must be written as [address]:port") from exc
        else:
            host_part, colon, port_text = item.rpartition(":")
            host = _decode(host_part if colon else port_text, "host")
            port_text = port_text if colon else ""
        if not host:
            raise _fail("the DSN must name a host: postgresql://user@host/database")
        if port_text and (not (port_text.isascii() and port_text.isdigit()) or not 1 <= int(port_text) <= MAX_PORT):
            raise _fail(f"port must be a number between 1 and {MAX_PORT}")
        _check_host(host)
        return DsnHost(host=host, port=int(port_text) if port_text else DEFAULT_PORT)

    @staticmethod
    def _parse_query(query: str) -> dict[str, str]:
        params: dict[str, str] = {}
        for pair in query.split("&"):
            name_part, equals, value_part = pair.partition("=")
            name = _decode(name_part, "parameter name")
            if not equals or not name:
                raise _fail("query parameters must be written as name=value")
            if name in FORBIDDEN_PARAM_MESSAGES:
                raise _fail(FORBIDDEN_PARAM_MESSAGES[name])
            if name not in ALLOWED_PARAMS:
                shown = name if name.isprintable() and len(name) <= MAX_ECHOED_PARAM_NAME else "(unnamed)"
                raise _fail(f"parameter {shown!r} is not allowed; allowed: {', '.join(ALLOWED_PARAMS)}")
            if name in params:
                raise _fail(f"parameter {name!r} is repeated")
            value = _decode(value_part, name)
            _check_text(value, name, max_chars=MAX_PARAM_VALUE_CHARS)
            params[name] = value
        return params

    def connect_overrides(self, origin: DsnOrigin, passfile: str | PathLike[str]) -> Mapping[str, str]:
        """Extra libpq keywords every connection made with this DSN must pass (pure, no I/O).

        WEB: ``passfile`` must be an existing EMPTY file, mode 0600, created by the caller,
        so ~/.pgpass of the server's user is never consulted; client certificates, GSS
        encryption and every authentication method that does not use the DSN's password
        are refused. ENV: the operator's environment is trusted; nothing is overridden.
        Use as ``psycopg.connect(dsn.to_uri(), **dsn.connect_overrides(origin, path))``;
        requires libpq >= MIN_LIBPQ_VERSION for WEB.
        """
        if origin is DsnOrigin.ENV:
            return {}
        if origin is not DsnOrigin.WEB:
            raise _fail("unknown DSN origin")
        path = str(passfile)
        if not path:
            raise _fail("an empty passfile path is required for a web DSN")
        return {"passfile": path, "sslcertmode": "disable", "gssencmode": "disable",
                "require_auth": WEB_REQUIRE_AUTH}

    @property
    def effective_sslmode(self) -> SslMode:
        return self.sslmode or LIBPQ_DEFAULT_SSLMODE

    @property
    def is_local(self) -> bool:
        return all(host.is_local for host in self.hosts)

    def warnings(self) -> tuple[str, ...]:
        if self.is_local or self.effective_sslmode not in WEAK_SSL_MODES:
            return ()
        return ((f"sslmode={self.effective_sslmode.value} does not require TLS to a non-local host; "
                 "use sslmode=require or verify-full"),)

    def _query(self) -> str:
        pairs = []
        for name in ALLOWED_PARAMS:
            value = getattr(self, name)
            if value is not None:
                pairs.append(f"{name}={quote(str(value), safe='/:')}")
        return "?" + "&".join(pairs) if pairs else ""

    def _uri(self, password: str | None) -> str:
        userinfo = quote(self.user, safe="")
        if password is not None:
            userinfo += ":" + password
        hosts = ",".join(host.encoded() for host in self.hosts)
        return f"{CANONICAL_SCHEME}{userinfo}@{hosts}/{quote(self.database, safe='')}{self._query()}"

    def to_uri(self) -> str:
        """Full canonical URI including the password: for the driver and the encrypted config only."""
        secret = None if self.password is None else quote(self.password.get_secret_value(), safe="")
        return self._uri(secret)

    def masked(self) -> str:
        return self._uri(None if self.password is None else PASSWORD_MASK)

    def host_db(self) -> str:
        return ",".join(host.display() for host in self.hosts) + "/" + self.database

    def audit(self) -> DsnAudit:
        return DsnAudit(host=",".join(host.display() for host in self.hosts), database=self.database,
                        sslmode=self.effective_sslmode)

    def __repr__(self) -> str:
        return f"DatabaseDsn({self.masked()!r})"

    def __str__(self) -> str:
        return self.masked()


class ConfigSource(StrEnum):
    """Where the effective database came from; precedence web > env > default (plan 4.2)."""

    WEB = "web"
    ENV = "env"
    DEFAULT = "default"


class DatabaseConfigSnapshot(DTO):
    """One version of ``<root>/database.json`` without its rollback link."""

    format: Literal[1] = DATABASE_CONFIG_FORMAT
    backend: Backend
    dsn_token: str | None = Field(default=None, min_length=1, max_length=8192,
                                  description="Fernet token (load_cipher(root)) of DatabaseDsn.to_uri()")
    instance_id: UUID
    generation: int = Field(ge=1)
    updated_at: AwareDatetime
    updated_by: str = Field(min_length=1, max_length=128)
    archive: str | None = Field(default=None, pattern=ARCHIVE_PATH_PATTERN,
                                description="Root-relative SQLite archive made when PostgreSQL was activated")
    origin: DsnOrigin = Field(default=DsnOrigin.ENV,
                              description="Who supplied the DSN: WEB for the dashboard or setup wizard (writers "
                                          "must set it; connections then apply connect_overrides(WEB)), ENV for "
                                          "TAM_TEAM_DATABASE_URL / CLI. Files written before this field read as ENV.")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.backend is Backend.POSTGRES) != (self.dsn_token is not None):
            raise ValueError("dsn_token is required for postgres and forbidden for sqlite")
        if self.archive is not None and self.backend is not Backend.POSTGRES:
            raise ValueError("only a postgres config records a SQLite archive")
        return self


class PostgresMarker(DTO):
    """``<root>/postgres-installation.json`` (mode 0600): this directory runs on PostgreSQL. Written
    on every PostgreSQL start; a start without any DSN refuses instead of opening an empty SQLite
    installation next to it. Holds no secret."""

    format: Literal[1] = DATABASE_CONFIG_FORMAT
    instance_id: UUID
    target: str = Field(min_length=1, max_length=2048, description="DatabaseDsn.host_db() of the database")


class DatabaseConfig(DatabaseConfigSnapshot):
    """``<root>/database.json`` (mode 0600, atomic replace). ``previous`` is the config this
    one replaced, kept for rollback (plan 4.1 / D2)."""

    previous: DatabaseConfigSnapshot | None = None

    @model_validator(mode="after")
    def _validate_previous(self) -> Self:
        if self.previous is not None:
            if self.previous.instance_id != self.instance_id:
                raise ValueError("previous config belongs to another installation")
            if self.previous.generation >= self.generation:
                raise ValueError("previous generation must be lower")
        return self

    def snapshot(self) -> DatabaseConfigSnapshot:
        return DatabaseConfigSnapshot(**self.model_dump(exclude={"previous"}))


class EffectiveDatabase(DTO):
    """The resolved choice before the instance guard runs: decrypted DSN plus its source."""

    backend: Backend
    source: ConfigSource
    dsn: DatabaseDsn | None = None
    instance_id: UUID | None = None
    generation: int = Field(default=0, ge=0)
    origin: DsnOrigin = Field(default=DsnOrigin.ENV, description="DatabaseConfig.origin for web configs")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.backend is Backend.POSTGRES) != (self.dsn is not None):
            raise ValueError("dsn is required for postgres and forbidden for sqlite")
        if self.origin is DsnOrigin.WEB and self.source is not ConfigSource.WEB:
            raise ValueError("only a web config can have origin web")
        if self.source is ConfigSource.DEFAULT and self.backend is not Backend.SQLITE:
            raise ValueError("the default backend is sqlite")
        if self.source is ConfigSource.WEB and self.instance_id is None:
            raise ValueError("a web config always carries its instance_id")
        return self

    def active(self, instance_id: UUID, settings: DatabaseSettings | None = None,
               passfile: str | PathLike[str] | None = None) -> ActiveDatabase:
        """The control-plane target. A WEB-origin PostgreSQL DSN needs ``passfile`` (an
        existing empty 0600 file) and carries ``connect_overrides(WEB, passfile)``."""
        if self.instance_id is not None and self.instance_id != instance_id:
            raise DatabaseStartupRefused(StartupRefusal.FOREIGN_INSTALLATION,
                                         "The configured database belongs to another TAM installation")
        options: tuple[tuple[str, str], ...] = ()
        if self.dsn is not None and self.origin is DsnOrigin.WEB:
            if passfile is None:
                raise ValueError("a web-origin DSN needs an empty passfile to connect")
            options = tuple(sorted(self.dsn.connect_overrides(self.origin, passfile).items()))
        return ActiveDatabase(backend=self.backend, instance_id=str(instance_id), generation=self.generation,
                              url=self.dsn.to_uri() if self.dsn is not None else None,
                              settings=settings or DatabaseSettings(), connect_options=options)


class CheckId(StrEnum):
    CONNECT = "connect"
    SERVER_VERSION = "server_version"
    ENCODING = "encoding"
    EXTENSIONS = "extensions"
    PRIVILEGES = "privileges"
    TARGET_STATE = "target_state"


CHECK_ORDER = tuple(CheckId)


class CheckStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class ErrorCategory(StrEnum):
    """Fixed categories that replace driver error text (which may echo the DSN)."""

    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    SSL_REQUIRED = "ssl_required"
    TIMEOUT = "timeout"
    NO_DATABASE = "no_database"
    PERMISSION = "permission"
    UNEXPECTED = "unexpected"


class TargetState(StrEnum):
    EMPTY = "empty"
    SAME_INSTALLATION = "same_installation"
    FOREIGN_INSTALLATION = "foreign_installation"
    UNKNOWN = "unknown"


class DatabaseCheck(DTO):
    id: CheckId
    status: CheckStatus
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    category: ErrorCategory | None = None
    dba_sql: tuple[str, ...] = Field(default=(), description="Exact SQL for the DBA that fixes a failed check")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.status in (CheckStatus.PASSED, CheckStatus.SKIPPED) and (self.category or self.dba_sql):
            raise ValueError("only failed or warning checks carry a category or DBA SQL")
        return self


class CheckReport(OutputDTO):
    """Result of the Test button (plan 4.7): all six checks, in CHECK_ORDER."""

    dsn_masked: str
    checks: tuple[DatabaseCheck, ...]
    target_state: TargetState
    target_instance_id: UUID | None = None
    server_version: str | None = None
    warnings: tuple[str, ...] = ()
    checked_at: AwareDatetime
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if tuple(check.id for check in self.checks) != CHECK_ORDER:
            raise ValueError("a report carries every check exactly once, in CHECK_ORDER")
        if (self.target_state is TargetState.EMPTY) and self.target_instance_id is not None:
            raise ValueError("an empty target has no instance_id")
        return self

    @computed_field
    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAILED for check in self.checks)

    def check(self, check_id: CheckId) -> DatabaseCheck:
        return self.checks[CHECK_ORDER.index(check_id)]


class SqliteArchive(DTO):
    path: str = Field(pattern=ARCHIVE_PATH_PATTERN)
    created_at: AwareDatetime
    bytes: int = Field(ge=0)


class DatabaseKind(StrEnum):
    IDENTITY = "identity"
    LEARNING = "learning"
    WORKSPACE = "workspace"


class TableEstimate(DTO):
    name: str = Field(min_length=1, max_length=128)
    rows: int = Field(ge=0)
    bytes: int = Field(ge=0)


class DatabaseEstimate(OutputDTO):
    kind: DatabaseKind
    name: str = Field(min_length=1, max_length=128, description="'identity', 'learning' or the workspace key")
    tables: tuple[TableEstimate, ...]

    @computed_field
    @property
    def rows(self) -> int:
        return sum(table.rows for table in self.tables)

    @computed_field
    @property
    def bytes(self) -> int:
        return sum(table.bytes for table in self.tables)


class QuarantineEstimate(DTO):
    """Orphan rows of one table that the migration moves to the workspace's quarantine
    table instead of copying. A warning, never a blocker."""

    database: str = Field(min_length=1, max_length=128, description="Workspace key")
    table: str = Field(min_length=1, max_length=128)
    rows: int = Field(ge=0)
    reasons: tuple[str, ...] = Field(max_length=8,
                                     description="e.g. fk:knowledge_nodes.node_id->graph_nodes.id")
    sample_pks: tuple[str, ...] = Field(default=(), max_length=5, description="JSON text of primary keys")
    audit: bool = Field(default=False, description="Rows of tam_history / tam_authorship")


class MigrationPlan(OutputDTO):
    """Dry-run result (plan 5.1). Holds no secret: the runner keeps the DSN under ``plan_id``."""

    plan_id: UUID
    created_at: AwareDatetime
    expires_at: AwareDatetime
    created_by: str = Field(min_length=1, max_length=128)
    target: str = Field(description="Masked DSN")
    report: CheckReport
    databases: tuple[DatabaseEstimate, ...]
    estimated_seconds: int = Field(ge=0)
    blockers: tuple[str, ...] = ()
    quarantine: tuple[QuarantineEstimate, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        blocked = not self.report.ok or self.report.target_state not in (TargetState.EMPTY,
                                                                          TargetState.SAME_INSTALLATION)
        if blocked and not self.blockers:
            raise ValueError("a plan with failed checks or a foreign target must list its blockers")
        return self

    @computed_field
    @property
    def resumable(self) -> bool:
        return self.report.target_state is TargetState.SAME_INSTALLATION

    @computed_field
    @property
    def ready(self) -> bool:
        return not self.blockers

    @computed_field
    @property
    def total_rows(self) -> int:
        return sum(database.rows for database in self.databases)

    @computed_field
    @property
    def total_bytes(self) -> int:
        return sum(database.bytes for database in self.databases)

    @computed_field
    @property
    def quarantined_rows(self) -> int:
        return sum(item.rows for item in self.quarantine)


class MigrationPhase(StrEnum):
    PREFLIGHT = "preflight"
    MAINTENANCE = "maintenance"
    COPY_CONTROL = "copy_control"
    COPY_WORKSPACES = "copy_workspaces"
    VERIFY = "verify"
    ACTIVATE = "activate"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PHASES = frozenset({MigrationPhase.DONE, MigrationPhase.FAILED, MigrationPhase.CANCELLED})
CANCELLABLE_PHASES = frozenset({MigrationPhase.PREFLIGHT, MigrationPhase.MAINTENANCE,
                                MigrationPhase.COPY_CONTROL, MigrationPhase.COPY_WORKSPACES,
                                MigrationPhase.VERIFY})


class MigrationProgress(OutputDTO):
    """Job state, also the journal ``<root>/migration/<job_id>.json`` (plan 5.3)."""

    job_id: UUID
    plan_id: UUID
    phase: MigrationPhase
    started_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    started_by: str = Field(min_length=1, max_length=128)
    target: str = Field(description="Masked DSN")
    workspace_index: int = Field(default=0, ge=0)
    workspace_total: int = Field(default=0, ge=0)
    database: str | None = Field(default=None, description="'identity', 'learning' or the workspace key being copied")
    table: str | None = None
    rows_copied: int = Field(default=0, ge=0)
    rows_total: int = Field(default=0, ge=0)
    completed_workspaces: tuple[str, ...] = Field(default=(), description="Keys skipped on resume")
    resumed: bool = False
    cancel_requested: bool = False
    error: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    error_category: ErrorCategory | None = None
    quarantined_rows: int = Field(default=0, ge=0, description="Counted in rows_copied as processed")
    quarantine: tuple[QuarantineEstimate, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.phase in TERMINAL_PHASES) != (self.finished_at is not None):
            raise ValueError("finished_at is set exactly for done, failed and cancelled")
        if (self.phase is MigrationPhase.FAILED) != (self.error is not None):
            raise ValueError("error is set exactly for a failed job")
        if self.error_category is not None and self.phase is not MigrationPhase.FAILED:
            raise ValueError("error_category belongs to a failed job")
        if self.workspace_index > self.workspace_total or self.rows_copied > self.rows_total:
            raise ValueError("progress counters exceed their totals")
        return self

    @computed_field
    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @computed_field
    @property
    def cancellable(self) -> bool:
        return self.phase in CANCELLABLE_PHASES and not self.cancel_requested

    @computed_field
    @property
    def percent(self) -> float:
        if self.phase is MigrationPhase.DONE:
            return 100.0
        if not self.rows_total:
            return 0.0
        return round(100.0 * self.rows_copied / self.rows_total, 1)


class MaintenanceReason(StrEnum):
    MIGRATION = "migration"
    ROLLBACK = "rollback"
    # Another server took the PostgreSQL lease: sticky (leave() cannot lift it, job_id is
    # None) until this process restarts.
    LEASE_LOST = "lease_lost"


class MaintenanceState(DTO):
    reason: MaintenanceReason
    job_id: UUID | None = None
    since: AwareDatetime
    retry_after_seconds: int = Field(default=MAINTENANCE_RETRY_AFTER_SECONDS, ge=1)


class DatabaseConfigView(DTO):
    """GET /dashboard/api/admin/database. Never carries the DSN itself, only its mask."""

    backend: Backend
    source: ConfigSource
    dsn_masked: str | None = None
    host_db: str | None = None
    sslmode: SslMode | None = None
    warnings: tuple[str, ...] = ()
    instance_id: UUID | None = None
    generation: int = Field(default=0, ge=0)
    updated_at: AwareDatetime | None = None
    updated_by: str | None = None
    env_configured: bool = Field(default=False, description=f"{DATABASE_URL_ENV} is set in the environment")
    last_check: CheckReport | None = None
    migration: MigrationProgress | None = None
    archive: SqliteArchive | None = None
    rollback_available: bool = False
    maintenance: MaintenanceState | None = Field(default=None, description="Current gate state, lease_lost included")


def maintenance_allows(method: str, path: str) -> bool:
    """Whether a request passes the maintenance gate (decision D3).

    MCP and the worker call API get 503. Any other mutating request gets 503 wherever it is
    routed (dashboard, learning, reports and future routes alike, sign-in and sign-out
    included), except cancelling the migration, so no write can land in a database that is
    being copied.
    Reads, static assets and health pass.
    """
    if any(path == prefix or path.startswith(prefix + "/") for prefix in MCP_PATH_PREFIXES):
        return False
    if method.upper() in SAFE_METHODS:
        return True
    return path in MAINTENANCE_EXEMPT_PATHS


class DatabaseTestRequest(DTO):
    dsn: SecretStr


class DatabasePlanRequest(DTO):
    dsn: SecretStr


class MigrationStartRequest(DTO):
    plan_id: UUID
    confirm: Literal[True]


class DatabaseRepointRequest(DTO):
    dsn: SecretStr


class DatabaseRollbackRequest(DTO):
    organization: str = Field(min_length=1, max_length=128,
                              description="Typed confirmation: must equal the organization name")


class SetupDatabaseTestRequest(DTO):
    token: str = Field(min_length=1, max_length=64)
    dsn: SecretStr


class SetupDatabaseRequest(DTO):
    token: str = Field(min_length=1, max_length=64)
    backend: Backend
    dsn: SecretStr | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if (self.backend is Backend.POSTGRES) != (self.dsn is not None):
            raise ValueError("a DSN is required for postgres and forbidden for sqlite")
        return self


class DatabaseConfigStore(Protocol):
    """``<root>/database.json`` plus DSN precedence (W4, database_config.py)."""

    def load(self) -> DatabaseConfig | None:
        """None when the file does not exist; DatabaseStartupRefused(CONFIG_UNREADABLE) when corrupt."""
        ...

    def save(self, config: DatabaseConfig) -> None:
        """Atomic: tmp file, fsync, os.replace, mode 0600."""
        ...

    def seal(self, dsn: DatabaseDsn) -> str: ...

    def unseal(self, config: DatabaseConfigSnapshot) -> DatabaseDsn | None:
        """Decrypted DSN (None for sqlite); DatabaseStartupRefused(KEY_UNAVAILABLE) when the key is lost."""
        ...

    def effective(self) -> EffectiveDatabase:
        """web (database.json) > TAM_TEAM_DATABASE_URL > SQLite default."""
        ...


class DatabaseChecker(Protocol):
    """Runs the Test-button checks against a DSN that may not be saved (W4, db_check.py)."""

    def check(self, dsn: DatabaseDsn, *, instance_id: UUID | None) -> CheckReport: ...


class DatabaseConfigService(Protocol):
    """Dashboard-facing configuration operations (W4); handlers only call these."""

    def view(self) -> DatabaseConfigView: ...

    def test(self, dsn: DatabaseDsn, actor: str) -> CheckReport:
        """Audited as database_tested; the DSN is neither stored nor logged."""
        ...

    def repoint(self, dsn: DatabaseDsn, actor: str) -> DatabaseConfigView:
        """Same installation only (instance_id must match); Conflict otherwise."""
        ...


class MigrationRunner(Protocol):
    """SQLite -> PostgreSQL migration job and rollback (W5, migration_service.py)."""

    def plan(self, dsn: DatabaseDsn, actor: str) -> MigrationPlan: ...

    def start(self, plan_id: UUID, actor: str) -> MigrationProgress:
        """NotFound for an unknown or expired plan, Conflict when a job runs or the plan is not ready."""
        ...

    def progress(self) -> MigrationProgress | None: ...

    def cancel(self, actor: str) -> MigrationProgress: ...

    def rollback(self, organization: str, actor: str) -> DatabaseConfig:
        """Restore the SQLite archive; writes made on PostgreSQL after the switch are lost (D2)."""
        ...


class MaintenanceGate(Protocol):
    """Process-wide maintenance switch consulted by the app middleware and WorkerPool."""

    def state(self) -> MaintenanceState | None: ...

    def enter(self, reason: MaintenanceReason, job_id: UUID | None) -> MaintenanceState: ...

    def leave(self) -> None: ...

