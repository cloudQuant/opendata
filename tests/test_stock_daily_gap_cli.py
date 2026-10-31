"""Behavioral tests for the explicit-path offline stock-daily gap CLI."""

from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from opendata.data.instrument_reference import (
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
from scripts.ops import reconcile_stock_daily_history as cli

_DATASET = "fixture.stock.daily"
_SOURCE = "fixture.provider.daily"
_INSTRUMENT = "FIXTURE:600519"
_START = date(2024, 2, 5)
_END = date(2024, 2, 9)
_SESSIONS = tuple(date.fromisoformat(f"2024-02-{day:02}") for day in range(5, 10))


def _json_bytes(value: object) -> bytes:
    """Encode deterministic fixture JSON independently from the CLI serializer."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _valid_manifest_pin_value() -> dict[str, Any]:
    """Return the minimal complete manifest shape consumed by the pin parser."""
    registry_ref = "fixture:registry:v1"
    return {
        "schema_version": 1,
        "registry_ref": registry_ref,
        "artifacts": [
            {
                "evidence_ref": registry_ref,
                "kind": "instrument_registry",
                "sha256": "0" * 64,
            }
        ],
        "series": [{}],
    }


def _artifact(
    reference: str,
    *,
    kind: str,
    payload: dict[str, object] | None = None,
) -> bytes:
    """Build one synthetic schema-v1 evidence artifact with stable identity."""
    return _json_bytes(
        {
            "evidence_ref": reference,
            "schema_version": 1,
            "dataset_id": _DATASET,
            "source_id": _SOURCE,
            "instrument_id": _INSTRUMENT,
            "publication_id": "fixture-publication-v1" if kind == "daily_publication" else None,
            "payload": payload or {},
        }
    )


def _bundle_files(root: Path) -> tuple[Path, dict[str, Path]]:
    """Write a fully synthetic hash-pinned bundle beneath a fresh test root."""
    registry = InstrumentReferenceRegistry(
        registry_version="fixture-registry-v1",
        evidence_ref="fixture:registry:v1",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id=_DATASET,
                source_id=_SOURCE,
                instrument_id=_INSTRUMENT,
                lifecycle_version="fixture-lifecycle-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=_END,
                status_evidence_ref="fixture:status:v1",
                known_start_date=_START,
                start_boundary_unknown=False,
                start_evidence_ref="fixture:start:v1",
            ),
        ),
    )
    registry_bytes = encode_instrument_reference_snapshot(registry)
    sessions = [item.isoformat() for item in _SESSIONS]
    publication_content_ref = "fixture:publication-content:v1"
    publication_content = _json_bytes(
        {
            "rows": [
                {"trade_date": item, "close": 100.0 + index} for index, item in enumerate(sessions)
            ]
        }
    )
    raw_artifacts: dict[str, tuple[str, bytes]] = {
        registry.evidence_ref: ("instrument_registry", registry_bytes),
        "fixture:status:v1": (
            "instrument_status",
            _artifact(
                "fixture:status:v1",
                kind="instrument_status",
                payload={
                    "registry_version": registry.registry_version,
                    "lifecycle_version": "fixture-lifecycle-v1",
                    "status": "active",
                    "status_as_of": _END.isoformat(),
                    "status_evidence_ref": "fixture:status:v1",
                    "known_start_date": _START.isoformat(),
                    "start_boundary_unknown": False,
                    "start_evidence_ref": "fixture:start:v1",
                    "delisted_on": None,
                    "delist_evidence_ref": None,
                },
            ),
        ),
        "fixture:start:v1": (
            "instrument_start",
            _artifact(
                "fixture:start:v1",
                kind="instrument_start",
                payload={"start_date": _START.isoformat()},
            ),
        ),
        "fixture:calendar:v1": (
            "business_calendar",
            _artifact(
                "fixture:calendar:v1",
                kind="business_calendar",
                payload={
                    "calendar_id": "FIXTURE-CALENDAR",
                    "calendar_version": "fixture-calendar-v1",
                    "covered_from": _START.isoformat(),
                    "covered_through": _END.isoformat(),
                    "session_dates": sessions,
                },
            ),
        ),
        "fixture:expectation:v1": (
            "instrument_expectation",
            _artifact(
                "fixture:expectation:v1",
                kind="instrument_expectation",
                payload={
                    "calendar_ref": "fixture:calendar:v1",
                    "calendar_id": "FIXTURE-CALENDAR",
                    "calendar_version": "fixture-calendar-v1",
                    "covered_from": _START.isoformat(),
                    "covered_through": _END.isoformat(),
                    "expected_business_dates": sessions,
                    "unresolved_observation_dates": [],
                },
            ),
        ),
        "fixture:publication:v1": (
            "daily_publication",
            _artifact(
                "fixture:publication:v1",
                kind="daily_publication",
                payload={"content_ref": publication_content_ref},
            ),
        ),
        publication_content_ref: ("daily_publication_content", publication_content),
    }
    artifacts_directory = root / "artifacts"
    artifacts_directory.mkdir(parents=True)
    descriptors: list[dict[str, str]] = []
    paths: dict[str, Path] = {}
    for reference, (kind, content) in sorted(raw_artifacts.items()):
        digest = hashlib.sha256(content).hexdigest()
        artifact_path = artifacts_directory / f"{digest}.json"
        artifact_path.write_bytes(content)
        paths[reference] = artifact_path
        descriptors.append({"evidence_ref": reference, "kind": kind, "sha256": digest})

    manifest = {
        "schema_version": 1,
        "registry_ref": registry.evidence_ref,
        "artifacts": descriptors,
        "series": [
            {
                "dataset_id": _DATASET,
                "source_id": _SOURCE,
                "instrument_identifier": _INSTRUMENT,
                "identity_as_of": None,
                "request_start": _START.isoformat(),
                "request_end": _END.isoformat(),
                "publication_id": "fixture-publication-v1",
                "publication_ref": "fixture:publication:v1",
                "calendar_ref": "fixture:calendar:v1",
                "expectation_ref": "fixture:expectation:v1",
                "lifecycle_ref": "fixture:status:v1",
                "retention_ref": None,
                "delisted_end_ref": None,
            },
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest_path, paths


def _run(root: Path) -> tuple[int, str]:
    """Invoke the CLI entry point and capture its sole stdout document."""
    output = io.StringIO()
    with redirect_stdout(output):
        result = cli.main(["--evidence-dir", str(root)])
    return result, output.getvalue()


def test_valid_bundle_emits_same_canonical_json_and_never_claims_all_a(
    tmp_path: Path,
) -> None:
    """A complete synthetic single security stays explicitly short of an A-share census."""
    manifest_path, paths = _bundle_files(tmp_path)
    before = sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))

    first_status, first_output = _run(tmp_path)
    second_status, second_output = _run(tmp_path)

    parsed = json.loads(first_output)
    assert first_status == second_status == 0
    assert first_output == second_output
    assert parsed["schema_version"] == 1
    assert parsed["scope"]["all_a_universe_complete"] is False
    assert parsed["scope"]["universe_census_attested"] is False
    assert parsed["scope"]["dispatch_authorized"] is False
    assert parsed["scope"]["bars_created"] is False
    assert (
        parsed["scope"]["publication_content_identity_binding"]
        == "caller_asserted_by_envelope_content_ref"
    )
    publication_content = paths["fixture:publication-content:v1"].read_bytes()
    content_pin = next(
        item
        for item in parsed["artifacts"]
        if item["evidence_ref"] == "fixture:publication-content:v1"
    )
    assert content_pin["sha256"] == hashlib.sha256(publication_content).hexdigest()
    assert content_pin["hash_semantics"] == "exact_input_publication_content_artifact_bytes"
    assert content_pin["identity_binding"] == "caller_asserted_by_publication_envelope_content_ref"
    assert content_pin["authority_authenticated"] is False
    assert (
        parsed["scope"]["publication_hash_semantics"]
        == "exact_input_publication_content_artifact_bytes"
    )
    assert manifest_path.name == "manifest.json"
    assert before == sorted(str(item.relative_to(tmp_path)) for item in tmp_path.rglob("*"))


def test_script_entrypoint_bootstraps_project_root_and_runs_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The executable script can import the project and emit its stable report."""
    _bundle_files(tmp_path)
    script_path = cli.PROJECT_ROOT / "scripts" / "ops" / "reconcile_stock_daily_history.py"
    project_root = str(cli.PROJECT_ROOT)
    monkeypatch.setattr(sys, "path", [item for item in sys.path if item != project_root])
    monkeypatch.setattr(sys, "argv", [str(script_path), "--evidence-dir", str(tmp_path)])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script_path), run_name="__main__")

    assert exit_info.value.code == 0
    assert project_root in sys.path
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["scope"]["all_a_universe_complete"] is False


