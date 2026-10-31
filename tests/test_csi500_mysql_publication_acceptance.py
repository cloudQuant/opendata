"""Opt-in MySQL acceptance for warehouse migrations and fenced publication.

Run this file only with ``pytest --noconftest -m e2e`` and the dedicated
``OPENDATA_CSI500_MYSQL_ACCEPTANCE_URL`` and
``OPENDATA_CSI500_MYSQL_ACCEPTANCE_DATABASE`` variables, plus
``OPENDATA_CSI500_MYSQL_ACCEPTANCE_CONFIRM_EMPTY`` set to
``I_OWN_AND_ALLOW_MIGRATIONS:<database-name>``. ``--noconftest`` is
required because the repository conftest imports application settings, whose
Pydantic configuration reads ``.env``. The Alembic child runs from a fresh
temporary working directory with an environment allowlist, so its settings
fallback cannot read the repository ``.env`` either.

The URL must point to an operator-created, empty, disposable local MySQL 8
schema named ``opendata_csi500_acceptance_<12-32 lowercase hex characters>``.
The test never creates or drops databases. It applies the warehouse migrations
and leaves the resulting schema and synthetic rows in place for inspection;
do not reuse the schema for another run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.pool import NullPool

from opendata.pipeline.publication_store import (
    PublicationRecord,
    PublishedRecordInput,
    publish_in_connection,
)
from opendata.pipeline.run_ledger import RunLedger

if TYPE_CHECKING:
    from collections.abc import Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_MIGRATION_DIRECTORY = REPOSITORY_ROOT / "alembic_data"
MYSQL_URL_ENV = "OPENDATA_CSI500_MYSQL_ACCEPTANCE_URL"
MYSQL_DATABASE_ENV = "OPENDATA_CSI500_MYSQL_ACCEPTANCE_DATABASE"
MYSQL_CONFIRM_ENV = "OPENDATA_CSI500_MYSQL_ACCEPTANCE_CONFIRM_EMPTY"
MYSQL_DATABASE_PREFIX = "opendata_csi500_acceptance_"
_DATABASE_NAME = re.compile(rf"^{re.escape(MYSQL_DATABASE_PREFIX)}[0-9a-f]{{12,32}}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_MIGRATION_DIAGNOSTIC_PREFIX = "OPENDATA_MYSQL_MIGRATION_DIAGNOSTIC"
_MIGRATION_DIAGNOSTIC = re.compile(
    rf"^{_MIGRATION_DIAGNOSTIC_PREFIX} "
    r"stage=(runtime_imports|config_setup|alembic_upgrade) "
    r"category=(missing_dependency|import_error|database_operational_error|"
    r"database_programming_error|database_integrity_error|database_error|timeout|"
    r"connection_error|os_error|runtime_error|invalid_configuration|migration_error) "
    r"source=(none|[A-Za-z0-9_./-]{1,256}) line=(0|[1-9][0-9]{0,6})$"
)
_MIGRATION_REVISION_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MIGRATION_CHILD = """
import os
import sys

def migration_source(error):
    try:
        import traceback
        from pathlib import Path

        migration_root = Path(os.environ["OPENDATA_MYSQL_SCRIPT_LOCATION"]).resolve()
        repository_root = migration_root.parent.resolve()
        candidates = [migration_root / "env.py"]
        candidates.extend((migration_root / "versions").glob("*.py"))
        allowed_sources = {}
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if (
                    candidate.is_file()
                    and resolved.is_relative_to(migration_root)
                    and resolved.is_relative_to(repository_root)
                ):
                    allowed_sources[resolved] = resolved.relative_to(repository_root).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue

        for frame in reversed(traceback.extract_tb(error.__traceback__)):
            if type(frame.lineno) is not int or not 1 <= frame.lineno <= 1000000:
                continue
            try:
                frame_path = Path(frame.filename).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            source = allowed_sources.get(frame_path)
            if source is not None:
                return source, frame.lineno
    except Exception:
        pass
    return "none", 0

def report_failure(stage, error):
    categories = {
        "ModuleNotFoundError": "missing_dependency",
        "ImportError": "import_error",
        "OperationalError": "database_operational_error",
        "ProgrammingError": "database_programming_error",
        "IntegrityError": "database_integrity_error",
        "DBAPIError": "database_error",
        "TimeoutError": "timeout",
        "ConnectionError": "connection_error",
        "OSError": "os_error",
        "RuntimeError": "runtime_error",
        "ValueError": "invalid_configuration",
    }
    category = categories.get(type(error).__name__, "migration_error")
    source, line = migration_source(error)
    print(
        "OPENDATA_MYSQL_MIGRATION_DIAGNOSTIC "
        f"stage={stage} category={category} source={source} line={line}",
        file=sys.stderr,
        flush=True,
    )

