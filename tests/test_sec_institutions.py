"""Offline contract and exact-route tests for the SEC 13F manager search."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any as AnyType
from unittest.mock import Mock

import pytest
import requests
from pydantic import ValidationError

from opendata.data import http_client as http_client_module
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.sec_institutions import SecInstitutionSearchResult
from opendata.data.protocol import FetchContext
from opendata.data.providers.sec.models import _common as common
from opendata.data.providers.sec.models import iter2_institutions as institutions
from opendata.data.raw_response_cache import RawResponseCache
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

_PAGE_URL = institutions.SEC_13F_DATASETS_PAGE_URL
_ARCHIVE_URL = (
    "https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/"
    "01jun2026-31aug2026_form13f.zip"
)
_SUBMISSION_COLUMNS = (
    "ACCESSION_NUMBER",
    "FILING_DATE",
    "SUBMISSIONTYPE",
    "CIK",
    "PERIODOFREPORT",
)
_COVERPAGE_COLUMNS = (
    "ACCESSION_NUMBER",
    "REPORTCALENDARORQUARTER",
    "FILINGMANAGER_NAME",
)


def _submission(
    accession: str,
    *,
    filing_date: str = "15-AUG-2026",
    cik: str = "1",
    submission_type: str = "13F-HR",
    as_of: str = "30-JUN-2026",
) -> dict[str, str]:
    return {
        "ACCESSION_NUMBER": accession,
        "FILING_DATE": filing_date,
        "SUBMISSIONTYPE": submission_type,
        "CIK": cik,
        "PERIODOFREPORT": as_of,
    }


def _coverpage(
    accession: str,
    name: str,
    *,
    report_period: str = "30-JUN-2026",
) -> dict[str, str]:
    return {
        "ACCESSION_NUMBER": accession,
        "REPORTCALENDARORQUARTER": report_period,
        "FILINGMANAGER_NAME": name,
    }


def _tsv(headers: tuple[str, ...], rows: tuple[dict[str, str], ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=headers,
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _archive(
    *,
    submissions: tuple[dict[str, str], ...] | None = None,
    coverpages: tuple[dict[str, str], ...] | None = None,
    submission_columns: tuple[str, ...] = _SUBMISSION_COLUMNS,
    coverpage_columns: tuple[str, ...] = _COVERPAGE_COLUMNS,
    extras: dict[str, bytes] | None = None,
    include_submission: bool = True,
    include_coverpage: bool = True,
) -> bytes:
    if submissions is None:
        submissions = (_submission("0000000001-26-000001"),)
    if coverpages is None:
        coverpages = (_coverpage("0000000001-26-000001", "Example Manager"),)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_submission:
            archive.writestr("SUBMISSION.tsv", _tsv(submission_columns, submissions))
        if include_coverpage:
            archive.writestr("COVERPAGE.tsv", _tsv(coverpage_columns, coverpages))
        archive.writestr("INFOTABLE.tsv", b"holdings data must not be read")
        for filename, content in (extras or {}).items():
            archive.writestr(filename, content)
    return stream.getvalue()


def _ctx(*, attempts: int = 2) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="sec",
        canonical_model="InstitutionsSearch",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:synthetic SEC 13F response",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"www.sec.gov"},
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


class _FixtureSession(requests.Session):
    """Low-level offline transport installed beneath GovernedHttpClient."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        super().__init__()
        self.routes = routes
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kwargs: AnyType) -> requests.Response:
        del kwargs
        self.calls.append(url)
        status, content = self.routes.get(url, (404, b"not found"))
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.encoding = "utf-8"
        response.headers["content-type"] = "application/zip"
        response._content = content
        response.request = requests.Request(method, url).prepare()
        return response


def _install_routes(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, tuple[int, bytes]],
    *,
    raw_response_cache: RawResponseCache | None = None,
) -> _FixtureSession:
    monkeypatch.setenv(common.SEC_USER_AGENT_ENV, "OpenData tests contact tests@example.com")
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    session = _FixtureSession(routes)
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
        session=session,
        raw_response_cache=raw_response_cache,
    )
    monkeypatch.setattr(common, "get_shared_http_client", lambda: client)
    return session


def _page(*hrefs: str) -> bytes:
    links = "".join(f'<a href="{href}">13F data</a>' for href in hrefs)
    return f"<html><body>{links}</body></html>".encode()


def _batch() -> institutions._DatasetBatch:
    return institutions._DatasetBatch(
        url=_ARCHIVE_URL,
        filename="01jun2026-31aug2026_form13f.zip",
        filing_window_start=datetime(2026, 6, 1).date(),
        filing_window_end=datetime(2026, 8, 31).date(),
    )


