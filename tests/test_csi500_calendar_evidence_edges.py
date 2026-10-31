"""Isolated boundary tests for CSI 500 calendar evidence and target manifests."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from opendata.pipeline import ths_csi500_sample_composition as sample

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def isolated_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sample.SampleTarget:
    """Create a fresh target under pytest's private temporary directory."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    return sample.create_sample_target()


def _calendar_pair(
    directory: Path,
    sessions: object,
    *,
    updates: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """Write a local manifest/artifact pair without contacting an authority."""
    artifact = directory / "sse-sessions.txt"
    artifact.write_bytes(b"Offline SSE session evidence fixture.\n")
    manifest = directory / "calendar-evidence.json"
    value: dict[str, object] = {
        "schema_version": 1,
        "exchange": "SSE",
        "calendar_version": "fixture-v1",
        "source_reference": "offline fixture; source authenticity is unverified",
        "retrieved_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "reviewed_by": "offline-reviewer",
        "artifact_path": str(artifact),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "open_sessions": sessions,
    }
    if updates:
        value.update(updates)
    manifest.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    artifact.chmod(0o600)
    return manifest, artifact


def _rewrite_target_manifest(target: sample.SampleTarget, state: dict[str, object]) -> Path:
    """Replace only the fixture target's manifest while preserving private mode."""
    path = target.directory / sample.SAMPLE_MANIFEST_FILENAME
    path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="ascii")
    path.chmod(0o600)
    return path


def test_calendar_evidence_pins_exact_bytes_and_selects_from_multiple_sessions(
    tmp_path: Path,
) -> None:
    """A requested past session is accepted only with matching local byte pins."""
    last_days = sample._shanghai_today() - timedelta(days=10)
    sessions = [
        (last_days - timedelta(days=2)).isoformat(),
        (last_days - timedelta(days=1)).isoformat(),
        last_days.isoformat(),
    ]
    manifest, artifact = _calendar_pair(tmp_path, sessions)

    evidence = sample.load_csi500_trading_day_evidence(
        manifest,
        expected_business_date=last_days,
    )

    assert evidence.business_date == last_days
    assert evidence.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert evidence.artifact_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert evidence.source_reference == "offline fixture; source authenticity is unverified"


