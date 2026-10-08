"""Offline tests for the AC-11 cross-source adjust checker (pure logic)."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "qfq_official_check.py"
_spec = importlib.util.spec_from_file_location("qfq_official_check", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

sina_symbol = _module.sina_symbol
_compare = _module._compare
_official_series = _module._official_series
leg_spec = _module.leg_spec
leg_module = _module.leg_module
leg_fetcher = _module.leg_fetcher
OFFICIAL_LEGS = _module.OFFICIAL_LEGS
DEFAULT_OFFICIAL_LEG = _module.DEFAULT_OFFICIAL_LEG
EXPECTED_MODULES = {
    "akshare": "opendata.data.providers.akshare._vendor.stock_feature.stock_hist_em",
    "sina": "opendata.data.providers.akshare._vendor.stock.stock_zh_a_sina",
}


class TestSinaSymbol:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600519", "sh600519"),
            ("688981", "sh688981"),
            ("000001", "sz000001"),
            ("300750", "sz300750"),
            ("510300", "sh510300"),
            ("159915", "sz159915"),
            ("920992", "bj920992"),
        ],
    )
    def test_exchange_prefix_rules(self, code: str, expected: str) -> None:
        assert sina_symbol(code) == expected

    def test_unknown_prefix_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="cannot derive the sina symbol"):
            sina_symbol("123456")


class TestOfficialLegMap:
    """The comparison leg is a table entry, so it can be read back as evidence."""

    def test_the_map_offers_akshare_and_keeps_sina(self) -> None:
        assert set(OFFICIAL_LEGS) == {"akshare", "sina"}

    def test_default_leg_is_akshare_as_the_criterion_names(self) -> None:
        assert DEFAULT_OFFICIAL_LEG == "akshare"

    def test_akshare_leg_resolves_to_the_ported_module(self) -> None:
        assert leg_module("akshare") == EXPECTED_MODULES["akshare"]
        assert leg_module("sina") == EXPECTED_MODULES["sina"]

    def test_the_resolved_akshare_module_declares_its_provenance(self) -> None:
        origin = importlib.util.find_spec(leg_module("akshare")).origin
        header = "".join(Path(origin).read_text(encoding="utf-8").splitlines(keepends=True)[:6])

        assert "# Ported from akshare" in header

    def test_each_leg_fetcher_is_the_function_its_target_names(self) -> None:
        for leg in sorted(OFFICIAL_LEGS):
            fetcher = leg_fetcher(leg)
            _, _, function_name = leg_spec(leg).target.partition(":")
            module = importlib.import_module(EXPECTED_MODULES[leg])

            assert fetcher.__qualname__ == function_name
            assert fetcher.__module__ == EXPECTED_MODULES[leg]
            assert fetcher is getattr(module, function_name)

    def test_unknown_leg_fails_closed_instead_of_defaulting(self) -> None:
        with pytest.raises(ValueError, match="unknown official leg"):
            leg_spec("ths")

    def test_cli_refuses_a_leg_that_is_not_in_the_map(self) -> None:
        with pytest.raises(SystemExit) as caught:
            _module.main(["--official", "ths"])

        assert caught.value.code == 2

    def test_akshare_leg_gets_the_bare_code_and_chinese_columns(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def fake_hist(**kwargs):
            seen.update(kwargs)
            return pd.DataFrame(
                {"日期": [date(2026, 1, 14), date(2026, 1, 15)], "收盘": [10.0, 11.5]}
            )

        monkeypatch.setattr(_module, "leg_fetcher", lambda leg: fake_hist)

        series = _official_series("600519", date(2026, 1, 14), date(2026, 1, 15), "qfq")

        assert series.values == {"2026-01-14": 10.0, "2026-01-15": 11.5}
        assert series.retries == 0
        assert seen["symbol"] == "600519"
        assert seen["start_date"] == "20260114"
        assert seen["adjust"] == "qfq"

    def test_prefixed_leg_gets_the_exchange_symbol(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def fake_daily(**kwargs):
            seen.update(kwargs)
            return pd.DataFrame({"date": ["2026-01-14"], "close": [10.0]})

        monkeypatch.setattr(_module, "leg_fetcher", lambda leg: fake_daily)

        series = _official_series("600519", date(2026, 1, 14), date(2026, 1, 15), "hfq", "sina")

        assert seen["symbol"] == "sh600519"
        assert series.values == {"2026-01-14": 10.0}

    def test_a_flaky_leg_retries_and_says_so(self, monkeypatch) -> None:
        calls: list[int] = []
        slept: list[float] = []

        def flaky(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("502 Bad Gateway")
            return pd.DataFrame({"日期": ["2026-01-14"], "收盘": [10.0]})

        monkeypatch.setattr(_module, "leg_fetcher", lambda leg: flaky)
        monkeypatch.setattr(_module.time, "sleep", lambda seconds: slept.append(seconds))

        series = _official_series("600519", date(2026, 1, 14), date(2026, 1, 15), "qfq")

        assert series.retries == 2
        assert series.values == {"2026-01-14": 10.0}
        assert len(slept) == 2

    def test_exhausted_leg_raises_instead_of_comparing_nothing(self, monkeypatch) -> None:
        attempts: list[int] = []

        def empty(**kwargs):
            attempts.append(1)
            return pd.DataFrame()

        monkeypatch.setattr(_module, "leg_fetcher", lambda leg: empty)
        monkeypatch.setattr(_module.time, "sleep", lambda seconds: None)

        with pytest.raises(RuntimeError, match="gave no series"):
            _official_series("600519", date(2026, 1, 14), date(2026, 1, 15), "qfq")

        assert len(attempts) == _module.LEG_ATTEMPTS


class TestCompare:
    def test_matching_series_passes(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 19)]
        rows = [{"trade_date": day, "close": 10.0 * index} for index, day in enumerate(days, 1)]
        official = {day.isoformat(): 10.0 * index for index, day in enumerate(days, 1)}

        report = _compare(rows, official, tolerance=2e-3)

        assert report["failed"] is False
        assert report["compared"] == 5
        assert report["over_tolerance"] == 0
        assert report["profile"] == [(1.0, 5)]
        assert report["max_shape_dev"] == pytest.approx(0.0)

    def test_constant_anchor_offset_is_a_level_difference_not_a_failure(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 19)]
        rows = [{"trade_date": day, "close": 2.0 * index} for index, day in enumerate(days, 1)]
        official = {day.isoformat(): 1.0 * index for index, day in enumerate(days, 1)}

        report = _compare(rows, official, tolerance=2e-3)

        assert report["anchor"] == pytest.approx(2.0)
        assert report["max_dev"] > 0.5  # levels differ by the anchoring constant
        assert report["failed"] is False

    def test_shape_divergence_fails(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 17)]
        rows = [
            {"trade_date": day, "close": close}
            for day, close in zip(days, (10.0, 11.0, 10.0), strict=True)
        ]
        official = dict(zip((day.isoformat() for day in days), (10.0, 11.0, 12.0), strict=True))

        report = _compare(rows, official, tolerance=2e-3)

        assert report["failed"] is True
        assert report["diffs"]
        assert report["over_tolerance"] == 1
        assert report["deviating_span"] == "2026-01-16..2026-01-16"

    def test_the_over_count_is_the_population_and_not_the_listing_cap(self) -> None:
        # A five-line list once read as "five bad bars"; the counter has to say how many
        # actually deviated, which is the difference between an event and a whole band.
        days = [date(2026, 1, day) for day in range(14, 29)]
        rows = [{"trade_date": day, "close": 100.0} for day in days]
        official = {
            day.isoformat(): (100.0 if index < 8 else 100.6) for index, day in enumerate(days)
        }

        report = _compare(rows, official, tolerance=2e-3)

        assert report["compared"] == 15
        assert report["over_tolerance"] == 7
        assert len(report["diffs"]) == _module.MAX_DIFFS
        assert report["deviating_span"] == "2026-01-22..2026-01-28"
        assert [hits for _, hits in report["profile"]] == [8, 7]

    def test_no_common_dates_is_a_failure(self) -> None:
        report = _compare([{"trade_date": date(2026, 1, 1), "close": 10.0}], {}, tolerance=2e-3)

        assert report["failed"] is True
        assert report["compared"] == 0
