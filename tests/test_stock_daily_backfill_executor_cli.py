"""Behavioral tests for the offline stock-daily backfill executor CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, func, select

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
)
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE
from opendata.pipeline.stock_daily_backfill_executor import (
    build_stock_daily_execution_contract,
)

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "execute_stock_daily_backfill.py"
)
START = date(2020, 1, 1)
END = date(2020, 1, 2)


def _load_cli() -> ModuleType:
    """Load the standalone operations script without invoking its main guard."""
    spec = importlib.util.spec_from_file_location("execute_stock_daily_backfill_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    """Return the imported CLI module for direct main() calls."""
    return _load_cli()


def _registry() -> InstrumentReferenceRegistry:
    """Build the exact snapshot scope accepted by the fixed execution target."""
    contract = build_stock_daily_execution_contract()
    return InstrumentReferenceRegistry(
        registry_version="cli-registry-v1",
        evidence_ref="review:cli-registry-v1",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id=contract.dataset_id,
                source_id=contract.source.source_id,
                instrument_id="CN:600519",
                lifecycle_version="life-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=END,
                status_evidence_ref="review:status-v1",
                known_start_date=START,
                start_boundary_unknown=False,
                start_evidence_ref="exchange:listing-v1",
            ),
        ),
        aliases=(
            InstrumentAlias(
                dataset_id=contract.dataset_id,
                source_id=contract.source.source_id,
                alias="600519.SH",
                instrument_id="CN:600519",
                alias_version="ticker-v1",
                evidence_ref="review:600519.SH",
                valid_from=START,
                valid_through=END,
            ),
        ),
    )


def _snapshot(tmp_path: Path) -> tuple[Path, str]:
    """Write one valid snapshot and return its path and exact byte digest."""
    content = encode_instrument_reference_snapshot(_registry())
    path = tmp_path / "snapshot.json"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _wire_body(day: date) -> bytes:
    """Return one synthetic response whose row stays inside the requested day."""
    return json.dumps(
        {
            "rc": 0,
            "data": {
                "code": "600519",
                "market": 1,
                "klines": [f"{day.isoformat()},10.0,11.0,11.5,9.5,12,134,20,10,1,2"],
            },
        },
        separators=(",", ":"),
    ).encode()


def _base_args(snapshot: Path, digest: str) -> list[str]:
    """Build the minimal deterministic dry-run argument list."""
    return [
        "--snapshot",
        str(snapshot),
        "--snapshot-sha256",
        digest,
        "--as-of",
        END.isoformat(),
        "--max-window-days",
        "1",
        "--max-chunks",
        "2",
    ]


def _database(tmp_path: Path) -> Path:
    """Create an owned SQLite ledger and publication schema."""
    path = tmp_path / "cli.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    RUN_LEDGER_METADATA.create_all(engine)
    PUBLICATION_METADATA.create_all(engine)
    engine.dispose()
    return path


def _counts(path: Path) -> tuple[int, int, int]:
    """Return ledger, publication, and fact-version counts."""
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return (
                int(connection.scalar(select(func.count()).select_from(RUN_LEDGER_TABLE))),
                int(connection.scalar(select(func.count()).select_from(PUBLICATIONS))),
                int(connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS))),
            )
    finally:
        engine.dispose()


def _error(stderr: str) -> dict[str, object]:
    """Parse the CLI's stable JSON error object."""
    return json.loads(stderr)


