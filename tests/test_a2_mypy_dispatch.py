"""Mock-only coverage for A2 mypy target isolation and result aggregation."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from scripts.quality import a2_check


def _install_fake_mypy(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[tuple[int, str, str]],
) -> list[list[str]]:
    """Replace process execution and retain the argv sent for each target."""
    calls: list[list[str]] = []
    pending = list(replies)

    def fake_run(args: list[str]) -> tuple[int, str, str]:
        calls.append(list(args))
        if not pending:
            pytest.fail("mypy runner received more targets than configured replies")
        return pending.pop(0)

    monkeypatch.setattr(a2_check, "_run_streams", fake_run)
    return calls


class TestMypyTargetDispatch:
    """Keep every unique typed path visible to a single-target mypy process."""

    def test_same_basename_paths_are_each_separate_mypy_targets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Different directory-local probe modules never share one mypy build."""
        targets = ["docs/a/probe.py", "docs/b/probe.py"]
        calls = _install_fake_mypy(monkeypatch, [(0, "", ""), (0, "", "")])

        passed, results = a2_check._run_mypy_targets(targets)

        assert passed is True
        assert [result.path for result in results] == sorted(targets)
        assert calls == [
            [
                sys.executable,
                "-m",
                "mypy",
                "--no-color-output",
                "--no-error-summary",
                "--follow-imports=silent",
                path,
            ]
            for path in sorted(targets)
        ]

    def test_target_order_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Caller order does not change the order of per-path mypy invocations."""
        targets = ["z/last.py", "b/probe.py", "a/probe.py"]
        calls = _install_fake_mypy(monkeypatch, [(0, "", "")] * len(targets))

        passed, results = a2_check._run_mypy_targets(targets)

        assert passed is True
        assert [result.path for result in results] == sorted(targets)
        assert [argv[-1] for argv in calls] == sorted(targets)

    def test_exact_duplicate_path_runs_once_without_dropping_other_targets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated CLI path strings are deduplicated while unique paths remain."""
        targets = ["docs/a/probe.py", "docs/a/probe.py", "docs/b/probe.py"]
        calls = _install_fake_mypy(monkeypatch, [(0, "", ""), (0, "", "")])

        passed, results = a2_check._run_mypy_targets(targets)

        assert passed is True
        assert len(results) == 2
        assert [result.path for result in results] == ["docs/a/probe.py", "docs/b/probe.py"]
        assert [argv[-1] for argv in calls] == ["docs/a/probe.py", "docs/b/probe.py"]

    def test_exit_two_fails_and_remaining_targets_still_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mypy invocation error remains red and does not abort the target loop."""
        targets = ["a/probe.py", "b/probe.py", "c/module.py"]
        calls = _install_fake_mypy(
            monkeypatch,
            [(2, "first stdout\n", "first stderr\n"), (0, "", ""), (0, "", "")],
        )

        passed, results = a2_check._run_mypy_targets(targets)

        assert passed is False
        assert [argv[-1] for argv in calls] == sorted(targets)
        assert [result.returncode for result in results] == [2, 0, 0]
        assert results[0].stdout == "first stdout\n"
        assert results[0].stderr == "first stderr\n"

    def test_each_stream_and_target_status_are_rendered(self) -> None:
        """Reports retain per-target exit codes and separate captured streams."""
        results = [
            a2_check._MypyTargetResult(
                "docs/a/probe.py", 2, "diagnostic out\n", "invocation err\n"
            ),
            a2_check._MypyTargetResult("docs/b/probe.py", 0, "", ""),
        ]

        report = a2_check._mypy_results_text(results)

        assert "docs/a/probe.py: exit=2 stdout_chars=15 stderr_chars=15" in report
        assert "    stdout:\ndiagnostic out\n" in report
        assert "    stderr:\ninvocation err\n" in report
        assert "docs/b/probe.py: exit=0 stdout_chars=0 stderr_chars=0" in report

    def test_empty_target_list_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A direct empty helper call starts no mypy process."""
        calls = _install_fake_mypy(monkeypatch, [])

        passed, results = a2_check._run_mypy_targets([])

        assert passed is True
        assert results == []
        assert calls == []

    def test_gate_prints_unique_target_count_and_each_target_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate output names all mypy targets and their actual return codes."""
        targets = ["scripts/quality/a2_check.py", "tests/test_a2_mypy_dispatch.py"]
        validation = SimpleNamespace(
            blocked=False,
            errors=(),
            historical_ruff_count=0,
            blocked_details=(),
        )
        secret_scan = SimpleNamespace(ok=True, scanned_paths=(), errors=())
        monkeypatch.setattr(a2_check, "resolve_files", lambda _explicit: targets)
        monkeypatch.setattr(a2_check, "validate_manifest", lambda _root: validation)
        monkeypatch.setattr(
            a2_check,
            "partition_static_files",
            lambda files, _validation: (files, []),
        )
        monkeypatch.setattr(a2_check, "scan_snapshot_bytes", lambda _root, _evidence: secret_scan)
        monkeypatch.setattr(a2_check, "_run", lambda _args: (True, ""))
        monkeypatch.setattr(a2_check, "_bandit", lambda _files: (True, ""))
        calls = _install_fake_mypy(monkeypatch, [(0, "", "")])

        result = a2_check.run(targets)

        captured = capsys.readouterr()
        assert result == 0
        assert "ok  mypy (1 unique target(s))" in captured.out
        assert "scripts/quality/a2_check.py: exit=0 stdout_chars=0 stderr_chars=0" in captured.out
        assert [argv[-1] for argv in calls] == ["scripts/quality/a2_check.py"]
