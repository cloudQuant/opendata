"""Offline contract and governed-service tests for clean Federal Reserve rates."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from pydantic import ValidationError

from opendata.data.domains import DomainSpec
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models.federal_reserve_clean_rates import (
    FederalReserveFederalFundsRate,
    FederalReserveMoneyMeasure,
    FederalReserveOvernightBankFundingRate,
    FederalReserveSvenssonYieldCurve,
    FederalReserveTreasuryRate,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.federal_reserve.models.clean_rates import (
    FED_DDP_URL,
    FEDS_CURVE_URL,
    H6_PACKAGE_ID,
    H15_WEEK4_PACKAGE_ID,
    NYFED_RATE_URL,
    FederalFundsRateFetcher,
    FederalReserveQueryError,
    MoneyMeasuresFetcher,
    OvernightBankFundingRateFetcher,
    SvenssonYieldCurveFetcher,
    TreasuryRatesFetcher,
)
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import (
    ProviderModelQueryUpstreamError,
    ProviderModelQueryValidationError,
    query_provider_model,
)

SOURCE = "federal_reserve"
MARKET = "us"
H6_SERIES = (
    "M1.M",
    "M2.M",
    "MCU.M",
    "MDD.M",
    "MMFGB.M",
    "MDL.M",
    "MDTS.M",
    "M1_N.M",
    "M2_N.M",
    "MCU_N.M",
    "MDD_N.M",
    "MMFGB_N.M",
    "MDL_N.M",
    "MDTS_N.M",
)
H15_SERIES = (
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
    "RIFSGFSW04_N.B",
)
SVENSSON_FIELDS = (
    "BETA0",
    "BETA1",
    "BETA2",
    "BETA3",
    "TAU1",
    "TAU2",
    "SVEN1F01",
    "SVEN1F04",
    "SVEN1F09",
    *(f"SVENF{number:02d}" for number in range(1, 31)),
    *(f"SVENPY{number:02d}" for number in range(1, 31)),
    *(f"SVENY{number:02d}" for number in range(1, 31)),
)
NYFED_OBFR_URL = NYFED_RATE_URL.format(rate_type="obfr")
NYFED_EFFR_URL = NYFED_RATE_URL.format(rate_type="effr")
NYFED_SOURCE_CAPTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/C100/acceptance-federal-reserve-clean/source-structure-investigation"
)


def _csv_document(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def _h6_document(*, invalid_m1: bool = False) -> bytes:
    rows = [
        ["Series Description", *[f"H.6 {series}" for series in H6_SERIES]],
        ["Unit:", *(["Billions of Dollars"] * len(H6_SERIES))],
        ["Multiplier:", *(["1"] * len(H6_SERIES))],
        ["Currency:", *(["NA"] * len(H6_SERIES))],
        [
            "Unique Identifier: ",
            *[f"H6/H6/{series}" for series in H6_SERIES],
        ],
        ["Time Period", *H6_SERIES],
        [
            "2024-01",
            "15000" if not invalid_m1 else "NaN",
            "21000",
            "2300",
            "5500",
            "2800",
            "10800",
            "1500",
            "15100",
            "21100",
            "2310",
            "2810",
            "5510",
            "10810",
            "1510",
        ],
        [
            "2024-02",
            "15020",
            "21020",
            "2302",
            "5502",
            "2802",
            "10802",
            "1502",
            "15120",
            "21120",
            "2312",
            "2812",
            "5512",
            "10812",
            "1512",
        ],
    ]
    return _csv_document(rows)


def _h15_document(*, invalid_rate: bool = False, unknown_marker: bool = False) -> bytes:
    descriptions = [f"H.15 source {series}" for series in H15_SERIES]
    row_values = [
        "4.1",
        "4.2",
        "4.3",
        "4.4",
        "4.5",
        "4.6",
        "4.8",
        "5.0",
        "5.2",
        "5.5",
        "5.6",
        "5.71",
    ]
    if invalid_rate:
        row_values[0] = "Infinity"
    row_values[1] = "TBD" if unknown_marker else "ND"
    return _csv_document(
        [
            ["Series Description", *descriptions],
            ["Unit:", *("Percent:_Per_Year" for _ in H15_SERIES)],
            ["Multiplier:", *("1" for _ in H15_SERIES)],
            ["Currency:", *("NA" for _ in H15_SERIES)],
            ["Unique Identifier: ", *(f"H15/H15/{series}" for series in H15_SERIES)],
            ["Time Period", *H15_SERIES],
            ["2024-01-02", *row_values],
        ]
    )


def _feds_document(*, invalid_sveny: bool = False) -> bytes:
    values = dict.fromkeys(SVENSSON_FIELDS, "")
    values.update(
        {
            "BETA0": "4.1",
            "BETA1": "-1.2",
            "TAU2": "-999.99",
            "SVEN1F04": "4.4",
            "SVENF01": "4.5",
            "SVENPY10": "4.6",
            "SVENY10": "4.7",
            "SVENY30": "4.8",
        }
    )
    if invalid_sveny:
        values["SVENY10"] = "not-a-number"
    return _csv_document(
        [
            ["Series", "Compounding Convention", "Mnemonic(s)"],
            ["Zero-coupon yield", "Continuously Compounded", "SVENYXX"],
            ["Par yield", "Coupon-Equivalent", "SVENPYXX"],
            ["Instantaneous forward rate", "Continuously Compounded", "SVENFXX"],
            ["One-year forward rate", "Coupon-Equivalent", "SVEN1FXX"],
            ["Parameters", "N/A", "BETA0 to TAU2"],
            [],
            ["Date", *SVENSSON_FIELDS],
            ["2024-01-02", *(values[name] for name in SVENSSON_FIELDS)],
        ]
    )


def _nyfed_document(
    *, invalid_rate: bool = False, rate_fields: dict[str, object] | None = None
) -> bytes:
    row: dict[str, object] = {
        "effectiveDate": "2024-01-02",
        "type": "EFFR",
        "percentPercentile1": "5.30",
        "percentPercentile25": "5.32",
        "percentPercentile75": "5.34",
        "percentPercentile99": "5.36",
        "targetRateFrom": "5.25",
        "targetRateTo": "5.50",
        "volumeInBillions": "70.0",
        "footnoteId": None,
        "revisionIndicator": None,
    }
    row.update(
        rate_fields if rate_fields is not None else {"percent": "NaN" if invalid_rate else 5.33}
    )
    return json.dumps({"refRates": [row]}).encode("utf-8")


def _official_nyfed_capture(filename: str, *, sha256: str) -> bytes:
    """Read one preserved raw response after checking its evidence hash."""
    payload = (NYFED_SOURCE_CAPTURE_DIR / filename).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == sha256
    return payload


def _nyfed_pre_2016_document() -> bytes:
    """Return a synthetic row using the NY Fed's documented legacy schema."""
    return json.dumps(
        {
            "refRates": [
                {
                    "effectiveDate": "2016-02-29",
                    "type": "EFFR",
                    "percent": 0.38,
                    "targetRange": 0.25,
                    "intraDayLow": 0.25,
                    "intraDayHigh": 0.40,
                    "stdDeviation": 0.10,
                }
            ]
        }
    ).encode("utf-8")