def test_public_query_returns_typed_manager_rows_with_stable_latest_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submissions = (
        _submission("0000000001-26-000001", filing_date="10-AUG-2026", cik="1"),
        _submission("0000000001-26-000002", filing_date="20-AUG-2026", cik="1"),
        _submission(
            "0000000001-26-000003",
            filing_date="20-AUG-2026",
            cik="1",
            submission_type="13F-HR/A",
        ),
        _submission("0000000002-26-000001", filing_date="21-AUG-2026", cik="2"),
    )
    coverpages = (
        _coverpage("0000000001-26-000001", "Old Manager"),
        _coverpage("0000000001-26-000002", "Lower Tie"),
        _coverpage("0000000001-26-000003", "Recent Manager"),
        _coverpage("0000000002-26-000001", "   "),
    )
    session = _install_routes(
        monkeypatch,
        {
            _PAGE_URL: (
                200,
                _page(
                    "/files/datastandardsinnovation/data/form-13f-data-sets/"
                    "01mar2026-31may2026_form13f.zip",
                    _ARCHIVE_URL,
                ),
            ),
            _ARCHIVE_URL: (200, _archive(submissions=submissions, coverpages=coverpages)),
        },
    )

    rows = institutions.InstitutionsSearchFetcher().fetch(
        ctx=_ctx(), source="sec", query=None, use_cache=True
    )

    assert len(rows) == 2
    by_cik = {row.cik: row for row in rows}
    recent = by_cik["0000000001"]
    assert recent.name == "Recent Manager"
    assert recent.accession_number == "0000000001-26-000003"
    assert recent.submission_type == "13F-HR/A"
    assert recent.as_of.isoformat() == "2026-06-30"
    assert recent.filing_window_start.isoformat() == "2026-06-01"
    assert recent.filing_window_end.isoformat() == "2026-08-31"
    assert recent.source_batch == "01jun2026-31aug2026_form13f.zip"
    assert by_cik["0000000002"].name is None
    assert set(recent.model_dump()) == set(SecInstitutionSearchResult.model_fields)
    assert len(session.calls) == 2
    assert session.calls == [_PAGE_URL, _ARCHIVE_URL]


def test_query_filters_only_selected_latest_manager_name_or_cik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _archive(
        submissions=(
            _submission("0000000001-26-000001", filing_date="10-AUG-2026", cik="1"),
            _submission("0000000001-26-000002", filing_date="20-AUG-2026", cik="1"),
        ),
        coverpages=(
            _coverpage("0000000001-26-000001", "Old Manager"),
            _coverpage("0000000001-26-000002", "Recent Manager"),
        ),
    )
    _install_routes(
        monkeypatch,
        {_PAGE_URL: (200, _page(_ARCHIVE_URL)), _ARCHIVE_URL: (200, archive)},
    )

    rows = institutions.InstitutionsSearchFetcher().fetch(ctx=_ctx(), source="sec", query="RECENT")

    assert [(row.name, row.cik) for row in rows] == [("Recent Manager", "0000000001")]


def test_institutions_query_allowlist_keeps_inherited_symbol_closed() -> None:
    fetcher = institutions.InstitutionsSearchFetcher()
    validated = fetcher.transform_query(source="sec", market="us", query="manager")
    assert validated.source == "sec"
    assert validated.market == "us"

    for extra in ("not_a_query_field", "symbol"):
        with pytest.raises(ValidationError) as invalid:
            fetcher.transform_query(
                source="sec", market="us", query="manager", **{extra: "rejected"}
            )
        assert [(error["type"], error["loc"]) for error in invalid.value.errors()] == [
            ("extra_forbidden", (extra,))
        ]


@pytest.mark.parametrize("invalid", ("false", "true", 0, 1))
def test_use_cache_requires_a_strict_bool_before_io(
    monkeypatch: pytest.MonkeyPatch,
    invalid: object,
) -> None:
    session = _install_routes(monkeypatch, {})

    with pytest.raises(ValidationError):
        institutions.InstitutionsSearchFetcher().fetch(ctx=_ctx(), source="sec", use_cache=invalid)

    assert session.calls == []


def test_cache_options_preserve_default_transport_calls_and_budget_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_routes(monkeypatch, {})
    fetcher = institutions.InstitutionsSearchFetcher()

    with pytest.raises(ValidationError) as invalid_query:
        fetcher.fetch(ctx=_ctx(), source="sec", limit=10)
    assert invalid_query.value.errors()[0]["type"] == "extra_forbidden"
    with pytest.raises(RequestAttemptLimitError, match="SEC_REQUEST_BUDGET_EXCEEDED") as exhausted:
        fetcher.fetch(ctx=_ctx(attempts=1), source="sec", query="x", use_cache=False)
    assert isinstance(exhausted.value, common.SecProviderError)
    with pytest.raises(common.SecProviderError, match="SEC_13F_TIMEOUT_OUT_OF_RANGE"):
        fetcher.fetch(
            ctx=FetchContext(timeout=61.0, request_budget=_ctx().request_budget),
            source="sec",
            query="x",
            use_cache=False,
        )

    assert session.calls == []


