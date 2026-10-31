"""User-facing CLI boundaries for the offline CSI 500 sample workflow."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest

from opendata.data.providers.ths.transport import credentials
from opendata.pipeline import ths_csi500_sample_composition as sample
from scripts.ops import run_csi500_one_shot_sample as sample_cli


def _write_calendar_manifest(directory: Path, business_date: date) -> Path:
    """Create a synthetic, internally consistent calendar for offline CLI tests."""
    artifact = directory / "calendar.txt"
    artifact.write_text(f"session={business_date.isoformat()}\n", encoding="utf-8")
    manifest = directory / "calendar.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exchange": "SSE",
                "calendar_version": "cli-fixture-v1",
                "source_reference": "synthetic CLI fixture; not an authority proof",
                "retrieved_at_utc": "2026-10-01T00:00:00Z",
                "reviewed_by": "offline-cli-test",
                "artifact_path": str(artifact),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "open_sessions": [business_date.isoformat()],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def private_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sample.SampleTarget:
    """Keep target creation inside pytest's private directory."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    return sample.create_sample_target()


def test_parser_requires_explicit_authorization_and_rejects_scheduler_controls() -> None:
    """There is no recurring schedule or submission switch in this candidate CLI."""
    parser = sample_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "authorize",
                "--target",
                "/tmp/unused",
                "--calendar-manifest",
                "/tmp/unused.json",
                "--actor",
                "offline-user",
                "--principal-scope",
                "offline-csi500-worker",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["schedule", "--cron", "0 18 * * 1-5"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "prepare",
                "--target",
                "/tmp/unused",
                "--calendar-manifest",
                "/tmp/unused.json",
                "--schedule",
                "daily",
            ]
        )


def test_init_cli_reports_only_a_private_unmigrated_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reachable init operation creates a private candidate, not warehouse data."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)

    result = sample_cli.main(["init"])

    output = json.loads(capsys.readouterr().out)
    target = Path(output["target_directory"])
    assert result == 0
    assert output["status"] == "created"
    assert output["target_profile"] == sample.SAMPLE_TARGET_PROFILE
    assert output["warehouse_schema_status"] == "NOT_APPLIED"
    assert output["database_identity"] == "pinned"
    assert target.parent == tmp_path
    assert target.is_dir()


def test_prepare_cli_returns_real_unauthorized_phase_error_without_writes(
    private_target: sample.SampleTarget,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid date cannot make a newly created target eligible for preparation."""
    business_date = date.today() - timedelta(days=10)
    calendar_manifest = _write_calendar_manifest(tmp_path, business_date)

    result = sample_cli.main(
        [
            "prepare",
            "--target",
            str(private_target.directory),
            "--calendar-manifest",
            str(calendar_manifest),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == 3
    assert output == {"status": "blocked", "code": "target_not_authorized"}
    assert sample.load_sample_target(private_target.directory).phase == "created"
    assert private_target.database_path.stat().st_size == 0


def test_preview_cli_reports_invalid_calendar_date_as_safe_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bad user input is rejected by the real date parser without echoing it."""
    result = sample_cli.main(["preview", "--business-date", "2026-02-30-private-input"])

    assert result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "calendar_date_invalid",
    }


def test_preview_cli_fails_closed_when_the_fixed_provider_route_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A changed transport base URL blocks preview instead of describing another host."""
    monkeypatch.setattr(credentials, "DEFAULT_FUYAO_API_BASE_URL", "http://fuyao.aicubes.cn")

    result = sample_cli.main(["preview", "--business-date", "2026-10-30"])

    assert result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "preview_route_invalid",
    }


@pytest.mark.parametrize(
    "failure",
    [
        OSError("private filesystem detail"),
        subprocess.TimeoutExpired(cmd="isolated-migration-child", timeout=180),
    ],
    ids=["spawn-error", "timeout"],
)
def test_migration_process_errors_seal_the_target_and_are_not_retried(
    private_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    """An uncertain child outcome is reported safely and cannot be retried."""
    calls: list[int] = []

    def fail_to_start_child(*_args: object, **_kwargs: object) -> object:
        calls.append(1)
        raise failure

    monkeypatch.setattr(sample_cli.subprocess, "run", fail_to_start_child)

    result = sample_cli.main(["migrate", "--target", str(private_target.directory)])

    assert result == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "migration_outcome_uncertain",
    }
    sealed = sample.load_sample_target(private_target.directory)
    assert sealed.phase == "migration_uncertain"
    assert not (sealed.directory / sample.SAMPLE_LOCK_FILENAME).exists()

    retry = sample_cli.main(["migrate", "--target", str(sealed.directory)])

    assert retry == 3
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "migration_target_not_fresh",
    }
    assert calls == [1]


@pytest.mark.parametrize(
    ("error_factory", "expected_exit", "expected_payload"),
    [
        (
            lambda: sample.Csi500SampleCompositionError("invalid code value"),
            3,
            {"status": "blocked", "code": "sample_composition_failed"},
        ),
        (
            lambda: KeyboardInterrupt(),
            130,
            {"status": "uncertain", "code": "interrupted_do_not_retry"},
        ),
        (
            lambda: RuntimeError("private implementation detail"),
            3,
            {"status": "blocked", "code": "operation_outcome_uncertain"},
        ),
    ],
    ids=["invalid-safe-code", "interrupt", "unexpected-error"],
)
def test_cli_outer_error_boundary_emits_only_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_factory: Callable[[], BaseException],
    expected_exit: int,
    expected_payload: dict[str, str],
) -> None:
    """Expected, interrupted, and unexpected failures never print exception text."""
    def raise_configured_error(_args: object) -> int:
        raise error_factory()

    monkeypatch.setattr(sample_cli, "_run", raise_configured_error)

    result = sample_cli.main(["preview", "--business-date", "2026-10-30"])

    captured = capsys.readouterr()
    assert result == expected_exit
    assert json.loads(captured.out) == expected_payload
    assert "invalid code value" not in captured.out
    assert "private implementation detail" not in captured.out