def test_publication_content_ref_reuse_across_series_is_blocked(tmp_path: Path) -> None:
    """Different refs cannot bind the same raw row bytes to multiple publications."""
    manifest_path, paths = _bundle_files(tmp_path)
    manifest = json.loads(manifest_path.read_bytes())
    publication_ref = "fixture:publication:second:v1"
    publication_id = "fixture-publication-second-v1"
    content_ref = "fixture:publication-content:second:v1"
    content_bytes = paths["fixture:publication-content:v1"].read_bytes()
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    second_series = {
        **manifest["series"][0],
        "dataset_id": "fixture.other.stock.daily",
        "instrument_identifier": "FIXTURE:000001",
        "publication_id": publication_id,
        "publication_ref": publication_ref,
    }
    publication_artifact = _json_bytes(
        {
            "evidence_ref": publication_ref,
            "schema_version": 1,
            "dataset_id": second_series["dataset_id"],
            "source_id": _SOURCE,
            "instrument_id": second_series["instrument_identifier"],
            "publication_id": publication_id,
            "payload": {"content_ref": content_ref},
        }
    )
    publication_sha256 = hashlib.sha256(publication_artifact).hexdigest()
    (tmp_path / "artifacts" / f"{publication_sha256}.json").write_bytes(publication_artifact)
    manifest["artifacts"].append(
        {
            "evidence_ref": publication_ref,
            "kind": "daily_publication",
            "sha256": publication_sha256,
        }
    )
    manifest["artifacts"].append(
        {
            "evidence_ref": content_ref,
            "kind": "daily_publication_content",
            "sha256": content_sha256,
        }
    )
    manifest["series"].append(second_series)
    manifest_path.write_bytes(_json_bytes(manifest))

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output) == {
        "error": {"code": "invalid_evidence_bundle"},
        "schema_version": 1,
        "status": "blocked",
    }


