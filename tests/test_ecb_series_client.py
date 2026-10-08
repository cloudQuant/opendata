"""Offline governed-client tests using only synthetic GenericData fixtures."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any

import pytest
import requests

import opendata.data.http_client as http_client_module
import opendata.data.providers.ecb.models._series_client as series_client
import opendata.data.request_budget as request_budget_module
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._series_query import (
    EcbBalanceOfPaymentsQuery,
    EcbYieldCurveQuery,
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

_HOST = "data-api.ecb.europa.eu"
_YC_MODEL = "YieldCurve"
_BPS_MODEL = "BalanceOfPayments"
_MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
_COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_YC_DIMENSIONS = (
    "FREQ",
    "REF_AREA",
    "CURRENCY",
    "PROVIDER_FM",
    "INSTRUMENT_FM",
    "PROVIDER_FM_ID",
    "DATA_TYPE_FM",
)
_BPS_DIMENSIONS = (
    "FREQ",
    "ADJUSTMENT",
    "REF_AREA",
    "COUNTERPART_AREA",
    "REF_SECTOR",
    "COUNTERPART_SECTOR",
    "FLOW_STOCK_ENTRY",
    "ACCOUNTING_ENTRY",
    "INT_ACC_ITEM",
    "FUNCTIONAL_CAT",
    "INSTR_ASSET",
    "MATURITY",
    "UNIT_MEASURE",
    "CURRENCY_DENOM",
    "VALUATION",
    "COMP_METHOD",
    "TYPE_ENTITY",
)
_YC_VALUES = {
    "FREQ": "B",
    "REF_AREA": "U2",
    "CURRENCY": "EUR",
    "PROVIDER_FM": "4F",
    "INSTRUMENT_FM": "G_N_A",
    "PROVIDER_FM_ID": "SV_C_YM",
    "DATA_TYPE_FM": "SR_3M",
}
_BPS_VALUES = {
    "FREQ": "M",
    "ADJUSTMENT": "N",
    "REF_AREA": "I10",
    "COUNTERPART_AREA": "W1",
    "REF_SECTOR": "S121",
    "COUNTERPART_SECTOR": "S1",
    "FLOW_STOCK_ENTRY": "T",
    "ACCOUNTING_ENTRY": "A",
    "INT_ACC_ITEM": "FA",
    "FUNCTIONAL_CAT": "R",
    "INSTR_ASSET": "F",
    "MATURITY": "_Z",
    "UNIT_MEASURE": "EUR",
    "CURRENCY_DENOM": "X1",
    "VALUATION": "_X",
    "COMP_METHOD": "N",
    "TYPE_ENTITY": "ALL",
}


class RecordingSession(requests.Session):
    """Capture actual requests.Session sends without opening sockets."""

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


def _values(dimensions: tuple[str, ...], values: dict[str, str]) -> str:
    return "".join(
        f'<generic:Value id="{dimension}" value="{values[dimension]}"/>' for dimension in dimensions
    )


def _observation(period: str, value: str) -> str:
    return (
        f'<generic:Obs><generic:ObsDimension id="TIME_PERIOD" value="{period}"/>'
        f'<generic:ObsValue id="OBS_VALUE" value="{value}"/>'
        '<generic:Attributes><generic:Value id="OBS_STATUS" value="raw-status"/>'
        '<generic:Value id="UNKNOWN_OBS_ATTRIBUTE" value="kept-observation"/>'
        "</generic:Attributes></generic:Obs>"
    )


def _series(dimensions: tuple[str, ...], values: dict[str, str], observations: list[str]) -> str:
    return (
        "<generic:Series><generic:SeriesKey>"
        f"{_values(dimensions, values)}</generic:SeriesKey>"
        '<generic:Attributes><generic:Value id="UNKNOWN_SERIES" value="kept-series"/>'
        "</generic:Attributes>" + "".join(observations) + "</generic:Series>"
    )


def _body(series: str) -> bytes:
    schema_location = f"{_MESSAGE_NS} SDMXMessage.xsd {_GENERIC_NS} SDMXDataGeneric.xsd"
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData xmlns:message="{_MESSAGE_NS}" xmlns:common="{_COMMON_NS}"
 xmlns:generic="{_GENERIC_NS}" xmlns:xsi="{_XSI_NS}"
 xsi:schemaLocation="{schema_location}">
 <message:Header>
  <message:Structure structureID="SYNTHETIC" dimensionAtObservation="TIME_PERIOD">
   <common:Structure><Ref agencyID="ECB" id="SYNTHETIC" version="1.0"
    class="DataStructure" package="datastructure"/></common:Structure>
  </message:Structure>
 </message:Header>
 <message:DataSet structureRef="SYNTHETIC">
  <generic:Attributes>
   <generic:Value id="UNKNOWN_DATASET" value="kept-dataset"/>
  </generic:Attributes>
  <generic:Group type="SYNTHETIC_GROUP">
   <generic:GroupKey><generic:Value id="FREQ" value="B"/></generic:GroupKey>
   <generic:Attributes><generic:Value id="UNKNOWN_GROUP" value="kept-group"/></generic:Attributes>
  </generic:Group>
  {series}
 </message:DataSet>
</message:GenericData>'''
    return xml.encode()


def _yc_body(*pairs: tuple[str, str]) -> bytes:
    observations = [_observation(period, value) for period, value in pairs]
    return _body(_series(_YC_DIMENSIONS, _YC_VALUES, observations))


def _bps_body(*pairs: tuple[str, str]) -> bytes:
    observations = [_observation(period, value) for period, value in pairs]
    return _body(_series(_BPS_DIMENSIONS, _BPS_VALUES, observations))


def _grant(
    canonical_model: str,
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    allowed_hosts: frozenset[str] = frozenset({_HOST}),
    expires_at: datetime | None = None,
    conditions: tuple[str, ...] = (),
    decision: GrantDecision = GrantDecision.ALLOWED,
    now: datetime | None = None,
) -> RequestGrant:
    valid_until = expires_at
    if valid_until is None:
        grant_time = now if now is not None else datetime.now(timezone.utc)
        valid_until = grant_time + timedelta(minutes=5)
    return RequestGrant(
        source="ecb",
        canonical_model=canonical_model,
        operation=operation,
        decision=decision,
        rights_evidence="fixture:synthetic-response-only",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=allowed_hosts,
        expires_at=valid_until,
        conditions=conditions,
    )


def _budget(grant: RequestGrant) -> RequestBudget:
    return RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))


@pytest.fixture(autouse=True)
def disable_raw_response_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's recording session as the only response source."""
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *outcomes: requests.Response | BaseException,
) -> tuple[GovernedHttpClient, RecordingSession]:
    session = RecordingSession(outcomes)
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    monkeypatch.setattr(series_client, "get_shared_http_client", lambda: client)
    return client, session


