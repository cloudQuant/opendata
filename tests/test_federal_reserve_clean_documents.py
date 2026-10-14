from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers.federal_reserve.models.clean_documents import (
    FED_DDP_OUTPUT_URL,
    FOMC_CALENDAR_URL,
    FOMC_HISTORICAL_URL,
    H15_TREASURY_PACKAGE_ID,
    FederalReserveSourceError,
    FomcDocumentsFetcher,
    YieldCurveFetcher,
    _parse_fomc_page,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

_H15_FIELDS = (
    "RIFLGFCM01_N.B",
    "RIFLGFCM03_N.B",
    "RIFLGFCM06_N.B",
    "RIFLGFCY01_N.B",
    "RIFLGFCY02_N.B",
    "RIFLGFCY03_N.B",
    "RIFLGFCY05_N.B",
    "RIFLGFCY07_N.B",
    "RIFLGFCY10_N.B",
    "RIFLGFCY20_N.B",
    "RIFLGFCY30_N.B",
)


class _FakeHttpClient:
    def __init__(
        self,
        response: bytes | None = None,
        *,
        fail_at: int | None = None,
        missing_calendar_year: int | None = None,
    ) -> None:
        self.response = response
        self.fail_at = fail_at
        self.missing_calendar_year = missing_calendar_year
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: object) -> SimpleNamespace:
        call = {"url": url, **kwargs}
        self.calls.append(call)
        if self.fail_at == len(self.calls):
            raise RuntimeError("synthetic transport failure")
        if self.response is not None:
            return SimpleNamespace(content=self.response, status_code=200)
        if url == FOMC_CALENDAR_URL:
            year = date.today().year
            body = _calendar_html(year, missing_year=self.missing_calendar_year)
        else:
            year_text = url.rsplit("fomchistorical", maxsplit=1)[1].removesuffix(".htm")
            body = _historical_html(int(year_text))
        return SimpleNamespace(content=body, status_code=200)


def _context(canonical_model: str, *, attempts: int = 1) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="federal_reserve",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline fixture transport only",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"www.federalreserve.gov"},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(request_budget=budget)


def _h15_csv() -> bytes:
    header = ",".join(("Time Period", *_H15_FIELDS))
    first = (
        "2024-05-10",
        "5.01",
        "5.02",
        "5.03",
        "5.04",
        "5.05",
        "5.06",
        "5.07",
        "5.08",
        "5.09",
        "5.10",
        "5.11",
    )
    second = (
        "2024-05-13",
        "ND",
        "5.12",
        "5.13",
        "5.14",
        "5.15",
        "5.16",
        "5.17",
        "5.18",
        "5.19",
        "5.20",
        "5.21",
    )
    return (header + "\n" + ",".join(first) + "\n" + ",".join(second) + "\n").encode()


def _historical_html(year: int) -> bytes:
    html = f"""<!doctype html><html><body>
    <h2>Federal Open Market Committee</h2>
    <h3>{year}</h3>
    <h5>January 28-29 Meeting - {year}</h5>
    <p>Statement:</p><a href="/monetarypolicy/files/monetary{year}0129a1.pdf">PDF</a>
    <p>Minutes (Released February 19, {year}):</p>
    <a href="/monetarypolicy/files/fomcminutes{year}0129.htm">HTML</a> |
    <a href="/monetarypolicy/files/fomcminutes{year}0129.pdf">PDF</a>
    <p>Projection Materials:</p><a href="/monetarypolicy/files/sep{year}0129.pdf">PDF</a>
    <a href="/monetarypolicy/fomchistorical{year}.htm#top">Back to Top</a>
    </body></html>"""
    return html.encode()


def _calendar_html(year: int, *, missing_year: int | None = None) -> bytes:
    year_headings = "".join(
        f"<h4>{calendar_year} FOMC Meetings</h4>"
        for calendar_year in range(2021, year + 1)
        if calendar_year != missing_year
    )
    html = f"""<!doctype html><html><body>
    <h2>Federal Open Market Committee</h2>
    {year_headings}
    <p>January</p><p>27-28</p>
    <p>Statement:</p><a href="/monetarypolicy/files/monetary{year}0128a1.pdf">PDF</a>
    <p>Minutes (Released February 18, {year}):</p>
    <a href="/monetarypolicy/files/fomcminutes{year}0128.htm">HTML</a>
    </body></html>"""
    return html.encode()


