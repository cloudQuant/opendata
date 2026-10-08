"""Offline GovHTTP tests for the controlled ECB reference-rate Fetcher.

The payloads are synthetic SDMX 2.1 GenericData fixtures, not ECB responses.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
import requests
from pydantic import ValidationError

import opendata.data.http_client as http_client_module
import opendata.data.providers.ecb.models._reference_client as reference_client
from opendata.data.http_client import GovernedHttpClient
from opendata.data.protocol import FetchContext
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._reference_client import fetch_reference_rates
from opendata.data.providers.ecb.models.reference_rates import (
    EcbCurrencyReferenceRatesFetcher,
    EcbCurrencyReferenceRatesQuery,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.providers.ecb.models._reference_rates import RawReferenceRateRecord


_HOST = "data-api.ecb.europa.eu"
_CANONICAL_MODEL = "CurrencyReferenceRates"
_MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
_COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


class RecordingSession(requests.Session):
    """Real requests.Session surface that records governed sends without sockets."""

    def __init__(self, outcomes: Sequence[requests.Response | BaseException]) -> None:
        super().__init__()
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        with self._lock:
            self.calls.append((method, url, dict(kwargs)))
            outcome = self._outcomes.popleft() if self._outcomes else _response(b"")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(body: bytes, *, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/vnd.sdmx.genericdata+xml;version=2.1"
    return response


def _observation(value: str, *, day: str = "2026-10-08") -> str:
    return (
        f'<generic:Obs><generic:ObsDimension id="TIME_PERIOD" value="{day}"/>'
        f'<generic:ObsValue id="OBS_VALUE" value="{value}"/>'
        '<generic:Attributes><generic:Value id="OBS_STATUS" value="A"/>'
        '<generic:Value id="UNKNOWN_OBSERVATION" value="kept raw"/>'
        "</generic:Attributes></generic:Obs>"
    )


def _series(currency: str, *observations: str) -> str:
    return (
        "<generic:Series><generic:SeriesKey>"
        '<generic:Value id="FREQ" value="D"/>'
        f'<generic:Value id="CURRENCY" value="{currency}"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="EXR_SUFFIX" value="A"/>'
        "</generic:SeriesKey>"
        '<generic:Attributes><generic:Value id="SERIES_NOTE" value="raw series"/>'
        "</generic:Attributes>" + "".join(observations) + "</generic:Series>"
    )


def _body(*series: str) -> bytes:
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData
  xmlns:message="{_MESSAGE_NS}"
  xmlns:common="{_COMMON_NS}"
  xmlns:generic="{_GENERIC_NS}"
  xmlns:xsi="{_XSI_NS}"
  xsi:schemaLocation="{_MESSAGE_NS} SDMXMessage.xsd {_GENERIC_NS} SDMXDataGeneric.xsd">
  <message:Header>
    <message:ID>synthetic-fetcher-test</message:ID>
    <message:Test>false</message:Test>
    <message:Prepared>2026-10-08T00:00:00Z</message:Prepared>
    <message:Sender id="SYNTHETIC"/>
    <message:Structure structureID="ECB_REF" dimensionAtObservation="TIME_PERIOD">
      <common:Structure>
        <Ref agencyID="ECB" id="EXR" version="1.0"
             class="DataStructure" package="datastructure"/>
      </common:Structure>
    </message:Structure>
  </message:Header>
  <message:DataSet structureRef="ECB_REF">
    <generic:Attributes>
      <generic:Value id="UNKNOWN_DATASET" value="preserved dataset value"/>
    </generic:Attributes>
    <generic:Group type="EXR_GROUP">
      <generic:GroupKey><generic:Value id="FREQ" value="D"/></generic:GroupKey>
      <generic:Attributes>
        <generic:Value id="COMMENT" value="preserved group value"/>
      </generic:Attributes>
    </generic:Group>
    {"".join(series)}
  </message:DataSet>
</message:GenericData>'''
    return xml.encode("utf-8")


def _grant(
    *,
    source: str = "ecb",
    canonical_model: str = _CANONICAL_MODEL,
    operation: RequestOperation = RequestOperation.QUERY,
    allowed_hosts: frozenset[str] = frozenset({_HOST}),
    expires_at: datetime | None = None,
    conditions: tuple[str, ...] = (),
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=canonical_model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:synthetic-ecb-payload-only",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts=allowed_hosts,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
        conditions=conditions,
    )


def _context(
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    grant: RequestGrant | None = None,
    timeout: float | None = 2.0,
) -> FetchContext:
    effective_grant = grant or _grant(operation=operation)
    budget = RequestBudget(
        task_attempts=2,
        source_attempts=2,
        grants=(effective_grant,),
    )
    return FetchContext(timeout=timeout, request_budget=budget, operation=operation)


@pytest.fixture(autouse=True)
def disable_process_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transport assertions isolated from configured response caching."""
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *outcomes: requests.Response | BaseException,
) -> tuple[GovernedHttpClient, RecordingSession]:
    session = RecordingSession(outcomes)
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr(reference_client, "get_shared_http_client", lambda: client)
    return client, session


