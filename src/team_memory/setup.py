"""First-run setup of the team server: a one-time setup code guards creating the first superadmin."""
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import Field

from team_memory.accounts import (
    INVITE_ALPHABET,
    Accounts,
    Session,
    hash_password,
    normalize_invite,
    validate_password,
)
from team_memory.contracts import (
    DTO,
    Conflict,
    NotFound,
    RateLimited,
    SetupComplete,
    Unauthorized,
)
from team_memory.database import serializable
from team_memory.metrics import Metrics
from team_memory.registry import NOW, Registry

LOGGER = logging.getLogger(__name__)
TOKEN_LENGTH = 24
TOKEN_GROUP = 4
TTL_ENV = "TAM_TEAM_SETUP_TOKEN_TTL_MINUTES"
SETUP_PATH = "/dashboard/"
SUPPORT_URL = "https://totalmemory.dev/pricing"
SUPPORT_LINE = "Need help with rollout or support? See " + SUPPORT_URL


class SetupPolicy(DTO):
    token_ttl_seconds: int = Field(default=60 * 60, gt=0)
    max_failures_per_ip: int = Field(default=5, gt=0)
    max_failures_total: int = Field(default=50, gt=0)
    failure_window_seconds: int = Field(default=15 * 60, gt=0)
    lock_seconds: int = Field(default=15 * 60, gt=0)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SetupPolicy":
        env = os.environ if environ is None else environ
        raw = env.get(TTL_ENV, "").strip()
        return cls(token_ttl_seconds=int(raw) * 60) if raw else cls()


class SetupToken(DTO):
    token: str
    expires_at: str


class SetupStatus(DTO):
    required: bool
    token_active: bool
    expires_at: str | None
    organization: dict[str, str]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def setup_url(base_url: str, token: str) -> str:
    return base_url.rstrip("/") + SETUP_PATH + "#setup=" + token