def _count_client_gets(
    monkeypatch: pytest.MonkeyPatch,
    client: GovernedHttpClient,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Count client-level calls while preserving the governed implementation."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_get = client.get

    def counted_get(*args: Any, **kwargs: Any) -> requests.Response:
        calls.append((args, kwargs))
        return original_get(*args, **kwargs)

    monkeypatch.setattr(client, "get", counted_get)
    return calls


def test_yc_and_bps_use_exact_single_scoped_request_and_return_raw_parser_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        (
            "YC",
            _YC_MODEL,
            EcbYieldCurveQuery(
                series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M",
                start_date="2026-10-07",
                end_date="2026-10-09",
                max_records=3,
            ),
            _yc_body(("2026-10-08", "-0.000")),
            "startPeriod",
            "2026-10-07",
            "endPeriod",
            "2026-10-09",
        ),
        (
            "BPS",
            _BPS_MODEL,
            EcbBalanceOfPaymentsQuery(
                series_key="BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL",
                start_period="2014-08",
                end_period="2014-10",
                max_records=3,
            ),
            _bps_body(("2014-09", "1.234500")),
            "startPeriod",
            "2014-08",
            "endPeriod",
            "2014-10",
        ),
    )
    for (
        product,
        canonical_model,
        query,
        body,
        start_name,
        start_value,
        end_name,
        end_value,
    ) in cases:
        client, session = _install_client(monkeypatch, _response(body))
        client_calls = _count_client_gets(monkeypatch, client)
        grant = _grant(canonical_model)
        budget = _budget(grant)
        fetch = (
            series_client.fetch_yield_curve
            if product == "YC"
            else series_client.fetch_balance_of_payments
        )
        with request_execution_scope(
            source="ecb",
            canonical_model=canonical_model,
            operation=RequestOperation.QUERY,
            budget=budget,
        ):
            rows = fetch(query, timeout=2.75)  # type: ignore[arg-type]

        assert len(client_calls) == 1
        assert len(session.calls) == 1
        method, url, sent = session.calls[0]
        assert method == "GET"
        assert url == (
            f"https://data-api.ecb.europa.eu/service/data/{product}/"
            f"{query.series_key.removeprefix(product + '.')}"
        )
        assert sent["params"] == {
            "format": "genericdata",
            "detail": "full",
            "includeHistory": "false",
            start_name: start_value,
            end_name: end_value,
        }
        assert sent["headers"]["Accept"] == "application/vnd.sdmx.genericdata+xml;version=2.1"
        assert sent["allow_redirects"] is False
        assert sent["timeout"] == 2.75
        assert len(rows) == 1
        assert rows[0]["period"] == ("2026-10-08" if product == "YC" else "2014-09")
        assert rows[0]["value"] == ("-0.000" if product == "YC" else "1.234500")
        assert rows[0]["dataset_attributes"] == {"UNKNOWN_DATASET": "kept-dataset"}
        assert rows[0]["series_attributes"] == {"UNKNOWN_SERIES": "kept-series"}
        assert rows[0]["observation_attributes"] == {
            "OBS_STATUS": "raw-status",
            "UNKNOWN_OBS_ATTRIBUTE": "kept-observation",
        }
        assert rows[0]["group_context"] == [
            {
                "group_type": "SYNTHETIC_GROUP",
                "key": {"FREQ": "B"},
                "attributes": {"UNKNOWN_GROUP": "kept-group"},
            }
        ]
        assert budget.attempts_used == 1
        assert budget.attempts_used_for("ecb") == 1


