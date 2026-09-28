import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from tam_db.contracts import Backend
from team_memory.contracts import DomainError
from team_memory.database_contracts import (
    CHECK_ORDER,
    DATABASE_MIGRATION_API,
    DATABASE_MIGRATION_CANCEL_API,
    DATABASE_PLAN_API,
    MAX_DSN_CHARS,
    MIN_LIBPQ_VERSION,
    PASSWORD_MASK,
    CheckId,
    CheckReport,
    CheckStatus,
    ConfigSource,
    DatabaseCheck,
    DatabaseConfig,
    DatabaseConfigSnapshot,
    DatabaseConfigView,
    DatabaseDsn,
    DatabaseEstimate,
    DatabaseKind,
    DatabaseStartupRefused,
    DatabaseTestRequest,
    DsnOrigin,
    EffectiveDatabase,
    ErrorCategory,
    InvalidDsn,
    MaintenanceReason,
    MaintenanceState,
    MigrationPhase,
    MigrationPlan,
    MigrationProgress,
    MigrationStartRequest,
    QuarantineEstimate,
    SetupDatabaseRequest,
    SslMode,
    StartupRefusal,
    TableEstimate,
    TargetState,
    maintenance_allows,
)

SECRET = "Sup3r-S3cret"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
INSTANCE = uuid.UUID("0b8f7c1e-8a52-4c1d-9d57-3a3b2f4e5d6c")
OTHER_INSTANCE = uuid.UUID("5f0e3a9b-1c2d-4e5f-8a7b-9c0d1e2f3a4b")


def assert_hidden(dsn: DatabaseDsn) -> None:
    for text in (repr(dsn), str(dsn), dsn.masked(), dsn.host_db(), dsn.model_dump_json(),
                 dsn.audit().model_dump_json(), " ".join(dsn.warnings())):
        assert SECRET not in text


VALID = [
    (f"postgresql://tam:{SECRET}@db.example.com:5433/tam?sslmode=verify-full",
     f"postgresql://tam:{PASSWORD_MASK}@db.example.com:5433/tam?sslmode=verify-full", False),
    ("postgres://tam@localhost/tam", "postgresql://tam@localhost:5432/tam", True),
    (f"POSTGRESQL://tam:{SECRET}@127.0.0.1/tam", f"postgresql://tam:{PASSWORD_MASK}@127.0.0.1:5432/tam", True),
    (f"postgresql://tam:{SECRET}@[::1]:6543/tam", f"postgresql://tam:{PASSWORD_MASK}@[::1]:6543/tam", True),
    ("postgresql://tam@%2Fvar%2Frun%2Fpostgresql/tam", "postgresql://tam@%2Fvar%2Frun%2Fpostgresql:5432/tam", True),
    (f"postgresql://tam:{SECRET}@h1:5432,h2:5433/tam?target_session_attrs=read-write&sslmode=require",
     f"postgresql://tam:{PASSWORD_MASK}@h1:5432,h2:5433/tam?sslmode=require&target_session_attrs=read-write",
     False),
    ((f"  postgresql://tam:{SECRET}@db/tam?connect_timeout=5&application_name=tam-team&channel_binding=require"
      "&sslrootcert=/etc/ssl/root.crt&sslcert=/c.crt&sslkey=/c.key&sslmode=verify-ca  "),
     (f"postgresql://tam:{PASSWORD_MASK}@db:5432/tam?sslmode=verify-ca&sslrootcert=/etc/ssl/root.crt"
      "&sslcert=/c.crt&sslkey=/c.key&connect_timeout=5&application_name=tam-team&channel_binding=require"), False),
]


@pytest.mark.parametrize(("raw", "masked", "local"), VALID)
def test_valid_dsns(raw, masked, local):
    dsn = DatabaseDsn.parse(raw, origin=DsnOrigin.ENV)
    assert dsn.masked() == masked
    assert dsn.is_local is local
    assert DatabaseDsn.parse(dsn.to_uri(), origin=DsnOrigin.ENV) == dsn
    assert_hidden(dsn)