class _FixtureSession(requests.Session):
    """Serve exact local bytes and record the governed request arguments."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if url not in self.responses:
            raise AssertionError(f"unexpected request: {url}")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = "utf-8"
        response.headers["Content-Type"] = (
            "application/json" if url.startswith("https://markets.newyorkfed.org/") else "text/csv"
        )
        response._content = self.responses[url]
        return response


def _context(model: str, host: str) -> FetchContext:
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fed-clean-rates",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    budget = RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))
    return FetchContext(timeout=30, request_budget=budget)


def _domain_spec(domain: str, contract_name: str, time_field: str) -> DomainSpec:
    return DomainSpec(
        display_name=contract_name,
        rest_path=f"macro/{domain}",
        contract=contract_name,
        temporal_kind="series",
        time_field=time_field,
        natural_key=(time_field,),
        filter_dims=(time_field,),
        storage_mode="transient",
        permissions=("query",),
    )


def _service_query(
    *,
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Any,
    model: str,
    contract: type[object],
    query: dict[str, object],
    host: str,
    payload: bytes,
    time_field: str,
) -> tuple[dict[str, object], _FixtureSession, FetchContext, dict[str, object]]:
    domain = fetcher.capability.domain
    spec = _domain_spec(domain, contract.__name__, time_field)
    monkeypatch.setattr(
        "opendata.services.provider_model_query._reviewed_query_contract",
        lambda _domain: (spec, contract),
    )
    session = _FixtureSession({fetcher.fixture_url: payload})
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    fetcher._http_client = client
    registry = ProviderRegistry()
    registry.register_provider_models(SOURCE, (model,), fetcher)
    ctx = _context(model, host)
    observed_query: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        observed_query.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    result = asyncio.run(
        query_provider_model(
            registry=registry,
            source=SOURCE,
            model=model,
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=ctx,
        )
    )
    return result, session, ctx, observed_query


def test_money_measures_service_injects_identity_and_serializes_h6_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = MoneyMeasuresFetcher()
    fetcher.fixture_url = FED_DDP_URL
    result, session, ctx, observed_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="MoneyMeasures",
        contract=FederalReserveMoneyMeasure,
        query={"start_date": "2024-01-01", "end_date": "2024-01-31", "adjusted": True},
        host=urlsplit(FED_DDP_URL).netloc,
        payload=_h6_document(),
        time_field="month",
    )

    assert observed_query["source"] == SOURCE
    assert observed_query["market"] == MARKET
    assert result["verified"] is False
    assert result["results"] == [
        {
            "month": "2024-01-01",
            "m1": 15000.0,
            "m2": 21000.0,
            "currency": 2300.0,
            "demand_deposits": 5500.0,
            "retail_money_market_funds": 2800.0,
            "other_liquid_deposits": 10800.0,
            "small_denomination_time_deposits": 1500.0,
        }
    ]
    assert session.calls[0]["params"] == {
        "rel": "H6",
        "series": H6_PACKAGE_ID,
        "lastobs": "",
        "from": "2024-01-01",
        "to": "2024-01-31",
        "filetype": "csv",
        "label": "include",
        "layout": "seriescolumn",
    }
    assert ctx.request_budget.attempts_used == 1

    nsa_fetcher = MoneyMeasuresFetcher()
    nsa_fetcher.fixture_url = FED_DDP_URL
    nsa_result, nsa_session, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=nsa_fetcher,
        model="MoneyMeasures",
        contract=FederalReserveMoneyMeasure,
        query={"start_date": "2024-02-01", "end_date": "2024-02-29", "adjusted": False},
        host=urlsplit(FED_DDP_URL).netloc,
        payload=_h6_document(),
        time_field="month",
    )
    assert nsa_result["results"][0]["m1"] == 15120.0
    assert nsa_result["results"][0]["m2"] == 21120.0
    assert nsa_session.calls[0]["params"]["from"] == "2024-02-01"


def test_treasury_rates_service_keeps_discount_basis_and_missing_month_2_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = TreasuryRatesFetcher()
    fetcher.fixture_url = FED_DDP_URL
    result, session, _, observed_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="TreasuryRates",
        contract=FederalReserveTreasuryRate,
        query={"start_date": "2024-01-02", "end_date": "2024-01-02"},
        host=urlsplit(FED_DDP_URL).netloc,
        payload=_h15_document(),
        time_field="date",
    )
    row = result["results"][0]

    assert observed_query["source"] == SOURCE
    assert observed_query["market"] == MARKET
    assert row["date"] == "2024-01-02"
    assert row["week_4"] == 5.71
    assert row["month_1"] == 4.1
    assert row["month_3"] is None
    assert row["month_2"] is None
    assert row["year_30"] == 5.6
    assert session.calls[0]["params"]["series"] == H15_WEEK4_PACKAGE_ID
    assert session.calls[0]["params"]["from"] == "2024-01-02"


def test_svensson_service_injects_identity_filters_columns_and_preserves_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SvenssonYieldCurveFetcher()
    fetcher.fixture_url = FEDS_CURVE_URL
    result, session, ctx, observed_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="SvenssonYieldCurve",
        contract=FederalReserveSvenssonYieldCurve,
        query={
            "start_date": "2024-01-02",
            "end_date": "2024-01-02",
            "series_type": "beta0,sveny10,tau2",
        },
        host=urlsplit(FEDS_CURVE_URL).netloc,
        payload=_feds_document(),
        time_field="date",
    )
    row = result["results"][0]

    assert observed_query["source"] == SOURCE
    assert observed_query["market"] == MARKET
    assert row["date"] == "2024-01-02"
    assert row["beta0"] == 4.1
    assert row["sveny10"] == 4.7
    assert row["sveny30"] is None
    assert row["tau2"] == -999.99
    assert session.calls[0]["params"] is None
    assert ctx.request_budget.attempts_used == 1


def test_svensson_contract_preserves_complete_typed_field_cardinality() -> None:
    expected_fields = (
        "date",
        "beta0",
        "beta1",
        "beta2",
        "beta3",
        "tau1",
        "tau2",
        "sven1f01",
        "sven1f04",
        "sven1f09",
        *(f"svenf{maturity:02d}" for maturity in range(1, 31)),
        *(f"svenpy{maturity:02d}" for maturity in range(1, 31)),
        *(f"sveny{maturity:02d}" for maturity in range(1, 31)),
    )
    assert len(expected_fields) == 100
    assert tuple(FederalReserveSvenssonYieldCurve.model_fields) == expected_fields

    sample_values = {
        field_name: float(index) for index, field_name in enumerate(expected_fields[1:], start=1)
    }
    complete = FederalReserveSvenssonYieldCurve.model_validate(
        {"date": date(2024, 1, 2), **sample_values}
    )
    complete_dump = complete.model_dump(mode="json")
    assert tuple(complete_dump) == expected_fields
    assert complete_dump == {"date": "2024-01-02", **sample_values}

    null_boundary = FederalReserveSvenssonYieldCurve.model_validate(
        {"date": date(2024, 1, 2)}
    ).model_dump(mode="json")
    assert null_boundary == {
        "date": "2024-01-02",
        **dict.fromkeys(expected_fields[1:], None),
    }

    schema = FederalReserveSvenssonYieldCurve.model_json_schema()
    assert schema["required"] == ["date"]
    for field_name in expected_fields[1:]:
        field_schema = schema["properties"][field_name]
        assert field_schema["default"] is None
        assert {"type": "null"} in field_schema["anyOf"]

    with pytest.raises(ValidationError):
        FederalReserveSvenssonYieldCurve.model_validate({"beta0": 1.0})
    with pytest.raises(ValidationError):
        FederalReserveSvenssonYieldCurve.model_validate(
            {"date": date(2024, 1, 2), "unexpected_field": 1.0}
        )


def test_obfr_service_maps_official_nyfed_percentile_and_volume_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = OvernightBankFundingRateFetcher()
    fetcher.fixture_url = NYFED_OBFR_URL
    result, session, _, observed_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="OvernightBankFundingRate",
        contract=FederalReserveOvernightBankFundingRate,
        query={"start_date": "2024-01-02", "end_date": "2024-01-02"},
        host=urlsplit(NYFED_OBFR_URL).netloc,
        payload=_nyfed_document(),
        time_field="date",
    )
    row = result["results"][0]

    assert observed_query["source"] == SOURCE
    assert observed_query["market"] == MARKET
    assert row["date"] == "2024-01-02"
    assert row["rate"] == 5.33
    assert row["percentile_1"] == 5.3
    assert row["percentile_99"] == 5.36
    assert row["volume"] == 70.0
    assert row["revision_indicator"] is None
    assert session.calls[0]["params"] == {
        "startDate": "2024-01-02",
        "endDate": "2024-01-02",
    }


def test_official_nyfed_capture_replay_maps_percent_rate_and_legacy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obfr_payload = _official_nyfed_capture(
        "obfr-modern-2020-01-02_03.response.bin",
        sha256="66ff5a765bc2d525a47ad9ba22358fe4c6a89d2b93a1123dab105d7b4ea2ca95",
    )
    obfr = OvernightBankFundingRateFetcher()
    obfr.fixture_url = NYFED_OBFR_URL
    obfr_result, _, obfr_context, obfr_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=obfr,
        model="OvernightBankFundingRate",
        contract=FederalReserveOvernightBankFundingRate,
        query={"start_date": "2020-01-02", "end_date": "2020-01-03"},
        host=urlsplit(NYFED_OBFR_URL).netloc,
        payload=obfr_payload,
        time_field="date",
    )
    obfr_rows = {row["date"]: row for row in obfr_result["results"]}
    assert obfr_query["source"] == SOURCE
    assert obfr_query["market"] == MARKET
    assert obfr_context.request_budget.attempts_used == 1
    assert obfr_rows["2020-01-02"]["rate"] == 1.54
    assert obfr_rows["2020-01-02"]["volume"] == 170.0
    assert obfr_rows["2020-01-03"]["rate"] == 1.54
    assert obfr_rows["2020-01-03"]["volume"] == 187.0

    effr_payload = _official_nyfed_capture(
        "effr-modern-2020-01-02_03.response.bin",
        sha256="9857cb0984968e3e2c42a9d5e1204d58f1774c3d6d2e207ee87deb1adb9bf90d",
    )
    effr = FederalFundsRateFetcher()
    effr.fixture_url = NYFED_EFFR_URL
    effr_result, _, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=effr,
        model="FederalFundsRate",
        contract=FederalReserveFederalFundsRate,
        query={"start_date": "2020-01-02", "end_date": "2020-01-03"},
        host=urlsplit(NYFED_EFFR_URL).netloc,
        payload=effr_payload,
        time_field="date",
    )
    effr_rows = {row["date"]: row for row in effr_result["results"]}
    assert effr_rows["2020-01-02"]["rate"] == 1.55
    assert effr_rows["2020-01-02"]["target_range_lower"] == 1.5
    assert effr_rows["2020-01-02"]["target_range_upper"] == 1.75
    assert effr_rows["2020-01-02"]["volume"] == 74.0

    legacy_payload = _official_nyfed_capture(
        "effr-legacy-2015-12-31.response.bin",
        sha256="22938ed6f8d8ca8d4fb21c2f5dd0a69ea9f1bc9e0e6b52d0cc0265c3f0a6e93b",
    )
    legacy = FederalFundsRateFetcher()
    legacy.fixture_url = NYFED_EFFR_URL
    legacy_result, _, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=legacy,
        model="FederalFundsRate",
        contract=FederalReserveFederalFundsRate,
        query={"start_date": "2015-12-31", "end_date": "2015-12-31"},
        host=urlsplit(NYFED_EFFR_URL).netloc,
        payload=legacy_payload,
        time_field="date",
    )
    legacy_row = legacy_result["results"][0]
    assert legacy_row["rate"] == 0.2
    assert legacy_row["target_range_lower"] == 0.25
    assert legacy_row["target_range_upper"] == 0.5
    assert legacy_row["intraday_low"] == 0.08
    assert legacy_row["intraday_high"] == 0.63
    assert legacy_row["standard_deviation"] == 0.1


def test_nyfed_rate_aliases_require_equal_finite_json_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = OvernightBankFundingRateFetcher()
    fetcher.fixture_url = NYFED_OBFR_URL
    matching_aliases, _, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="OvernightBankFundingRate",
        contract=FederalReserveOvernightBankFundingRate,
        query={"start_date": "2024-01-02", "end_date": "2024-01-02"},
        host=urlsplit(NYFED_OBFR_URL).netloc,
        payload=_nyfed_document(rate_fields={"percentRate": 5.33, "percent": 5.33}),
        time_field="date",
    )
    assert matching_aliases["results"][0]["rate"] == 5.33

    invalid_aliases = (
        {"percentRate": 5.33, "percent": 5.34},
        {},
        {"percentRate": True},
        {"percent": "5.33"},
        {"percentRate": float("inf")},
        {"percentRate": None, "percent": 5.33},
    )
    for rate_fields in invalid_aliases:
        invalid_fetcher = OvernightBankFundingRateFetcher()
        invalid_fetcher.fixture_url = NYFED_OBFR_URL
        with pytest.raises(ProviderModelQueryUpstreamError):
            _service_query(
                monkeypatch=monkeypatch,
                fetcher=invalid_fetcher,
                model="OvernightBankFundingRate",
                contract=FederalReserveOvernightBankFundingRate,
                query={"start_date": "2024-01-02", "end_date": "2024-01-02"},
                host=urlsplit(NYFED_OBFR_URL).netloc,
                payload=_nyfed_document(rate_fields=rate_fields),
                time_field="date",
            )


def test_effr_service_serializes_current_statistics_and_effr_only_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FederalFundsRateFetcher()
    fetcher.fixture_url = NYFED_EFFR_URL
    result, session, _, observed_query = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="FederalFundsRate",
        contract=FederalReserveFederalFundsRate,
        query={"start_date": "2024-01-02", "end_date": "2024-01-02"},
        host=urlsplit(NYFED_EFFR_URL).netloc,
        payload=_nyfed_document(),
        time_field="date",
    )
    row = result["results"][0]
    assert observed_query["source"] == SOURCE
    assert observed_query["market"] == MARKET
    assert row["target_range_lower"] == 5.25
    assert row["target_range_upper"] == 5.5
    assert row["rate"] == 5.33
    assert row["volume"] == 70.0
    assert row["intraday_low"] is None
    assert session.calls[0]["params"] == {
        "startDate": "2024-01-02",
        "endDate": "2024-01-02",
    }

    effr_only = FederalFundsRateFetcher()
    effr_only.fixture_url = NYFED_EFFR_URL
    projected, _, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=effr_only,
        model="FederalFundsRate",
        contract=FederalReserveFederalFundsRate,
        query={"effr_only": True},
        host=urlsplit(NYFED_EFFR_URL).netloc,
        payload=_nyfed_document(),
        time_field="date",
    )
    projected_row = projected["results"][0]
    assert projected_row["rate"] == 5.33
    assert projected_row["target_range_lower"] is None
    assert projected_row["percentile_25"] is None
    assert projected_row["volume"] is None


def test_effr_service_keeps_documented_scalar_target_range_unsplit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FederalFundsRateFetcher()
    fetcher.fixture_url = NYFED_EFFR_URL
    result, _, _, _ = _service_query(
        monkeypatch=monkeypatch,
        fetcher=fetcher,
        model="FederalFundsRate",
        contract=FederalReserveFederalFundsRate,
        query={"start_date": "2016-02-29", "end_date": "2016-02-29"},
        host=urlsplit(NYFED_EFFR_URL).netloc,
        payload=_nyfed_pre_2016_document(),
        time_field="date",
    )
    row = result["results"][0]

    assert row["intraday_low"] == 0.25
    assert row["intraday_high"] == 0.4
    assert row["standard_deviation"] == 0.1
    # An OpenAPI scalar targetRange cannot populate separate public bounds.
    assert row["target_range_lower"] is None
    assert row["target_range_upper"] is None


@pytest.mark.parametrize(
    ("fetcher_factory", "model", "contract", "query", "url", "host", "payload", "time_field"),
    [
        (
            MoneyMeasuresFetcher,
            "MoneyMeasures",
            FederalReserveMoneyMeasure,
            {},
            FED_DDP_URL,
            urlsplit(FED_DDP_URL).netloc,
            _h6_document(invalid_m1=True),
            "month",
        ),
        (
            TreasuryRatesFetcher,
            "TreasuryRates",
            FederalReserveTreasuryRate,
            {},
            FED_DDP_URL,
            urlsplit(FED_DDP_URL).netloc,
            _h15_document(invalid_rate=True),
            "date",
        ),
        (
            TreasuryRatesFetcher,
            "TreasuryRates",
            FederalReserveTreasuryRate,
            {},
            FED_DDP_URL,
            urlsplit(FED_DDP_URL).netloc,
            _h15_document(unknown_marker=True),
            "date",
        ),
        (
            SvenssonYieldCurveFetcher,
            "SvenssonYieldCurve",
            FederalReserveSvenssonYieldCurve,
            {},
            FEDS_CURVE_URL,
            urlsplit(FEDS_CURVE_URL).netloc,
            _feds_document(invalid_sveny=True),
            "date",
        ),
        (
            OvernightBankFundingRateFetcher,
            "OvernightBankFundingRate",
            FederalReserveOvernightBankFundingRate,
            {},
            NYFED_OBFR_URL,
            urlsplit(NYFED_OBFR_URL).netloc,
            _nyfed_document(invalid_rate=True),
            "date",
        ),
        (
            FederalFundsRateFetcher,
            "FederalFundsRate",
            FederalReserveFederalFundsRate,
            {},
            NYFED_EFFR_URL,
            urlsplit(NYFED_EFFR_URL).netloc,
            _nyfed_document(invalid_rate=True),
            "date",
        ),
    ],
)
def test_malformed_numeric_values_fail_through_provider_service(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_factory: type[Any],
    model: str,
    contract: type[object],
    query: dict[str, object],
    url: str,
    host: str,
    payload: bytes,
    time_field: str,
) -> None:
    fetcher = fetcher_factory()
    fetcher.fixture_url = url
    with pytest.raises(ProviderModelQueryUpstreamError):
        _service_query(
            monkeypatch=monkeypatch,
            fetcher=fetcher,
            model=model,
            contract=contract,
            query=query,
            host=host,
            payload=payload,
            time_field=time_field,
        )


def test_queries_reject_bad_date_ranges_selector_and_wrong_injected_identity() -> None:
    for fetcher in (
        MoneyMeasuresFetcher(),
        TreasuryRatesFetcher(),
        SvenssonYieldCurveFetcher(),
        OvernightBankFundingRateFetcher(),
        FederalFundsRateFetcher(),
    ):
        with pytest.raises(FederalReserveQueryError):
            fetcher.transform_query(
                source=SOURCE,
                market=MARKET,
                start_date="2024-02-01",
                end_date="2024-01-01",
            )

    svensson = SvenssonYieldCurveFetcher()
    with pytest.raises(FederalReserveQueryError):
        svensson.transform_query(source=SOURCE, market=MARKET, series_type="beta0,all")
    with pytest.raises(FederalReserveQueryError):
        svensson.transform_query(source=SOURCE, market=MARKET, series_type="unknown")
    with pytest.raises(FederalReserveQueryError):
        svensson.transform_query(source="fred", market=MARKET)
    with pytest.raises(FederalReserveQueryError):
        MoneyMeasuresFetcher().transform_query(source=SOURCE, market="eu")
    assert (
        MoneyMeasuresFetcher().transform_query(source=SOURCE, market=MARKET, adjusted=None).adjusted
        is None
    )
    assert (
        SvenssonYieldCurveFetcher()
        .transform_query(source=SOURCE, market=MARKET, series_type=None)
        .series_type
        == "all"
    )
    assert (
        FederalFundsRateFetcher()
        .transform_query(source=SOURCE, market=MARKET, effr_only=None)
        .effr_only
        is None
    )
    with pytest.raises(ValidationError):
        FederalReserveSvenssonYieldCurve.model_validate({"date": "2024-01-02", "extra": 1})


def test_invalid_query_is_redacted_and_classified_at_service_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = MoneyMeasuresFetcher()
    fetcher.fixture_url = FED_DDP_URL
    monkeypatch.setattr(
        fetcher,
        "extract_data",
        lambda *_args: pytest.fail("invalid query must be rejected before source I/O"),
    )

    with pytest.raises(ProviderModelQueryValidationError) as error:
        _service_query(
            monkeypatch=monkeypatch,
            fetcher=fetcher,
            model="MoneyMeasures",
            contract=FederalReserveMoneyMeasure,
            query={
                "start_date": "2024-02-01",
                "end_date": "2024-01-01",
                "secret_value": "must-not-appear",
            },
            host=urlsplit(FED_DDP_URL).netloc,
            payload=_h6_document(),
            time_field="month",
        )

    assert str(error.value) == ""
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "must-not-appear" not in str(error.value)
