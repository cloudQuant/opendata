"""Offline fixtures for the SEC filing and document fetcher shard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers.sec.models import _common as sec_common
from opendata.data.providers.sec.models import iter2_documents as docs
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

FIXTURES = Path(__file__).parent.parent / "docs/evidence/C100/sec-documents/fixtures"
ALL_HOSTS = {"www.sec.gov", "data.sec.gov", "xbrl.sec.gov"}


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _json_fixture(name: str) -> dict[str, Any]:
    return json.loads(_fixture(name))


def _context(model: str, attempts: int = 8) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="sec",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="fixture:offline-only:sec-documents",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=ALL_HOSTS,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


def _queue_json(monkeypatch: pytest.MonkeyPatch, *items: object) -> list[str]:
    queue = list(items)
    requested: list[str] = []

    def fetch_json(ctx: FetchContext, url: str) -> object:
        assert ctx.timeout == 5.0
        requested.append(url)
        if not queue:
            raise AssertionError(f"unexpected SEC JSON request: {url}")
        item = queue.pop(0)
        if isinstance(item, bytes):
            return json.loads(item)
        return item

    monkeypatch.setattr(docs, "fetch_json", fetch_json)
    return requested


def _queue_bytes(monkeypatch: pytest.MonkeyPatch, *items: bytes) -> list[str]:
    queue = list(items)
    requested: list[str] = []

    def fetch_bytes(
        ctx: FetchContext, url: str, *, max_bytes: int = docs.MAX_TEXT_RESPONSE_BYTES
    ) -> bytes:
        assert ctx.timeout == 5.0
        assert len(queue) > 0, f"unexpected SEC document request: {url}"
        requested.append(url)
        body = queue.pop(0)
        if len(body) > max_bytes:
            raise sec_common.SecProviderError("SEC_RESPONSE_TOO_LARGE")
        return body

    monkeypatch.setattr(docs, "fetch_bytes", fetch_bytes)
    return requested


def _submissions(name: str) -> dict[str, Any]:
    return _json_fixture(name)


@pytest.mark.parametrize(
    ("fetcher", "query", "inherited_field"),
    (
        (
            docs.CompanyFilingsFetcher,
            {
                "symbol": "AAPL",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
            None,
        ),
        (
            docs.SecFilingFetcher,
            {
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-20251227.htm",
            },
            "symbol",
        ),
        (
            docs.SecHtmFileFetcher,
            {
                "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm",
            },
            "symbol",
        ),
        (docs.RssLitigationFetcher, {"limit": 5}, "start_date"),
        (docs.SchemaFilesFetcher, {"taxonomy": "dei", "year": 2026}, "start_date"),
        (
            docs.LatestFinancialReportsFetcher,
            {"filing_date": "2026-10-12", "form": "10-K"},
            "start_date",
        ),
        (
            docs.ManagementDiscussionAnalysisFetcher,
            {"symbol": "AAPL", "year": 2026, "period": "Q1"},
            "start_date",
        ),
        (
            docs.Form13FHRFetcher,
            {"cik": "0000320193", "year": 2026, "quarter": 1},
            "start_date",
        ),
        (docs.NportDisclosureFetcher, {"cik": "0000320193"}, "start_date"),
    ),
)
def test_document_query_allowlists_keep_inherited_fields_closed(
    fetcher: Any,
    query: dict[str, object],
    inherited_field: str | None,
) -> None:
    """Accept service identity while rejecting unlisted inherited query fields."""
    instance = fetcher()
    validated = instance.transform_query(source="sec", market="us", **query)
    assert validated.source == "sec"
    assert validated.market == "us"

    with pytest.raises(ValidationError) as unknown:
        instance.transform_query(source="sec", market="us", **query, not_a_query_field="value")
    assert [(error["type"], error["loc"]) for error in unknown.value.errors()] == [
        ("extra_forbidden", ("not_a_query_field",))
    ]

    if inherited_field is None:
        return
    with pytest.raises(ValidationError) as inherited:
        instance.transform_query(
            source="sec", market="us", **query, **{inherited_field: "rejected"}
        )
    assert [(error["type"], error["loc"]) for error in inherited.value.errors()] == [
        ("extra_forbidden", (inherited_field,))
    ]


def test_company_filings_positive_and_malformed_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _queue_json(monkeypatch, _submissions("company-filings/positive.json"))
    result = docs.CompanyFilingsFetcher().fetch(ctx=_context("CompanyFilings"), cik="0000320193")
    assert len(result) == 1
    filing = result[0]
    assert filing.filing_date.isoformat() == "2026-02-01"
    assert filing.report_date.isoformat() == "2025-12-27"
    assert filing.report_url.endswith("/aapl-20251227.htm")
    assert len(requested) == 1

    _queue_json(monkeypatch, _submissions("company-filings/negative.json"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_SUBMISSIONS_BAD_SHAPE"):
        docs.CompanyFilingsFetcher().fetch(ctx=_context("CompanyFilings"), cik="0000320193")


def test_filings_positive_and_malformed_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue_json(monkeypatch, _submissions("filings/positive.json"))
    result = docs.FilingsFetcher().fetch(ctx=_context("Filings"), cik="0000320193", form="10-k")
    assert len(result) == 1
    assert result[0].report_type == "10-K"
    assert result[0].accession_number == "0000320193-26-000001"

    _queue_json(monkeypatch, _submissions("filings/negative.json"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_SUBMISSIONS_BAD_SHAPE"):
        docs.FilingsFetcher().fetch(ctx=_context("Filings"), cik="0000320193")


def test_sec_filing_positive_and_unsafe_index_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue_json(
        monkeypatch,
        _submissions("sec-filing/positive-submissions.json"),
        _json_fixture("sec-filing/positive-index.json"),
    )
    result = docs.SecFilingFetcher().fetch(
        ctx=_context("SecFiling"),
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/aapl-20251227.htm",
    )
    filing = result[0]
    assert filing.cik == "0000320193"
    assert filing.accession_number == "0000320193-26-000001"
    assert filing.period_ending.isoformat() == "2025-12-27"
    assert filing.documents[0].url.endswith("/aapl-20251227.htm")

    _queue_json(
        monkeypatch,
        _submissions("sec-filing/positive-submissions.json"),
        _json_fixture("sec-filing/negative-index.json"),
    )
    with pytest.raises(sec_common.SecProviderError, match="SEC_FILING_INDEX_BAD_NAME"):
        docs.SecFilingFetcher().fetch(
            ctx=_context("SecFiling"),
            cik="0000320193",
            accession_number="0000320193-26-000001",
        )


def test_sec_html_file_positive_and_empty_document_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _queue_bytes(monkeypatch, _fixture("sec-htm-file/positive.html"))
    result = docs.SecHtmFileFetcher().fetch(
        ctx=_context("SecHtmFile"),
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm",
    )
    assert "Revenue" in result[0].content
    assert len(requested) == 1

    _queue_bytes(monkeypatch, _fixture("sec-htm-file/negative.html"))
    with pytest.raises(ValidationError):
        docs.SecHtmFileFetcher().fetch(
            ctx=_context("SecHtmFile"),
            url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm",
        )


def test_rss_litigation_positive_and_dtd_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue_bytes(monkeypatch, _fixture("rss-litigation/positive.xml"))
    result = docs.RssLitigationFetcher().fetch(ctx=_context("RssLitigation"), limit=5)
    assert len(result) == 1
    assert result[0].title == "SEC v. Example Corp."
    assert result[0].published.year == 2026
    assert result[0].link.startswith("https://www.sec.gov/")

    _queue_bytes(monkeypatch, _fixture("rss-litigation/negative.xml"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_XML_DTD_FORBIDDEN"):
        docs.RssLitigationFetcher().fetch(ctx=_context("RssLitigation"))


def test_schema_files_positive_and_dtd_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue_bytes(
        monkeypatch,
        _fixture("schema-files/positive.xsd"),
        _fixture("schema-files/positive-labels.xml"),
    )
    result = docs.SchemaFilesFetcher().fetch(ctx=_context("SchemaFiles"), taxonomy="dei", year=2026)
    assert len(result) == 1
    assert result[0].name == "EntityRegistrantName"
    assert result[0].label == "Entity Registrant Name"
    assert result[0].period_type == "duration"

    _queue_bytes(monkeypatch, _fixture("schema-files/negative.xsd"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_XML_DTD_FORBIDDEN"):
        docs.SchemaFilesFetcher().fetch(ctx=_context("SchemaFiles"), taxonomy="dei", year=2026)


def test_schema_file_period_and_balance_literals_are_validated_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_schema = b"""<?xml version="1.0" encoding="UTF-8"?>
    <schema xmlns="http://www.w3.org/2001/XMLSchema">
      <element name="DurationCredit" periodType="duration" balance="credit" />
      <element name="InstantDebit" periodType="instant" balance="debit" />
      <element name="InstantWithoutBalance" periodType="instant" />
    </schema>"""
    _queue_bytes(monkeypatch, valid_schema)
    records = docs.SchemaFilesFetcher().fetch(
        ctx=_context("SchemaFiles"), taxonomy="dei", year=2026
    )
    assert {record.name: (record.period_type, record.balance_type) for record in records} == {
        "DurationCredit": ("duration", "credit"),
        "InstantDebit": ("instant", "debit"),
        "InstantWithoutBalance": ("instant", None),
    }

    invalid_cases = (
        (
            b'<schema xmlns="http://www.w3.org/2001/XMLSchema">'
            b'<element name="BadPeriod" periodType="forever" />'
            b"</schema>",
            "SEC_SCHEMA_BAD_PERIOD_TYPE",
        ),
        (
            b'<schema xmlns="http://www.w3.org/2001/XMLSchema">'
            b'<element name="BadBalance" periodType="duration" balance="sideways" />'
            b"</schema>",
            "SEC_SCHEMA_BAD_BALANCE_TYPE",
        ),
    )
    for body, error_code in invalid_cases:
        _queue_bytes(monkeypatch, body)
        with pytest.raises(sec_common.SecProviderError) as error:
            docs.SchemaFilesFetcher().fetch(ctx=_context("SchemaFiles"), taxonomy="dei", year=2026)
        assert error.value.code == error_code


def test_latest_financial_reports_positive_and_bad_filename_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue_bytes(monkeypatch, _fixture("latest-financial-reports/positive.idx"))
    result = docs.LatestFinancialReportsFetcher().fetch(
        ctx=_context("LatestFinancialReports"), filing_date="2026-10-12", limit=10
    )
    assert len(result) == 2
    assert result[0].report_type == "10-K"
    assert result[0].filing_date.isoformat() == "2026-10-12"
    assert result[0].period_ending is None
    assert result[1].report_type == "8-K"
    assert result[1].cik == "0000789019"

    _queue_bytes(monkeypatch, _fixture("latest-financial-reports/negative.idx"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_DAILY_INDEX_MISSING_HEADER"):
        docs.LatestFinancialReportsFetcher().fetch(
            ctx=_context("LatestFinancialReports"), filing_date="2026-10-12"
        )


def test_mda_positive_and_missing_section_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docs,
        "company_ticker_rows",
        lambda ctx: (
            {"cik": "0000320193", "ticker": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ"},
        ),
    )
    _queue_json(monkeypatch, _submissions("mda/positive-submissions.json"))
    _queue_bytes(monkeypatch, _fixture("mda/positive.html"))
    result = docs.ManagementDiscussionAnalysisFetcher().fetch(
        ctx=_context("ManagementDiscussionAnalysis"), symbol="AAPL", year=2026, period="Q1"
    )
    assert len(result[0].content) > 40
    assert result[0].period_ending.isoformat() == "2026-03-28"

    _queue_json(monkeypatch, _submissions("mda/negative-submissions.json"))
    _queue_bytes(monkeypatch, _fixture("mda/negative.html"))
    with pytest.raises(sec_common.SecProviderError, match="SEC_MDA_SECTION_NOT_FOUND"):
        docs.ManagementDiscussionAnalysisFetcher().fetch(
            ctx=_context("ManagementDiscussionAnalysis"), symbol="AAPL", year=2026, period="Q1"
        )


def test_form_13f_hr_positive_and_missing_fields_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    for submission, table in (
        ("form-13f-hr/positive-submissions.json", "form-13f-hr/positive.xml"),
        ("form-13f-hr/negative-submissions.json", "form-13f-hr/negative.xml"),
    ):
        _queue_json(
            monkeypatch,
            _submissions(submission),
            {
                "directory": {
                    "item": [{"name": "infotable.xml", "type": "INFORMATION TABLE", "size": "512"}]
                }
            },
        )
        _queue_bytes(monkeypatch, _fixture(table))
        if "negative" in submission:
            with pytest.raises(sec_common.SecProviderError, match="SEC_13F_ROW_MISSING_FIELD"):
                docs.Form13FHRFetcher().fetch(
                    ctx=_context("Form13FHR"), cik="0000320193", year=2026, quarter=1
                )
        else:
            result = docs.Form13FHRFetcher().fetch(
                ctx=_context("Form13FHR"), cik="0000320193", year=2026, quarter=1
            )
            assert len(result) == 1
            assert result[0].issuer == "Example Incorporated"
            assert result[0].period_ending.isoformat() == "2026-03-31"
            assert result[0].filing_date.isoformat() == "2026-05-15"
            assert result[0].manager_cik == "0000320193"
            assert result[0].manager_name == "Example Adviser LLC"
            assert result[0].accession_number == "0000320193-26-000004"
            assert result[0].other_manager is None
            assert result[0].value == 1200
            assert result[0].weight == 1.0


def test_form_13f_hr_legacy_value_uses_filing_date_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    _queue_json(
        monkeypatch,
        _submissions("form-13f-hr/pre-2023-submissions.json"),
        {
            "directory": {
                "item": [{"name": "infotable.xml", "type": "INFORMATION TABLE", "size": "512"}]
            }
        },
    )
    _queue_bytes(monkeypatch, _fixture("form-13f-hr/pre-2023.xml"))
    result = docs.Form13FHRFetcher().fetch(
        ctx=_context("Form13FHR"), cik="0000320193", year=2022, quarter=1
    )
    assert len(result) == 1
    assert result[0].filing_date.isoformat() == "2022-05-15"
    assert result[0].period_ending.isoformat() == "2022-03-31"
    assert result[0].value == 1_200_000


def test_form_13f_hr_preserves_other_manager_row_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue_json(
        monkeypatch,
        _submissions("form-13f-hr/positive-submissions.json"),
        {
            "directory": {
                "item": [{"name": "infotable.xml", "type": "INFORMATION TABLE", "size": "512"}]
            }
        },
    )
    _queue_bytes(monkeypatch, _fixture("form-13f-hr/other-manager-rows.xml"))
    result = docs.Form13FHRFetcher().fetch(
        ctx=_context("Form13FHR"), cik="0000320193", year=2026, quarter=1
    )
    assert [row.other_manager for row in result] == ["1", "2"]
    assert (
        len(
            {
                (
                    row.manager_cik,
                    row.accession_number,
                    row.period_ending,
                    row.cusip,
                    row.other_manager,
                )
                for row in result
            }
        )
        == 2
    )


def test_nport_disclosure_positive_and_missing_holdings_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for submission, body in (
        ("nport-disclosure/positive-submissions.json", "nport-disclosure/positive.xml"),
        ("nport-disclosure/negative-submissions.json", "nport-disclosure/negative.xml"),
    ):
        _queue_json(
            monkeypatch,
            _submissions(submission),
            {"directory": {"item": [{"name": "primary.xml", "type": "NPORT-P", "size": "1024"}]}},
        )
        _queue_bytes(monkeypatch, _fixture(body))
        if "negative" in submission:
            with pytest.raises(sec_common.SecProviderError, match="SEC_NPORT_HOLDINGS_MISSING"):
                docs.NportDisclosureFetcher().fetch(
                    ctx=_context("NportDisclosure"), cik="0000320193"
                )
        else:
            result = docs.NportDisclosureFetcher().fetch(
                ctx=_context("NportDisclosure"), cik="0000320193"
            )
            assert len(result) == 1
            assert result[0].symbol is None
            assert result[0].asset_symbol == "EXM"
            assert result[0].name == "Example Issuer"
            assert result[0].title == "Common Stock"
            assert result[0].value == 5000.0
            assert result[0].fund_cik == "0000320193"
            assert result[0].fund_name == "Example Fund"
            assert result[0].accession_number == "0000320193-26-000005"
            assert result[0].filing_date.isoformat() == "2026-05-01"
            assert result[0].period_ending.isoformat() == "2026-03-31"
            assert result[0].acceptance_datetime is None
            assert result[0].as_of is None
            assert result[0].maturity_date.isoformat() == "2028-01-15"


def test_nport_disclosure_preserves_other_identifier_row_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue_json(
        monkeypatch,
        _submissions("nport-disclosure/positive-submissions.json"),
        {"directory": {"item": [{"name": "primary.xml", "type": "NPORT-P", "size": "1024"}]}},
    )
    _queue_bytes(monkeypatch, _fixture("nport-disclosure/other-id-rows.xml"))
    result = docs.NportDisclosureFetcher().fetch(ctx=_context("NportDisclosure"), cik="0000320193")
    assert [row.other_id for row in result] == ["ALTERNATE-A", "ALTERNATE-B"]
    assert [row.asset_symbol for row in result] == ["EXM", "EXM"]
    assert (
        len(
            {
                (
                    row.fund_cik,
                    row.accession_number,
                    row.period_ending,
                    row.cusip,
                    row.isin,
                    row.other_id,
                )
                for row in result
            }
        )
        == 2
    )


def test_nport_disclosure_acceptance_datetime_is_not_report_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _queue_json(
        monkeypatch,
        _submissions("nport-disclosure/positive-acceptance-submissions.json"),
        {"directory": {"item": [{"name": "primary.xml", "type": "NPORT-P", "size": "1024"}]}},
    )
    _queue_bytes(monkeypatch, _fixture("nport-disclosure/positive.xml"))
    result = docs.NportDisclosureFetcher().fetch(ctx=_context("NportDisclosure"), cik="0000320193")
    assert len(result) == 1
    assert result[0].acceptance_datetime.isoformat() == "2026-05-01T18:30:45+00:00"
    assert result[0].as_of.isoformat() == "2026-05-01"
    assert result[0].period_ending.isoformat() == "2026-03-31"


def test_query_validation_rejects_bad_uri_and_date_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(docs, "fetch_json", lambda ctx, url: calls.append(url))
    with pytest.raises(ValidationError):
        docs.SecFilingFetcher().fetch(
            ctx=_context("SecFiling"),
            url="https://example.com/Archives/edgar/data/320193/000032019326000001/report.htm",
        )
    with pytest.raises(ValidationError):
        docs.CompanyFilingsFetcher().fetch(
            ctx=_context("CompanyFilings"),
            cik="0000320193",
            start_date="2026-03-01",
            end_date="2026-02-01",
        )
    assert calls == []


def test_governed_http_preserves_scope_budget_timeout_and_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FixtureSession(requests.Session):
        def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
            calls.append({"method": method, "url": url, **kwargs})
            response = requests.Response()
            response.status_code = 200
            response._content = _fixture("sec-htm-file/positive.html")
            response.url = url
            return response

    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=1,
            rate_limit_per_host=1_000,
            rate_burst=2,
            default_headers={"User-Agent": "opendata-test/1"},
        ),
        session=FixtureSession(),
    )
    monkeypatch.setattr(sec_common, "get_shared_http_client", lambda: client)
    monkeypatch.setenv("SEC_USER_AGENT", "OpenDataTest/1 compliance@example.org")
    context = _context("SecHtmFile", attempts=1)
    result = docs.SecHtmFileFetcher().fetch(
        ctx=context,
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/report.htm",
    )
    assert "Revenue" in result[0].content
    assert len(calls) == 1
    assert calls[0]["timeout"] == 5.0
    assert calls[0].get("source", True)
    assert calls[0]["headers"]["User-Agent"] == "OpenDataTest/1 compliance@example.org"  # type: ignore[index]
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 1