def test_symlink_artifact_is_rejected_without_reading_outside_target(tmp_path: Path) -> None:
    """Hash-shaped names do not permit following an artifact symlink out of the root."""
    _, paths = _bundle_files(tmp_path)
    victim = paths["fixture:calendar:v1"]
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_bytes(victim.read_bytes())
    victim.unlink()
    try:
        victim.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        outside.unlink(missing_ok=True)
        pytest.skip(f"symlinks are unavailable on this filesystem: {type(exc).__name__}")

    try:
        status, output = _run(tmp_path)

        assert status == 2
        assert json.loads(output) == {
            "error": {"code": "evidence_unavailable_or_unsafe"},
            "schema_version": 1,
            "status": "blocked",
        }
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_symlink_artifact_directory_is_rejected(tmp_path: Path) -> None:
    """The fixed artifacts directory cannot redirect reads outside the evidence root."""
    _bundle_files(tmp_path)
    artifact_directory = tmp_path / "artifacts"
    outside = tmp_path.parent / f"{tmp_path.name}-artifacts"
    artifact_directory.rename(outside)
    try:
        artifact_directory.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        outside.rename(artifact_directory)
        pytest.skip(f"symlinks are unavailable on this filesystem: {type(exc).__name__}")

    try:
        status, output = _run(tmp_path)

        assert status == 2
        assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"
    finally:
        artifact_directory.unlink(missing_ok=True)
        outside.rename(artifact_directory)


