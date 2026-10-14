"""Offline tests against hand-authored Kenneth French archive structures."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.models.famafrench import (
    FamaFrenchBreakpoint,
    FamaFrenchCountryPortfolioReturn,
    FamaFrenchFactorObservation,
    FamaFrenchInternationalIndexReturn,
    FamaFrenchPortfolioReturn,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.famafrench import _source
from opendata.data.providers.famafrench.models import fetchers
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def _zip_member(name: str, text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text.encode("utf-8"))
    return output.getvalue()


def _dat_text(
    period: str = "202501",
    *,
    marker: str = "1.00",
    data_requirement: str = "Required",
) -> str:
    return "\n".join(
        (
            f"    Value-Weight Dollar Returns      All 4 Data Items {data_requirement}",
            "                -- BE/ME --   --- E/P ---   --- CE/P --   ------  Yld  -----",
            "          Mkt   High    Low   High    Low   High    Low   High    Low   Zero",
            f"{period}  " + "  ".join([marker] * 10),
        )
    )


def _country_ratio_text() -> str:
    return "\n".join(
        (
            "Average of Annual Value-Weight Averages",
            "(Annual Sum of Num / Annual Sum of Den)",
            "All 4 Data Items Not Reqd",
            "Ratios x 100   Firms is number in B/M",
            "     Firms    B/M    E/P   CE/P    Yld",
            "2024 1197 59.79 8.90 13.56 4.14",
            "Average of Annual Value-Weight Averages",
            "(Annual Sum of Num / Annual Sum of Den)",
            "All 4 Data Items Required",
            "Ratios x 100",
            "     Firms    B/M    E/P   CE/P    Yld",
            "2024 533 54.64 7.36 12.81 3.85",
        )
    )


def _factor_text(period: str = "192607", value: str = "2.89") -> str:
    return "\n".join(
        (
            "This file was created using the 202608 CRSP database.",
            "Monthly returns are in percent.",
            "",
            ",Mkt-RF,SMB,HML,RF",
            f"{period}, {value}, -2.42, -2.75, 0.22",
        )
    )


def _portfolio_text(period: str = "192607", value: str = "1.0866") -> str:
    return "\n".join(
        (
            "This file was created using the 202608 CRSP database.",
            "Missing data are indicated by -99.99 or -999.",
            "",
            "Average Value Weighted Returns -- Monthly",
            ",SMALL LoBM,ME1 BM2,SMALL HiBM,BIG LoBM,ME2 BM2,BIG HiBM",
            f"{period}, {value}, 0.9068, -0.1275, 5.7178, 1.9709, 1.4225",
        )
    )


def _us_default_portfolio_text() -> str:
    return "\n".join(
        (
            "This file contains value- and equal-weighted returns.",
            "",
            "Average Value Weighted Returns -- Monthly",
            ",Lo 10,Dec 2,Dec 3,Dec 4,Dec 5,Dec 6,Dec 7,Dec 8,Dec 9,Hi 10",
            "202501, 1.00, 2.00, 3.00, 4.00, 5.00, 6.00, 7.00, 8.00, 9.00, 10.00",
        )
    )


def _regional_default_portfolio_text() -> str:
    return "\n".join(
        (
            "This file contains value- and equal-weighted returns.",
            "",
            "Average Value Weighted Returns -- Monthly",
            ",SMALL LoOP,SMALL 2OP,SMALL HiOP,BIG LoOP,BIG 2OP,BIG HiOP",
            "202501, 1.00, 2.00, 3.00, 4.00, 5.00, 6.00",
        )
    )


def _factor_txt_text() -> str:
    return "\n".join(
        (
            "This file was created using the 202608 CRSP database.",
            "Monthly returns are in percent.",
            "",
            "Mkt-RF     SMB     HML      RF",
            "192607    2.89   -2.42   -2.75    0.22",
            "192608    2.64   -1.44    4.13    0.25",
            "",
            "Annual Factors: January-December",
            "Mkt-RF     SMB     HML      RF",
            "1927      29.44  -10.43   -3.73    2.90",
        )
    )


def _portfolio_2x2_txt_text(period: str = "199107") -> str:
    return "\n".join(
        (
            "This file contains value- and equal-weighted returns.",
            "",
            "  Average Value Weighted Returns -- Monthly",
            "             LoBM          HiBM",
            "         LoOP   HiOP   LoOP   HiOP",
            f"{period}  -4.10   3.48   1.04   6.22",
        )
    )


def _portfolio_32_txt_text(period: str = "199007") -> str:
    size_and_beme = " ".join(["Lo BE/ME 2 3 Hi BE/ME"] * 2)
    op_labels = " ".join(["Lo OP 2 3 Hi OP"] * 8)
    values = " ".join(f"{value:.2f}" for value in range(1, 33))
    return "\n".join(
        (
            "This file contains value- and equal-weighted returns.",
            "",
            "  Average Value Weighted Returns -- Monthly",
            "Small Big",
            size_and_beme,
            op_labels,
            f"{period} {values}",
        )
    )


def _set_archive(monkeypatch: pytest.MonkeyPatch, filename: str, member: str, text: str) -> None:
    payload = _zip_member(member, text)
    monkeypatch.setattr(fetchers, "fetch_archive", lambda requested, timeout: payload)


def _set_requested_archive(
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    text: str,
) -> list[str]:
    payload = _zip_member(member, text)
    requests: list[str] = []

    def fetch(filename: str, _timeout: float | None) -> bytes:
        requests.append(filename)
        return payload

    monkeypatch.setattr(fetchers, "fetch_archive", fetch)
    return requests


def _context() -> FetchContext:
    return FetchContext(timeout=3.0)


def _service_context(model_id: str) -> FetchContext:
    grant = RequestGrant(
        source="famafrench",
        canonical_model=model_id,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=frozenset({"mba.tuck.dartmouth.edu"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,)),
    )


def test_breakpoints_model_parses_csv_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    values = ", ".join(str(index) for index in range(1, 21))
    text = f"This file contains every 5th NYSE ME percentile.\n\n192512, 488, {values}\n"
    _set_archive(monkeypatch, "ME_Breakpoints_CSV.zip", "ME_Breakpoints.csv", text)
    fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    params = fetcher.transform_query(
        dataset="me", frequency="monthly", start_date="1925-12-01", end_date="1925-12-31"
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert isinstance(rows[0], FamaFrenchBreakpoint)
    assert (rows[0].date, rows[0].percentile_5, rows[0].percentile_100, rows[0].num_firms) == (
        date(1925, 12, 1),
        1.0,
        20.0,
        488,
    )


def test_breakpoints_model_parses_official_txt_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    values = " ".join(f"{index:.2f}" for index in range(1, 21))
    text = f"This file contains every 5th NYSE ME percentile.\n\n192512 488 {values}\n"
    requests = _set_requested_archive(monkeypatch, "ME_Breakpoints.txt", text)
    fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    params = fetcher.transform_query(
        dataset="me",
        frequency="monthly",
        archive_format="txt",
        start_date="1925-12-01",
        end_date="1925-12-31",
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert requests == ["ME_Breakpoints_TXT.zip"]
    assert len(rows) == 1
    assert (rows[0].percentile_5, rows[0].percentile_100) == (1.0, 20.0)


@pytest.mark.parametrize(
    ("breakpoint_type", "archive", "member", "period", "count_columns", "expected_date"),
    [
        (
            "be-me",
            "BE-ME_Breakpoints_CSV.zip",
            "BE-ME_Breakpoints.csv",
            "1926",
            2,
            date(1926, 6, 30),
        ),
        ("op", "OP_Breakpoints_CSV.zip", "OP_Breakpoints.csv", "1926", 1, date(1926, 6, 30)),
        ("inv", "INV_Breakpoints_CSV.zip", "INV_Breakpoints.csv", "1926", 1, date(1926, 6, 30)),
        ("e-p", "E-P_Breakpoints_CSV.zip", "E-P_Breakpoints.csv", "1926", 2, date(1926, 6, 30)),
        ("cf-p", "CF-P_Breakpoints_CSV.zip", "CF-P_Breakpoints.csv", "1926", 2, date(1926, 6, 30)),
        ("d-p", "D-P_Breakpoints_CSV.zip", "D-P_Breakpoints.csv", "192606", 2, date(1926, 6, 1)),
        ("me", "ME_Breakpoints_CSV.zip", "ME_Breakpoints.csv", "192512", 1, date(1925, 12, 1)),
        (
            "2-12",
            "Prior_2-12_Breakpoints_CSV.zip",
            "Prior_2-12_Breakpoints.csv",
            "192512",
            1,
            date(1925, 12, 1),
        ),
    ],
)
def test_breakpoint_dates_follow_official_period_and_formation_rules(
    monkeypatch: pytest.MonkeyPatch,
    breakpoint_type: str,
    archive: str,
    member: str,
    period: str,
    count_columns: int,
    expected_date: date,
) -> None:
    counts = ["225", "285"][:count_columns]
    values = [str(index) for index in range(1, 21)]
    text = (
        "This file contains official breakpoint rows.\n\n"
        + ", ".join([period, *counts, *values])
        + "\n"
    )
    _set_archive(monkeypatch, archive, member, text)
    fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    params = fetcher.transform_query(
        breakpoint_type=breakpoint_type,
        start_date=expected_date,
        end_date=expected_date,
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert {row.date for row in rows} == {expected_date}
    assert (rows[0].percentile_5, rows[-1].percentile_100) == (1.0, 20.0)
    if breakpoint_type == "d-p":
        assert (rows[0].num_firms_less_than_0, rows[0].num_firms_greater_than_0) == (
            225,
            285,
        )


def test_breakpoint_wide_rows_reject_missing_or_duplicate_percentiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = ", ".join(str(index) for index in range(1, 21))
    text = f"This file contains every 5th NYSE ME percentile.\n\n192512, 488, {values}\n"
    _set_archive(monkeypatch, "ME_Breakpoints_CSV.zip", "ME_Breakpoints.csv", text)
    fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    params = fetcher.transform_query(dataset="me", frequency="monthly")
    source_rows = fetcher.extract_data(params, _context())
    with pytest.raises(
        _source.FrenchProviderError, match="FRENCH_BREAKPOINT_PERCENTILE_INCOMPLETE"
    ):
        fetcher.transform_data(source_rows[:-1], params)
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_DUPLICATE_BREAKPOINT_ROW"):
        fetcher.transform_data([*source_rows, source_rows[0]], params)


def test_country_dat_archive_selects_member_and_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(monkeypatch, "F-F_International_Countries.zip", "UK.Dat", _dat_text())
    fetcher = fetchers.FamaFrenchCountryPortfolioReturnsFetcher()
    params = fetcher.transform_query(
        country="united_kingdom", frequency="monthly", sort="be_me", portfolio="high"
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert isinstance(rows[0], FamaFrenchCountryPortfolioReturn)
    assert rows[0].model_dump() == {"date": date(2025, 1, 1), "be_me_high": 1.0}


def test_country_ratios_parse_annual_fields_and_ignore_frequency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_archive(
        monkeypatch,
        "F-F_International_Countries.zip",
        "UK.Dat",
        _country_ratio_text(),
    )
    fetcher = fetchers.FamaFrenchCountryPortfolioReturnsFetcher()
    params = fetcher.transform_query(
        country="united_kingdom",
        frequency="monthly",
        measure="ratios",
        all_data_items_required=True,
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert rows[0].model_dump() == {
        "date": date(2024, 1, 1),
        "firms": 533,
        "bm": 54.64,
        "ep": 7.36,
        "ce_p": 12.81,
        "yld": 3.85,
    }


def test_country_ratio_selector_respects_all_data_items_counterfact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_archive(
        monkeypatch,
        "F-F_International_Countries.zip",
        "UK.Dat",
        _country_ratio_text(),
    )
    fetcher = fetchers.FamaFrenchCountryPortfolioReturnsFetcher()
    params = fetcher.transform_query(
        country="united_kingdom", measure="ratios", all_data_items_required=False
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert rows[0].model_dump() == {
        "date": date(2024, 1, 1),
        "firms": 1197,
        "bm": 59.79,
        "ep": 8.90,
        "ce_p": 13.56,
        "yld": 4.14,
    }


def test_index_ratio_shape_is_rejected_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_transport(_filename: str, _timeout: float | None) -> bytes:
        raise AssertionError("unsupported index ratios must fail before source I/O")

    monkeypatch.setattr(fetchers, "fetch_archive", unexpected_transport)
    fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_INDEX_RATIO_SHAPE_UNSUPPORTED"):
        fetcher.transform_query(region="all", measure="ratios")


def test_factors_model_parses_multi_frequency_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(
        monkeypatch,
        "F-F_Research_Data_Factors_CSV.zip",
        "F-F_Research_Data_Factors.csv",
        _factor_text(),
    )
    fetcher = fetchers.FamaFrenchFactorsFetcher()
    params = fetcher.transform_query(dataset="us_ff3", frequency="monthly")
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert isinstance(rows[0], FamaFrenchFactorObservation)
    assert rows[0].model_dump() == {
        "date": date(1926, 7, 1),
        "mkt_rf": 2.89,
        "smb": -2.42,
        "hml": -2.75,
        "rf": 0.22,
    }


def test_factors_model_parses_whitespace_txt_period_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _set_requested_archive(
        monkeypatch, "F-F_Research_Data_Factors.txt", _factor_txt_text()
    )
    fetcher = fetchers.FamaFrenchFactorsFetcher()
    params = fetcher.transform_query(
        dataset="us_ff3",
        frequency="monthly",
        archive_format="txt",
        start_date="1926-07-01",
        end_date="1926-07-31",
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert requests == ["F-F_Research_Data_Factors_TXT.zip"]
    assert len(rows) == 1
    assert rows[0].model_dump() == {
        "date": date(1926, 7, 1),
        "mkt_rf": 2.89,
        "smb": -2.42,
        "hml": -2.75,
        "rf": 0.22,
    }


def test_international_index_model_parses_dat_member(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(monkeypatch, "F-F_International_Indices.zip", "Ind_all.Dat", _dat_text())
    fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    params = fetcher.transform_query(
        region="all", frequency="monthly", sort="dividend_yield", portfolio="zero"
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 1
    assert isinstance(rows[0], FamaFrenchInternationalIndexReturn)
    assert rows[0].model_dump() == {"date": date(2025, 1, 1), "yld_zero": 1.0}


def test_regional_portfolio_model_parses_value_weighted_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_archive(
        monkeypatch,
        "Developed_6_Portfolios_ME_BE-ME_CSV.zip",
        "Developed_6_Portfolios_ME_BE-ME.csv",
        _portfolio_text(),
    )
    fetcher = fetchers.FamaFrenchRegionalPortfolioReturnsFetcher()
    params = fetcher.transform_query(dataset="developed_6_beme", frequency="monthly")
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 6
    assert isinstance(rows[0], FamaFrenchPortfolioReturn)
    assert rows[0].model_dump() == {
        "date": date(1926, 7, 1),
        "portfolio": "SMALL LoBM",
        "measure": "value",
        "value": 1.0866,
    }


def test_us_portfolio_model_parses_value_weighted_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(
        monkeypatch,
        "6_Portfolios_2x3_CSV.zip",
        "6_Portfolios_2x3.csv",
        _portfolio_text(),
    )
    fetcher = fetchers.FamaFrenchUSPortfolioReturnsFetcher()
    params = fetcher.transform_query(dataset="6_beme", frequency="monthly")
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert len(rows) == 6
    assert rows[0].date == date(1926, 7, 1)
    assert rows[-1].model_dump() == {
        "date": date(1926, 7, 1),
        "portfolio": "BIG HiBM",
        "measure": "value",
        "value": 1.4225,
    }


def test_us_portfolio_model_parses_txt_multirow_header(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "\n".join(
        (
            "This file contains value- and equal-weighted returns.",
            "",
            "Average Value Weighted Returns -- Monthly",
            "               Small                 Big",
            "        Lo BM     2   Hi BM     Lo BM     2   Hi BM",
            "192607   1.09   0.91   -0.13     5.72   1.97   1.42",
        )
    )
    requests = _set_requested_archive(monkeypatch, "6_Portfolios_2x3.txt", text)
    fetcher = fetchers.FamaFrenchUSPortfolioReturnsFetcher()
    params = fetcher.transform_query(dataset="6_beme", frequency="monthly", archive_format="txt")
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert requests == ["6_Portfolios_2x3_TXT.zip"]
    assert len(rows) == 6
    assert [(row.portfolio, row.value) for row in rows[:3]] == [
        ("Small Lo BM", 1.09),
        ("Small 2", 0.91),
        ("Small Hi BM", -0.13),
    ]


def test_regional_portfolio_model_parses_emerging_2x2_txt(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _set_requested_archive(
        monkeypatch,
        "Emerging_Markets_4_Portfolios_BE-ME_OP.txt",
        _portfolio_2x2_txt_text(),
    )
    fetcher = fetchers.FamaFrenchRegionalPortfolioReturnsFetcher()
    params = fetcher.transform_query(
        portfolio="emerging_markets_4_portfolios_be_me_op", archive_format="txt"
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert requests == ["Emerging_Markets_4_Portfolios_BE-ME_OP_TXT.zip"]
    assert len(rows) == 4
    assert [(row.portfolio, row.value) for row in rows] == [
        ("Lo BM Lo OP", -4.1),
        ("Lo BM Hi OP", 3.48),
        ("Hi BM Lo OP", 1.04),
        ("Hi BM Hi OP", 6.22),
    ]


def test_regional_portfolio_model_parses_32_column_2x4x4_txt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _set_requested_archive(
        monkeypatch,
        "Developed_32_Portfolios_ME_BE-ME_OP_2x4x4.txt",
        _portfolio_32_txt_text(),
    )
    fetcher = fetchers.FamaFrenchRegionalPortfolioReturnsFetcher()
    params = fetcher.transform_query(
        portfolio="developed_32_portfolios_me_be_me_op_2x4x4", archive_format="txt"
    )
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert requests == ["Developed_32_Portfolios_ME_BE-ME_OP_2x4x4_TXT.zip"]
    assert len(rows) == 32
    assert rows[0].portfolio == "Small Lo BE/ME Lo OP"
    assert rows[-1].portfolio == "Big Hi BE/ME Hi OP"
    assert (rows[0].value, rows[-1].value) == (1.0, 32.0)


def test_rejects_invalid_parameters_and_frequency_mismatch() -> None:
    fetcher = fetchers.FamaFrenchFactorsFetcher()
    with pytest.raises(ValidationError):
        fetcher.transform_query(dataset="not_a_dataset", frequency="monthly")
    assert fetcher.transform_query(dataset="us_ff3").frequency == "monthly"
    with pytest.raises(ValidationError):
        fetcher.transform_query(dataset="us_ff3", frequency="monthly", source="other")
    with pytest.raises(ValidationError):
        fetcher.transform_query(dataset="us_ff3", frequency="monthly", archive_format="xml")
    with pytest.raises(ValueError, match="precision"):
        _source.parse_period("192607", "daily")
    with pytest.raises(ValidationError, match="declared market"):
        fetcher.transform_query(dataset="us_ff3", frequency="monthly", market="us")


def test_public_query_defaults_and_canonical_selectors_resolve_to_static_archives() -> None:
    breakpoint = fetchers.FamaFrenchBreakpointsFetcher().transform_query()
    assert (breakpoint.dataset_key, breakpoint.source_frequency) == ("me", "monthly")
    assert (
        fetchers.FamaFrenchBreakpointsFetcher().transform_query(breakpoint="op").dataset_key
        == "operating_profitability"
    )

    factors = fetchers.FamaFrenchFactorsFetcher().transform_query()
    assert factors.dataset_key == "us_ff3"
    europe_factors = fetchers.FamaFrenchFactorsFetcher().transform_query(
        region="europe", factor="5_factors", start_date="1990-01-01"
    )
    assert (europe_factors.dataset_key, europe_factors.frequency) == ("europe_ff5", "monthly")

    us_portfolio = fetchers.FamaFrenchUSPortfolioReturnsFetcher().transform_query()
    assert (
        us_portfolio.resolved_dataset,
        us_portfolio.dataset_table[us_portfolio.resolved_dataset].files[
            us_portfolio.source_frequency
        ],
    ) == ("single_me", "Portfolios_Formed_on_ME_CSV.zip")
    regional_portfolio = fetchers.FamaFrenchRegionalPortfolioReturnsFetcher().transform_query()
    assert (
        regional_portfolio.resolved_dataset,
        regional_portfolio.dataset_table[regional_portfolio.resolved_dataset].files[
            regional_portfolio.source_frequency
        ],
    ) == ("developed_ex_us_6_op", "Developed_ex_US_6_Portfolios_ME_OP_CSV.zip")

    country = fetchers.FamaFrenchCountryPortfolioReturnsFetcher().transform_query()
    assert (country.country, country.measure, country.frequency) == (
        "united_kingdom",
        "usd",
        "monthly",
    )
    assert country.dividends is None and country.source_dividends is True
    index_fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    index = index_fetcher.transform_query()
    assert (index.index, index.source_index_key) == ("all", "all")
    assert index_fetcher.transform_query(index="europe_ex_uk").source_index_key == (
        "europe_without_uk"
    )


def test_public_selector_aliases_conflicts_and_frequency_suffixes() -> None:
    breakpoint_fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    with pytest.raises(ValidationError, match="selectors conflict"):
        breakpoint_fetcher.transform_query(breakpoint_type="op", breakpoint="inv")
    with pytest.raises(ValidationError, match="selectors conflict"):
        breakpoint_fetcher.transform_query(breakpoint_type="op", dataset="me")

    factor_fetcher = fetchers.FamaFrenchFactorsFetcher()
    with pytest.raises(ValidationError, match="conflicts"):
        factor_fetcher.transform_query(dataset="us_ff5", region="america", factor="3_factors")
    with pytest.raises(ValidationError, match="allowlist"):
        factor_fetcher.transform_query(region="europe", factor="3_factors", frequency="weekly")

    us_fetcher = fetchers.FamaFrenchUSPortfolioReturnsFetcher()
    with pytest.raises(ValidationError, match="conflicts"):
        us_fetcher.transform_query(dataset="6_beme", portfolio="portfolios_formed_on_me")
    daily = us_fetcher.transform_query(
        portfolio="portfolios_formed_on_me_daily", frequency="monthly"
    )
    assert daily.source_frequency == "daily"
    weekly = us_fetcher.transform_query(portfolio="6_portfolios_2x3_weekly", frequency="monthly")
    assert weekly.source_frequency == "weekly"
    explicit_daily = us_fetcher.transform_query(
        portfolio="portfolios_formed_on_me", frequency="daily"
    )
    assert explicit_daily.source_frequency == "daily"

    index_fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    assert index_fetcher.transform_query(region="europe_without_uk").source_index_key == (
        "europe_without_uk"
    )
    with pytest.raises(ValidationError, match="conflict"):
        index_fetcher.transform_query(index="all", region="europe_ex_uk")
    partial_date = factor_fetcher.transform_query(start_date="2020-01-01")
    assert partial_date.start_date == date(2020, 1, 1) and partial_date.end_date is None
    with pytest.raises(ValidationError, match="must not be after"):
        factor_fetcher.transform_query(start_date="2021-01-01", end_date="2020-12-31")
    assert breakpoint_fetcher.transform_query(start_date="1925-01-01").start_date == date(
        1925, 1, 1
    )


def test_public_selector_archive_resolution_and_index_alias_wire_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = ", ".join(str(index) for index in range(1, 21))
    breakpoint_requests = _set_requested_archive(
        monkeypatch,
        "OP_Breakpoints.csv",
        f"This file contains annual OP breakpoints.\n\n1926, 488, {values}\n",
    )
    breakpoint_fetcher = fetchers.FamaFrenchBreakpointsFetcher()
    breakpoint_query = breakpoint_fetcher.transform_query(breakpoint="op")
    assert breakpoint_fetcher.extract_data(breakpoint_query, _context())[0].value == 1.0
    assert breakpoint_requests == ["OP_Breakpoints_CSV.zip"]

    factor_requests = _set_requested_archive(
        monkeypatch,
        "Europe_5_Factors.csv",
        "This file was created using the 202608 CRSP database.\n"
        "Monthly returns are in percent.\n\n"
        ",Mkt-RF,SMB,HML,RMW,CMA,RF\n"
        "192607, 2.89, -2.42, -2.75, 3.15, -1.25, 0.22\n",
    )
    factor_fetcher = fetchers.FamaFrenchFactorsFetcher()
    factor_query = factor_fetcher.transform_query(region="europe", factor="5_factors")
    assert factor_fetcher.extract_data(factor_query, _context())[0].key == "Mkt-RF"
    assert factor_requests == ["Europe_5_Factors_CSV.zip"]

    index_model = "FamaFrenchInternationalIndexReturns"
    index_fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    registry = ProviderRegistry()
    registry.register_provider_models("famafrench", (index_model,), index_fetcher)
    payload = _zip_member("Ind_Eur_WOut_UK.Dat", _dat_text())
    index_requests: list[str] = []

    def fetch_index(filename: str, _timeout: float | None) -> bytes:
        index_requests.append(filename)
        return payload

    monkeypatch.setattr(fetchers, "fetch_archive", fetch_index)
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, FamaFrenchInternationalIndexReturn),
    )
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="famafrench",
            model=index_model,
            query={"index": "europe_ex_uk"},
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_service_context(index_model),
        )
    )
    assert response["results"][0] == {
        "date": "2025-01-01",
        "mkt": 1.0,
        "be_me_high": 1.0,
        "be_me_low": 1.0,
        "e_p_high": 1.0,
        "e_p_low": 1.0,
        "ce_p_high": 1.0,
        "ce_p_low": 1.0,
        "yld_high": 1.0,
        "yld_low": 1.0,
        "yld_zero": 1.0,
    }
    assert index_requests == ["F-F_International_Indices.zip"]


def test_invalid_public_aliases_and_index_ratios_reject_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def record_transport(filename: str, _timeout: float | None) -> bytes:
        requests.append(filename)
        raise AssertionError("invalid query reached the transport")

    monkeypatch.setattr(fetchers, "fetch_archive", record_transport)
    index_fetcher = fetchers.FamaFrenchInternationalIndexReturnsFetcher()
    index_model = "FamaFrenchInternationalIndexReturns"
    with pytest.raises(ValidationError):
        asyncio.run(
            index_fetcher.fetch_async(
                ctx=_service_context(index_model), index="all", region="europe_ex_uk"
            )
        )
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_INDEX_RATIO_SHAPE_UNSUPPORTED"):
        asyncio.run(
            index_fetcher.fetch_async(
                ctx=_service_context(index_model), index="all", measure="ratios"
            )
        )
    assert requests == []


def test_documented_missing_sentinels_keep_source_token_and_null_value() -> None:
    assert _source.parse_number("-99.99") == (None, "-99.99")
    assert _source.parse_number(" -999 ") == (None, "-999")
    with pytest.raises(ValueError, match="numeric"):
        _source.parse_number("not-a-number")


def test_factor_missing_sentinel_is_a_typed_null(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(
        monkeypatch,
        "F-F_Research_Data_Factors_CSV.zip",
        "F-F_Research_Data_Factors.csv",
        _factor_text(value="-99.99"),
    )
    fetcher = fetchers.FamaFrenchFactorsFetcher()
    params = fetcher.transform_query(dataset="us_ff3", frequency="monthly")
    rows = fetcher.transform_data(fetcher.extract_data(params, _context()), params)
    assert rows[0].mkt_rf is None
    assert rows[0].model_dump() == {
        "date": date(1926, 7, 1),
        "mkt_rf": None,
        "smb": -2.42,
        "hml": -2.75,
        "rf": 0.22,
    }


def test_factor_wide_rows_reject_unknown_and_duplicate_columns() -> None:
    fetcher = fetchers.FamaFrenchFactorsFetcher()
    params = fetcher.transform_query(dataset="us_ff3", frequency="monthly")
    unknown = _source.SourceObservation(date(1926, 7, 1), "UNKNOWN", 1.0, "1")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_FACTOR_FIELD_UNMAPPED"):
        fetcher.transform_data([unknown], params)
    duplicate = _source.SourceObservation(date(1926, 7, 1), "Mkt-RF", 2.0, "2")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_DUPLICATE_FACTOR_ROW"):
        fetcher.transform_data([duplicate, duplicate], params)


def test_rejects_wrong_table_bad_value_and_empty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_archive(
        monkeypatch,
        "6_Portfolios_2x3_CSV.zip",
        "6_Portfolios_2x3.csv",
        _portfolio_text(),
    )
    fetcher = fetchers.FamaFrenchUSPortfolioReturnsFetcher()
    equal_params = fetcher.transform_query(dataset="6_beme", frequency="monthly", measure="equal")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_PORTFOLIO_TABLE_NOT_FOUND"):
        fetcher.extract_data(equal_params, _context())

    _set_archive(
        monkeypatch,
        "F-F_Research_Data_Factors_CSV.zip",
        "F-F_Research_Data_Factors.csv",
        _factor_text(value="NaN"),
    )
    factor_fetcher = fetchers.FamaFrenchFactorsFetcher()
    factor_params = factor_fetcher.transform_query(dataset="us_ff3", frequency="monthly")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_BAD_NUMERIC_VALUE"):
        factor_fetcher.extract_data(factor_params, _context())

    _set_archive(
        monkeypatch,
        "F-F_Research_Data_Factors_CSV.zip",
        "F-F_Research_Data_Factors.csv",
        _factor_text(),
    )
    empty_params = factor_fetcher.transform_query(
        dataset="us_ff3", frequency="monthly", start_date="1927-01-01", end_date="1927-12-31"
    )
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_EMPTY_RESULT"):
        factor_fetcher.extract_data(empty_params, _context())

    _set_archive(
        monkeypatch,
        "F-F_International_Countries.zip",
        "UK.Dat",
        _dat_text().replace("Mkt   High", "Date  High"),
    )
    country_fetcher = fetchers.FamaFrenchCountryPortfolioReturnsFetcher()
    country_params = country_fetcher.transform_query(country="united_kingdom", frequency="monthly")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_DAT_HEADER_MISMATCH"):
        country_fetcher.extract_data(country_params, _context())


def test_zip_member_selection_rejects_paths_and_ambiguous_csv() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("nested/file.csv", "date,value\n192607,1\n")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_UNSAFE_ARCHIVE_MEMBER"):
        _source.archive_text(output.getvalue(), suffix=".csv")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("a.csv", "date,value\n192607,1\n")
        archive.writestr("b.csv", "date,value\n192607,2\n")
    with pytest.raises(_source.FrenchProviderError, match="FRENCH_ARCHIVE_MEMBER_MISMATCH"):
        _source.archive_text(output.getvalue(), suffix=".csv")


def test_provider_model_query_injects_declared_market_and_serializes_all_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise public defaults, route source/market pinning, and typed JSON rows."""
    breakpoint_expected = {"date": "1925-12-01", "num_firms": 488}
    breakpoint_expected.update(
        {
            f"percentile_{percentile}": float(index)
            for index, percentile in enumerate(range(5, 101, 5), 1)
        }
    )
    cases = (
        (
            "FamaFrenchBreakpoints",
            fetchers.FamaFrenchBreakpointsFetcher,
            FamaFrenchBreakpoint,
            {},
            "ME_Breakpoints.csv",
            "This file contains every 5th NYSE ME percentile.\n\n192512, 488, "
            + ", ".join(str(index) for index in range(1, 21))
            + "\n",
            breakpoint_expected,
        ),
        (
            "FamaFrenchCountryPortfolioReturns",
            fetchers.FamaFrenchCountryPortfolioReturnsFetcher,
            FamaFrenchCountryPortfolioReturn,
            {},
            "UK.Dat",
            _dat_text(),
            {
                "date": "2025-01-01",
                "mkt": 1.0,
                "be_me_high": 1.0,
                "be_me_low": 1.0,
                "e_p_high": 1.0,
                "e_p_low": 1.0,
                "ce_p_high": 1.0,
                "ce_p_low": 1.0,
                "yld_high": 1.0,
                "yld_low": 1.0,
                "yld_zero": 1.0,
            },
        ),
        (
            "FamaFrenchFactors",
            fetchers.FamaFrenchFactorsFetcher,
            FamaFrenchFactorObservation,
            {},
            "F-F_Research_Data_Factors.csv",
            _factor_text(),
            {
                "date": "1926-07-01",
                "mkt_rf": 2.89,
                "smb": -2.42,
                "hml": -2.75,
                "rf": 0.22,
            },
        ),
        (
            "FamaFrenchInternationalIndexReturns",
            fetchers.FamaFrenchInternationalIndexReturnsFetcher,
            FamaFrenchInternationalIndexReturn,
            {},
            "Ind_all.Dat",
            _dat_text(),
            {
                "date": "2025-01-01",
                "mkt": 1.0,
                "be_me_high": 1.0,
                "be_me_low": 1.0,
                "e_p_high": 1.0,
                "e_p_low": 1.0,
                "ce_p_high": 1.0,
                "ce_p_low": 1.0,
                "yld_high": 1.0,
                "yld_low": 1.0,
                "yld_zero": 1.0,
            },
        ),
        (
            "FamaFrenchRegionalPortfolioReturns",
            fetchers.FamaFrenchRegionalPortfolioReturnsFetcher,
            FamaFrenchPortfolioReturn,
            {},
            "Developed_ex_US_6_Portfolios_ME_OP.csv",
            _regional_default_portfolio_text(),
            {
                "date": "2025-01-01",
                "portfolio": "SMALL LoOP",
                "measure": "value",
                "value": 1.0,
            },
        ),
        (
            "FamaFrenchUSPortfolioReturns",
            fetchers.FamaFrenchUSPortfolioReturnsFetcher,
            FamaFrenchPortfolioReturn,
            {},
            "Portfolios_Formed_on_ME.csv",
            _us_default_portfolio_text(),
            {
                "date": "2025-01-01",
                "portfolio": "Lo 10",
                "measure": "value",
                "value": 1.0,
            },
        ),
    )

    for model_id, fetcher_type, contract, query, member, text, expected_first in cases:
        fetcher = fetcher_type()
        registry = ProviderRegistry()
        registry.register_provider_models("famafrench", (model_id,), fetcher)
        payload = _zip_member(member, text)
        monkeypatch.setattr(
            fetchers,
            "fetch_archive",
            lambda _filename, _timeout, body=payload: body,
        )
        monkeypatch.setattr(
            provider_model_query,
            "_reviewed_query_contract",
            lambda _domain, row_type=contract: (None, row_type),
        )
        response = asyncio.run(
            provider_model_query.query_provider_model(
                registry=registry,
                source="famafrench",
                model=model_id,
                query=query,
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                ctx=_service_context(model_id),
            )
        )
        assert response["model"] == model_id
        assert response["results"][0] == expected_first