def test_percent_encoded_credentials_round_trip():
    dsn = DatabaseDsn.parse("postgresql://t%40m:p%40ss%3Aw%2Fd%25@db.example.com/my%20db?sslmode=require")
    assert dsn.user == "t@m" and dsn.database == "my db"
    assert dsn.password.get_secret_value() == "p@ss:w/d%"
    assert dsn.to_uri() == "postgresql://t%40m:p%40ss%3Aw%2Fd%25@db.example.com:5432/my%20db?sslmode=require"
    assert "p@ss" not in dsn.masked() and "p%40ss" not in dsn.masked()


def test_passwordless_dsn_is_allowed():
    dsn = DatabaseDsn.parse("postgresql://tam@db.example.com/tam?sslmode=verify-full&sslcert=/c&sslkey=/k",
                            origin=DsnOrigin.ENV)
    assert dsn.password is None and ":" not in dsn.masked().split("@")[0].removeprefix("postgresql://")


def test_empty_password_is_treated_as_none():
    assert DatabaseDsn.parse("postgresql://tam:@db/tam?sslmode=require", origin=DsnOrigin.ENV).password is None


@pytest.mark.parametrize(("raw", "fragment"), [
    (f"host=db user=tam password={SECRET} dbname=tam", "key=value"),
    (f"mysql://tam:{SECRET}@db/tam", "postgresql://"),
    (f"http://tam:{SECRET}@db/tam", "postgresql://"),
    (f"postgresql://tam:{SECRET}@db/tam?options=-csearch_path%3Dws_x", "options"),
    (f"postgresql://tam:{SECRET}@db/tam?options=", "options"),
    (f"postgresql://tam:{SECRET}@db/tam?host=evil", "not allowed"),
    (f"postgresql://tam@db/tam?password={SECRET}", "not allowed"),
    (f"postgresql://tam:{SECRET}@db/tam?sslmode=require&sslmode=disable", "repeated"),
    (f"postgresql://tam:{SECRET}@db/tam?sslmode=bogus", "sslmode"),
    (f"postgresql://tam:{SECRET}@db/tam?target_session_attrs=whatever", "sslmode"),
    (f"postgresql://tam:{SECRET}@db/tam?connect_timeout=abc", "connect_timeout"),
    (f"postgresql://tam:{SECRET}@db/tam?connect_timeout=0", "connect_timeout"),
    (f"postgresql://tam:{SECRET}@db/tam?sslmode", "name=value"),
    (f"postgresql://tam:{SECRET}@db/tam?sslmode=", "must not be empty"),
    (f"postgresql://{SECRET}db.example.com/tam", "user"),
    (f"postgresql://tam:{SECRET}@db", "database"),
    (f"postgresql://tam:{SECRET}@db/", "database"),
    (f"postgresql://tam:{SECRET}@db/tam/extra", "'/'"),
    (f"postgresql://tam:{SECRET}@/tam", "host"),
    (f"postgresql://tam:{SECRET}@db:0/tam", "port"),
    (f"postgresql://tam:{SECRET}@db:70000/tam", "port"),
    (f"postgresql://tam:{SECRET}@db:abc/tam", "port"),
    (f"postgresql://tam:{SECRET}@db:\u00b2/tam", "port"),
    (f"postgresql://tam:{SECRET}@db/tam?connect_timeout=\u00b2", "connect_timeout"),
    (f"postgresql://tam:{SECRET}@[::1/tam", "IPv6"),
    (f"postgresql://tam:{SECRET}@[127.0.0.1]:5432/tam", "IPv6"),
    (f"postgresql://tam:{SECRET}@exa..mple/tam", "host"),
    (f"postgresql://tam:{SECRET}@db/tam#frag", "fragment"),
    (f"postgresql://tam:{SECRET} @db/tam", "spaces"),
    (f"postgresql://tam:{SECRET}\x00@db/tam", "control"),
    (f"postgresql://tam:{SECRET}%zz@db/tam", "percent"),
    ("postgresql://tam:%FF@db/tam", "UTF-8"),
    ("   ", "empty"),
    ("postgresql://tam@db/" + "d" * 64, "63 bytes"),
    ("postgresql://tam@db/tam?application_name=" + "a" * 64, "63 bytes"),
])
def test_invalid_dsns_fail_without_echoing_the_secret(raw, fragment):
    with pytest.raises(InvalidDsn) as caught:
        DatabaseDsn.parse(raw)
    assert isinstance(caught.value, DomainError)
    assert fragment in str(caught.value)
    assert SECRET not in str(caught.value)


