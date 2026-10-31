"""Offline filesystem safety boundaries for CSI 500 sample targets."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from opendata.pipeline import ths_csi500_sample_composition as sample


@pytest.fixture
def isolated_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sample.SampleTarget:
    """Keep every synthetic target below pytest's private root."""
    monkeypatch.setattr(sample, "SAMPLE_TEMP_ROOT", tmp_path)
    return sample.create_sample_target()


@pytest.mark.parametrize(
    "directory_value",
    ["", Path("relative-target")],
)
def test_loader_rejects_noncanonical_directory_arguments(
    directory_value: str | Path,
) -> None:
    """A caller must provide an explicit absolute path without traversal."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(directory_value)

    assert error.value.code == "target_directory_invalid"


def test_loader_rejects_parent_traversal_before_resolving_it(tmp_path: Path) -> None:
    """A traversal path is refused before filesystem resolution reaches its parent."""
    traversal = tmp_path / ".." / "outside-target"

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(traversal)

    assert error.value.code == "target_directory_invalid"


def test_loader_rejects_a_symlink_alias_to_an_owned_target(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """A link name cannot stand in for the exact program-created directory."""
    alias = tmp_path / "opendata-csi500-sample-alias"
    alias.symlink_to(isolated_target.directory, target_is_directory=True)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(alias)

    assert error.value.code == "target_directory_identity_invalid"
    assert isolated_target.directory.is_dir()


def test_loader_rejects_a_non_candidate_directory_name(
    tmp_path: Path,
) -> None:
    """An unrelated directory under the fixture root is never treated as a target."""
    unrelated = tmp_path / "not-a-candidate"
    unrelated.mkdir()

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(unrelated)

    assert error.value.code == "target_directory_identity_invalid"
    assert unrelated.is_dir()


def test_loader_requires_private_directory_permissions(
    isolated_target: sample.SampleTarget,
) -> None:
    """Changing the target directory from mode 0700 invalidates its identity."""
    isolated_target.directory.chmod(0o755)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_directory_identity_invalid"


def test_loader_rejects_database_symlink_without_reading_its_referent(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """Database identity is checked with lstat before any content is followed."""
    sentinel = tmp_path / "keep.txt"
    sentinel.write_bytes(b"leave this sibling untouched")
    isolated_target.database_path.unlink()
    isolated_target.database_path.symlink_to(sentinel)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_database_identity_invalid"
    assert sentinel.read_bytes() == b"leave this sibling untouched"


def test_loader_rejects_manifest_symlink(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """The no-follow manifest reader does not accept a linked metadata file."""
    sentinel = tmp_path / "manifest-copy.json"
    sentinel.write_bytes(b"synthetic fixture bytes")
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest.unlink()
    manifest.symlink_to(sentinel)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_manifest_unavailable"
    assert sentinel.read_bytes() == b"synthetic fixture bytes"


def test_loader_rejects_duplicate_manifest_keys(
    isolated_target: sample.SampleTarget,
) -> None:
    """Duplicate JSON keys cannot shadow the identity recorded in the manifest."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_manifest_invalid"


def test_loader_rejects_unexpected_directory_entries_without_removing_them(
    isolated_target: sample.SampleTarget,
) -> None:
    """Unrecognized files block use while remaining available for inspection."""
    extra = isolated_target.directory / "operator-note.txt"
    extra.write_text("synthetic unexpected entry", encoding="utf-8")
    database_before = isolated_target.database_path.read_bytes()
    manifest_path = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_contains_unexpected_files"
    assert extra.read_text(encoding="utf-8") == "synthetic unexpected entry"
    assert isolated_target.database_path.read_bytes() == database_before
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize("operation", ["", "invalid operation"])
def test_lock_rejects_invalid_operation_without_creating_a_lock(
    isolated_target: sample.SampleTarget,
    operation: str,
) -> None:
    """Operation names are bounded before a target lock is created."""
    with (
        pytest.raises(sample.Csi500SampleCompositionError) as error,
        sample.lock_sample_target(
            isolated_target,
            operation=operation,
        ),
    ):
        pytest.fail("invalid operation acquired the target lock")

    assert error.value.code == "operation_invalid"
    assert not (isolated_target.directory / sample.SAMPLE_LOCK_FILENAME).exists()