def test_query_is_strict_and_json_roundtrips_canonical_dates() -> None:
    query = EcbCurrencyReferenceRatesQuery.model_validate(
        {
            "quote_currencies": ["USD", "X-LOCAL_1"],
            "start_date": date(2026, 10, 8),
            "end_date": "2026-10-09",
        }
    )
    assert query.quote_currencies == ("USD", "X-LOCAL_1")
    assert query.start_date == date(2026, 10, 8)
    assert query.end_date == date(2026, 10, 9)
    assert query.source == "auto"
    assert query.market is None
    assert query.symbol is None
    assert query.max_records == 10_000
    assert EcbCurrencyReferenceRatesQuery.model_validate_json(query.model_dump_json()) == query

    max_currencies = [f"X{index}" for index in range(32)]
    assert (
        len(EcbCurrencyReferenceRatesQuery(quote_currencies=max_currencies).quote_currencies) == 32
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"quote_currencies": "USD"},
        {"quote_currencies": []},
        {"quote_currencies": ["USD", "USD"]},
        {"quote_currencies": ["usd"]},
        {"quote_currencies": ["D.USD"]},
        {"quote_currencies": ["USD/JPY"]},
        {"quote_currencies": ["USD+JPY"]},
        {"quote_currencies": ["http://example.test"]},
        {"quote_currencies": ["USD\n"]},
        {"quote_currencies": ["A" * 33]},
        {"quote_currencies": [f"X{index}" for index in range(33)]},
        {"quote_currencies": [42]},
        {"start_date": datetime(2026, 10, 8)},
        {"start_date": 20261008},
        {"start_date": True},
        {"start_date": "2026-10-8"},
        {"start_date": "2026-10-08T00:00:00"},
        {"start_date": "2026-10-09", "end_date": "2026-10-08"},
        {"source": "fred"},
        {"market": "us"},
        {"symbol": "USD"},
        {"max_records": True},
        {"max_records": 0},
        {"max_records": 10_001},
        {"max_records": "10"},
        {"includeHistory": "true"},
        {"as_of": "2020-01-01"},
        {"request_budget": object()},
        {"http_client": object()},
        {"max_output_bytes": 99},
    ],
)
def test_invalid_query_shapes_fail_before_transport(
    overrides: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_body(_series("USD", _observation("1")))))
    kwargs: dict[str, object] = {"quote_currencies": ["USD"], **overrides}

    with pytest.raises(ValidationError):
        EcbCurrencyReferenceRatesFetcher().fetch(**kwargs)

    assert session.calls == []