def test_single_session_calendar_can_select_its_only_date_without_a_hint(tmp_path: Path) -> None:
    """A one-session file has one unambiguous completed sample date."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, _ = _calendar_pair(tmp_path, [session.isoformat()])

    evidence = sample.load_csi500_trading_day_evidence(manifest)

    assert evidence.business_date == session


def test_multi_session_calendar_requires_an_explicit_sample_date(tmp_path: Path) -> None:
    """The parser does not silently choose a date from a wider calendar."""
    first = sample._shanghai_today() - timedelta(days=3)
    manifest, _ = _calendar_pair(
        tmp_path,
        [first.isoformat(), (first + timedelta(days=1)).isoformat()],
    )

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert error.value.code == "calendar_sample_date_required"


@pytest.mark.parametrize(
    ("sessions", "code"),
    [
        ([], "calendar_sessions_invalid"),
        ("2026-01-02", "calendar_sessions_invalid"),
        (["2026-01-02", "2026-01-02"], "calendar_sessions_not_canonical"),
        (["2026-01-03", "2026-01-02"], "calendar_sessions_not_canonical"),
        (["2026-1-02"], "calendar_date_invalid"),
        (["2026-02-30"], "calendar_date_invalid"),
    ],
)
def test_calendar_sessions_reject_invalid_shape_order_and_dates(
    tmp_path: Path,
    sessions: object,
    code: str,
) -> None:
    """Open sessions must be a bounded, sorted, unique list of exact ISO dates."""
    manifest, _ = _calendar_pair(tmp_path, sessions)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(
            manifest,
            expected_business_date=sample._shanghai_today() - timedelta(days=10),
        )

    assert error.value.code == code


def test_calendar_session_count_is_bounded_before_sample_selection(tmp_path: Path) -> None:
    """Oversized calendars fail before a caller can select a date from them."""
    first = date(1950, 1, 1)
    sessions = [(first + timedelta(days=offset)).isoformat() for offset in range(10_001)]
    manifest, _ = _calendar_pair(tmp_path, sessions)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert error.value.code == "calendar_sessions_invalid"


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"schema_version": 2}, "calendar_manifest_shape_invalid"),
        ({"schema_version": True}, "calendar_manifest_shape_invalid"),
        ({"exchange": "SZSE"}, "calendar_exchange_invalid"),
        ({"retrieved_at_utc": "not-a-timestamp"}, "timestamp_invalid"),
        ({"retrieved_at_utc": "2026-10-01T00:00:00"}, "timestamp_invalid"),
        (
            {
                "retrieved_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=10))
                .isoformat()
                .replace("+00:00", "Z")
            },
            "calendar_timestamp_invalid",
        ),
        ({"retrieved_at_utc": "x" * 41}, "timestamp_invalid"),
        ({"calendar_version": "calendar version with spaces"}, "calendar_version_invalid"),
        ({"reviewed_by": ""}, "reviewed_by_invalid"),
        (
            {"source_reference": "https://example.invalid/?api_key=secret"},
            "calendar_source_reference_invalid",
        ),
        ({"source_reference": "reference\nforged"}, "calendar_source_reference_invalid"),
        ({"source_reference": " padded-reference "}, "calendar_source_reference_invalid"),
        ({"artifact_path": "relative-calendar-artifact.txt"}, "evidence_path_invalid"),
    ],
)
def test_calendar_manifest_metadata_is_validated_before_evidence_is_returned(
    tmp_path: Path,
    updates: dict[str, object],
    code: str,
) -> None:
    """Schema, exchange, retrieval time and metadata constraints fail closed."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, _ = _calendar_pair(tmp_path, [session.isoformat()], updates=updates)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert error.value.code == code


def test_calendar_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Duplicate keys cannot hide which exchange or evidence pin was reviewed."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, _ = _calendar_pair(tmp_path, [session.isoformat()])
    raw = manifest.read_bytes().replace(
        b'"exchange":"SSE"',
        b'"exchange":"SSE","exchange":"SZSE"',
        1,
    )
    manifest.write_bytes(raw)
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert error.value.code == "calendar_manifest_invalid"


def test_calendar_loader_rejects_invalid_utf8_and_non_json_bytes(tmp_path: Path) -> None:
    """Malformed manifest bytes do not leak parser details to callers."""
    manifest = tmp_path / "calendar-evidence.json"
    manifest.write_bytes(b"\xff\xfe{")
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert error.value.code == "calendar_manifest_invalid"


def test_calendar_loader_rejects_unknown_and_missing_manifest_fields(tmp_path: Path) -> None:
    """The frozen schema has neither implicit defaults nor extension fields."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, _ = _calendar_pair(tmp_path, [session.isoformat()])
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["unreviewed_override"] = "ignored-if-permitted"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as extra_error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert extra_error.value.code == "calendar_manifest_shape_invalid"
    value.pop("unreviewed_override")
    value.pop("reviewed_by")
    manifest.write_text(json.dumps(value), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as missing_error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert missing_error.value.code == "calendar_manifest_shape_invalid"


def test_calendar_loader_requires_exact_past_date_value(tmp_path: Path) -> None:
    """Today's incomplete session and datetime subclasses cannot become samples."""
    today = sample._shanghai_today()
    manifest, _ = _calendar_pair(tmp_path, [today.isoformat()])

    with pytest.raises(sample.Csi500SampleCompositionError) as today_error:
        sample.load_csi500_trading_day_evidence(
            manifest,
            expected_business_date=today,
        )

    assert today_error.value.code == "sample_date_must_be_completed"

    past = today - timedelta(days=2)
    past_manifest, _ = _calendar_pair(tmp_path, [past.isoformat()])
    with pytest.raises(sample.Csi500SampleCompositionError) as type_error:
        sample.load_csi500_trading_day_evidence(
            past_manifest,
            expected_business_date=datetime.combine(past, datetime.min.time()),
        )

    assert type_error.value.code == "sample_date_not_in_calendar"