def test_use_cache_false_bypasses_raw_cache_for_page_and_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    cache_get = Mock(wraps=cache.get)
    cache_put = Mock(wraps=cache.put)
    monkeypatch.setattr(cache, "get", cache_get)
    monkeypatch.setattr(cache, "put", cache_put)
    archive = _archive()
    session = _install_routes(
        monkeypatch,
        {
            _PAGE_URL: (200, _page(_ARCHIVE_URL)),
            _ARCHIVE_URL: (200, archive),
        },
        raw_response_cache=cache,
    )

    def unexpected_cache_configuration() -> RawResponseCache | None:
        raise AssertionError("use_cache=False must skip global cache initialization")

    monkeypatch.setattr(
        http_client_module,
        "get_configured_raw_response_cache",
        unexpected_cache_configuration,
    )
    rows = institutions.InstitutionsSearchFetcher().fetch(
        ctx=_ctx(), source="sec", query=None, use_cache=False
    )

    assert len(rows) == 1
    assert session.calls == [_PAGE_URL, _ARCHIVE_URL]
    cache_get.assert_not_called()
    cache_put.assert_not_called()
    assert not cache.raw_dir.exists()


@pytest.mark.parametrize(
    "query_options",
    ({}, {"use_cache": None}, {"use_cache": True}),
    ids=("default", "none-defaults-to-true", "true"),
)
def test_default_and_none_cache_options_preserve_fetch_bytes_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    query_options: dict[str, object],
) -> None:
    responses = [_page(_ARCHIVE_URL), _archive()]
    calls: list[tuple[str, int]] = []

    def legacy_fetch_bytes(
        ctx: FetchContext,
        url: str,
        *,
        max_bytes: int,
        **_kwargs: object,
    ) -> bytes:
        del ctx
        calls.append((url, max_bytes))
        return responses.pop(0)

    monkeypatch.setattr(institutions, "fetch_bytes", legacy_fetch_bytes)

    rows = institutions.InstitutionsSearchFetcher().fetch(
        ctx=_ctx(), source="sec", query=None, **query_options
    )

    assert len(rows) == 1
    assert calls == [
        (_PAGE_URL, institutions.MAX_DATASETS_PAGE_BYTES),
        (_ARCHIVE_URL, institutions.MAX_DATASET_ARCHIVE_BYTES),
    ]


def test_cache_bypass_still_requires_sec_user_agent_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_routes(monkeypatch, {})
    monkeypatch.delenv(common.SEC_USER_AGENT_ENV)

    with pytest.raises(common.SecProviderError, match="SEC_USER_AGENT_MISSING_OR_INVALID"):
        institutions.InstitutionsSearchFetcher().fetch(
            ctx=_ctx(), source="sec", query=None, use_cache=False
        )

    assert session.calls == []


def test_latest_batch_link_is_selected_only_from_sec_safe_paths() -> None:
    page = _page(
        "/files/datastandardsinnovation/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip",
        _ARCHIVE_URL,
        "https://attacker.invalid/files/datastandardsinnovation/data/form-13f-data-sets/"
        "01sep2026-30nov2026_form13f.zip",
    )

    batch = institutions._discover_latest_batch(page)

    assert batch.url == _ARCHIVE_URL
    assert batch.filing_window_start.isoformat() == "2026-06-01"
    assert batch.filing_window_end.isoformat() == "2026-08-31"
    with pytest.raises(common.SecProviderError, match="LATEST_BATCH_NOT_FOUND"):
        institutions._discover_latest_batch(_page("https://sec.gov/unsafe.zip"))


