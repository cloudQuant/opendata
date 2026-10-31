#!/usr/bin/env python3
"""Offline control-plane CLI for one digest-pinned stock-daily backfill plan.

The CLI reads only explicit arguments and local files. It never loads ``.env``,
contacts a provider, registers a scheduler, enables the existing disabled task,
or selects a remote database. Dry-run is the default. ``--execute`` requires an
explicit local offline raw-response file or directory, a local SQLite ledger
and publication schema, and a private artifact root; it still sends no network
request. Only the fixed ``stock-daily-akshare`` execution target is accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import stat
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from opendata.data.dataset_contract_registry import DatasetContractRegistry  # noqa: E402
from opendata.data.instrument_reference_snapshot import (  # noqa: E402
    MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES,
    InstrumentReferenceSnapshotError,
)
from opendata.data.providers.akshare.stock_daily_wire import MAX_RAW_RESPONSE_BYTES  # noqa: E402
from opendata.pipeline.publication_store import PUBLICATION_METADATA  # noqa: E402
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter  # noqa: E402
from opendata.pipeline.reviewed_snapshot_backfill import (  # noqa: E402
    build_stock_daily_backfill_plan_from_snapshot,
)
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher  # noqa: E402
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger  # noqa: E402
from opendata.pipeline.run_service import (  # noqa: E402
    ReviewedJob,
    ReviewedJobRegistry,
    RunOutcome,
    RunService,
)
from opendata.pipeline.stock_daily_backfill_executor import (  # noqa: E402
    EXECUTION_AUTHORIZATION,
    STOCK_DAILY_EXECUTION_TARGET,
    StockDailyBackfillExecutionError,
    StockDailyBackfillExecutionReceipt,
    StockDailyBackfillExecutor,
    build_stock_daily_execution_contract,
    build_stock_daily_execution_task,
)
from opendata.pipeline.stock_daily_backfill_plan import (  # noqa: E402
    MAX_STOCK_DAILY_WINDOW_DAYS,
    StockDailyBackfillPlan,
    StockDailyBackfillPlanIntegrityError,
)
from opendata.pipeline.stock_daily_job import (  # noqa: E402
    StockDailyJsonResponse,
    build_stock_daily_job,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.engine import Engine

    from opendata.data.platform_contracts import DatasetContract
    from opendata.data.providers.akshare.models.stock_daily import StockDailyQuery
    from opendata.pipeline.run_identity import TaskDefinition

_LEASE_TTL = timedelta(seconds=60)
_FAILURE_STOP_REASONS = frozenset(
    {
        "chunk_failed",
        "chunk_cancelled",
        "chunk_leased",
        "chunk_uncertain",
        "execution_unclaimed",
        "execution_error",
        "unexpected_run_claimed",
        "dispatch_rejected",
        "dispatch_not_persisted",
        "ledger_state_invalid",
    }
)


class _CliError(Exception):
    """Carry one stable error code that never includes a local path."""

    def __init__(self, code: str) -> None:
        """Store the machine-readable refusal code."""
        self.code = code
        super().__init__(code)


class _StrictArgumentParser(argparse.ArgumentParser):
    """Turn argparse diagnostics into one stable JSON-compatible error code."""

    def error(self, message: str) -> NoReturn:
        """Reject invalid arguments without echoing their values."""
        raise _CliError("invalid_arguments")


class _ServiceWorker:
    """Adapt the generic run service to the executor's run-once protocol."""

    def __init__(self, service: RunService) -> None:
        """Bind the exact service that owns the local ledger."""
        self._service = service

    def run_once(self) -> RunOutcome | None:
        """Claim and execute at most one queued local run."""
        return self._service.run_once("stock-daily-backfill-executor", _LEASE_TTL)


