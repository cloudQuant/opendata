"""Gate-integrity tests for the coverage threshold (C6).

``make gate`` once printed "FAIL Required test coverage of 84% not reached.
Total coverage: 83.96%" while exiting 0: coverage.py compares the threshold
after rounding the total to ``precision`` decimals, and the default precision
of 0 turns 83.96 into 84. The report must therefore compare at two decimals,
so the printed verdict and the process exit code cannot disagree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import tomllib
from coverage.results import should_fail_under

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture(scope="module")
def report_config() -> dict[str, Any]:
    with PYPROJECT.open("rb") as handle:
        loaded: dict[str, Any] = tomllib.load(handle)
    return loaded["tool"]["coverage"]["report"]


class TestCoverageThreshold:
    def test_the_ratchet_is_declared(self, report_config):
        assert report_config["fail_under"] == 84

    def test_a_sub_threshold_total_actually_fails_the_gate(self, report_config):
        precision = report_config["precision"]

        assert should_fail_under(83.96, report_config["fail_under"], precision) is True
        assert should_fail_under(84.36, report_config["fail_under"], precision) is False