def test_governed_fetch_uses_one_exact_scoped_genericdata_request_and_captures_raw_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body(
        _series("USD", _observation("1.234500")),
        _series("JPY", _observation("160.125")),
    )
    client, session = _install_client(monkeypatch, _response(body))
    context = _context(timeout=3.5)

    def capture(
        raw: tuple[RawReferenceRateRecord, ...],
        params: EcbCurrencyReferenceRatesQuery,
    ) -> tuple[tuple[RawReferenceRateRecord, ...], dict[str, object]]:
        return raw, params.model_dump(mode="json")

    captured = asyncio.run(
        EcbCurrencyReferenceRatesFetcher().fetch_captured_async(
            ctx=context,
            capture=capture,
            quote_currencies=["USD", "JPY"],
            start_date="2026-10-08",
            end_date="2026-10-08",
        )
    )

    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD+JPY.EUR.SP00.A")
    assert kwargs["params"] == {
        "format": "genericdata",
        "detail": "full",
        "includeHistory": "false",
        "startPeriod": "2026-10-08",
        "endPeriod": "2026-10-08",
    }
    assert kwargs["headers"]["Accept"] == "application/vnd.sdmx.genericdata+xml;version=2.1"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 3.5
    assert client._session_override is session
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 1

    raw_rows, dumped_query = captured.capture
    assert dumped_query["quote_currencies"] == ["USD", "JPY"]
    assert [row["series_key"] for row in raw_rows] == [
        "D.USD.EUR.SP00.A",
        "D.JPY.EUR.SP00.A",
    ]
    assert raw_rows[0]["value"] == "1.234500"
    assert raw_rows[0]["dataset_attributes"] == {"UNKNOWN_DATASET": "preserved dataset value"}
    assert raw_rows[0]["series_attributes"] == {"SERIES_NOTE": "raw series"}
    assert raw_rows[0]["observation_attributes"] == {
        "OBS_STATUS": "A",
        "UNKNOWN_OBSERVATION": "kept raw",
    }
    assert raw_rows[0]["group_context"] == [
        {
            "group_type": "EXR_GROUP",
            "key": {"FREQ": "D"},
            "attributes": {"COMMENT": "preserved group value"},
        }
    ]
    assert [row.value for row in captured.results] == [1.2345, 160.125]
    assert [row.source_value for row in captured.results] == ["1.234500", "160.125"]


def test_store_operation_uses_matching_store_grant_without_query_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session = _install_client(
        monkeypatch, _response(_body(_series("USD", _observation("1"))))
    )
    store_grant = _grant(operation=RequestOperation.STORE)
    context = _context(operation=RequestOperation.STORE, grant=store_grant)

    rows = EcbCurrencyReferenceRatesFetcher().fetch(
        ctx=context,
        quote_currencies=["USD"],
    )

    assert len(rows) == 1
    assert len(session.calls) == 1
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 1
    assert client._session_override is session


def test_missing_scope_rejects_direct_client_helper_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_body(_series("USD", _observation("1")))))
    query = EcbCurrencyReferenceRatesQuery(quote_currencies=["USD"])

    with pytest.raises(RequestAuthorizationError, match="authorized Fetcher execution scope"):
        fetch_reference_rates(query)

    assert session.calls == []


def test_direct_helper_rejects_wrong_active_identity_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_body(_series("USD", _observation("1")))))
    query = EcbCurrencyReferenceRatesQuery(quote_currencies=["USD"])
    budget = RequestBudget()

    with (
        request_execution_scope(
            source="fred",
            canonical_model=_CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError, match="exact source and canonical model"),
    ):
        fetch_reference_rates(query)

    assert session.calls == []