def test_non_string_dsn_is_rejected():
    with pytest.raises(InvalidDsn):
        DatabaseDsn.parse(None)


def test_dsn_length_limit():
    prefix, suffix = "postgresql://tam:", "@db/tam?sslmode=require"
    longest = prefix + "p" * (MAX_DSN_CHARS - len(prefix) - len(suffix)) + suffix
    assert len(longest) == MAX_DSN_CHARS
    assert_hidden(DatabaseDsn.parse(longest))
    with pytest.raises(InvalidDsn, match=str(MAX_DSN_CHARS)):
        DatabaseDsn.parse(prefix + "p" * (MAX_DSN_CHARS - len(prefix) - len(suffix) + 1) + suffix)


@pytest.mark.parametrize(("raw", "warns"), [
    ("postgresql://tam@db.example.com/tam", True),
    ("postgresql://tam@db.example.com/tam?sslmode=prefer", True),
    ("postgresql://tam@db.example.com/tam?sslmode=disable", True),
    ("postgresql://tam@10.0.0.5/tam?sslmode=allow", True),
    ("postgresql://tam@db.example.com/tam?sslmode=require", False),
    ("postgresql://tam@localhost/tam?sslmode=disable", False),
    ("postgresql://tam@127.0.0.1,db.example.com/tam?sslmode=disable", True),
    ("postgresql://tam@%2Ftmp/tam", False),
])
def test_weak_tls_to_non_local_hosts_warns(raw, warns):
    assert bool(DatabaseDsn.parse(raw, origin=DsnOrigin.ENV).warnings()) is warns


def test_host_db_and_audit_carry_no_credentials():
    dsn = DatabaseDsn.parse(f"postgresql://tam:{SECRET}@db.example.com/tam")
    assert dsn.host_db() == "db.example.com:5432/tam"
    audit = dsn.audit()
    assert (audit.host, audit.database, audit.sslmode) == ("db.example.com:5432", "tam", SslMode.PREFER)
    assert "tam:" not in audit.model_dump_json()


def test_direct_construction_is_validated_too():
    with pytest.raises(InvalidDsn):
        DatabaseDsn(user="", hosts=({"host": "db"},), database="tam")
    with pytest.raises(InvalidDsn):
        DatabaseDsn(user="tam", hosts=({"host": "bad host"},), database="tam")
    with pytest.raises(ValidationError):
        DatabaseDsn(user="tam", hosts=(), database="tam")


def snapshot(**overrides) -> dict:
    values = {"backend": Backend.SQLITE, "instance_id": INSTANCE, "generation": 1, "updated_at": NOW,
              "updated_by": "admin"}
    return {**values, **overrides}


def test_database_config_file_schema_round_trips():
    previous = DatabaseConfigSnapshot(**snapshot())
    config = DatabaseConfig(**snapshot(backend=Backend.POSTGRES, dsn_token="gAAAA-token", generation=2,
                                       archive="archive/sqlite-20260925T120000Z"), previous=previous)
    assert DatabaseConfig.model_validate_json(config.model_dump_json()) == config
    assert config.snapshot().generation == 2 and config.previous == previous


