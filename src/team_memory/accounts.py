import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import Field

from team_memory.contracts import DTO, Actor, DomainError, RateLimited, Unauthorized
from team_memory.database import serializable
from team_memory.registry import AUDIT_ACTOR, NOW, Registry

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
SESSION_BYTES = 32
CSRF_BYTES = 32
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_LENGTH = 20
INVITE_GROUP = 4
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 256
DASHBOARD_CLIENT = "dashboard"
TOKEN_SUBJECT = "(token)"
LOGIN_LOG_RETENTION_SECONDS = 30 * 24 * 3600
_DUMMY_HASH = None


class AccountPolicy(DTO):
    session_idle_seconds: int = Field(default=30 * 60, gt=0)
    session_max_seconds: int = Field(default=12 * 3600, gt=0)
    invite_ttl_seconds: int = Field(default=72 * 3600, gt=0)
    max_failures: int = Field(default=5, gt=0)
    failure_window_seconds: int = Field(default=15 * 60, gt=0)
    lock_seconds: int = Field(default=15 * 60, gt=0)
    max_failures_per_ip: int = Field(default=30, gt=0)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "AccountPolicy":
        env = os.environ if environ is None else environ
        mapping = {"session_idle_seconds": ("TAM_TEAM_SESSION_IDLE_MINUTES", 60),
                   "session_max_seconds": ("TAM_TEAM_SESSION_MAX_HOURS", 3600),
                   "invite_ttl_seconds": ("TAM_TEAM_INVITE_TTL_HOURS", 3600),
                   "max_failures": ("TAM_TEAM_LOGIN_MAX_FAILURES", 1),
                   "lock_seconds": ("TAM_TEAM_LOGIN_LOCK_MINUTES", 60)}
        values = {field: int(env[name]) * factor for field, (name, factor) in mapping.items() if env.get(name)}
        return cls(**values)


class Session(DTO):
    session_id: str
    csrf: str
    actor: Actor
    method: str
    expires_at: float


class Invite(DTO):
    user_id: str
    code: str
    expires_at: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                             maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                 maxmem=SCRYPT_MAXMEM, dklen=len(bytes.fromhex(expected)))
    except ValueError:
        return False
    return hmac.compare_digest(derived, bytes.fromhex(expected))


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def validate_password(password: str, user_id: str) -> None:
    if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
        raise DomainError(f"Password must contain {MIN_PASSWORD_CHARS}–{MAX_PASSWORD_CHARS} characters")
    if password.strip().lower() == user_id.lower():
        raise DomainError("Password must differ from the user ID")