def test_yield_curve_selects_latest_observation_not_after_each_date_and_nd_is_null() -> None:
    client = _FakeHttpClient(_h15_csv())
    rows = YieldCurveFetcher(client).fetch(
        ctx=_context("YieldCurve"),
        date=["2024-05-11", date(2024, 5, 13), None],
    )

    assert len(client.calls) == 1
    assert client.calls[0]["url"] == FED_DDP_OUTPUT_URL
    assert client.calls[0]["params"]["rel"] == "H15"
    assert client.calls[0]["params"]["series"] == H15_TREASURY_PACKAGE_ID
    assert client.calls[0]["params"]["type"] == "package"
    assert {row.date for row in rows} == {date(2024, 5, 10), date(2024, 5, 13)}
    assert [row.maturity for row in rows[:11]] == [
        "1-month",
        "3-month",
        "6-month",
        "1-year",
        "2-year",
        "3-year",
        "5-year",
        "7-year",
        "10-year",
        "20-year",
        "30-year",
    ]
    assert rows[11].date == date(2024, 5, 13)
    assert rows[11].rate is None
    assert rows[12].model_dump(mode="json")["rate"] == 5.12


def test_yield_curve_empty_date_list_returns_without_io() -> None:
    client = _FakeHttpClient(_h15_csv())

    assert YieldCurveFetcher(client).fetch(ctx=_context("YieldCurve"), date=[]) == []
    assert client.calls == []


def test_yield_curve_rejects_unknown_fields_and_bad_dates_before_io() -> None:
    client = _FakeHttpClient(_h15_csv())
    fetcher = YieldCurveFetcher(client)

    with pytest.raises(ValidationError):
        fetcher.fetch(yield_curve_type="nominal")
    with pytest.raises(ValidationError):
        fetcher.fetch(date="2024-02-30")
    with pytest.raises(ValidationError):
        fetcher.fetch(date=["2024-01-01", ""])
    assert client.calls == []


def test_yield_curve_rejects_missing_series_and_non_numeric_source_cells() -> None:
    incomplete = b"Time Period,RIFLGFCM01_N.B\n2024-05-10,5.01\n"
    client = _FakeHttpClient(incomplete)
    with pytest.raises(FederalReserveSourceError, match="missing series"):
        YieldCurveFetcher(client).fetch(ctx=_context("YieldCurve"), date="2024-05-10")

    malformed = _h15_csv().replace(b"5.01", b"not-a-rate", 1)
    with pytest.raises(FederalReserveSourceError, match="invalid H.15 rate"):
        YieldCurveFetcher(_FakeHttpClient(malformed)).fetch(
            ctx=_context("YieldCurve"), date="2024-05-10"
        )


def test_fomc_specific_year_maps_public_types_formats_and_explicit_release_date() -> None:
    year = 2020
    client = _FakeHttpClient(_historical_html(year))
    rows = FomcDocumentsFetcher(client).fetch(ctx=_context("FomcDocuments"), year=year)

    assert len(client.calls) == 1
    assert client.calls[0]["url"] == FOMC_HISTORICAL_URL.format(year=year)
    assert len(rows) == 4
    statement = next(row for row in rows if row.doc_type == "monetary_policy")
    minutes = [row for row in rows if row.doc_type == "minutes"]
    projections = next(row for row in rows if row.doc_type == "projections")
    assert statement.date == date(year, 1, 28)
    assert statement.doc_format == "pdf"
    assert len(minutes) == 2
    minutes_html = next(row for row in minutes if row.doc_format == "htm")
    minutes_pdf = next(row for row in minutes if row.doc_format == "pdf")
    assert minutes_html.release_date == date(year, 2, 19)
    assert minutes_pdf.release_date == date(year, 2, 19)
    assert projections.doc_type == "projections"
    assert projections.release_date is None
    assert all("federalreserve.gov/monetarypolicy/files/" in row.url for row in rows)


