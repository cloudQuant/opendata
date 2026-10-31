#!/usr/bin/env python3
"""Manage local authorization and candidate preparation for one CSI 500 sample.

The CLI never accepts a database URL or provider command. ``preview`` is pure
offline metadata. ``authorize`` writes one user-declared grant and quota bucket
only to a fresh, isolated target. The migration initializes source-gate only;
the warehouse schema remains unapplied. ``prepare`` reads back the actual
target configuration and stores a deterministic candidate intent in its
manifest. Gate locking, submission, and V4 execution remain disabled. The
pre-existing authorized V1 job path is outside this CLI and is not changed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic.config import Config  # noqa: E402

from alembic import command  # noqa: E402
from alembic_source_gate.path_contract import assert_database_file_identity  # noqa: E402
from opendata.pipeline import ths_csi500_sample_composition as sample  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence

_MIGRATION_TIMEOUT_SECONDS = 180


def build_parser() -> argparse.ArgumentParser:
    """Build only fixed operations over program-owned temporary targets."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create a fresh private sample target")

    migrate = subparsers.add_parser(
        "migrate", help="initialize only source-gate schema in a fresh isolated target"
    )
    migrate.add_argument("--target", required=True)

    authorize = subparsers.add_parser(
        "authorize",
        help="declare one local, isolated-target grant and quota without sending a request",
    )
    authorize.add_argument("--target", required=True)
    authorize.add_argument("--calendar-manifest", required=True)
    authorize.add_argument("--business-date")
    authorize.add_argument("--actor", required=True)
    authorize.add_argument(
        "--principal-scope",
        required=True,
        help="explicit narrow identity of the intended local research caller",
    )
    authorize.add_argument(
        "--confirm-user-declared-internal-use", action="store_true", required=True
    )

    prepare = subparsers.add_parser(
        "prepare",
        help="persist one deterministic offline intent; does not lock or send",
    )
    prepare.add_argument("--target", required=True)
    prepare.add_argument("--calendar-manifest", required=True)
    prepare.add_argument("--business-date")

    preview = subparsers.add_parser(
        "preview",
        help="show the fixed request shape without target, calendar, or authorization",
    )
    preview.add_argument("--business-date", required=True)

    child = subparsers.add_parser("_migration-child", help=argparse.SUPPRESS)
    child.add_argument("--target", required=True)
    child.add_argument("--device", required=True, type=int)
    child.add_argument("--inode", required=True, type=int)
    return parser


def _migration_environment(target: sample.SampleTarget) -> dict[str, str]:
    """Build the child allowlist with HOME pinned to a validated private target."""
    if not isinstance(target, sample.SampleTarget):
        raise sample.Csi500SampleCompositionError("migration_target_identity_invalid")
    current = sample.load_sample_target(target.directory)
    sample._require_source_gate_only_target(current)
    if (
        current.directory != target.directory
        or current.database_path != target.database_path
        or current.identity != target.identity
        or current.phase != "migration_started"
        or target.phase != "migration_started"
        or current.directory.parent != sample.SAMPLE_TEMP_ROOT
    ):
        raise sample.Csi500SampleCompositionError("migration_target_identity_invalid")
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        # Keep Python's default text I/O deterministic. The pinned Alembic
        # parser explicitly uses the locale encoding for ini files, so those
        # files must remain ASCII-compatible for C-locale hosts.
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PROJECT_ROOT),
        "HOME": str(current.directory),
    }