def test_calendar_loader_rejects_changed_artifact_and_malformed_digest(tmp_path: Path) -> None:
    """Artifact contents and their lowercase SHA-256 pin must agree exactly."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, artifact = _calendar_pair(tmp_path, [session.isoformat()])
    artifact.write_bytes(b"replacement content\n")

    with pytest.raises(sample.Csi500SampleCompositionError) as changed_error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert changed_error.value.code == "calendar_artifact_pin_mismatch"

    manifest, _ = _calendar_pair(
        tmp_path,
        [session.isoformat()],
        updates={"artifact_sha256": "A" * 64},
    )
    with pytest.raises(sample.Csi500SampleCompositionError) as digest_error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert digest_error.value.code == "artifact_sha256_invalid"


@pytest.mark.parametrize(
    ("path_value", "code"),
    [
        ("relative-calendar.json", "evidence_path_invalid"),
        ("/path/that/does/not/exist/calendar.json", "evidence_file_unavailable"),
    ],
)
def test_calendar_loader_rejects_unpinned_or_unavailable_manifest_paths(
    path_value: str,
    code: str,
) -> None:
    """The caller must name an existing absolute regular file without traversal."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(path_value)

    assert error.value.code == code


@pytest.mark.parametrize("path_value", [None, "", 7])
def test_calendar_loader_rejects_non_path_manifest_values(path_value: object) -> None:
    """Malformed caller configuration cannot be coerced into an evidence path."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_csi500_trading_day_evidence(path_value)

    assert error.value.code == "evidence_path_invalid"


def test_calendar_loader_rejects_directory_and_oversized_manifest(tmp_path: Path) -> None:
    """Evidence inputs must be regular files within the fixed manifest byte bound."""
    with pytest.raises(sample.Csi500SampleCompositionError) as directory_error:
        sample.load_csi500_trading_day_evidence(tmp_path)

    assert directory_error.value.code == "evidence_file_invalid"
    oversized = tmp_path / "oversized-calendar.json"
    oversized.write_bytes(b" " * (sample._MAX_CALENDAR_MANIFEST_BYTES + 1))
    oversized.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as size_error:
        sample.load_csi500_trading_day_evidence(oversized)

    assert size_error.value.code == "evidence_file_invalid"


def test_calendar_loader_rejects_symlinked_manifest_and_artifact(tmp_path: Path) -> None:
    """Symlink components cannot redirect a reviewed artifact after pinning."""
    session = sample._shanghai_today() - timedelta(days=2)
    manifest, artifact = _calendar_pair(tmp_path, [session.isoformat()])
    manifest_link = tmp_path / "calendar-link.json"
    manifest_link.symlink_to(manifest)

    with pytest.raises(sample.Csi500SampleCompositionError) as manifest_error:
        sample.load_csi500_trading_day_evidence(manifest_link)

    assert manifest_error.value.code == "evidence_path_symlink"

    artifact_link = tmp_path / "artifact-link.txt"
    artifact_link.symlink_to(artifact)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["artifact_path"] = str(artifact_link)
    manifest.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as artifact_error:
        sample.load_csi500_trading_day_evidence(manifest)

    assert artifact_error.value.code == "evidence_path_symlink"


def test_preview_rejects_datetime_values_as_business_dates() -> None:
    """Preview accepts a date object, not a timestamp that might shift a day."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.preview_csi500_sample_request(
            business_date=datetime.combine(sample._shanghai_today(), datetime.min.time())
        )

    assert error.value.code == "calendar_date_invalid"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://fuyao.aicubes.cn",
        "https://unapproved.example.invalid",
    ],
)
def test_preview_fails_closed_when_fixed_fuyao_route_is_not_https_and_pinned(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    """A preview never describes requests to a downgraded or substituted host."""
    from opendata.data.providers.ths.transport import credentials

    monkeypatch.setattr(credentials, "DEFAULT_FUYAO_API_BASE_URL", base_url)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.preview_csi500_sample_request(
            business_date=sample._shanghai_today() - timedelta(days=1)
        )

    assert error.value.code == "preview_route_invalid"


def test_target_manifest_rejects_corruption_and_duplicate_identity_keys(
    isolated_target: sample.SampleTarget,
) -> None:
    """A malformed manifest cannot be parsed into a different target identity."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest.write_bytes(b"{broken json")
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as invalid_error:
        sample.load_sample_target(isolated_target.directory)

    assert invalid_error.value.code == "target_manifest_invalid"

    state = dict(isolated_target.state)
    manifest.write_bytes(
        json.dumps(state, sort_keys=True, separators=(",", ":"))
        .replace('"target_id":"', '"target_id":"' + state["target_id"] + '","target_id":"', 1)
        .encode("ascii")
    )
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as duplicate_error:
        sample.load_sample_target(isolated_target.directory)

    assert duplicate_error.value.code == "target_manifest_invalid"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda state: state.update({"unexpected_key": "value"}), "target_manifest_shape_invalid"),
        (lambda state: state.update({"schema_version": True}), "target_manifest_shape_invalid"),
        (
            lambda state: state.update({"target_profile": "other-profile"}),
            "target_manifest_profile_invalid",
        ),
        (
            lambda state: state.update({"database_inode": state["database_inode"] + 1}),
            "target_database_identity_invalid",
        ),
    ],
)
def test_target_manifest_rejects_unknown_profile_and_database_identity_pins(
    isolated_target: sample.SampleTarget,
    change: Any,
    code: str,
) -> None:
    """Manifest shape, selected target profile and file inode are independent pins."""
    state = dict(isolated_target.state)
    change(state)
    _rewrite_target_manifest(isolated_target, state)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == code


def test_target_manifest_rejects_manifest_identity_that_differs_from_directory(
    isolated_target: sample.SampleTarget,
) -> None:
    """The manifest target id must remain bound to its generated directory name."""
    state = dict(isolated_target.state)
    state["target_id"] = "f" * 32
    _rewrite_target_manifest(isolated_target, state)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_manifest_identity_mismatch"


def test_target_manifest_rejects_insecure_mode_and_symlink(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """The target state stays private and no-follow even after replacement."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest.chmod(0o640)

    with pytest.raises(sample.Csi500SampleCompositionError) as mode_error:
        sample.load_sample_target(isolated_target.directory)

    assert mode_error.value.code == "target_manifest_permissions_invalid"
    manifest.chmod(0o600)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(manifest.read_bytes())
    external.chmod(0o600)
    manifest.unlink()
    manifest.symlink_to(external)

    with pytest.raises(sample.Csi500SampleCompositionError) as symlink_error:
        sample.load_sample_target(isolated_target.directory)

    assert symlink_error.value.code == "target_manifest_unavailable"


def test_target_manifest_reader_bounds_bytes_and_rejects_unexpected_files(
    isolated_target: sample.SampleTarget,
) -> None:
    """Corruptly large state and untracked target files cannot be accepted."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest.write_bytes(b" " * (sample._MAX_STATE_BYTES + 1))
    manifest.chmod(0o600)

    with pytest.raises(sample.Csi500SampleCompositionError) as size_error:
        sample.load_sample_target(isolated_target.directory)

    assert size_error.value.code == "target_manifest_unavailable"
    _rewrite_target_manifest(isolated_target, dict(isolated_target.state))
    (isolated_target.directory / "untracked.tmp").write_text("unexpected", encoding="ascii")

    with pytest.raises(sample.Csi500SampleCompositionError) as file_error:
        sample.load_sample_target(isolated_target.directory)

    assert file_error.value.code == "target_contains_unexpected_files"