@pytest.mark.parametrize("overrides", [
    {"backend": Backend.POSTGRES},
    {"dsn_token": "gAAAA-token"},
    {"generation": 0},
    {"archive": "archive/sqlite-x"},
    {"backend": Backend.POSTGRES, "dsn_token": "t", "archive": "../outside"},
    {"backend": Backend.POSTGRES, "dsn_token": "t", "archive": "archive/sqlite-a/../../x"},
    {"format": 2},
    {"updated_at": datetime(2026, 9, 25, 12, 0)},  # noqa: DTZ001 - a naive timestamp must be refused
    {"unknown": 1},
])
def test_database_config_rejects_inconsistent_files(overrides):
    with pytest.raises(ValidationError):
        DatabaseConfig(**snapshot(**overrides))


def test_database_config_previous_must_be_older_and_same_installation():
    current = snapshot(backend=Backend.POSTGRES, dsn_token="t", generation=2)
    with pytest.raises(ValidationError):
        DatabaseConfig(**current, previous=DatabaseConfigSnapshot(**snapshot(generation=2)))
    with pytest.raises(ValidationError):
        DatabaseConfig(**current, previous=DatabaseConfigSnapshot(**snapshot(instance_id=OTHER_INSTANCE)))


def test_effective_database_activation_guards_the_instance():
    dsn = DatabaseDsn.parse(f"postgresql://tam:{SECRET}@db/tam?sslmode=require")
    effective = EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.WEB, dsn=dsn,
                                  instance_id=INSTANCE, generation=3)
    active = effective.active(INSTANCE)
    assert active.url == dsn.to_uri() and active.generation == 3 and active.instance_id == str(INSTANCE)
    assert SECRET not in repr(effective)
    with pytest.raises(DatabaseStartupRefused) as caught:
        effective.active(OTHER_INSTANCE)
    assert caught.value.reason is StartupRefusal.FOREIGN_INSTALLATION
    env = EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.ENV, dsn=dsn)
    assert env.active(OTHER_INSTANCE).instance_id == str(OTHER_INSTANCE)
    default = EffectiveDatabase(backend=Backend.SQLITE, source=ConfigSource.DEFAULT)
    assert default.active(INSTANCE).url is None
    for kwargs in ({"backend": Backend.POSTGRES, "source": ConfigSource.DEFAULT, "dsn": dsn},
                   {"backend": Backend.SQLITE, "source": ConfigSource.ENV, "dsn": dsn},
                   {"backend": Backend.POSTGRES, "source": ConfigSource.WEB, "dsn": dsn}):
        with pytest.raises(ValidationError):
            EffectiveDatabase(**kwargs)


def checks(failed: CheckId | None = None) -> tuple[DatabaseCheck, ...]:
    result = []
    for check_id in CHECK_ORDER:
        if check_id is failed:
            result.append(DatabaseCheck(id=check_id, status=CheckStatus.FAILED, message="missing",
                                        category=ErrorCategory.PERMISSION,
                                        dba_sql=("ALTER ROLE tam CREATEROLE;",)))
        else:
            result.append(DatabaseCheck(id=check_id, status=CheckStatus.PASSED, message="ok"))
    return tuple(result)


def report(failed: CheckId | None = None, state: TargetState = TargetState.EMPTY,
           instance: uuid.UUID | None = None) -> CheckReport:
    return CheckReport(dsn_masked="postgresql://tam@db:5432/tam", checks=checks(failed), target_state=state,
                       target_instance_id=instance, checked_at=NOW, duration_ms=12)


def test_check_report_contract():
    assert report().ok
    failing = report(CheckId.PRIVILEGES)
    assert not failing.ok and failing.check(CheckId.PRIVILEGES).dba_sql
    dumped = failing.model_dump(mode="json")
    assert dumped["ok"] is False
    assert CheckReport.model_validate(dumped) == failing
    with pytest.raises(ValidationError):
        CheckReport(dsn_masked="x", checks=checks()[1:], target_state=TargetState.EMPTY, checked_at=NOW,
                    duration_ms=0)
    with pytest.raises(ValidationError):
        CheckReport(dsn_masked="x", checks=tuple(reversed(checks())), target_state=TargetState.EMPTY,
                    checked_at=NOW, duration_ms=0)
    with pytest.raises(ValidationError):
        report(instance=INSTANCE)
    with pytest.raises(ValidationError):
        DatabaseCheck(id=CheckId.CONNECT, status=CheckStatus.PASSED, message="ok", dba_sql=("SELECT 1",))