def test_cli_default_dry_run_emits_a_deterministic_receipt(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    args = _base_args(snapshot, digest)

    assert cli.main(args) == 0
    first = capsys.readouterr()
    assert cli.main(args) == 0
    second = capsys.readouterr()

    receipt = json.loads(first.out)
    assert first.out == second.out
    assert first.err == second.err == ""
    assert receipt["dry_run"] is True
    assert receipt["execution_authorized"] is False
    assert receipt["full_history_ready"] is False
    assert receipt["chunks_total"] == 2
    assert receipt["chunks_considered"] == 2
    assert receipt["chunks_executed"] == 0
    assert receipt["stop_reason"] == "dry_run"
    assert str(tmp_path) not in first.out
    assert "sqlite" not in first.out


def test_cli_accepts_an_explicit_local_sqlite_ledger_url(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    database = _database(tmp_path)
    args = [*_base_args(snapshot, digest), "--ledger-url", f"sqlite:///{database}"]

    assert cli.main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["dry_run"] is True
    assert receipt["chunks_executed"] == 0
    assert _counts(database) == (0, 0, 0)


def test_cli_rejects_missing_or_mismatched_digests_with_stable_json(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)

    assert cli.main(["--snapshot", str(snapshot), "--as-of", END.isoformat()]) == 2
    assert _error(capsys.readouterr().err) == {
        "error": "invalid_arguments",
        "status": "error",
    }

    mismatched = ["0" if character != "0" else "1" for character in digest]
    args = _base_args(snapshot, "".join(mismatched))
    assert cli.main(args) == 2
    assert _error(capsys.readouterr().err) == {
        "error": "snapshot_sha256_mismatch",
        "status": "error",
    }


def test_cli_rejects_unknown_targets_and_unsafe_database_inputs(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    unknown_target = [*_base_args(snapshot, digest), "--target", "production-mysql"]
    assert cli.main(unknown_target) == 2
    assert _error(capsys.readouterr().err)["error"] == "unknown_target"

    for extra in (
        ["--ledger-url", "postgresql://user:secret@example.invalid/db"],
        ["--ledger-url", "sqlite:///:memory:"],
        ["--database-path", "relative.sqlite"],
    ):
        assert cli.main([*_base_args(snapshot, digest), *extra]) == 2
        assert _error(capsys.readouterr().err)["error"] == "unsafe_database_url"


def test_cli_execute_requires_local_offline_response_inputs(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    database = _database(tmp_path)
    args = [*_base_args(snapshot, digest), "--database-path", str(database), "--execute"]

    assert cli.main(args) == 2
    assert _error(capsys.readouterr().err)["error"] == "execute_requires_offline_response"

    single = tmp_path / "single.json"
    single.write_bytes(_wire_body(START))
    args = [
        *args,
        "--raw-response",
        str(single),
        "--artifact-root",
        str(tmp_path / "raw"),
        "--observed-at",
        "2020-01-03T08:15:00Z",
    ]
    assert cli.main(args) == 2
    assert _error(capsys.readouterr().err)["error"] == "raw_response_requires_single_chunk"
    assert _counts(database) == (0, 0, 0)


def test_cli_execute_publishes_two_offline_chunks_without_network(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    database = _database(tmp_path)
    responses = tmp_path / "responses"
    responses.mkdir()
    (responses / "600519_20200101_20200101.json").write_bytes(_wire_body(START))
    (responses / "600519_20200102_20200102.json").write_bytes(_wire_body(END))
    args = [
        *_base_args(snapshot, digest),
        "--database-path",
        str(database),
        "--execute",
        "--raw-response-dir",
        str(responses),
        "--artifact-root",
        str(tmp_path / "raw-artifacts"),
        "--observed-at",
        "2020-01-03T08:15:00Z",
    ]

    assert cli.main(args) == 0
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    assert receipt["dry_run"] is False
    assert receipt["chunks_executed"] == 2
    assert receipt["chunks_failed"] == 0
    assert receipt["chunks_remaining"] == 0
    assert receipt["stop_reason"] == "plan_finished"
    assert _counts(database) == (2, 2, 2)
    assert str(tmp_path) not in output.out


def test_cli_execute_reuses_succeeded_chunks_on_a_second_invocation(
    cli: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, digest = _snapshot(tmp_path)
    database = _database(tmp_path)
    responses = tmp_path / "responses"
    responses.mkdir()
    (responses / "600519_20200101_20200101.json").write_bytes(_wire_body(START))
    (responses / "600519_20200102_20200102.json").write_bytes(_wire_body(END))
    args = [
        *_base_args(snapshot, digest),
        "--database-path",
        str(database),
        "--execute",
        "--raw-response-dir",
        str(responses),
        "--artifact-root",
        str(tmp_path / "raw-artifacts"),
        "--observed-at",
        "2020-01-03T08:15:00Z",
    ]

    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["chunks_executed"] == 0
    assert receipt["chunks_skipped"] == 2
    assert _counts(database) == (2, 2, 2)