def test_symlink_in_evidence_directory_path_is_rejected(tmp_path: Path) -> None:
    """The root itself and its ancestor components are opened without following links."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    _bundle_files(real_root)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable on this filesystem: {type(exc).__name__}")

    status, output = _run(alias)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_parent_component_in_evidence_directory_is_rejected_before_open(tmp_path: Path) -> None:
    """The root walk rejects `..` before touching any directory component."""
    status, output = _run(Path(f"{tmp_path}/../not-an-evidence-root"))

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_missing_manifest_is_reported_as_unsafe_input(tmp_path: Path) -> None:
    """An OS-level missing-file error is converted to the stable blocked result."""
    manifest_path, _ = _bundle_files(tmp_path)
    manifest_path.unlink()

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output) == {
        "error": {"code": "evidence_unavailable_or_unsafe"},
        "schema_version": 1,
        "status": "blocked",
    }


def test_unsafe_fixed_filename_is_rejected_before_open(tmp_path: Path) -> None:
    """The file reader accepts only basenames before calling openat."""
    try:
        directory_fd = cli._open_absolute_directory(str(tmp_path))
    except cli._EvidenceInputError as exc:
        pytest.skip(str(exc))
    try:
        with pytest.raises(cli._EvidenceInputError, match="unsafe evidence filename"):
            cli._read_regular_file_at(directory_fd, "../manifest.json", limit=32)
    finally:
        os.close(directory_fd)


def test_file_growth_after_initial_stat_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that exceeds the initial byte limit is rejected even after fstat."""
    artifact = tmp_path / "growing.json"
    artifact.write_bytes(b"")
    real_read = cli.os.read
    wrote_bytes = False

    def grow_before_read(file_fd: int, size: int) -> bytes:
        nonlocal wrote_bytes
        if not wrote_bytes:
            artifact.write_bytes(b"x")
            wrote_bytes = True
        return real_read(file_fd, size)

    monkeypatch.setattr(cli.os, "read", grow_before_read)
    try:
        directory_fd = cli._open_absolute_directory(str(tmp_path))
    except cli._EvidenceInputError as exc:
        pytest.skip(str(exc))
    try:
        with pytest.raises(cli._EvidenceInputError, match="exceeds its byte limit"):
            cli._read_regular_file_at(directory_fd, artifact.name, limit=0)
    finally:
        os.close(directory_fd)


def test_file_changed_during_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appending while a file is being read fails the before/after metadata check."""
    artifact = tmp_path / "changing.json"
    artifact.write_bytes(b"x")
    real_read = cli.os.read
    appended = False

    def append_after_first_read(file_fd: int, size: int) -> bytes:
        nonlocal appended
        chunk = real_read(file_fd, size)
        if chunk and not appended:
            with artifact.open("ab") as stream:
                stream.write(b"y")
            appended = True
        return chunk

    monkeypatch.setattr(cli.os, "read", append_after_first_read)
    try:
        directory_fd = cli._open_absolute_directory(str(tmp_path))
    except cli._EvidenceInputError as exc:
        pytest.skip(str(exc))
    try:
        with pytest.raises(cli._EvidenceInputError, match="changed while it was being read"):
            cli._read_regular_file_at(directory_fd, artifact.name, limit=8)
    finally:
        os.close(directory_fd)


def test_relative_evidence_directory_is_blocked_with_stable_json() -> None:
    """A caller must pin a concrete absolute evidence root."""
    first_status, first_output = _run(Path("relative-evidence"))
    second_status, second_output = _run(Path("relative-evidence"))

    assert first_status == second_status == 2
    assert first_output == second_output
    assert json.loads(first_output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_invalid_cli_arguments_emit_json_without_usage_text() -> None:
    """Argument failures keep the output contract stable and machine-readable."""
    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main(["--unexpected"])

    assert status == 2
    assert json.loads(output.getvalue()) == {
        "error": {"code": "invalid_arguments"},
        "schema_version": 1,
        "status": "blocked",
    }


def test_manifest_duplicate_keys_are_blocked_before_artifact_directory_read(
    tmp_path: Path,
) -> None:
    """Duplicate manifest fields cannot use parser last-wins behavior."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "manifest.json").write_bytes(b'{"schema_version":1,"artifacts":[],"artifacts":[]}')

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_pinned_bytes_with_a_stale_digest_are_not_reconciled(tmp_path: Path) -> None:
    """The CLI passes exact bytes to the canonical loader, which checks each pin."""
    _, paths = _bundle_files(tmp_path)
    calendar = paths["fixture:calendar:v1"]
    calendar.write_bytes(calendar.read_bytes() + b" ")

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "invalid_evidence_bundle"