def test_archive_parser_reads_only_submission_and_coverpage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive()
    original_open = zipfile.ZipFile.open
    opened: list[str] = []

    def tracked_open(
        archive: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> AnyType:
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        opened.append(filename)
        if PurePosixPath(filename).name.casefold() == "infotable.tsv":
            raise AssertionError("INFOTABLE is holdings and must not be read")
        return original_open(archive, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", tracked_open)

    parsed = institutions._parse_dataset_archive(payload, _batch())

    assert len(parsed.rows) == 1
    assert {PurePosixPath(name).name.upper() for name in opened} == {
        "SUBMISSION.TSV",
        "COVERPAGE.TSV",
    }


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    (
        ("bad_zip", "SEC_13F_BAD_ARCHIVE"),
        ("missing_column", "SEC_13F_SUBMISSION_BAD_SHAPE"),
        ("duplicate_accession", "SEC_13F_SUBMISSION_DUPLICATE_ACCESSION_CONFLICT"),
        ("missing_join", "SEC_13F_ACCESSION_JOIN_MISMATCH"),
        ("path_traversal", "SEC_13F_ARCHIVE_MEMBER_INVALID"),
        ("too_many_members", "SEC_13F_ARCHIVE_MEMBER_COUNT_INVALID"),
        ("ambiguous_target", "SEC_13F_TARGET_MEMBER_AMBIGUOUS"),
        ("missing_target", "SEC_13F_TARGET_MEMBER_MISSING"),
    ),
    ids=(
        "bad-zip",
        "required-column",
        "duplicate-accession",
        "join-mismatch",
        "path-traversal",
        "member-count",
        "ambiguous-target",
        "missing-target",
    ),
)
def test_malformed_or_conflicting_archives_are_rejected(scenario: str, expected_code: str) -> None:
    if scenario == "bad_zip":
        payload = b"not a zip"
    elif scenario == "missing_column":
        payload = _archive(submission_columns=("ACCESSION_NUMBER", "FILING_DATE"))
    elif scenario == "duplicate_accession":
        payload = _archive(
            submissions=(
                _submission("0000000001-26-000001", cik="1"),
                _submission("0000000001-26-000001", cik="2"),
            )
        )
    elif scenario == "missing_join":
        payload = _archive(
            submissions=(
                _submission("0000000001-26-000001"),
                _submission("0000000001-26-000002"),
            )
        )
    elif scenario == "path_traversal":
        payload = _archive(extras={"../outside.txt": b"not allowed"})
    elif scenario == "too_many_members":
        payload = _archive(extras={f"extra-{index}.txt": b"x" for index in range(15)})
    elif scenario == "ambiguous_target":
        payload = _archive(extras={"submission.tsv": b"duplicate target"})
    else:
        assert scenario == "missing_target"
        payload = _archive(include_coverpage=False)
    with pytest.raises(common.SecProviderError, match=expected_code):
        institutions._parse_dataset_archive(payload, _batch())


def test_archive_size_table_size_ratio_and_member_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive()
    monkeypatch.setattr(institutions, "MAX_DATASET_ARCHIVE_BYTES", len(payload) - 1)
    with pytest.raises(common.SecProviderError, match="SEC_13F_ARCHIVE_BAD_SIZE"):
        institutions._parse_dataset_archive(payload, _batch())

    monkeypatch.setattr(institutions, "MAX_DATASET_ARCHIVE_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(institutions, "MAX_TABLE_BYTES", 10)
    with pytest.raises(common.SecProviderError, match="SEC_13F_TARGET_TABLE_TOO_LARGE"):
        institutions._parse_dataset_archive(_archive(), _batch())

    monkeypatch.setattr(institutions, "MAX_TABLE_BYTES", 64 * 1024 * 1024)
    compressed_payload = _archive(extras={"HOLDINGS.tsv": b"x" * 100_000})
    with pytest.raises(common.SecProviderError, match="SEC_13F_COMPRESSION_RATIO_EXCEEDED"):
        institutions._parse_dataset_archive(compressed_payload, _batch())


def test_typed_contract_normalizes_cik_and_rejects_bad_identity_or_window() -> None:
    fields = {
        "name": "  Example   Manager ",
        "cik": 42,
        "source_batch": "01jun2026-31aug2026_form13f.zip",
        "as_of": "2026-06-30",
        "filing_window_start": "2026-06-01",
        "filing_window_end": "2026-08-31",
        "filing_date": "2026-08-15",
        "submission_type": "13F-HR",
        "accession_number": "0000000001-26-000001",
        "report_calendar_or_quarter": "2026-06-30",
        "submission_line_number": 2,
        "coverpage_line_number": 2,
    }
    row = SecInstitutionSearchResult.model_validate(fields)
    assert row.cik == "0000000042"
    assert row.name == "Example Manager"
    assert row.model_dump(mode="json")["cik"] == "0000000042"

    with pytest.raises(ValidationError):
        SecInstitutionSearchResult.model_validate({**fields, "cik": True})
    with pytest.raises(ValidationError):
        SecInstitutionSearchResult.model_validate({**fields, "filing_window_start": "2026-09-01"})
    with pytest.raises(ValidationError):
        SecInstitutionSearchResult.model_validate(
            {**fields, "accession_number": "not-an-accession"}
        )