def test_provider_model_query_injects_market_for_txt_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "FamaFrenchRegionalPortfolioReturns"
    fetcher = fetchers.FamaFrenchRegionalPortfolioReturnsFetcher()
    registry = ProviderRegistry()
    registry.register_provider_models("famafrench", (model_id,), fetcher)
    payload = _zip_member("Emerging_Markets_4_Portfolios_BE-ME_OP.txt", _portfolio_2x2_txt_text())
    monkeypatch.setattr(fetchers, "fetch_archive", lambda _filename, _timeout: payload)
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, FamaFrenchPortfolioReturn),
    )
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="famafrench",
            model=model_id,
            query={
                "dataset": "emerging_4_beme_op",
                "frequency": "monthly",
                "archive_format": "txt",
            },
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_service_context(model_id),
        )
    )
    assert response["model"] == model_id
    assert response["results"][0] == {
        "date": "1991-07-01",
        "portfolio": "Lo BM Lo OP",
        "measure": "value",
        "value": -4.1,
    }


def test_provider_model_query_serializes_country_ratio_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "FamaFrenchCountryPortfolioReturns"
    fetcher = fetchers.FamaFrenchCountryPortfolioReturnsFetcher()
    registry = ProviderRegistry()
    registry.register_provider_models("famafrench", (model_id,), fetcher)
    payload = _zip_member("UK.Dat", _country_ratio_text())
    monkeypatch.setattr(fetchers, "fetch_archive", lambda _filename, _timeout: payload)
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, FamaFrenchCountryPortfolioReturn),
    )
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="famafrench",
            model=model_id,
            query={
                "country": "united_kingdom",
                "frequency": "monthly",
                "measure": "ratios",
                "all_data_items_required": True,
            },
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_service_context(model_id),
        )
    )
    assert response["results"] == [
        {
            "date": "2024-01-01",
            "firms": 533,
            "bm": 54.64,
            "ep": 7.36,
            "ce_p": 12.81,
            "yld": 3.85,
        }
    ]