def test_file_size_limit_blocks_before_loader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit lower bound demonstrates that oversized artifacts stop at read time."""
    _, paths = _bundle_files(tmp_path)
    monkeypatch.setattr(cli, "MAX_ARTIFACT_BYTES", 8)
    calendar = paths["fixture:calendar:v1"]
    assert calendar.stat().st_size > cli.MAX_ARTIFACT_BYTES

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_manifest_byte_limit_blocks_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manifest byte bound is applied before parsing its contents."""
    _bundle_files(tmp_path)
    monkeypatch.setattr(cli, "MAX_MANIFEST_BYTES", 8)

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_registry_snapshot_uses_its_separate_size_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The canonical registry snapshot receives the codec's independent limit."""
    _, paths = _bundle_files(tmp_path)
    monkeypatch.setattr(cli, "MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES", 8)
    assert paths["fixture:registry:v1"].stat().st_size > 8

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_total_artifact_limit_is_applied_across_all_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Several individually small files cannot exceed the aggregate byte cap."""
    _bundle_files(tmp_path)
    monkeypatch.setattr(cli, "MAX_TOTAL_ARTIFACT_BYTES", 8)

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_artifact_count_limit_blocks_before_opening_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The descriptor count is bounded before any hash-named artifact is opened."""
    _bundle_files(tmp_path)
    monkeypatch.setattr(cli, "MAX_ARTIFACTS", 0)

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


@pytest.mark.parametrize("missing_flag", ["_NOFOLLOW_FLAG", "_NONBLOCK_FLAG", "_DIRECTORY_FLAG"])
def test_platform_without_safe_open_support_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_flag: str,
) -> None:
    """The root is not read when a required no-link or nonblocking flag is missing."""
    _bundle_files(tmp_path)
    monkeypatch.setattr(cli, missing_flag, None)

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_hard_link_artifact_is_rejected(tmp_path: Path) -> None:
    """The CLI rejects an artifact whose inode has another filesystem name."""
    _, paths = _bundle_files(tmp_path)
    artifact = paths["fixture:calendar:v1"]
    alias = tmp_path.parent / f"{tmp_path.name}-calendar-link.json"
    try:
        os.link(artifact, alias)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {type(exc).__name__}")

    try:
        status, output = _run(tmp_path)

        assert status == 2
        assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"
    finally:
        alias.unlink(missing_ok=True)


def test_fifo_artifact_without_writer_is_rejected_without_blocking(tmp_path: Path) -> None:
    """A FIFO is opened nonblocking and rejected before any writer is present."""
    _, paths = _bundle_files(tmp_path)
    artifact = paths["fixture:calendar:v1"]
    artifact.unlink()
    try:
        os.mkfifo(artifact)
    except (AttributeError, OSError) as exc:
        pytest.skip(f"named pipes are unavailable on this filesystem: {type(exc).__name__}")

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_non_regular_artifact_entry_is_rejected(tmp_path: Path) -> None:
    """A directory cannot masquerade as a pinned JSON artifact."""
    _, paths = _bundle_files(tmp_path)
    artifact = paths["fixture:calendar:v1"]
    artifact.unlink()
    artifact.mkdir()

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "evidence_unavailable_or_unsafe"


def test_invalid_registry_snapshot_is_reported_without_detail_leakage(tmp_path: Path) -> None:
    """A matching outer hash cannot make a malformed inner registry snapshot valid."""
    manifest_path, paths = _bundle_files(tmp_path)
    registry_path = paths["fixture:registry:v1"]
    registry_path.write_bytes(b"{}")
    manifest_value: dict[str, Any] = json.loads(manifest_path.read_bytes())
    old_descriptor = next(
        row for row in manifest_value["artifacts"] if row["kind"] == "instrument_registry"
    )
    old_digest = old_descriptor["sha256"]
    new_digest = hashlib.sha256(b"{}").hexdigest()
    new_path = tmp_path / "artifacts" / f"{new_digest}.json"
    registry_path.rename(new_path)
    old_descriptor["sha256"] = new_digest
    manifest_path.write_bytes(_json_bytes(manifest_value))

    status, output = _run(tmp_path)

    assert status == 2
    assert json.loads(output)["error"]["code"] == "invalid_evidence_bundle"
    assert str(tmp_path) not in output
    assert old_digest != new_digest