def test_lock_rejects_a_stale_target_snapshot(
    isolated_target: sample.SampleTarget,
) -> None:
    """A manifest phase change invalidates a previously loaded target handle."""
    sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )

    with (
        pytest.raises(sample.Csi500SampleCompositionError) as error,
        sample.lock_sample_target(
            isolated_target,
            operation="migration",
        ),
    ):
        pytest.fail("stale target acquired the operation lock")

    assert error.value.code == "target_changed_before_lock"
    assert not (isolated_target.directory / sample.SAMPLE_LOCK_FILENAME).exists()


def test_lock_refuses_a_symlink_and_preserves_its_referent(
    isolated_target: sample.SampleTarget,
    tmp_path: Path,
) -> None:
    """O_NOFOLLOW prevents a lock path from redirecting outside the target."""
    sentinel = tmp_path / "keep-lock-referent.txt"
    sentinel.write_bytes(b"preserve")
    lock_path = isolated_target.directory / sample.SAMPLE_LOCK_FILENAME
    lock_path.symlink_to(sentinel)

    with (
        pytest.raises(sample.Csi500SampleCompositionError) as error,
        sample.lock_sample_target(
            isolated_target,
            operation="intent-preparation",
        ),
    ):
        pytest.fail("symlink lock was accepted")

    assert error.value.code == "target_locked_or_lock_unavailable"
    assert lock_path.is_symlink()
    assert sentinel.read_bytes() == b"preserve"


def test_operation_lock_must_match_the_requested_mutation(
    isolated_target: sample.SampleTarget,
) -> None:
    """A lock for one operation cannot authorize a different manifest write."""
    with sample.lock_sample_target(isolated_target, operation="intent-preparation"):
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            sample._require_target_operation_lock(
                isolated_target,
                operation="authorization",
            )

        assert error.value.code == "target_operation_lock_required"

    assert not (isolated_target.directory / sample.SAMPLE_LOCK_FILENAME).exists()


@pytest.mark.parametrize("new_phase", ["authorized", "not-a-phase"])
def test_transition_rejects_skipped_or_unknown_phases_without_rewriting_manifest(
    isolated_target: sample.SampleTarget,
    new_phase: str,
) -> None:
    """Only the explicit transition graph can advance the target state."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    before = manifest.read_bytes()

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.transition_sample_target(
            isolated_target,
            expected_phase="created",
            new_phase=new_phase,
        )

    assert error.value.code == "target_phase_transition_invalid"
    assert manifest.read_bytes() == before


def test_transition_rejects_stale_expected_phase_without_rewriting_manifest(
    isolated_target: sample.SampleTarget,
) -> None:
    """Concurrent progress cannot be overwritten using an older phase snapshot."""
    sample.transition_sample_target(
        isolated_target,
        expected_phase="created",
        new_phase="migration_started",
    )
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    before = manifest.read_bytes()

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.transition_sample_target(
            isolated_target,
            expected_phase="created",
            new_phase="migration_started",
        )

    assert error.value.code == "target_phase_conflict"
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    ("updates", "expected_code"),
    [
        ({"unrecognized-field": "fixture"}, "target_state_field_invalid"),
        ({"target_profile": "different-profile"}, "target_profile_fields_immutable"),
        ({"grant_id": "line-one\nline-two"}, "target_state_value_invalid"),
    ],
)
def test_transition_rejects_untrusted_manifest_updates_without_partial_write(
    isolated_target: sample.SampleTarget,
    updates: dict[str, object],
    expected_code: str,
) -> None:
    """Unknown, immutable, and control-character updates cannot enter the manifest."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    before = manifest.read_bytes()

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.transition_sample_target(
            isolated_target,
            expected_phase="created",
            new_phase="migration_started",
            updates=updates,
        )

    assert error.value.code == expected_code
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("invalid_code", ["invalid code", object()])
def test_error_class_replaces_untrusted_codes_with_a_fixed_message(
    invalid_code: object,
) -> None:
    """Unexpected error labels never become paths or free-form diagnostics."""
    error = sample.Csi500SampleCompositionError(invalid_code)  # type: ignore[arg-type]

    assert error.code == "sample_composition_failed"
    assert str(error) == "sample_composition_failed"


def test_target_repr_hides_directory_and_database_paths(
    isolated_target: sample.SampleTarget,
) -> None:
    """Routine diagnostics reveal a pinned identity without local filesystem paths."""
    rendered = repr(isolated_target)

    assert "identity=<pinned>" in rendered
    assert str(isolated_target.directory) not in rendered
    assert str(isolated_target.database_path) not in rendered