def test_store_scope_uses_only_the_matching_store_grant_without_operation_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_yc_body(("2026-10-08", "1"))))
    grant = _grant(_YC_MODEL, operation=RequestOperation.STORE)
    budget = _budget(grant)
    query = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")

    with request_execution_scope(
        source="ecb",
        canonical_model=_YC_MODEL,
        operation=RequestOperation.STORE,
        budget=budget,
    ):
        rows = series_client.fetch_yield_curve(query)

    assert len(rows) == 1
    assert len(session.calls) == 1
    assert budget.attempts_used == 1


@pytest.mark.parametrize(
    ("scope_source", "scope_model", "grant_case", "grant_model"),
    [
        pytest.param("fred", _YC_MODEL, "allowed", _YC_MODEL, id="fred-YieldCurve-grant0"),
        pytest.param("ecb", "OtherModel", "allowed", "OtherModel", id="ecb-OtherModel-grant1"),
        pytest.param("ecb", _YC_MODEL, "missing", _YC_MODEL, id="ecb-YieldCurve-None"),
        pytest.param("ecb", _YC_MODEL, "expired", _YC_MODEL, id="ecb-YieldCurve-grant3"),
        pytest.param("ecb", _YC_MODEL, "conditional", _YC_MODEL, id="ecb-YieldCurve-grant4"),
        pytest.param("ecb", _YC_MODEL, "wrong-host", _YC_MODEL, id="ecb-YieldCurve-grant5"),
        pytest.param("ecb", _YC_MODEL, "denied", _YC_MODEL, id="ecb-YieldCurve-grant6"),
    ],
)
def test_missing_wrong_zero_expired_conditional_or_wrong_host_grant_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    scope_source: str,
    scope_model: str,
    grant_case: str,
    grant_model: str,
) -> None:
    _, session = _install_client(monkeypatch, _response(_yc_body(("2026-10-08", "1"))))
    query = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
    now = datetime.now(timezone.utc)
    if grant_case == "missing":
        grant = None
    elif grant_case == "expired":
        grant = _grant(grant_model, expires_at=now - timedelta(seconds=1))
    elif grant_case == "conditional":
        grant = _grant(grant_model, conditions=("rights unresolved",), now=now)
    elif grant_case == "wrong-host":
        grant = _grant(
            grant_model,
            allowed_hosts=frozenset({"other.example.invalid"}),
            now=now,
        )
    elif grant_case == "denied":
        grant = _grant(grant_model, decision=GrantDecision.DENIED, now=now)
    else:
        grant = _grant(grant_model, now=now)
    budget = RequestBudget() if grant is None else _budget(grant)

    with (
        request_execution_scope(
            source=scope_source,
            canonical_model=scope_model,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError),
    ):
        series_client.fetch_yield_curve(query)

    assert session.calls == []
    assert budget.attempts_used == 0