def test_zero_and_nonmatching_grants_fail_before_any_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_body(_series("USD", _observation("1")))))
    fetcher = EcbCurrencyReferenceRatesFetcher()

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(quote_currencies=["USD"])
    assert session.calls == []

    cases = [
        (_grant(operation=RequestOperation.STORE), RequestOperation.QUERY),
        (_grant(canonical_model="OtherModel"), RequestOperation.QUERY),
        (_grant(source="fred"), RequestOperation.QUERY),
        (
            _grant(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            RequestOperation.QUERY,
        ),
        (_grant(conditions=("rights evidence pending",)), RequestOperation.QUERY),
        (_grant(allowed_hosts=frozenset({"other.example.test"})), RequestOperation.QUERY),
    ]
    for grant, operation in cases:
        with pytest.raises(RequestAuthorizationError):
            fetcher.fetch(
                ctx=_context(operation=operation, grant=grant),
                quote_currencies=["USD"],
            )
        assert session.calls == []


def test_unrequested_currency_and_out_of_window_rows_fail_without_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(
        monkeypatch,
        _response(_body(_series("JPY", _observation("160")))),
        _response(_body(_series("USD", _observation("1", day="2026-10-09")))),
    )
    fetcher = EcbCurrencyReferenceRatesFetcher()
    context = _context()

    with pytest.raises(EcbProviderError) as currency_error:
        fetcher.fetch(ctx=context, quote_currencies=["USD"])
    assert currency_error.value.code == "ECB_BAD_OBSERVATION"

    with pytest.raises(EcbProviderError) as date_error:
        fetcher.fetch(
            ctx=context,
            quote_currencies=["USD"],
            start_date="2026-10-08",
            end_date="2026-10-08",
        )
    assert date_error.value.code == "ECB_BAD_OBSERVATION"
    assert len(session.calls) == 2


def test_missing_requested_series_does_not_get_filled_or_treated_as_proven_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, _response(_body(_series("USD", _observation("1")))))
    rows = EcbCurrencyReferenceRatesFetcher().fetch(
        ctx=_context(),
        quote_currencies=["USD", "JPY"],
    )
    assert len(rows) == 1
    assert rows[0].quote_currency == "USD"


def test_bad_last_row_and_record_cap_fail_closed_with_fixed_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_last_row = _body(
        _series(
            "USD",
            _observation("1.0", day="2026-10-08"),
            _observation("NaN", day="2026-10-09"),
        )
    )
    two_rows = _body(
        _series(
            "USD",
            _observation("1.0", day="2026-10-08"),
            _observation("2.0", day="2026-10-09"),
        )
    )
    _, session = _install_client(monkeypatch, _response(bad_last_row), _response(two_rows))
    fetcher = EcbCurrencyReferenceRatesFetcher()

    with pytest.raises(EcbProviderError) as malformed:
        fetcher.fetch(ctx=_context(), quote_currencies=["USD"])
    assert malformed.value.code == "ECB_BAD_RESPONSE"
    assert "NaN" not in str(malformed.value)

    with pytest.raises(EcbProviderError) as over_limit:
        fetcher.fetch(ctx=_context(), quote_currencies=["USD"], max_records=1)
    assert over_limit.value.code == "ECB_BAD_RESPONSE"
    assert len(session.calls) == 2


def test_http_and_body_errors_do_not_echo_url_query_values_or_response_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(
        monkeypatch,
        _response(b"private-response-marker", status=400),
        _response(b"private-response-marker"),
    )
    fetcher = EcbCurrencyReferenceRatesFetcher()

    with pytest.raises(EcbProviderError) as http_error:
        fetcher.fetch(ctx=_context(), quote_currencies=["USD"])
    assert http_error.value.code == "ECB_HTTP_ERROR"
    assert http_error.value.status == 400
    assert http_error.value.url is None
    assert "USD" not in str(http_error.value)
    assert "private-response-marker" not in str(http_error.value)

    with pytest.raises(EcbProviderError) as body_error:
        fetcher.fetch(ctx=_context(), quote_currencies=["USD"])
    assert body_error.value.code == "ECB_BAD_RESPONSE"
    assert body_error.value.url is None
    assert "USD" not in str(body_error.value)
    assert "private-response-marker" not in str(body_error.value)
    assert len(session.calls) == 2