def build_parser() -> argparse.ArgumentParser:
    """Build the strict offline control-plane argument parser."""
    parser = _StrictArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--snapshot", required=True, help="local instrument snapshot file")
    parser.add_argument(
        "--snapshot-sha256",
        required=True,
        help="expected lowercase SHA-256 of the exact snapshot bytes",
    )
    parser.add_argument("--as-of", required=True, help="explicit plan cutoff YYYY-MM-DD")
    parser.add_argument(
        "--target",
        default=STOCK_DAILY_EXECUTION_TARGET,
        help="fixed execution target; only stock-daily-akshare is supported",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    mode.add_argument(
        "--execute",
        action="store_true",
        help="acknowledge offline execution against the local ledger",
    )
    parser.add_argument("--max-chunks", type=int, default=1, help="maximum chunks this invocation")
    parser.add_argument(
        "--max-failures",
        type=int,
        default=1,
        help="fixed fail-closed failure cap; only 1 is supported",
    )
    parser.add_argument(
        "--max-window-days",
        type=int,
        default=MAX_STOCK_DAILY_WINDOW_DAYS,
        help="inclusive chunk window cap used to build the plan",
    )
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--ledger-url", help="explicit local SQLite SQLAlchemy URL")
    database.add_argument("--database-path", help="explicit local SQLite database file")
    responses = parser.add_mutually_exclusive_group()
    responses.add_argument("--raw-response", help="one local offline wire-response file")
    responses.add_argument(
        "--raw-response-dir",
        help="local directory of <symbol>_<YYYYMMDD>_<YYYYMMDD>.json fixtures",
    )
    parser.add_argument("--observed-at", help="explicit timezone-aware fixture observation time")
    parser.add_argument("--artifact-root", help="private local raw-evidence directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one bounded invocation and emit a deterministic compact JSON result."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        receipt = _execute(args)
    except _CliError as exc:
        _emit_error(exc.code)
        return 2
    except StockDailyBackfillExecutionError as exc:
        _emit_error(exc.code)
        return 2
    except StockDailyBackfillPlanIntegrityError as exc:
        _emit_error(exc.code)
        return 2
    except InstrumentReferenceSnapshotError:
        _emit_error("snapshot_invalid")
        return 2
    except OSError:
        _emit_error("input_unavailable")
        return 2
    except Exception:
        _emit_error("internal_error")
        return 2
    sys.stdout.write(receipt.to_json() + "\n")
    return 1 if receipt.chunks_failed or receipt.stop_reason in _FAILURE_STOP_REASONS else 0


def _execute(args: argparse.Namespace) -> StockDailyBackfillExecutionReceipt:
    """Validate local inputs, build the pinned plan, and execute one prefix."""
    if args.target != STOCK_DAILY_EXECUTION_TARGET:
        raise _CliError("unknown_target")
    as_of = _parse_date(args.as_of)
    if args.max_failures != 1:
        raise _CliError("unsupported_max_failures")
    if not 1 <= args.max_chunks <= 1_000:
        raise _CliError("invalid_limits")
    if not 1 <= args.max_window_days <= MAX_STOCK_DAILY_WINDOW_DAYS:
        raise _CliError("invalid_limits")
    expected_sha256 = _validate_sha256(args.snapshot_sha256)
    snapshot_bytes = _read_bounded_file(
        Path(args.snapshot),
        MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES,
        "snapshot_unavailable",
        "snapshot_too_large",
    )
    actual_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise _CliError("snapshot_sha256_mismatch")

    execute = bool(args.execute)
    contract = build_stock_daily_execution_contract()
    task = build_stock_daily_execution_task()
    try:
        candidate = build_stock_daily_backfill_plan_from_snapshot(
            snapshot_bytes,
            task=task,
            contract=contract,
            as_of=as_of,
            max_window_days=args.max_window_days,
        )
    except InstrumentReferenceSnapshotError:
        raise _CliError("snapshot_invalid") from None
    except (TypeError, ValueError):
        raise _CliError("plan_invalid") from None
    if not hmac.compare_digest(candidate.snapshot_bytes_sha256, actual_sha256):
        raise _CliError("snapshot_sha256_mismatch")

    raw_source: Callable[[object], StockDailyJsonResponse] | None = None
    artifact_root: Path | None = None
    if execute:
        raw_source = _offline_response_source(args, candidate.plan)
        artifact_root = _validate_artifact_root(args.artifact_root)
    engine = _open_ledger_engine(args, execute=execute)
    try:
        job = _reviewed_execution_job(
            task=task,
            contract=contract,
            artifact_root=artifact_root or Path("/opendata-dry-run-unused"),
            raw_source=raw_source,
        )
        registry = ReviewedJobRegistry([job])
        catalog = ReviewedTaskCatalog([job])
        ledger = RunLedger(engine)
        executor = StockDailyBackfillExecutor(
            task=task,
            registry=registry,
            dispatcher=RunDispatcher(ledger, catalog),
            ledger=ledger,
            worker=_ServiceWorker(RunService(ledger, registry)),
            max_chunks_per_invocation=args.max_chunks,
            max_failures=args.max_failures,
            dry_run=not execute,
            execution_authorization=EXECUTION_AUTHORIZATION if execute else None,
        )
        return executor.execute_once(candidate.plan, snapshot_sha256=actual_sha256)
    finally:
        engine.dispose()


def _open_ledger_engine(args: argparse.Namespace, *, execute: bool) -> Engine:
    """Open only an explicit local SQLite file or an in-memory dry-run ledger."""
    if args.database_path is not None:
        path = _validated_sqlite_file(args.database_path)
    elif args.ledger_url is not None:
        path = _validated_sqlite_url(args.ledger_url)
    elif execute:
        raise _CliError("ledger_required")
    else:
        engine = create_engine("sqlite:///:memory:")
        RUN_LEDGER_METADATA.create_all(engine)
        return engine

    engine = create_engine(f"sqlite:///{path}")
    tables = set(inspect(engine).get_table_names())
    if "od_run_ledger" not in tables:
        engine.dispose()
        raise _CliError("ledger_table_missing")
    if execute and not set(PUBLICATION_METADATA.tables).issubset(tables):
        engine.dispose()
        raise _CliError("publication_schema_missing")
    return engine


def _validated_sqlite_url(value: str) -> Path:
    """Accept only a credential-free, absolute local SQLite file URL."""
    try:
        url = make_url(value)
    except Exception:
        raise _CliError("unsafe_database_url") from None
    if (
        url.drivername != "sqlite"
        or url.username is not None
        or url.password is not None
        or url.host is not None
        or url.port is not None
        or url.query
        or not url.database
    ):
        raise _CliError("unsafe_database_url")
    return _validated_sqlite_file(url.database)


def _validated_sqlite_file(value: str) -> Path:
    """Require an existing canonical absolute regular SQLite file."""
    path = Path(value)
    if not path.is_absolute():
        raise _CliError("unsafe_database_url")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError:
        raise _CliError("database_unavailable") from None
    if resolved != path or not stat.S_ISREG(info.st_mode):
        raise _CliError("unsafe_database_url")
    return resolved


def _validate_artifact_root(value: object) -> Path:
    """Require an explicit absolute private artifact root without traversal."""
    if not isinstance(value, str) or not value:
        raise _CliError("artifact_root_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise _CliError("artifact_root_invalid")
    return path


def _offline_response_source(
    args: argparse.Namespace,
    plan: StockDailyBackfillPlan,
) -> Callable[[StockDailyQuery], StockDailyJsonResponse]:
    """Build a local-only response source and preflight every planned chunk."""
    single_body: bytes | None = None
    directory: Path | None = None
    if args.raw_response is not None:
        single_body = _read_bounded_file(
            Path(args.raw_response),
            MAX_RAW_RESPONSE_BYTES,
            "raw_response_unavailable",
            "raw_response_too_large",
        )
    elif args.raw_response_dir is not None:
        directory = _validated_directory(args.raw_response_dir)
    else:
        raise _CliError("execute_requires_offline_response")
    observed_at = _parse_observed_at(args.observed_at)

    chunks = plan.candidate_chunks
    if single_body is not None and len(chunks) != 1:
        raise _CliError("raw_response_requires_single_chunk")
    if directory is not None:
        for chunk in chunks:
            _fixture_path(directory, chunk)

    def source(query: StockDailyQuery) -> StockDailyJsonResponse:
        body = single_body
        if body is None:
            if directory is None:
                raise RuntimeError("offline response directory is not configured")
            body = _read_bounded_file(
                _fixture_path(directory, query),
                MAX_RAW_RESPONSE_BYTES,
                "raw_response_unavailable",
                "raw_response_too_large",
            )
        return StockDailyJsonResponse(body=body, status_code=200, observed_at=observed_at)

    return source


def _fixture_path(directory: Path, source: object) -> Path:
    """Resolve one fixture file from a query or chunk without guessing a window."""
    symbol = getattr(source, "symbol", None)
    start = getattr(source, "start_date", None)
    end = getattr(source, "end_date", None)
    if end is None:
        end = getattr(source, "window_end", None)
    if symbol is None:
        symbol = getattr(source, "provider_symbol", None)
    if start is None:
        start = getattr(source, "window_start", None)
    if not isinstance(symbol, str) or not isinstance(start, date) or not isinstance(end, date):
        raise _CliError("raw_response_invalid")
    path = directory / f"{symbol}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.json"
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError:
        raise _CliError("raw_response_missing") from None
    if resolved != path or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RAW_RESPONSE_BYTES:
        raise _CliError("raw_response_invalid")
    return resolved


def _validated_directory(value: str) -> Path:
    """Require an existing canonical absolute local fixture directory."""
    path = Path(value)
    if not path.is_absolute():
        raise _CliError("raw_response_invalid")
    try:
        resolved = path.resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except OSError:
        raise _CliError("raw_response_unavailable") from None
    if resolved != path or not stat.S_ISDIR(info.st_mode):
        raise _CliError("raw_response_invalid")
    return resolved


def _reviewed_execution_job(
    *,
    task: TaskDefinition,
    contract: DatasetContract,
    artifact_root: Path,
    raw_source: Callable[[StockDailyQuery], StockDailyJsonResponse] | None,
) -> ReviewedJob:
    """Build the fixed adapter and mark it reviewed and execution-only."""

    def rejecting_source(_query: StockDailyQuery) -> StockDailyJsonResponse:
        raise RuntimeError("dry-run must not read a response source")

    unreviewed = build_stock_daily_job(
        task,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        contract_registry=DatasetContractRegistry([contract]),
        artifact_writer=RawResponseArtifactWriter(artifact_root),
        response_source=raw_source or rejecting_source,
    )
    return replace(unreviewed, reviewed=True, execution_only=True)


def _parse_date(value: str) -> date:
    """Parse one canonical explicit business-date cutoff."""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise _CliError("as_of_invalid") from None
    if parsed.isoformat() != value:
        raise _CliError("as_of_invalid")
    return parsed


def _parse_observed_at(value: object) -> datetime:
    """Parse one explicit timezone-aware fixture observation time."""
    if not isinstance(value, str):
        raise _CliError("observed_at_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _CliError("observed_at_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _CliError("observed_at_invalid")
    return parsed.astimezone(timezone.utc)


def _validate_sha256(value: object) -> str:
    """Require a lowercase hexadecimal SHA-256 digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _CliError("snapshot_sha256_invalid")
    return value


def _read_bounded_file(
    path: Path,
    maximum: int,
    unavailable_code: str,
    too_large_code: str,
) -> bytes:
    """Read one regular local file with a hard byte bound and no symlink hops."""
    try:
        resolved = path.resolve(strict=True)
        expected = path.absolute()
        info = resolved.stat(follow_symlinks=False)
    except OSError:
        raise _CliError(unavailable_code) from None
    if resolved != expected or not stat.S_ISREG(info.st_mode):
        raise _CliError(unavailable_code)
    if info.st_size > maximum:
        raise _CliError(too_large_code)
    try:
        with resolved.open("rb") as handle:
            content = handle.read(maximum + 1)
    except OSError:
        raise _CliError(unavailable_code) from None
    if len(content) > maximum:
        raise _CliError(too_large_code)
    return content


def _emit_error(code: str) -> None:
    """Write one stable compact error object without local paths or secrets."""
    sys.stderr.write(
        json.dumps(
            {"error": code, "status": "error"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