def plan(check_report: CheckReport, blockers: tuple[str, ...] = ()) -> MigrationPlan:
    tables = (TableEstimate(name="knowledge", rows=10, bytes=2048), TableEstimate(name="embeddings", rows=5, bytes=100))
    return MigrationPlan(plan_id=uuid.uuid4(), created_at=NOW, expires_at=NOW + timedelta(minutes=15),
                         created_by="admin", target=check_report.dsn_masked, report=check_report,
                         databases=(DatabaseEstimate(kind=DatabaseKind.IDENTITY, name="identity", tables=tables[:1]),
                                    DatabaseEstimate(kind=DatabaseKind.WORKSPACE, name="shared", tables=tables)),
                         estimated_seconds=3, blockers=blockers)


def test_migration_plan_contract():
    ready = plan(report())
    assert ready.ready and not ready.resumable
    assert (ready.total_rows, ready.total_bytes) == (25, 4196)
    assert MigrationPlan.model_validate_json(ready.model_dump_json()) == ready
    assert plan(report(state=TargetState.SAME_INSTALLATION, instance=INSTANCE)).resumable
    with pytest.raises(ValidationError):
        plan(report(CheckId.EXTENSIONS))
    with pytest.raises(ValidationError):
        plan(report(state=TargetState.FOREIGN_INSTALLATION, instance=OTHER_INSTANCE))
    blocked = plan(report(state=TargetState.FOREIGN_INSTALLATION, instance=OTHER_INSTANCE),
                   blockers=("The database belongs to another TAM installation",))
    assert not blocked.ready


def progress(**overrides) -> MigrationProgress:
    values = {"job_id": uuid.uuid4(), "plan_id": uuid.uuid4(), "phase": MigrationPhase.COPY_WORKSPACES,
              "started_at": NOW, "updated_at": NOW, "started_by": "admin", "target": "postgresql://tam@db:5432/tam",
              "workspace_index": 1, "workspace_total": 4, "database": "shared", "table": "knowledge",
              "rows_copied": 50, "rows_total": 200}
    return MigrationProgress(**{**values, **overrides})


def test_migration_progress_contract():
    running = progress()
    assert running.percent == 25.0 and running.cancellable and not running.terminal
    assert MigrationProgress.model_validate_json(running.model_dump_json()) == running
    assert not progress(cancel_requested=True).cancellable
    assert not progress(phase=MigrationPhase.ACTIVATE).cancellable
    done = progress(phase=MigrationPhase.DONE, finished_at=NOW, rows_copied=200)
    assert done.terminal and done.percent == 100.0 and not done.cancellable
    failed = progress(phase=MigrationPhase.FAILED, finished_at=NOW, error="copy failed",
                      error_category=ErrorCategory.UNREACHABLE)
    assert failed.terminal
    assert [phase.value for phase in MigrationPhase] == [
        "preflight", "maintenance", "copy_control", "copy_workspaces", "verify", "activate",
        "done", "failed", "cancelled"]
    for overrides in ({"phase": MigrationPhase.DONE}, {"finished_at": NOW}, {"error": "x"},
                      {"phase": MigrationPhase.FAILED, "finished_at": NOW},
                      {"error_category": ErrorCategory.TIMEOUT}, {"workspace_index": 5},
                      {"rows_copied": 201}):
        with pytest.raises(ValidationError):
            progress(**overrides)