@pytest.mark.parametrize(
    "business_date",
    [datetime(2020, 1, 2), date(9999, 12, 31)],
)
def test_calendar_evidence_rejects_datetime_and_nonhistorical_dates(
    business_date: date,
) -> None:
    """A datetime or future day cannot be treated as reviewed historical evidence."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.Csi500TradingDayEvidence(
            business_date=business_date,
            calendar_version="fixture-calendar-v1",
            source_reference="fixture:calendar-evidence",
            reviewed_by="offline-reviewer",
            manifest_sha256="a" * 64,
            artifact_sha256="b" * 64,
        )

    assert error.value.code == "calendar_sample_date_invalid"


def test_calendar_evidence_repr_hides_reviewer_and_artifact_references() -> None:
    """Evidence repr reports only its date and pin state, not reference metadata."""
    evidence = sample.Csi500TradingDayEvidence(
        business_date=date(2000, 1, 3),
        calendar_version="fixture-calendar-v1",
        source_reference="fixture:calendar-private-reference",
        reviewed_by="offline-reviewer-marker",
        manifest_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    rendered = repr(evidence)

    assert "pinned=True" in rendered
    assert "fixture:calendar-private-reference" not in rendered
    assert "offline-reviewer-marker" not in rendered
    assert "a" * 64 not in rendered


def test_preview_rejects_datetime_before_loading_provider_modules() -> None:
    """The unauthenticated preview rejects a datetime at its input boundary."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.preview_csi500_sample_request(business_date=datetime(2026, 10, 30))

    assert error.value.code == "calendar_date_invalid"


def test_create_rejects_relative_temporary_root_before_filesystem_access() -> None:
    """A relative root cannot redirect target creation through the process cwd."""
    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._create_sample_target(Path("relative-temp-root"))

    assert error.value.code == "temporary_root_invalid"


def test_create_rejects_missing_temporary_root(tmp_path: Path) -> None:
    """A missing root produces a fixed unavailable result without creating a target."""
    missing_root = tmp_path / "missing-private-root"

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._create_sample_target(missing_root)

    assert error.value.code == "temporary_root_unavailable"
    assert not missing_root.exists()


def test_create_rejects_file_and_symlink_temporary_roots(tmp_path: Path) -> None:
    """Only an existing real directory can own temporary targets."""
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("fixture", encoding="utf-8")
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    for root in (file_root, linked_root):
        with pytest.raises(sample.Csi500SampleCompositionError) as error:
            sample._create_sample_target(root)
        assert error.value.code == "temporary_root_invalid"


