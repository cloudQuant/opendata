"""Offline behavioral tests for the ECB YC and BPS Fetcher templates."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.models.ecb_series import (
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.ecb.models import balance_of_payments as bps_module
from opendata.data.providers.ecb.models import yield_curve as yc_module
from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.ecb.models._series_query import (
    EcbBalanceOfPaymentsQuery,
    EcbYieldCurveQuery,
)
from opendata.data.providers.ecb.models.balance_of_payments import EcbBalanceOfPaymentsFetcher
from opendata.data.providers.ecb.models.yield_curve import EcbYieldCurveFetcher
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
)

_HOST = "data-api.ecb.europa.eu"
_YC_KEY = "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"
_BPS_MONTH_KEY = "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_BPS_QUARTER_KEY = "BPS.Q.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_YC_DIMENSIONS = {
    "FREQ": "B",
    "REF_AREA": "U2",
    "CURRENCY": "EUR",
    "PROVIDER_FM": "4F",
    "INSTRUMENT_FM": "G_N_A",
    "PROVIDER_FM_ID": "SV_C_YM",
    "DATA_TYPE_FM": "SR_3M",
}
_BPS_DIMENSIONS = {
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


def _raw_record(
    *,
    prefix: str,
    dimensions: dict[str, str],
    period: str,
    value: str | None,
) -> dict[str, object]:
    series_key = f"{prefix}." + ".".join(dimensions.values())
    return {
        "series_key": series_key,
        "period": period,
        "dimensions": dict(dimensions),
        "value": value,
        "dataset_attributes": {"DATA_NOTE": "source dataset value"},
        "series_attributes": {"SERIES_NOTE": "source series value"},
        "observation_attributes": {"OBS_STATUS": "A", "NOTE": "source observation value"},
        "group_context": [
            {
                "group_type": f"{prefix}_GROUP",
                "key": {"FREQ": dimensions["FREQ"]},
                "attributes": {"COMMENT": "source group value"},
            }
        ],
    }


def _yc_raw(
    period: str = "2026-10-08",
    value: str | None = "-0.000",
    *,
    ref_area: str = "U2",
) -> dict[str, object]:
    dimensions = {**_YC_DIMENSIONS, "REF_AREA": ref_area}
    return _raw_record(prefix="YC", dimensions=dimensions, period=period, value=value)


def _bps_raw(
    period: str = "2024-01",
    value: str | None = "-0.000",
    *,
    frequency: str = "M",
) -> dict[str, object]:
    dimensions = {**_BPS_DIMENSIONS, "FREQ": frequency}
    return _raw_record(prefix="BPS", dimensions=dimensions, period=period, value=value)


def _context(
    canonical_model: str,
    *,
    timeout: float | None = 3.0,
    operation: RequestOperation = RequestOperation.QUERY,
    source: str = "ecb",
    grant_model: str | None = None,
) -> FetchContext:
    grant = RequestGrant(
        source=source,
        canonical_model=grant_model or canonical_model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-synthetic-fetcher-test",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({_HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=timeout,
        operation=operation,
        request_budget=RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,)),
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    yc_rows: tuple[dict[str, object], ...] = (),
    bps_rows: tuple[dict[str, object], ...] = (),
) -> list[tuple[str, object, float | None]]:
    calls: list[tuple[str, object, float | None]] = []

    def fake_yc(query: EcbYieldCurveQuery, *, timeout: float | None = None) -> Any:
        calls.append(("yc", query, timeout))
        return yc_rows

    def fake_bps(query: EcbBalanceOfPaymentsQuery, *, timeout: float | None = None) -> Any:
        calls.append(("bps", query, timeout))
        return bps_rows

    monkeypatch.setattr(yc_module, "fetch_yield_curve", fake_yc)
    monkeypatch.setattr(bps_module, "fetch_balance_of_payments", fake_bps)
    return calls


_PIPELINE_CASES = [
    pytest.param(
        EcbYieldCurveFetcher,
        "YieldCurve",
        "yc",
        {"series_key": _YC_KEY, "start_date": "2026-01-01", "end_date": "2026-12-31"},
        (_yc_raw(), _yc_raw("2026-10-09", None)),
        id="yield-curve",
    ),
    pytest.param(
        EcbBalanceOfPaymentsFetcher,
        "BalanceOfPayments",
        "bps",
        {
            "series_key": _BPS_MONTH_KEY,
            "start_period": "2024-01",
            "end_period": "2024-12",
        },
        (_bps_raw(), _bps_raw("2024-02", None)),
        id="balance-of-payments",
    ),
]


@pytest.mark.parametrize(
    ("fetcher_type", "canonical_model", "client_name", "query_kwargs", "raw_rows"),
    _PIPELINE_CASES,
)
def test_sync_async_and_raw_async_share_the_bounded_fetcher_contract(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    canonical_model: str,
    client_name: str,
    query_kwargs: dict[str, object],
    raw_rows: tuple[dict[str, object], ...],
) -> None:
    calls = _install_fakes(
        monkeypatch,
        yc_rows=raw_rows if client_name == "yc" else (),
        bps_rows=raw_rows if client_name == "bps" else (),
    )
    fetcher = fetcher_type()

    sync_result = fetcher.fetch(ctx=_context(canonical_model, timeout=3.5), **query_kwargs)
    async_result = asyncio.run(
        fetcher.fetch_async(ctx=_context(canonical_model, timeout=4.5), **query_kwargs)
    )
    raw_result = asyncio.run(
        fetcher.fetch_raw_async(ctx=_context(canonical_model, timeout=5.5), **query_kwargs)
    )

    assert fetcher.async_mode == "bounded_thread"
    assert fetcher.capability.verified is False
    assert fetcher.capability.source == "ecb"
    assert fetcher.capability.market == "eu"
    assert sync_result == async_result
    assert type(sync_result[0]) is (
        EcbYieldCurveObservation if client_name == "yc" else EcbBalanceOfPaymentsObservation
    )
    assert raw_result == raw_rows
    assert [call[0] for call in calls] == [client_name, client_name, client_name]
    assert [call[2] for call in calls] == [3.5, 4.5, 5.5]
    assert all(call[1].source == "auto" for call in calls)
    assert sync_result[0].source_value == "-0.000"
    assert math.copysign(1.0, sync_result[0].value) == -1.0
    assert sync_result[1].value is None
    assert sync_result[1].source_value is None
    assert sync_result[0].dataset_attributes == {"DATA_NOTE": "source dataset value"}
    assert sync_result[0].series_attributes == {"SERIES_NOTE": "source series value"}
    assert sync_result[0].observation_attributes == {
        "OBS_STATUS": "A",
        "NOTE": "source observation value",
    }
    assert sync_result[0].group_context[0].attributes == {"COMMENT": "source group value"}


@pytest.mark.parametrize(
    ("fetcher_type", "canonical_model", "client_name", "query_kwargs", "raw_rows"),
    _PIPELINE_CASES,
)
def test_default_zero_grant_and_cross_model_grant_fail_before_client_call(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    canonical_model: str,
    client_name: str,
    query_kwargs: dict[str, object],
    raw_rows: tuple[dict[str, object], ...],
) -> None:
    calls = _install_fakes(
        monkeypatch,
        yc_rows=raw_rows if client_name == "yc" else (),
        bps_rows=raw_rows if client_name == "bps" else (),
    )
    fetcher = fetcher_type()
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(**query_kwargs)

    wrong_model = "BalanceOfPayments" if canonical_model == "YieldCurve" else "YieldCurve"
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(ctx=_context(canonical_model, grant_model=wrong_model), **query_kwargs)
    assert calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "query_kwargs", "raw_rows"),
    [
        (
            EcbYieldCurveFetcher,
            {"series_key": _YC_KEY},
            (_yc_raw(), {**_yc_raw("2026-10-09"), "unexpected": "last-row"}),
        ),
        (
            EcbBalanceOfPaymentsFetcher,
            {"series_key": _BPS_MONTH_KEY},
            (_bps_raw(), {**_bps_raw("2024-02"), "unexpected": "last-row"}),
        ),
    ],
)
def test_malformed_last_raw_row_fails_atomically_with_fixed_error(
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    query_kwargs: dict[str, object],
    raw_rows: tuple[dict[str, object], ...],
) -> None:
    query = fetcher_type().transform_query(**query_kwargs)
    with pytest.raises(EcbProviderError) as error:
        fetcher_type().transform_data(raw_rows, query)
    assert error.value.code == "ECB_BAD_OBSERVATION"
    assert str(error.value) == "ECB_BAD_OBSERVATION"


@pytest.mark.parametrize(
    ("fetcher_type", "query_kwargs", "raw_rows"),
    [
        (
            EcbYieldCurveFetcher,
            {"series_key": _YC_KEY, "max_records": 1},
            (_yc_raw(), _yc_raw("2026-10-09")),
        ),
        (
            EcbBalanceOfPaymentsFetcher,
            {"series_key": _BPS_MONTH_KEY, "max_records": 1},
            (_bps_raw(), _bps_raw("2024-02")),
        ),
    ],
)
def test_record_budget_overflow_is_not_silently_truncated(
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    query_kwargs: dict[str, object],
    raw_rows: tuple[dict[str, object], ...],
) -> None:
    query = fetcher_type().transform_query(**query_kwargs)
    with pytest.raises(EcbProviderError, match="^ECB_BAD_OBSERVATION$"):
        fetcher_type().transform_data(raw_rows, query)


@pytest.mark.parametrize(
    ("fetcher_type", "query_kwargs", "raw_row"),
    [
        (
            EcbYieldCurveFetcher,
            {"series_key": _YC_KEY, "start_date": "2026-01-01", "end_date": "2026-12-31"},
            _yc_raw(ref_area="U1"),
        ),
        (
            EcbYieldCurveFetcher,
            {"series_key": _YC_KEY, "start_date": "2026-01-01", "end_date": "2026-12-31"},
            _yc_raw("2025-12-31"),
        ),
        (
            EcbBalanceOfPaymentsFetcher,
            {
                "series_key": _BPS_MONTH_KEY,
                "start_period": "2024-02",
                "end_period": "2024-08",
            },
            _bps_raw("2024-09"),
        ),
        (
            EcbBalanceOfPaymentsFetcher,
            {"series_key": _BPS_QUARTER_KEY, "start_period": "2024-Q1", "end_period": "2024-Q4"},
            _bps_raw("2024-01", frequency="M"),
        ),
    ],
)
def test_wrong_key_frequency_or_out_of_window_row_fails_whole_result(
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    query_kwargs: dict[str, object],
    raw_row: dict[str, object],
) -> None:
    query = fetcher_type().transform_query(**query_kwargs)
    with pytest.raises(EcbProviderError, match="^ECB_BAD_OBSERVATION$"):
        fetcher_type().transform_data((raw_row,), query)


@pytest.mark.parametrize(
    ("fetcher_type", "query_type", "bad_query"),
    [
        (
            EcbYieldCurveFetcher,
            EcbYieldCurveQuery,
            EcbYieldCurveQuery.model_construct(series_key="YC.A.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"),
        ),
        (
            EcbBalanceOfPaymentsFetcher,
            EcbBalanceOfPaymentsQuery,
            EcbBalanceOfPaymentsQuery.model_construct(
                series_key="BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL",
                max_records=True,
            ),
        ),
    ],
)
def test_bypassed_or_tampered_query_is_revalidated_before_client_or_normalizer(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    query_type: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    bad_query: EcbYieldCurveQuery | EcbBalanceOfPaymentsQuery,
) -> None:
    calls = _install_fakes(monkeypatch)
    fetcher = fetcher_type()
    assert type(bad_query) is query_type

    with pytest.raises(EcbProviderError) as extract_error:
        fetcher.extract_data(bad_query, FetchContext())  # type: ignore[arg-type]
    assert extract_error.value.code == "ECB_BAD_QUERY"
    with pytest.raises(EcbProviderError) as normalize_error:
        fetcher.transform_data((), bad_query)  # type: ignore[arg-type]
    assert normalize_error.value.code == "ECB_BAD_QUERY"
    assert calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "canonical_model", "query_kwargs", "other_model"),
    [
        (EcbYieldCurveFetcher, "YieldCurve", {"series_key": _YC_KEY}, "BalanceOfPayments"),
        (
            EcbBalanceOfPaymentsFetcher,
            "BalanceOfPayments",
            {"series_key": _BPS_MONTH_KEY},
            "YieldCurve",
        ),
    ],
)
def test_expired_deadline_and_cancellation_are_inherited_before_async_client_io(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    canonical_model: str,
    query_kwargs: dict[str, object],
    other_model: str,
) -> None:
    calls = _install_fakes(monkeypatch)
    fetcher = fetcher_type()
    expired = _context(canonical_model, timeout=5.0)
    object.__setattr__(expired, "_deadline_monotonic", time.monotonic() - 1.0)
    with pytest.raises(AsyncExecutionTimeoutError):
        asyncio.run(fetcher.fetch_async(ctx=expired, **query_kwargs))

    cancellation = threading.Event()
    cancellation.set()
    cancelled = _context(canonical_model, timeout=5.0)
    object.__setattr__(cancelled, "_thread_cancel_event", cancellation)
    with pytest.raises(RequestExecutionCancelledError):
        asyncio.run(fetcher.fetch_async(ctx=cancelled, **query_kwargs))

    # A separate wrong-model grant remains denied before either client path.
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(ctx=_context(canonical_model, grant_model=other_model), **query_kwargs)
    assert calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "expected_period"),
    [(EcbYieldCurveFetcher, "1D"), (EcbBalanceOfPaymentsFetcher, "1M/1Q")],
)
def test_capabilities_are_unverified_and_do_not_enable_auto_routing(
    fetcher_type: type[EcbYieldCurveFetcher] | type[EcbBalanceOfPaymentsFetcher],
    expected_period: str,
) -> None:
    capability = fetcher_type.capability
    assert capability.asset_class == "macro"
    assert capability.domain == (
        "yield_curve" if fetcher_type is EcbYieldCurveFetcher else "balance_of_payments"
    )
    assert capability.period == expected_period
    assert capability.market == "eu"
    assert capability.source == "ecb"
    assert capability.verified is False
    assert not capability.participates_in_auto()


def test_query_validation_errors_remain_pydantic_errors_before_any_client_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fakes(monkeypatch)
    with pytest.raises(ValidationError):
        EcbYieldCurveFetcher().transform_query(
            series_key="YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M", as_of=None
        )
    with pytest.raises(ValidationError):
        EcbBalanceOfPaymentsFetcher().transform_query(
            series_key=_BPS_MONTH_KEY,
            start_date=date(2024, 1, 1),
        )
    assert calls == []