@pytest.mark.parametrize(("method", "path", "allowed"), [
    ("POST", "/mcp", False), ("GET", "/mcp/", False), ("POST", "/api/call", False),
    ("GET", "/dashboard/api/session", True), ("GET", DATABASE_MIGRATION_API, True),
    ("POST", DATABASE_MIGRATION_CANCEL_API, True), ("POST", "/dashboard/api/login", False),
    ("POST", "/dashboard/api/login/token", False), ("POST", "/dashboard/api/logout", False),
    ("POST", "/dashboard/api/invite/redeem", False), ("POST", "/dashboard/api/memory", False),
    ("POST", DATABASE_PLAN_API, False), ("DELETE", "/dashboard/api/admin/users/x", False),
    ("GET", "/healthz", True), ("GET", "/dashboard/static/app.js", True), ("GET", "/mcpx", True),
    ("POST", "/learning/api/answer", False), ("POST", "/reports/api/run", False),
    ("PUT", "/any/future/route", False), ("GET", "/learning/api/progress", True),
])
def test_maintenance_gate_policy(method, path, allowed):
    assert maintenance_allows(method, path) is allowed


def test_requests_keep_the_dsn_secret():
    request = DatabaseTestRequest.model_validate({"dsn": f"postgresql://tam:{SECRET}@db/tam"})
    assert SECRET not in repr(request) and SECRET not in request.model_dump_json()
    assert DatabaseDsn.parse(request.dsn.get_secret_value()).password.get_secret_value() == SECRET
    with pytest.raises(ValidationError):
        DatabaseTestRequest.model_validate({"dsn": "x", "extra": 1})
    with pytest.raises(ValidationError):
        MigrationStartRequest.model_validate({"plan_id": str(uuid.uuid4()), "confirm": False})
    assert SetupDatabaseRequest(token="code", backend=Backend.SQLITE).dsn is None
    with pytest.raises(ValidationError):
        SetupDatabaseRequest(token="code", backend=Backend.POSTGRES)
    with pytest.raises(ValidationError):
        SetupDatabaseRequest(token="code", backend=Backend.SQLITE, dsn="postgresql://tam@db/tam")


def quarantine(rows: int = 3, **overrides) -> QuarantineEstimate:
    values = {"database": "shared", "table": "knowledge_nodes", "rows": rows,
              "reasons": ("fk:knowledge_nodes.node_id->graph_nodes.id",), "sample_pks": ('[1, 7]',)}
    return QuarantineEstimate(**{**values, **overrides})


def test_quarantine_is_a_warning_not_a_blocker():
    base = plan(report())
    with_quarantine = base.model_copy(update={"quarantine": (quarantine(3), quarantine(2, table="tam_history",
                                                                                  audit=True))})
    checked = MigrationPlan.model_validate_json(with_quarantine.model_dump_json())
    assert checked.ready and checked.quarantined_rows == 5 and checked.quarantine[1].audit
    assert base.quarantined_rows == 0
    dumped = with_quarantine.model_dump(mode="json")
    assert dumped["quarantined_rows"] == 5


@pytest.mark.parametrize("overrides", [
    {"database": ""}, {"database": "x" * 129}, {"table": ""}, {"rows": -1},
    {"reasons": tuple(f"fk:{i}" for i in range(9))}, {"sample_pks": tuple(str(i) for i in range(6))},
    {"unknown": 1},
])
def test_quarantine_estimate_validation(overrides):
    with pytest.raises(ValidationError):
        quarantine(**overrides)


def test_progress_counts_quarantined_rows_as_processed():
    running = progress(rows_copied=200, quarantined_rows=4, quarantine=(quarantine(4),))
    assert MigrationProgress.model_validate_json(running.model_dump_json()) == running
    assert progress().quarantined_rows == 0 and progress().quarantine == ()
    with pytest.raises(ValidationError):
        progress(quarantined_rows=-1)
    with pytest.raises(ValidationError):
        progress(rows_copied=201, quarantined_rows=1)