def test_create_fails_closed_if_new_target_directory_mode_is_broadened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory created with broader access is rejected before a database file."""
    root = tmp_path / "private-root"
    root.mkdir()
    original_mkdir = os.mkdir

    def broaden_target_directory(
        path: str | bytes | Path,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        candidate = Path(path)
        if candidate.parent == root and candidate.name.startswith(sample.SAMPLE_TARGET_PREFIX):
            original_mkdir(path, 0o700, dir_fd=dir_fd)
            os.chmod(path, 0o755)
            return
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sample.os, "mkdir", broaden_target_directory)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._create_sample_target(root)

    assert error.value.code == "temporary_directory_permissions_invalid"
    created = tuple(root.glob(f"{sample.SAMPLE_TARGET_PREFIX}*"))
    assert len(created) == 1
    assert not (created[0] / sample.SAMPLE_DATABASE_FILENAME).exists()


def test_create_fails_closed_if_database_file_mode_is_too_restrictive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database file that misses the exact owner-only mode is never accepted."""
    root = tmp_path / "private-root"
    root.mkdir()
    original_open = os.open

    def restrict_target_database(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        candidate = Path(path)
        if candidate.name == sample.SAMPLE_DATABASE_FILENAME and candidate.parent.parent == root:
            os.fchmod(descriptor, 0o400)
        return descriptor

    monkeypatch.setattr(sample.os, "open", restrict_target_database)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._create_sample_target(root)

    assert error.value.code == "temporary_database_permissions_invalid"
    created = tuple(root.glob(f"{sample.SAMPLE_TARGET_PREFIX}*"))
    assert len(created) == 1
    database_path = created[0] / sample.SAMPLE_DATABASE_FILENAME
    assert database_path.is_file()
    assert database_path.stat().st_mode & 0o777 == 0o400


def test_loader_reports_missing_target_directory_without_fallback(tmp_path: Path) -> None:
    """An absent explicit directory does not fall back to a default target."""
    missing_target = tmp_path / "opendata-csi500-sample-00000000000000000000000000000000"

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(missing_target)

    assert error.value.code == "target_directory_unavailable"
    assert not missing_target.exists()


def test_loader_reports_missing_database_without_fallback(
    isolated_target: sample.SampleTarget,
) -> None:
    """The manifest cannot recreate a missing database file."""
    isolated_target.database_path.unlink()
    with pytest.raises(sample.Csi500SampleCompositionError) as missing_error:
        sample.load_sample_target(isolated_target.directory)
    assert missing_error.value.code == "target_database_missing"

    assert isolated_target.directory.is_dir()


def test_loader_rejects_database_with_broadened_permissions(
    isolated_target: sample.SampleTarget,
) -> None:
    """A matching inode still fails when its mode no longer matches the manifest contract."""
    isolated_target.database_path.chmod(0o640)

    with pytest.raises(sample.Csi500SampleCompositionError) as mode_error:
        sample.load_sample_target(isolated_target.directory)

    assert mode_error.value.code == "target_database_identity_invalid"


def test_loader_rejects_unknown_phase_in_an_otherwise_valid_manifest(
    isolated_target: sample.SampleTarget,
) -> None:
    """A valid identity cannot make an unsupported manifest phase loadable."""
    state = dict(isolated_target.state)
    state["phase"] = "unknown-phase"
    sample._write_manifest_atomically(isolated_target.directory, state)

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample.load_sample_target(isolated_target.directory)

    assert error.value.code == "target_phase_invalid"


def test_manifest_serializer_rejects_unknown_nonserializable_and_oversized_state(
    isolated_target: sample.SampleTarget,
) -> None:
    """Canonical manifest writes enforce the allowlist, JSON types, and byte cap."""
    unknown_state = dict(isolated_target.state)
    unknown_state["unrecognized-field"] = "fixture"
    with pytest.raises(sample.Csi500SampleCompositionError) as unknown_error:
        sample._manifest_bytes(unknown_state)
    assert unknown_error.value.code == "target_state_field_invalid"

    invalid_state = dict(isolated_target.state)
    invalid_state["grant_id"] = object()
    with pytest.raises(sample.Csi500SampleCompositionError) as value_error:
        sample._manifest_bytes(invalid_state)
    assert value_error.value.code == "target_state_value_invalid"

    oversized_state = dict(isolated_target.state)
    oversized_state["calendar_source_reference"] = "x" * sample._MAX_STATE_BYTES
    with pytest.raises(sample.Csi500SampleCompositionError) as size_error:
        sample._manifest_bytes(oversized_state)
    assert size_error.value.code == "target_manifest_too_large"


def test_atomic_manifest_write_refuses_a_changed_target_id_without_replacing_state(
    isolated_target: sample.SampleTarget,
) -> None:
    """A staged manifest for a different UUID is discarded before replace."""
    manifest = isolated_target.directory / sample.SAMPLE_MANIFEST_FILENAME
    original_bytes = manifest.read_bytes()
    state = dict(isolated_target.state)
    original_id = str(state["target_id"])
    state["target_id"] = ("0" if original_id[0] != "0" else "1") + original_id[1:]

    with pytest.raises(sample.Csi500SampleCompositionError) as error:
        sample._write_manifest_atomically(isolated_target.directory, state)

    assert error.value.code == "target_manifest_identity_changed"
    assert manifest.read_bytes() == original_bytes
    assert not tuple(isolated_target.directory.glob(".target-*.tmp"))


def test_lock_cleanup_failure_is_reported_and_lock_is_left_for_inspection(
    isolated_target: sample.SampleTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfirmed lock unlink is surfaced and the lock is not hidden."""
    lock_path = isolated_target.directory / sample.SAMPLE_LOCK_FILENAME
    original_unlink = Path.unlink

    def fail_lock_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == lock_path:
            raise OSError("synthetic unlink failure")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_lock_unlink)
        with (
            pytest.raises(sample.Csi500SampleCompositionError) as error,
            sample.lock_sample_target(isolated_target, operation="intent-preparation"),
        ):
            pass

    assert error.value.code == "target_lock_cleanup_uncertain"
    assert lock_path.is_file()
    lock_path.unlink()
