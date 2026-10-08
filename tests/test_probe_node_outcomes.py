"""Tests for exact node mapping from a batched pytest JUnit report."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from scripts.quality import acceptance_item_probe as probe

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


def _write_report(path: Path, cases: Sequence[str]) -> Path:
    """Write a minimal pytest-shaped JUnit report for parser tests."""
    path.write_text(
        "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _case(name: str, child: str = "") -> str:
    """Build one JUnit testcase with the shared file and class identity."""
    return (
        '<testcase classname="tests.test_probe_node_outcomes.TestProbeNodeOutcomes" '
        f'file="tests/test_probe_node_outcomes.py" name="{name}">{child}</testcase>'
    )


def test_junit_mapping_requires_exact_case_and_preserves_nonpassing_results(
    tmp_path: Path,
) -> None:
    """Exact cases pass; missing, skipped, failed, errored, and expanded cases do not."""
    nodes = (
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_pass",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_skip",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_fail",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_error",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_missing",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_wrong_location",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_expanded",
    )
    report = _write_report(
        tmp_path / "junit.xml",
        (
            _case("test_pass"),
            _case("test_skip", '<skipped message="skip"/>'),
            _case("test_fail", '<failure message="assertion"/>'),
            _case("test_error", '<error message="setup"/>'),
            '<testcase classname="tests.other_module.TestProbeNodeOutcomes" '
            'file="tests/other_module.py" name="test_wrong_location"/>',
            _case("test_expanded[one]"),
            _case("test_expanded[two]"),
        ),
    )

    assert probe._junit_node_outcomes(nodes, report, 0) == {
        nodes[0]: "passed",
        nodes[1]: "skipped",
        nodes[2]: "failed",
        nodes[3]: "error",
        nodes[4]: "missing",
        nodes[5]: "missing",
        nodes[6]: "ambiguous",
    }


def test_junit_duplicate_cases_and_parameter_siblings_are_ambiguous(tmp_path: Path) -> None:
    """Duplicate XML cases and widened parameter selection cannot pass by count."""
    duplicate = "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_duplicate"
    parameter = "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_parameter[one]"
    report = _write_report(
        tmp_path / "junit.xml",
        (
            _case("test_duplicate"),
            _case("test_duplicate"),
            _case("test_parameter[one]"),
            _case("test_parameter[two]"),
        ),
    )

    assert probe._junit_node_outcomes((duplicate, parameter), report, 0) == {
        duplicate: "ambiguous",
        parameter: "ambiguous",
    }
    parameter_two = "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_parameter[two]"
    assert probe._junit_node_outcomes((parameter, parameter_two), report, 0) == {
        parameter: "passed",
        parameter_two: "passed",
    }


def test_junit_runner_failure_and_missing_report_never_pass(tmp_path: Path) -> None:
    """The process exit status and report integrity take precedence over green XML cases."""
    nodes = (
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_pass",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_missing",
    )
    report = _write_report(tmp_path / "junit.xml", (_case("test_pass"),))

    assert probe._junit_node_outcomes(nodes, report, 1) == dict.fromkeys(nodes, "runner-exit=1")
    assert probe._junit_node_outcomes(nodes, tmp_path / "absent.xml", 0) == dict.fromkeys(
        nodes, "missing-report"
    )
    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<testsuites", encoding="utf-8")
    assert probe._junit_node_outcomes(nodes, malformed, 0) == dict.fromkeys(nodes, "missing-report")


def test_outcomes_batches_nodes_in_one_process_and_keeps_result_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group uses one default-environment pytest launch and retains terminal-name keys."""
    nodes = (
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_first",
        "tests/test_probe_node_outcomes.py::TestProbeNodeOutcomes::test_second",
    )
    calls: list[list[str]] = []

    def fake_run_argv(argv: Sequence[str]) -> tuple[int, str]:
        args = list(argv)
        calls.append(args)
        report_arg = next(arg for arg in args if arg.startswith("--junitxml="))
        report_path = Path(report_arg.split("=", 1)[1])
        report_path.write_text(
            "<testsuites><testsuite>"
            + _case("test_first")
            + _case("test_second")
            + "</testsuite></testsuites>",
            encoding="utf-8",
        )
        return 0, "2 passed"

    monkeypatch.setattr(probe, "run_argv", fake_run_argv)

    assert probe.outcomes(nodes) == {"test_first": "passed", "test_second": "passed"}
    assert len(calls) == 1
    assert "--no-cov" in calls[0]
    assert "-p" in calls[0] and "no:cacheprovider" in calls[0]
    assert "-m" in calls[0] and "not e2e" in calls[0]
    assert "-o" in calls[0] and "junit_family=xunit1" in calls[0]
    assert all(node in calls[0] for node in nodes)


def test_outcomes_marks_duplicate_request_and_terminal_key_collision_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy terminal-name mapping cannot hide repeated or colliding node ids."""
    duplicate = "tests/test_probe_node_outcomes.py::TestOne::test_same"
    collision = "tests/test_probe_node_outcomes.py::TestTwo::test_same"

    def fake_run_argv(argv: Sequence[str]) -> tuple[int, str]:
        report_arg = next(arg for arg in argv if arg.startswith("--junitxml="))
        Path(report_arg.split("=", 1)[1]).write_text(
            "<testsuites><testsuite>"
            '<testcase classname="tests.test_probe_node_outcomes.TestOne" '
            'file="tests/test_probe_node_outcomes.py" name="test_same"/>'
            '<testcase classname="tests.test_probe_node_outcomes.TestTwo" '
            'file="tests/test_probe_node_outcomes.py" name="test_same"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        return 0, "2 passed"

    monkeypatch.setattr(probe, "run_argv", fake_run_argv)

    assert probe.outcomes((duplicate, duplicate, collision)) == {"test_same": "ambiguous"}