@pytest.mark.parametrize(("raw", "fragment"), [
    ("postgresql://tam:pw@%2Fvar%2Frun%2Fpostgresql/tam", "unix-socket"),
    ("postgresql://tam:pw@db.example.com,%2Ftmp/tam?sslmode=require", "unix-socket"),
    ("postgresql://tam:pw@db.example.com/tam?sslmode=verify-full&sslcert=/etc/passwd", "sslcert"),
    ("postgresql://tam:pw@db.example.com/tam?sslmode=verify-full&sslkey=/root/.ssh/id_rsa", "sslkey"),
    ("postgresql://tam:pw@db.example.com/tam?sslmode=verify-full&sslrootcert=/etc/ssl/root.crt", "sslrootcert"),
    ("postgresql://tam:pw@db.example.com/tam?sslmode=verify-full&sslrootcert=System", "sslrootcert"),
])
def test_web_origin_rejects_server_local_files_and_sockets(raw, fragment):
    with pytest.raises(InvalidDsn, match=fragment):
        DatabaseDsn.parse(raw)
    with pytest.raises(InvalidDsn, match=fragment):
        DatabaseDsn.parse(raw, origin=DsnOrigin.WEB)
    dsn = DatabaseDsn.parse(raw, origin=DsnOrigin.ENV)
    with pytest.raises(InvalidDsn, match=fragment):
        dsn.validate_for(DsnOrigin.WEB)
    assert dsn.validate_for(DsnOrigin.ENV) is dsn


@pytest.mark.parametrize("raw", [
    f"postgresql://tam:{SECRET}@db.example.com:5433/tam?sslmode=verify-full&sslrootcert=system",
    f"postgresql://tam:{SECRET}@127.0.0.1/tam?sslmode=disable",
    f"postgresql://tam:{SECRET}@[::1]:6543/tam",
    f"postgresql://tam:{SECRET}@h1,h2:5433/tam?sslmode=require&target_session_attrs=read-write",
])
def test_web_origin_accepts_tcp_hosts(raw):
    dsn = DatabaseDsn.parse(raw)
    assert dsn == DatabaseDsn.parse(raw, origin=DsnOrigin.ENV)
    assert_hidden(dsn)


@pytest.mark.parametrize("raw", [
    "postgresql://tam@db.example.com/tam?sslmode=require",
    "postgresql://tam:@db.example.com/tam?sslmode=require",
    "postgresql://tam@db.example.com/tam?sslmode=verify-full&sslrootcert=system",
])
def test_web_origin_requires_a_password(raw):
    with pytest.raises(InvalidDsn, match="must include a password") as caught:
        DatabaseDsn.parse(raw)
    assert "TAM_TEAM_DATABASE_URL" in str(caught.value)
    assert DatabaseDsn.parse(raw, origin=DsnOrigin.ENV).password is None


def test_connect_overrides_block_ambient_credentials_for_web_only(tmp_path):
    dsn = DatabaseDsn.parse(f"postgresql://tam:{SECRET}@db.example.com/tam?sslmode=require")
    empty = tmp_path / "empty.pgpass"
    overrides = dsn.connect_overrides(DsnOrigin.WEB, empty)
    assert dict(overrides) == {"passfile": str(empty), "sslcertmode": "disable", "gssencmode": "disable",
                               "require_auth": "password,md5,scram-sha-256"}
    assert not empty.exists()
    assert dsn.connect_overrides(DsnOrigin.ENV, empty) == {}
    with pytest.raises(InvalidDsn):
        dsn.connect_overrides(DsnOrigin.WEB, "")
    assert all(SECRET not in value for value in overrides.values())


@pytest.mark.postgres
def test_connect_overrides_work_against_a_real_server(pg_database, tmp_path):
    import os

    psycopg = pytest.importorskip("psycopg")
    if psycopg.pq.version() < MIN_LIBPQ_VERSION:
        pytest.skip("libpq too old for sslcertmode")
    passfile = tmp_path / "empty.pgpass"
    passfile.touch()
    os.chmod(passfile, 0o600)
    dsn = DatabaseDsn.parse(pg_database.url)
    with psycopg.connect(dsn.to_uri(), **dsn.connect_overrides(DsnOrigin.WEB, passfile)) as connection:
        assert connection.execute("SELECT current_user").fetchone()[0] == dsn.user
    wrong = dsn.model_copy(update={"password": SecretStr("wrong-" + SECRET)})
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(wrong.to_uri(), **wrong.connect_overrides(DsnOrigin.WEB, passfile), connect_timeout=5)