class SetupService:
    def __init__(self, registry: Registry, accounts: Accounts, metrics: Metrics, policy: SetupPolicy | None = None,
                 clock: Callable[[], float] = time.time):
        self.registry, self.accounts, self.metrics = registry, accounts, metrics
        self.plane = registry.plane
        self.policy = policy or SetupPolicy()
        self.clock = clock

    def required(self) -> bool:
        return not self.registry.has_superadmin()

    def status(self) -> SetupStatus:
        self._require_open()
        expires = self._token_expiry()
        return SetupStatus(required=True, token_active=expires is not None,
                           expires_at=_iso(expires) if expires is not None else None,
                           organization=self.registry.organization())

    @serializable
    def _token_expiry(self) -> float | None:
        with self.registry.connect() as db:
            row = db.execute("SELECT MAX(expires_at) FROM setup_tokens WHERE used_at IS NULL AND expires_at>?",
                             (self.clock(),)).fetchone()
        return row[0] if row is not None else None

    @serializable
    def issue_token(self) -> SetupToken:
        raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(TOKEN_LENGTH))
        now = self.clock()
        expires = now + self.policy.token_ttl_seconds
        with self.registry.connect(write=True) as db:
            if Registry.superadmin_exists(db):
                raise Conflict("Setup is already complete; sign in to the dashboard instead")
            db.execute("DELETE FROM setup_tokens WHERE used_at IS NULL")
            db.execute("INSERT INTO setup_tokens(digest,created_at,expires_at) VALUES (?,?,?)",
                       (Registry.digest(raw), now, expires))
            Registry._event(db, "setup_token_issued", "setup", "expires " + _iso(expires))
        LOGGER.info(json.dumps({"event": "setup_token_issued", "expires_at": _iso(expires)}))
        return SetupToken(token="-".join(raw[i:i + TOKEN_GROUP] for i in range(0, TOKEN_LENGTH, TOKEN_GROUP)),
                          expires_at=_iso(expires))

    def announce(self, base_url: str) -> SetupToken | None:
        if not self.required():
            return None
        issued = self.issue_token()
        rule = "=" * 72
        LOGGER.warning("\n".join((
            rule,
            "  Team Memory has no administrator yet. Finish setup in the browser:",
            "",
            "    " + setup_url(base_url, issued.token),
            "",
            "  Setup code: " + issued.token,
            "  Valid until " + issued.expires_at + ", single use.",
            "  New code: restart the server or run `tam-team --root <data dir> setup-token`.",
            rule)))
        return issued

    def verify(self, token: str, ip: str) -> None:
        self._require_open()
        self._check_lock(ip)
        if not self._token_valid(token):
            self._fail(ip, "verify")
            raise Unauthorized("Invalid or expired setup code")
        self.metrics.count("setup", step="verify", outcome="success")

    @serializable
    def _token_valid(self, token: str) -> bool:
        with self.registry.connect() as db:
            return db.execute("SELECT 1 FROM setup_tokens WHERE digest=? AND used_at IS NULL AND expires_at>?",
                              (Registry.digest(normalize_invite(token)), self.clock())).fetchone() is not None

    def complete(self, request: SetupComplete, ip: str) -> Session:
        self._require_open()
        self._check_lock(ip)
        validate_password(request.password, request.user_id)
        Registry._identifier(request.user_id)
        Registry._name(request.name)
        organization = {"name": request.company_name, "setup_state": "admin_created"}
        if request.public_url:
            organization["public_url"] = request.public_url
        with self.registry.acting_as(request.user_id):
            claimed = self._claim(request, hash_password(request.password), organization)
        if not claimed:
            self._fail(ip, "complete")
            raise Unauthorized("Invalid or expired setup code")
        self._clear(ip)
        self.metrics.count("setup", step="complete", outcome="success")
        return self.accounts.start_session(request.user_id)

    @serializable
    def _claim(self, request: SetupComplete, password_hash: str, organization: dict[str, str]) -> bool:
        with self.registry.connect(write=True) as db:
            claimed = db.execute("UPDATE setup_tokens SET used_at=? WHERE digest=? AND used_at IS NULL AND expires_at>?",
                                 (self.clock(), Registry.digest(normalize_invite(request.token)),
                                  self.clock())).rowcount == 1
            if claimed:
                if Registry.superadmin_exists(db):
                    raise NotFound("Setup is already complete")
                try:
                    db.execute(f"INSERT INTO users(id,name,org_role,password_hash,created_at) "
                               f"VALUES (?,?,'superadmin',?,{NOW})", (request.user_id, request.name, password_hash))
                except sqlite3.IntegrityError as exc:
                    raise Conflict("That user ID already exists; choose another one") from exc
                Registry._event(db, "user_created", request.user_id)
                Registry._event(db, "org_role:superadmin", request.user_id)
                Registry.write_organization(db, organization)
                db.execute("DELETE FROM setup_tokens WHERE used_at IS NULL")
                Registry._event(db, "setup_completed", request.user_id, "web wizard")
        return claimed

    def _require_open(self) -> None:
        if not self.required():
            raise NotFound("Not found")

    def _keys(self, ip: str) -> list[tuple[str, int]]:
        return [(Registry.digest("setup|ip|" + ip), self.policy.max_failures_per_ip),
                (Registry.digest("setup|all"), self.policy.max_failures_total)]

    @serializable
    def _check_lock(self, ip: str) -> None:
        now = self.clock()
        with self.registry.connect() as db:
            for key, _limit in self._keys(ip):
                row = db.execute("SELECT locked_until FROM login_failures WHERE key=?", (key,)).fetchone()
                if row is not None and row["locked_until"] > now:
                    self.metrics.count("setup", step="lock", outcome="rate_limited")
                    raise RateLimited("Too many wrong setup codes; try again later")

    @serializable
    def _fail(self, ip: str, step: str) -> None:
        now = self.clock()
        self.metrics.count("setup", step=step, outcome="invalid_code")
        with self.registry.connect() as db:
            for key, limit in self._keys(ip):
                row = db.execute("SELECT failures,window_start FROM login_failures WHERE key=?", (key,)).fetchone()
                if row is None or row["window_start"] + self.policy.failure_window_seconds <= now:
                    failures, start = 1, now
                else:
                    failures, start = row["failures"] + 1, row["window_start"]
                locked = now + self.policy.lock_seconds if failures >= limit else 0
                db.execute("INSERT INTO login_failures(key,failures,window_start,locked_until) VALUES (?,?,?,?) "
                           "ON CONFLICT(key) DO UPDATE SET failures=excluded.failures,"
                           "window_start=excluded.window_start,locked_until=excluded.locked_until",
                           (key, failures, start, locked))
                if locked:
                    Registry._event(db, "setup_locked", "setup", "lockout")

    @serializable
    def _clear(self, ip: str) -> None:
        with self.registry.connect() as db:
            db.execute("DELETE FROM login_failures WHERE key=?", (self._keys(ip)[0][0],))