stage = "runtime_imports"
try:
    from alembic import command
    from alembic.config import Config
except Exception as error:
    report_failure(stage, error)
    raise SystemExit(1) from None

stage = "config_setup"
try:
    config = Config()
    config.set_main_option("script_location", os.environ["OPENDATA_MYSQL_SCRIPT_LOCATION"])
    url = os.environ["OPENDATA_CSI500_MYSQL_ACCEPTANCE_URL"]
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
except Exception as error:
    report_failure(stage, error)
    raise SystemExit(1) from None

stage = "alembic_upgrade"
try:
    command.upgrade(config, "head")
except Exception as error:
    report_failure(stage, error)
    raise SystemExit(1) from None
"""


def _explicit_mysql_url() -> tuple[URL, str]:
    """Read and validate the explicit disposable local-schema declaration."""
    raw_url = os.environ.get(MYSQL_URL_ENV)
    if not raw_url:
        pytest.skip(f"set {MYSQL_URL_ENV} to opt into the MySQL acceptance run")

    declared_database = os.environ.get(MYSQL_DATABASE_ENV, "")
    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail(f"{MYSQL_URL_ENV} is not a valid SQLAlchemy URL", pytrace=False)

    database = url.database or ""
    if (
        url.drivername != "mysql+pymysql"
        or (url.host or "").casefold() not in _LOCAL_HOSTS
        or url.port is None
        or url.query
        or _DATABASE_NAME.fullmatch(database) is None
        or declared_database != database
    ):
        pytest.fail(
            "the explicit target must be a local mysql+pymysql URL with an explicit port "
            "and a matching disposable database name",
            pytrace=False,
        )
    expected_confirmation = f"I_OWN_AND_ALLOW_MIGRATIONS:{database}"
    if os.environ.get(MYSQL_CONFIRM_ENV) != expected_confirmation:
        pytest.fail(
            f"set {MYSQL_CONFIRM_ENV} to the exact disposable database name to permit migrations",
            pytrace=False,
        )
    return url, database


def _migration_head() -> str:
    """Resolve the checked-in warehouse chain's one declared Alembic head."""
    config = Config()
    config.set_main_option("script_location", str(WAREHOUSE_MIGRATION_DIRECTORY))
    head = ScriptDirectory.from_config(config).get_current_head()
    if not head:
        pytest.fail("the warehouse migration graph has no single current head", pytrace=False)
    return head