def _migrate(target_value: str) -> dict[str, object]:
    """Initialize only the source-gate schema in one fresh isolated SQLite file."""
    target = sample.load_sample_target(target_value)
    sample._require_source_gate_only_target(target)
    if target.phase != "created":
        raise sample.Csi500SampleCompositionError("migration_target_not_fresh")
    with sample.lock_sample_target(target, operation="migration"):
        current = sample.transition_sample_target(
            target,
            expected_phase="created",
            new_phase="migration_started",
        )
        child_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_migration-child",
            "--target",
            str(current.directory),
            "--device",
            str(current.identity.device),
            "--inode",
            str(current.identity.inode),
        ]
        try:
            migration_environment = _migration_environment(current)
            result = subprocess.run(  # noqa: S603 - fixed interpreter/script and enumerated args
                child_command,
                cwd=current.directory,
                env=migration_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                timeout=_MIGRATION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            _mark_migration_uncertain(current)
            raise sample.Csi500SampleCompositionError("migration_outcome_uncertain") from None
        except sample.Csi500SampleCompositionError:
            _mark_migration_uncertain(current)
            raise
        current = sample.load_sample_target(current.directory)
        if result.returncode != 0:
            _mark_migration_uncertain(current)
            raise sample.Csi500SampleCompositionError("migration_outcome_uncertain")
        try:
            migration_state = sample._read_migration_state(current)
        except sample.Csi500SampleCompositionError:
            _mark_migration_uncertain(current)
            raise sample.Csi500SampleCompositionError("migration_state_unconfirmed") from None
        if migration_state != sample.SAMPLE_EXPECTED_MIGRATION_STATE:
            _mark_migration_uncertain(current)
            raise sample.Csi500SampleCompositionError("migration_state_mismatch")
        initialized = sample.transition_sample_target(
            current,
            expected_phase="migration_started",
            new_phase="source_gate_initialized",
        )
        return {
            "status": "source_gate_initialized",
            "target_id": initialized.state["target_id"],
            "target_profile": initialized.state["target_profile"],
            "warehouse_schema_status": migration_state.warehouse_schema_status,
            "source_gate_head": migration_state.source_gate_head,
            "database_identity": "pinned",
        }


def _mark_migration_uncertain(target: sample.SampleTarget) -> None:
    """Seal any migration result whose actual source-gate state is unconfirmed."""
    try:
        current = sample.load_sample_target(target.directory)
        if current.phase == "migration_started":
            sample.transition_sample_target(
                current,
                expected_phase="migration_started",
                new_phase="migration_uncertain",
            )
    except Exception:
        # An unreadable state is retained for manual inspection and is never
        # retried by this command.
        return


def _run_migration_child(
    target_value: str,
    *,
    device: int,
    inode: int,
) -> None:
    """Initialize the gate chain in an explicitly empty pinned SQLite file."""
    target = sample.load_sample_target(target_value)
    sample._require_source_gate_only_target(target)
    if (
        target.phase != "migration_started"
        or target.identity.device != device
        or target.identity.inode != inode
        or os.environ.get("HOME") != str(target.directory)
        or (target.directory / ".env").exists()
    ):
        raise sample.Csi500SampleCompositionError("migration_child_target_invalid")
    lock_path = target.directory / sample.SAMPLE_LOCK_FILENAME
    try:
        lock_stat = lock_path.lstat()
        lock_bytes = sample._read_bounded_file(lock_path, 256)
    except Exception:
        raise sample.Csi500SampleCompositionError("migration_lock_unavailable") from None
    if (
        not stat.S_ISREG(lock_stat.st_mode)
        or lock_stat.st_uid != os.getuid()
        or stat.S_IMODE(lock_stat.st_mode) != 0o600
        or lock_bytes != f"pid={os.getppid()} operation=migration\n".encode("ascii")
    ):
        raise sample.Csi500SampleCompositionError("migration_lock_invalid")
    assert_database_file_identity(target.database_path, target.identity)

    gate_config = Config(str(PROJECT_ROOT / "alembic_source_gate.ini"))
    _set_fixed_migration_script_location(gate_config, "alembic_source_gate")
    gate_config.attributes["source_gate_path"] = target.database_path
    gate_config.attributes["source_gate_file_identity"] = target.identity
    gate_config.attributes["source_gate_initialize_empty"] = True
    gate_config.attributes["source_gate_require_empty"] = True
    command.upgrade(gate_config, "head")
    if gate_config.attributes.get("source_gate_initialization_mode") != "initialize-empty":
        raise sample.Csi500SampleCompositionError("migration_initialization_mode_unconfirmed")
    assert_database_file_identity(target.database_path, target.identity)
    if sample._read_migration_state(target) != sample.SAMPLE_EXPECTED_MIGRATION_STATE:
        raise sample.Csi500SampleCompositionError("migration_state_unconfirmed")


def _set_fixed_migration_script_location(config: Config, directory_name: str) -> None:
    """Pin Alembic to one verified repository script directory, independent of cwd."""
    if directory_name != "alembic_source_gate":
        raise sample.Csi500SampleCompositionError("migration_script_location_invalid")
    expected = PROJECT_ROOT / directory_name
    try:
        resolved = expected.resolve(strict=True)
    except (OSError, RuntimeError):
        raise sample.Csi500SampleCompositionError("migration_script_location_unavailable") from None
    if (
        not expected.is_absolute()
        or resolved != expected
        or expected.parent != PROJECT_ROOT
        or not expected.is_dir()
    ):
        raise sample.Csi500SampleCompositionError("migration_script_location_invalid")
    config.set_main_option("script_location", str(expected).replace("%", "%%"))
    configured = config.get_main_option("script_location")
    if (
        not isinstance(configured, str)
        or configured != str(expected)
        or not Path(configured).is_absolute()
    ):
        raise sample.Csi500SampleCompositionError("migration_script_location_invalid")


def _run(args: argparse.Namespace) -> int:
    """Execute one bounded local operation and emit only sanitized metadata."""
    if args.command == "preview":
        report = sample.preview_csi500_sample_request(
            business_date=sample._parse_calendar_date(args.business_date)
        )
        _emit(
            {
                "status": report.status,
                "authorization_status": report.authorization_status,
                "calendar_evidence_status": report.calendar_evidence_status,
                "target_status": report.target_status,
                "gate_status": report.gate_status,
                "execution_enabled": report.execution_enabled,
                "dataset_id": report.dataset_id,
                "symbol": report.symbol,
                "business_date": report.business_date.isoformat(),
                "request": {
                    "method": report.method,
                    "host": report.host,
                    "path": report.path,
                    "query_summary_sha256": report.query_summary_sha256,
                },
            }
        )
        return 0
    if args.command == "init":
        target = sample.create_sample_target()
        _emit(
            {
                "status": "created",
                "target_id": target.state["target_id"],
                "target_directory": str(target.directory),
                "target_profile": target.state["target_profile"],
                "warehouse_schema_status": target.state["warehouse_schema_status"],
                "database_identity": "pinned",
            }
        )
        return 0
    if args.command == "_migration-child":
        _run_migration_child(args.target, device=args.device, inode=args.inode)
        return 0
    if args.command == "migrate":
        _emit(_migrate(args.target))
        return 0
    if args.command == "authorize":
        # Only a program-created target and explicit user declaration reach
        # the local grant/quota writers; this path never reads credentials.
        target = sample.load_sample_target(args.target)
        evidence = sample.load_csi500_trading_day_evidence(
            args.calendar_manifest,
            expected_business_date=(
                sample._parse_calendar_date(args.business_date)
                if args.business_date is not None
                else None
            ),
        )
        with sample.lock_sample_target(target, operation="authorization"):
            authorized = sample.provision_sample_source_authorization(
                target,
                calendar_evidence=evidence,
                actor=args.actor,
                principal_scope=args.principal_scope,
                confirm_user_declared_internal_use=args.confirm_user_declared_internal_use,
            )
        _emit(
            {
                "status": "authorized_for_review_only",
                "target_id": authorized.state["target_id"],
                "target_profile": authorized.state["target_profile"],
                "warehouse_schema_status": authorized.state["warehouse_schema_status"],
                "grant_id": authorized.state["grant_id"],
                "rights_basis": authorized.state["source_rights_basis"],
                "rights_independently_verified": False,
                "local_grant_configured": True,
                "local_quota_configured": True,
                "live_execution_enabled": False,
            }
        )
        return 0
    if args.command == "prepare":
        target = sample.load_sample_target(args.target)
        evidence = sample.load_csi500_trading_day_evidence(
            args.calendar_manifest,
            expected_business_date=(
                sample._parse_calendar_date(args.business_date)
                if args.business_date is not None
                else None
            ),
        )
        spec = sample.prepare_csi500_sample_one_shot_intent(
            target,
            calendar_evidence=evidence,
        )
        _emit(
            {
                "status": "intent_prepared",
                "target_id": target.state["target_id"],
                "target_profile": target.state["target_profile"],
                "warehouse_schema_status": target.state["warehouse_schema_status"],
                "intent_sha256": spec.intent_sha256,
                "capture_intent_id": spec.capture_intent_id,
                "run_key": spec.run_key,
                "request_summary_sha256": spec.source_attempt_scope.request_summary_sha256,
                "business_date": spec.business_date.isoformat(),
                "symbol": sample.SAMPLE_SYMBOL,
                "request_budget": {"task_attempts": 1, "source_attempts": 1},
                "gate_scope_status": "CANDIDATE_NOT_LOCKED",
                "source_rights_basis": target.state["source_rights_basis"],
                "rights_independently_verified": False,
                "live_execution_enabled": False,
            }
        )
        return 0
    raise sample.Csi500SampleCompositionError("command_invalid")


def _emit(payload: object) -> None:
    """Write compact JSON without printing environment or exception details."""
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with fixed error codes and no sensitive exception output."""
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        return _run(args)
    except sample.Csi500SampleCompositionError as error:
        _emit({"status": "blocked", "code": error.code})
        return 3
    except KeyboardInterrupt:
        _emit({"status": "uncertain", "code": "interrupted_do_not_retry"})
        return 130
    except Exception:
        _emit({"status": "blocked", "code": "operation_outcome_uncertain"})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