def test_manifest_pin_parser_rejects_unsafe_digest_before_path_construction(
    tmp_path: Path,
) -> None:
    """Evidence refs are logical keys and never become filesystem path components."""
    manifest_value = _valid_manifest_pin_value()
    unsafe_descriptor = manifest_value["artifacts"][0]
    unsafe_descriptor["evidence_ref"] = "../../.env"
    unsafe_descriptor["sha256"] = "../x"
    manifest = _json_bytes(manifest_value)

    with pytest.raises(cli._EvidenceInputError, match="lowercase SHA-256"):
        cli._manifest_artifact_pins(manifest)


def test_manifest_pin_parser_rejects_non_object_and_nonstandard_json() -> None:
    """The small pin parser rejects invalid roots and non-standard JSON constants."""
    with pytest.raises(cli._EvidenceInputError, match="root must be an object"):
        cli._manifest_artifact_pins(_json_bytes([]))
    with pytest.raises(cli._EvidenceInputError, match="strict UTF-8 JSON"):
        cli._manifest_artifact_pins(b'{"schema_version":NaN}')


def test_manifest_pin_parser_rejects_root_metadata_boundaries() -> None:
    """Required root keys, exact schema version, and registry ref are checked early."""
    missing_registry_key = _valid_manifest_pin_value()
    del missing_registry_key["registry_ref"]
    with pytest.raises(cli._EvidenceInputError, match="missing or unsupported fields"):
        cli._manifest_artifact_pins(_json_bytes(missing_registry_key))

    boolean_version = _valid_manifest_pin_value()
    boolean_version["schema_version"] = True
    with pytest.raises(cli._EvidenceInputError, match="schema_version must be integer 1"):
        cli._manifest_artifact_pins(_json_bytes(boolean_version))

    empty_registry_ref = _valid_manifest_pin_value()
    empty_registry_ref["registry_ref"] = ""
    with pytest.raises(cli._EvidenceInputError, match="registry_ref is invalid"):
        cli._manifest_artifact_pins(_json_bytes(empty_registry_ref))


def test_manifest_pin_parser_rejects_empty_series_and_bad_descriptors() -> None:
    """Series presence and each descriptor's ref/kind shape are validated."""
    no_series = _valid_manifest_pin_value()
    no_series["series"] = []
    with pytest.raises(cli._EvidenceInputError, match="series list"):
        cli._manifest_artifact_pins(_json_bytes(no_series))

    malformed_descriptor = _valid_manifest_pin_value()
    malformed_descriptor["artifacts"] = [None]
    with pytest.raises(cli._EvidenceInputError, match="descriptor has an invalid shape"):
        cli._manifest_artifact_pins(_json_bytes(malformed_descriptor))

    duplicate_refs = _valid_manifest_pin_value()
    duplicate_refs["artifacts"] = duplicate_refs["artifacts"] * 2
    with pytest.raises(cli._EvidenceInputError, match="references must be unique"):
        cli._manifest_artifact_pins(_json_bytes(duplicate_refs))

    invalid_kind = _valid_manifest_pin_value()
    invalid_kind["artifacts"][0]["kind"] = ""
    with pytest.raises(cli._EvidenceInputError, match="artifact kind is invalid"):
        cli._manifest_artifact_pins(_json_bytes(invalid_kind))

    missing_registry_pin = _valid_manifest_pin_value()
    missing_registry_pin["artifacts"] = [
        {"evidence_ref": "fixture:other:v1", "kind": "business_calendar", "sha256": "0" * 64}
    ]
    with pytest.raises(cli._EvidenceInputError, match="exactly one registry snapshot"):
        cli._manifest_artifact_pins(_json_bytes(missing_registry_pin))