def _migration_source_allowlist() -> frozenset[str]:
    """Return repository-relative paths for fixed warehouse migration sources."""
    repository_root = REPOSITORY_ROOT.resolve()
    migration_root = WAREHOUSE_MIGRATION_DIRECTORY.resolve()
    candidates = [WAREHOUSE_MIGRATION_DIRECTORY / "env.py"]
    candidates.extend((WAREHOUSE_MIGRATION_DIRECTORY / "versions").glob("*.py"))
    sources: set[str] = set()

    def relative_source(candidate: Path) -> str | None:
        try:
            resolved = candidate.resolve()
            if candidate.is_file() and resolved.is_relative_to(migration_root):
                return resolved.relative_to(repository_root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
        return None

    for candidate in candidates:
        source = relative_source(candidate)
        if source is not None:
            sources.add(source)
    return frozenset(sources)


def _apply_warehouse_migrations(url: URL, cwd: Path, target_revision: str) -> None:
    """Apply Alembic head in a fresh process without reading repository settings."""
    cwd.mkdir()
    if (cwd / ".env").exists():
        pytest.fail("the isolated migration working directory contains a .env file", pytrace=False)

    child_environment = {
        "APP_ENV": "testing",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PYTHONHOME": sys.base_prefix,
        "PYTHONNOUSERSITE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPOSITORY_ROOT),
        "REDIS_URL": "",
        "SENTRY_DSN": "",
        "TESTING": "true",
        "OPENDATA_CSI500_MYSQL_ACCEPTANCE_URL": url.render_as_string(hide_password=False),
        "OPENDATA_MYSQL_SCRIPT_LOCATION": str(WAREHOUSE_MIGRATION_DIRECTORY),
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and constant child code
            [sys.executable, "-c", _MIGRATION_CHILD],
            cwd=cwd,
            env=child_environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("warehouse migration child timed out; preserve the schema for inspection")

    if result.returncode != 0:
        diagnostic_lines = [
            line
            for line in (result.stdout + "\n" + result.stderr).splitlines()
            if line.startswith(f"{_MIGRATION_DIAGNOSTIC_PREFIX} ")
        ]
        diagnostic = (
            _MIGRATION_DIAGNOSTIC.fullmatch(diagnostic_lines[0])
            if len(diagnostic_lines) == 1
            else None
        )
        source_label = "none"
        source_line = 0
        if diagnostic is not None:
            source_label = diagnostic.group(3)
            source_line = int(diagnostic.group(4))
            if source_label == "none":
                if source_line != 0:
                    diagnostic = None
            elif (
                source_label not in _migration_source_allowlist() or not 1 <= source_line <= 1000000
            ):
                diagnostic = None
        revision_label = (
            target_revision
            if _MIGRATION_REVISION_IDENTIFIER.fullmatch(target_revision)
            else "unrecognized"
        )
        if diagnostic is None:
            failure_detail = "stage=child_startup_or_unreported_failure"
        else:
            failure_detail = (
                f"stage={diagnostic.group(1)} category={diagnostic.group(2)} "
                f"source={source_label} line={source_line}"
            )
        pytest.fail(
            f"warehouse migration to target revision {revision_label} failed "
            f"({failure_detail}, exit_code={result.returncode}); preserve the disposable schema "
            "and inspect it offline (raw child output suppressed to prevent credential disclosure)",
            pytrace=False,
        )


def _database_identity(engine: Engine, database: str, port: int) -> None:
    """Require the live connection to match the explicitly named local schema."""
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text("SELECT DATABASE(), @@port, @@read_only, VERSION(), @@version_comment")
            ).one()
            tables = (
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            routines = (
                connection.execute(
                    text(
                        "SELECT routine_name FROM information_schema.routines "
                        "WHERE routine_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            triggers = (
                connection.execute(
                    text(
                        "SELECT trigger_name FROM information_schema.triggers "
                        "WHERE trigger_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            events = (
                connection.execute(
                    text(
                        "SELECT event_name FROM information_schema.events "
                        "WHERE event_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        pytest.fail(
            f"could not verify the explicit MySQL target ({type(exc).__name__})",
            pytrace=False,
        )

    assert identity[0] == database
    assert int(identity[1]) == port
    assert int(identity[2]) == 0
    version = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(identity[3]))
    assert version is not None, "the target did not report a standard MySQL version"
    assert tuple(int(part) for part in version.groups()) >= (8, 0, 16)
    assert int(version.group(1)) == 8, "warehouse migrations support MySQL 8 only"
    assert "mariadb" not in str(identity[4]).casefold()
    assert not tables and not routines and not triggers and not events, (
        "the explicitly named disposable schema is not empty; no migrations were run"
    )


def _synthetic_publication_inputs(dataset_id: str) -> Mapping[str, object]:
    """Return one synthetic index row and its immutable publication metadata."""
    record = PublishedRecordInput(
        business_key='["XSHG","000905.SH","2030-01-02"]',
        payload={
            "market": "XSHG",
            "symbol": "000905.SH",
            "trade_date": "2030-01-02",
            "close": 1234.5,
        },
        business_date=date(2030, 1, 2),
        observed_at=datetime(2030, 1, 2, 8, 0, tzinfo=timezone.utc),
        market="XSHG",
        symbol="000905.SH",
    )
    return {
        "dataset_id": dataset_id,
        "source_id": "synthetic-fixture",
        "schema_version": "csi500-synthetic-v1",
        "semantic_version": "synthetic-close-v1",
        "unit": "index-point",
        "adjustment_basis": "none",
        "quality": {"status": "passed", "issue_count": 0, "synthetic": True},
        "provenance": {"synthetic": True, "source_history_limit": "not_applicable"},
        "records": (record,),
        "watermark": {"through": "2030-01-02", "synthetic": True},
    }


def _publish(
    connection: Connection,
    inputs: Mapping[str, object],
    idempotency_key: str,
) -> PublicationRecord:
    """Publish the fixed synthetic payload through the caller's lease transaction."""
    return publish_in_connection(
        connection,
        **inputs,
        idempotency_key=idempotency_key,
    )


def _commit_synthetic_run(
    ledger: RunLedger,
    run_key: str,
    inputs: Mapping[str, object],
    idempotency_key: str,
) -> tuple[object, PublicationRecord]:
    """Claim one synthetic run and commit publication plus ledger success."""
    now = datetime.now(timezone.utc)
    ledger.enqueue(run_key, '{"synthetic":true,"task":"csi500-publication-acceptance"}')
    claimed = ledger.claim("csi500-acceptance-worker", now, timedelta(minutes=5))
    assert claimed is not None and claimed.run_key == run_key
    committed = ledger.commit_result(
        run_key,
        worker="csi500-acceptance-worker",
        fencing_token=claimed.fencing_token,
        now=datetime.now(timezone.utc),
        operation=lambda connection: _publish(connection, inputs, idempotency_key),
    )
    assert committed is not None
    return committed


@pytest.mark.e2e
def test_mysql_empty_schema_migrations_and_fenced_publication_are_atomic(
    tmp_path: Path,
) -> None:
    """Migrate one empty disposable MySQL schema and exercise real SQL transactions."""
    assert "opendata.core.config" not in sys.modules, (
        "run this file with --noconftest so application settings cannot read .env"
    )
    url, database = _explicit_mysql_url()
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
    try:
        _database_identity(engine, database, int(url.port))
        expected_head = _migration_head()
        _apply_warehouse_migrations(url, tmp_path / "migration-cwd", expected_head)

        names = set(inspect(engine).get_table_names())
        required_tables = {
            "alembic_version_data",
            "od_run_ledger",
            "od_publication_heads",
            "od_publications",
            "od_publication_outbox",
            "od_published_fact_versions",
            "od_publication_snapshots",
        }
        assert required_tables <= names
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version_data"))
                == expected_head
            )
            table_engines = dict(
                connection.execute(
                    text(
                        "SELECT table_name, engine FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name IN "
                        "('od_run_ledger', 'od_publication_heads', 'od_publications', "
                        "'od_publication_outbox', 'od_published_fact_versions')"
                    )
                ).all()
            )
        assert set(table_engines) == {
            "od_run_ledger",
            "od_publication_heads",
            "od_publications",
            "od_publication_outbox",
            "od_published_fact_versions",
        }
        assert set(table_engines.values()) == {"InnoDB"}
        ledger_columns = {column["name"] for column in inspect(engine).get_columns("od_run_ledger")}
        assert "claim_policy" in ledger_columns

        ledger = RunLedger(engine, clock_policy="database")
        dataset_id = f"cn.csi500.synthetic.{uuid.uuid4().hex}"
        publication_inputs = _synthetic_publication_inputs(dataset_id)
        first_run_key = f"csi500-acceptance-first-{uuid.uuid4().hex}"
        stable_publication_key = f"csi500-payload-{uuid.uuid4().hex}"

        first_record, first_publication = _commit_synthetic_run(
            ledger,
            first_run_key,
            publication_inputs,
            stable_publication_key,
        )
        assert first_record.status == "succeeded"
        assert first_publication.record_count == 1
        assert first_publication.sequence == 1

        replay_record, replay_publication = _commit_synthetic_run(
            ledger,
            f"csi500-acceptance-replay-{uuid.uuid4().hex}",
            publication_inputs,
            stable_publication_key,
        )
        assert replay_record.status == "succeeded"
        assert replay_publication.publication_id == first_publication.publication_id
        assert replay_publication.sequence == first_publication.sequence

        failing_run_key = f"csi500-acceptance-rollback-{uuid.uuid4().hex}"
        ledger.enqueue(
            failing_run_key,
            '{"synthetic":true,"task":"csi500-publication-rollback"}',
        )
        failed_claim = ledger.claim(
            "csi500-acceptance-worker", datetime.now(timezone.utc), timedelta(minutes=5)
        )
        assert failed_claim is not None and failed_claim.run_key == failing_run_key

        def publish_then_fail(connection: object) -> PublicationRecord:
            """Write publication state in the active transaction, then force rollback."""
            _publish(connection, publication_inputs, f"rollback-{uuid.uuid4().hex}")
            raise RuntimeError("synthetic rollback trigger")

        with pytest.raises(RuntimeError, match="synthetic rollback trigger"):
            ledger.commit_result(
                failing_run_key,
                worker="csi500-acceptance-worker",
                fencing_token=failed_claim.fencing_token,
                now=datetime.now(timezone.utc),
                operation=publish_then_fail,
            )

        running_record = ledger.get(failing_run_key)
        assert running_record is not None and running_record.status == "running"
        failed_record = ledger.fail(
            failing_run_key,
            "csi500-acceptance-worker",
            failed_claim.fencing_token,
            datetime.now(timezone.utc),
            "synthetic rollback verified",
        )
        assert failed_record is not None and failed_record.status == "failed"

        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM od_publications WHERE dataset_id = :dataset"),
                    {"dataset": dataset_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT latest_sequence FROM od_publication_heads "
                        "WHERE dataset_id = :dataset"
                    ),
                    {"dataset": dataset_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM od_published_fact_versions "
                        "WHERE dataset_id = :dataset"
                    ),
                    {"dataset": dataset_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM od_publication_outbox WHERE dataset_id = :dataset"),
                    {"dataset": dataset_id},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM od_run_ledger "
                        "WHERE run_key = :run_key AND status = 'succeeded'"
                    ),
                    {"run_key": failing_run_key},
                )
                == 0
            )
    finally:
        engine.dispose()