def test_wrong_host_grant_remains_unexpired_after_six_minute_collection_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_yc_body(("2026-10-08", "1"))))
    collected_at = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
    execution_at = collected_at + timedelta(minutes=6)
    grant = _grant(
        _YC_MODEL,
        allowed_hosts=frozenset({"other.example.invalid"}),
        now=execution_at,
    )
    budget = _budget(grant)

    class FrozenRequestBudgetDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return execution_at.replace(tzinfo=None)
            return execution_at.astimezone(tz)

    monkeypatch.setattr(request_budget_module, "datetime", FrozenRequestBudgetDatetime)
    query = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
    assert execution_at - collected_at == timedelta(minutes=6)
    assert grant.expires_at > execution_at

    with (
        request_execution_scope(
            source="ecb",
            canonical_model=_YC_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError, match="HTTPS host is outside the request grant"),
    ):
        series_client.fetch_yield_curve(query)

    assert session.calls == []
    assert budget.attempts_used == 0


def test_unscoped_client_query_and_tampered_or_wrong_query_models_fail_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session = _install_client(monkeypatch, _response(_yc_body(("2026-10-08", "1"))))
    valid = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
    malformed_constructed = EcbYieldCurveQuery.model_construct(series_key="YC.B.U2/INVALID")
    mutated = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
    object.__setattr__(mutated, "series_key", "YC.B.U2/INVALID")
    wrong_type = EcbBalanceOfPaymentsQuery(
        series_key="BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
    )

    with pytest.raises(RequestAuthorizationError, match="authorized Fetcher execution scope"):
        series_client.fetch_yield_curve(valid)
    grant = _grant(_YC_MODEL)
    budget = _budget(grant)
    with request_execution_scope(
        source="ecb",
        canonical_model=_YC_MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        for invalid_query in (malformed_constructed, mutated, wrong_type):
            with pytest.raises(RequestAuthorizationError):
                series_client.fetch_yield_curve(invalid_query)  # type: ignore[arg-type]

    assert session.calls == []
    assert budget.attempts_used == 0


def test_http_and_parser_errors_expose_status_only_not_url_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "private-response-sentinel"
    _, session = _install_client(
        monkeypatch,
        _response(marker.encode(), status=400),
        _response(marker.encode(), status=200),
    )
    query = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")

    for expected_code, expected_status in (("ECB_HTTP_ERROR", 400), ("ECB_BAD_RESPONSE", 200)):
        budget = _budget(_grant(_YC_MODEL))
        with (
            request_execution_scope(
                source="ecb",
                canonical_model=_YC_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(EcbProviderError) as exc_info,
        ):
            series_client.fetch_yield_curve(query)
        assert exc_info.value.code == expected_code
        assert exc_info.value.status == expected_status
        assert exc_info.value.url is None
        assert marker not in str(exc_info.value)
        assert _HOST not in str(exc_info.value)
        assert budget.attempts_used == 1

    assert len(session.calls) == 2


def test_bad_tail_and_max_record_limit_fail_whole_response_without_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_tail = _yc_body(("2026-10-08", "1.0"), ("2026-10-09", "NaN"))
    two_rows = _yc_body(("2026-10-08", "1.0"), ("2026-10-09", "2.0"))
    _, session = _install_client(monkeypatch, _response(bad_tail), _response(two_rows))
    query = EcbYieldCurveQuery(series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")

    for body_query in (query, query.model_copy(update={"max_records": 1})):
        budget = _budget(_grant(_YC_MODEL))
        with (
            request_execution_scope(
                source="ecb",
                canonical_model=_YC_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(EcbProviderError) as exc_info,
        ):
            series_client.fetch_yield_curve(body_query)
        assert exc_info.value.code == "ECB_BAD_RESPONSE"
        assert "NaN" not in str(exc_info.value)
        assert budget.attempts_used == 1

    assert len(session.calls) == 2


def test_oversized_response_is_bad_response_and_consumes_only_one_shared_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b" " * (4 * 1024 * 1024 + 1)
    _, session = _install_client(monkeypatch, _response(body))
    budget = _budget(_grant(_BPS_MODEL))
    query = EcbBalanceOfPaymentsQuery(
        series_key="BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
    )

    with (
        request_execution_scope(
            source="ecb",
            canonical_model=_BPS_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(EcbProviderError) as exc_info,
    ):
        series_client.fetch_balance_of_payments(query)

    assert exc_info.value.code == "ECB_BAD_RESPONSE"
    assert exc_info.value.status == 200
    assert len(session.calls) == 1
    assert budget.attempts_used == 1