def test_fomc_calendar_parses_actual_cross_month_headings_and_cross_year_release_date() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "docs/evidence/C100/federal-reserve-clean-documents/"
        / "official-calendar-excerpts-2026-10-12.html"
    ).read_bytes()
    rows = _parse_fomc_page(
        fixture,
        page_year=2024,
        is_calendar=True,
        required_calendar_years=tuple(range(2021, 2027)),
    )

    def minutes_for(url_fragment: str) -> Any:
        return next(row for row in rows if url_fragment in row.url)

    assert minutes_for("fomcminutes20240501.pdf").date == date(2024, 4, 30)
    assert minutes_for("fomcminutes20240501.pdf").release_date == date(2024, 5, 22)
    assert minutes_for("fomcminutes20230201.pdf").date == date(2023, 1, 31)
    assert minutes_for("fomcminutes20231101.pdf").date == date(2023, 10, 31)
    december_minutes = minutes_for("fomcminutes20231213.pdf")
    assert december_minutes.date == date(2023, 12, 12)
    assert december_minutes.release_date == date(2024, 1, 3)


def test_fomc_document_type_filter_does_not_fetch_attachments() -> None:
    client = _FakeHttpClient(_historical_html(2020))
    rows = FomcDocumentsFetcher(client).fetch(
        ctx=_context("FomcDocuments"), year=2020, document_type="minutes"
    )

    assert len(client.calls) == 1
    assert len(rows) == 2
    assert {row.doc_type for row in rows} == {"minutes"}


def test_fomc_rejects_unknown_official_document_label_instead_of_silently_dropping_it() -> None:
    body = _historical_html(2020).replace(
        b"</body>",
        b'<p>New document:</p><a href="/monetarypolicy/files/new2020.pdf">PDF</a></body>',
    )

    with pytest.raises(FederalReserveSourceError, match="unrecognized document label"):
        FomcDocumentsFetcher(_FakeHttpClient(body)).fetch(ctx=_context("FomcDocuments"), year=2020)


def test_fomc_year_none_visits_every_year_page_and_current_calendar() -> None:
    client = _FakeHttpClient()
    rows = FomcDocumentsFetcher(client).fetch(ctx=_context("FomcDocuments", attempts=64))
    current_year = date.today().year
    expected_historical = list(range(1959, min(2020, current_year) + 1))

    assert len(client.calls) == len(expected_historical) + (current_year > 2020)
    assert [call["url"] for call in client.calls[: len(expected_historical)]] == [
        FOMC_HISTORICAL_URL.format(year=year) for year in expected_historical
    ]
    if current_year > 2020:
        assert client.calls[-1]["url"] == FOMC_CALENDAR_URL
    returned_years = {row.date.year for row in rows}
    assert set(expected_historical).issubset(returned_years)
    assert current_year in returned_years


def test_fomc_year_none_rejects_calendar_missing_any_current_year_heading() -> None:
    client = _FakeHttpClient(missing_calendar_year=2022)

    with pytest.raises(FederalReserveSourceError, match="missing year headings: 2022"):
        FomcDocumentsFetcher(client).fetch(ctx=_context("FomcDocuments", attempts=64))

    assert client.calls[-1]["url"] == FOMC_CALENDAR_URL


def test_fomc_full_year_failure_never_returns_partial_results() -> None:
    client = _FakeHttpClient(fail_at=3)
    fetcher = FomcDocumentsFetcher(client)

    with pytest.raises(RuntimeError, match="synthetic transport failure"):
        fetcher.fetch(ctx=_context("FomcDocuments", attempts=64))
    assert len(client.calls) == 3


def test_fomc_invalid_query_is_rejected_before_io() -> None:
    client = _FakeHttpClient(_historical_html(2020))
    fetcher = FomcDocumentsFetcher(client)

    with pytest.raises(ValidationError):
        fetcher.fetch(year=1958)
    with pytest.raises(ValidationError):
        fetcher.fetch(year=2020, document_type="greenbook")
    assert client.calls == []