def normalize_invite(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


class Accounts:
    def __init__(self, registry: Registry, policy: AccountPolicy | None = None,
                 clock: Callable[[], float] = time.time, maintenance: Callable[[], bool] | None = None):
        self.registry = registry
        self.plane = registry.plane
        # During a database migration nothing may be written; a session is then checked without touching it.
        self.maintenance = maintenance or registry.plane.maintenance_active
        self.policy = policy or AccountPolicy()
        self.clock = clock

    @serializable
    def issue_invite(self, user_id: str) -> Invite:
        raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))
        code = "-".join(raw[i:i + INVITE_GROUP] for i in range(0, INVITE_LENGTH, INVITE_GROUP))
        now = self.clock()
        expires = now + self.policy.invite_ttl_seconds
        with self.registry.connect() as db:
            if db.execute("SELECT 1 FROM users WHERE id=? AND active=1", (user_id,)).fetchone() is None:
                raise DomainError("Unknown or disabled user")
            db.execute("DELETE FROM invites WHERE user_id=? AND used_at IS NULL", (user_id,))
            db.execute("INSERT INTO invites(digest,user_id,created_at,expires_at,created_by) VALUES (?,?,?,?,?)",
                       (Registry.digest(raw), user_id, now, expires, self._audit_actor()))
            Registry._event(db, "invite_issued", user_id)
        return Invite(user_id=user_id, code=code, expires_at=_iso(expires))

    def redeem_invite(self, user_id: str, code: str, password: str, ip: str) -> Session:
        self._check_lock(user_id, ip)
        validate_password(password, user_id)
        now = self.clock()
        claimed = self._claim_invite(user_id, code, hash_password(password), now)
        if claimed != 1:
            self._fail(user_id, ip, "invite")
            raise Unauthorized("Invalid or expired invite code")
        self._clear(user_id, ip)
        return self._open(user_id, "invite", None)

    @serializable
    def _claim_invite(self, user_id: str, code: str, password_hash: str, now: float) -> int:
        with self.registry.connect() as db:
            row = db.execute("SELECT i.digest FROM invites i JOIN users u ON u.id=i.user_id WHERE i.digest=? "
                             "AND i.user_id=? AND i.used_at IS NULL AND i.expires_at>? AND u.active=1",
                             (Registry.digest(normalize_invite(code)), user_id, now)).fetchone()
            if row is None:
                return 0
            claimed = db.execute("UPDATE invites SET used_at=? WHERE digest=? AND used_at IS NULL",
                                 (now, row["digest"])).rowcount
            if claimed == 1:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                Registry._event(db, "invite_redeemed", user_id)
            return claimed

    @serializable
    def _stored_hash(self, user_id: str, active_only: bool) -> str | None:
        query = "SELECT password_hash FROM users WHERE id=?" + (" AND active=1" if active_only else "")
        with self.registry.connect() as db:
            row = db.execute(query, (user_id,)).fetchone()
        return row["password_hash"] if row is not None and row["password_hash"] else None

    def login_password(self, user_id: str, password: str, ip: str) -> Session:
        self._check_lock(user_id, ip)
        stored = self._stored_hash(user_id, active_only=True)
        valid = verify_password(password, stored or _dummy_hash()) and stored is not None
        if not valid:
            self._fail(user_id, ip, "password")
            raise Unauthorized("Invalid user ID or password")
        self._clear(user_id, ip)
        return self._open(user_id, "password", None)

    def login_token(self, token: str, ip: str) -> Session:
        self._check_lock("", ip)
        try:
            actor = self.registry.authenticate(token)
        except Unauthorized:
            self._fail("", ip, "token")
            raise
        return self._open(actor.user_id, "token", Registry.digest(token))

    def change_password(self, actor: Actor, current: str, new: str, keep_session: str, ip: str) -> None:
        self._check_lock(actor.user_id, ip)
        stored = self._stored_hash(actor.user_id, active_only=False)
        if stored is None or not verify_password(current, stored):
            self._fail(actor.user_id, ip, "password_change")
            raise Unauthorized("Current password is incorrect")
        validate_password(new, actor.user_id)
        self._set_password(actor.user_id, hash_password(new), keep_session)
        self._clear(actor.user_id, ip)

    @serializable
    def _set_password(self, user_id: str, password_hash: str, keep_session: str) -> None:
        with self.registry.connect() as db:
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
            db.execute("DELETE FROM sessions WHERE user_id=? AND digest<>?", (user_id, Registry.digest(keep_session)))
            Registry._event(db, "password_changed", user_id)

    @serializable
    def session(self, session_id: str, touch: bool = True) -> Session:
        now = self.clock()
        with self.registry.connect() as db:
            row = db.execute("""
                SELECT s.digest,s.csrf,s.method,s.token_digest,s.last_seen_at,s.expires_at,u.id,u.name,u.org_role
                FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.digest=? AND u.active=1
            """, (Registry.digest(session_id),)).fetchone()
            if row is None or row["expires_at"] <= now or row["last_seen_at"] + self.policy.session_idle_seconds <= now:
                if row is not None:
                    db.execute("DELETE FROM sessions WHERE digest=?", (row["digest"],))
                raise Unauthorized("Session expired; sign in again")
            if row["token_digest"] is not None and db.execute(
                    "SELECT 1 FROM tokens WHERE digest=? AND revoked=0", (row["token_digest"],)).fetchone() is None:
                db.execute("DELETE FROM sessions WHERE digest=?", (row["digest"],))
                raise Unauthorized("Session token was revoked")
            if touch and not self.maintenance():
                db.execute("UPDATE sessions SET last_seen_at=? WHERE digest=?", (now, row["digest"]))
        actor = Actor(user_id=row["id"], display_name=row["name"], client=DASHBOARD_CLIENT, org_role=row["org_role"])
        return Session(session_id=session_id, csrf=row["csrf"], actor=actor, method=row["method"],
                       expires_at=row["expires_at"])

    @serializable
    def has_password(self, user_id: str) -> bool:
        with self.registry.connect() as db:
            row = db.execute("SELECT password_hash IS NOT NULL FROM users WHERE id=?", (user_id,)).fetchone()
        return bool(row and row[0])

    @serializable
    def logout(self, session_id: str) -> None:
        with self.registry.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (Registry.digest(session_id),))

    def start_session(self, user_id: str) -> Session:
        return self._open(user_id, "password", None)

    @serializable
    def _open(self, user_id: str, method: str, token_digest: str | None) -> Session:
        session_id, csrf = secrets.token_urlsafe(SESSION_BYTES), secrets.token_urlsafe(CSRF_BYTES)
        now = self.clock()
        with self.registry.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=? OR last_seen_at<=?",
                       (now, now - self.policy.session_idle_seconds))
            db.execute("INSERT INTO sessions(digest,user_id,csrf,method,token_digest,created_at,last_seen_at,expires_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (Registry.digest(session_id), user_id, csrf, method, token_digest,
                                                     now, now, now + self.policy.session_max_seconds))
            db.execute(f"UPDATE users SET last_login_at={NOW} WHERE id=?", (user_id,))
            self._log(db, now, user_id, method, "success")
        return self.session(session_id, touch=False)

    def _keys(self, user_id: str, ip: str) -> list[tuple[str, int]]:
        keys = [(Registry.digest("ip|" + ip), self.policy.max_failures_per_ip)]
        if user_id:
            keys.append((Registry.digest("user|" + user_id + "|" + ip), self.policy.max_failures))
        return keys

    @serializable
    def _check_lock(self, user_id: str, ip: str) -> None:
        now = self.clock()
        with self.registry.connect() as db:
            for key, _limit in self._keys(user_id, ip):
                row = db.execute("SELECT locked_until FROM login_failures WHERE key=?", (key,)).fetchone()
                if row is not None and row["locked_until"] > now:
                    raise RateLimited("Too many failed attempts; try again later")

    @serializable
    def _fail(self, user_id: str, ip: str, method: str) -> None:
        now = self.clock()
        with self.registry.connect() as db:
            self._log(db, now, user_id or TOKEN_SUBJECT, method, "failure")
            db.execute("DELETE FROM login_failures WHERE window_start<=? AND locked_until<=?",
                       (now - self.policy.failure_window_seconds, now))
            for key, limit in self._keys(user_id, ip):
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
                    Registry._event(db, "login_locked", user_id or "ip", "lockout")

    @serializable
    def _clear(self, user_id: str, ip: str) -> None:
        with self.registry.connect() as db:
            db.execute("DELETE FROM login_failures WHERE key=?", (Registry.digest("user|" + user_id + "|" + ip),))

    @serializable
    def pending_invites(self) -> list[dict]:
        with self.registry.connect() as db:
            rows = db.execute("SELECT i.user_id,u.name,i.expires_at,i.created_by FROM invites i JOIN users u "
                              "ON u.id=i.user_id WHERE i.used_at IS NULL AND i.expires_at>? ORDER BY i.expires_at",
                              (self.clock(),)).fetchall()
        return [{"user_id": row["user_id"], "name": row["name"], "expires_at": _iso(row["expires_at"]),
                 "created_by": row["created_by"]} for row in rows]

    @serializable
    def login_summary(self, hours: int) -> dict:
        now = self.clock()
        since = now - hours * 3600
        with self.registry.connect() as db:
            rows = db.execute("SELECT at,user_id,outcome FROM login_log WHERE at>=?", (since,)).fetchall()
        buckets = [0] * hours
        for row in rows:
            if row["outcome"] == "failure":
                buckets[min(hours - 1, int((row["at"] - since) // 3600))] += 1
        failures = [row for row in rows if row["outcome"] == "failure"]
        return {"hours": hours, "failures": len(failures), "successes": len(rows) - len(failures),
                "targeted_users": len({row["user_id"] for row in failures}), "failures_by_hour": buckets}

    def _log(self, db, now: float, user_id: str, method: str, outcome: str) -> None:
        db.execute("DELETE FROM login_log WHERE at<?", (now - LOGIN_LOG_RETENTION_SECONDS,))
        db.execute("INSERT INTO login_log(at,user_id,method,outcome) VALUES (?,?,?,?)", (now, user_id, method, outcome))

    @staticmethod
    def _audit_actor() -> str:
        return AUDIT_ACTOR.get()