def test_web_origin_messages_never_echo_the_secret():
    with pytest.raises(InvalidDsn) as caught:
        DatabaseDsn.parse(f"postgresql://tam:{SECRET}@%2Ftmp/tam")
    assert SECRET not in str(caught.value)


def test_maintenance_reasons():
    assert [reason.value for reason in MaintenanceReason] == ["migration", "rollback", "lease_lost"]
    lost = MaintenanceState(reason=MaintenanceReason.LEASE_LOST, since=NOW)
    assert lost.job_id is None
    assert MaintenanceState.model_validate_json(lost.model_dump_json()) == lost


def test_config_origin_defaults_to_env_for_files_written_before_the_field():
    legacy = ('{"format": 1, "backend": "postgres", "dsn_token": "gAAAA-token", "generation": 2, '
              f'"instance_id": "{INSTANCE}", "updated_at": "2026-09-25T12:00:00Z", "updated_by": "cli"}}')
    assert DatabaseConfig.model_validate_json(legacy).origin is DsnOrigin.ENV
    web = DatabaseConfig(**snapshot(backend=Backend.POSTGRES, dsn_token="t", generation=2, origin=DsnOrigin.WEB),
                         previous=DatabaseConfigSnapshot(**snapshot()))
    reread = DatabaseConfig.model_validate_json(web.model_dump_json())
    assert reread.origin is DsnOrigin.WEB and reread.snapshot().origin is DsnOrigin.WEB
    assert reread.previous.origin is DsnOrigin.ENV
    assert '"origin":"web"' in web.model_dump_json()
    with pytest.raises(ValidationError):
        DatabaseConfig(**snapshot(origin="browser"))


def test_view_carries_the_maintenance_state():
    view = DatabaseConfigView(backend=Backend.POSTGRES, source=ConfigSource.WEB)
    assert view.maintenance is None
    lost = view.model_copy(update={"maintenance": MaintenanceState(reason=MaintenanceReason.LEASE_LOST, since=NOW)})
    dumped = lost.model_dump(mode="json")
    assert dumped["maintenance"]["reason"] == "lease_lost" and dumped["maintenance"]["job_id"] is None
    assert DatabaseConfigView.model_validate(dumped) == lost


def test_web_origin_effective_database_carries_connect_options(tmp_path):
    from tam_db.contracts import CONNECT_OPTION_KEYS

    dsn = DatabaseDsn.parse(f"postgresql://tam:{SECRET}@db.example.com/tam?sslmode=require")
    passfile = tmp_path / ".empty-pgpass"
    web = EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.WEB, dsn=dsn, instance_id=INSTANCE,
                            generation=2, origin=DsnOrigin.WEB)
    active = web.active(INSTANCE, passfile=passfile)
    assert active.connect_kwargs() == dict(dsn.connect_overrides(DsnOrigin.WEB, passfile))
    assert set(active.connect_kwargs()) == CONNECT_OPTION_KEYS
    assert SECRET not in repr(active)
    with pytest.raises(ValueError, match="passfile"):
        web.active(INSTANCE)
    env = EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.ENV, dsn=dsn)
    assert env.origin is DsnOrigin.ENV and env.active(INSTANCE, passfile=passfile).connect_options == ()
    sqlite = EffectiveDatabase(backend=Backend.SQLITE, source=ConfigSource.WEB, instance_id=INSTANCE,
                               generation=3, origin=DsnOrigin.WEB)
    assert sqlite.active(INSTANCE).connect_options == ()
    with pytest.raises(ValidationError):
        EffectiveDatabase(backend=Backend.POSTGRES, source=ConfigSource.ENV, dsn=dsn, origin=DsnOrigin.WEB)
