"""Regression pins for the scalar and multi-symbol query family split."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from pydantic import ValidationError

from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.cboe.models.index_historical_public import (
    CboeIndexHistoricalFetcher,
    CboeIndexHistoricalQuery,
)
from opendata.data.providers.deribit.models.fetchers import (
    FuturesHistoricalQuery,
    FuturesInfoQuery,
)
from opendata.data.providers.eia.models.short_term_energy_outlook import (
    ShortTermEnergyOutlookQuery,
)
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fmp.models.iter2_market import HistoricalQuery, MultiSymbolQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.providers.imf.models.iter2 import ImfEconomicIndicatorQuery
from opendata.data.providers.intrinio.models.iter2_snapshots_clean import (
    _EtfPricePerformanceQuery,
)
from opendata.data.providers.sec.models.iter2_financial import CompareCompanyFactsQuery
from opendata.data.providers.yfinance.models.iter2 import YfinanceMultiSymbolQuery
from opendata.data.registry import ProviderRegistry
from opendata.pipeline.provider_model_raw import make_provider_model_capture
from opendata.services.provider_models import find_provider_model, query_schema_for_model


def _ordered_sha(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_QUERY_CASES: tuple[
    tuple[
        str,
        type,
        dict[str, object],
        dict[str, object],
        str,
        str,
        str,
        tuple[str, ...],
    ],
    ...,
] = (
    (
        "CboeIndexHistorical",
        CboeIndexHistoricalQuery,
        {"symbol": "VIX"},
        {"symbol": ["VIX", "VVIX"]},
        "9ac887ecaa47027613d147defd9327d76154e37d7d00f5d8076f1ca8275e16ff",
        "c959f2ad6bc4aae534499c50e3d45962e3e42392b07b0ee9e3781a130a732826",
        "9a32538dfdef795dcfb780f7f989c4ba80b631f64b49c6a07698bab2fa9e0984",
        ("source", "market", "symbol", "start_date", "end_date", "interval", "use_cache"),
    ),
    (
        "CompareCompanyFacts",
        CompareCompanyFactsQuery,
        {"symbol": "AAPL"},
        {"symbol": ["AAPL", "MSFT"]},
        "820a77238e32e6af77fba5e657d00bfc09617d1d5d9051719f65c6c5589eff5e",
        "54bb866878dff131ab74605b37fdfef72f2e3b8a3f8207a424dfff9505d0e673",
        "f30f5744a4a7552a23c03ad1e4c003488287657fdb12aadc9e8faed5ebdbce2a",
        (
            "source",
            "market",
            "symbol",
            "start_date",
            "end_date",
            "fact",
            "year",
            "fiscal_period",
            "instantaneous",
            "use_cache",
        ),
    ),
    (
        "FmpHistorical",
        HistoricalQuery,
        {"symbol": "AAPL", "start_date": "2026-01-01", "end_date": "2026-01-02"},
        {
            "symbol": ["AAPL", "MSFT"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
        },
        "d23cf1063ed64723ed3792d2411e3be2196c5f723030648ae00b238fe54f34fa",
        "bf63f553e15ce971f873c2186d4e2153843897649cbfddbd12e4d721ad68ef12",
        "2945f835f7315b6cf75928c5ba22d3678bcb86f2ef77162cd565be2c555bcb7a",
        (
            "source",
            "market",
            "symbol",
            "start_date",
            "end_date",
            "max_records",
            "interval",
            "max_requests",
        ),
    ),
    (
        "FmpMultiSymbol",
        MultiSymbolQuery,
        {"symbol": "AAPL"},
        {"symbol": ["AAPL", "MSFT"]},
        "a6548b25c9e90f115a137911e6115444c564d238ba0ad74d74f3e7f65a2f30a1",
        "3960c1cc6de2a6c3768d395d93b6b58f162356d575e0471d5248707e6cb11abc",
        "d1285842e609d4ee1b2a0b503ef8f3b3f4397c3f745ee6a093e78ef56fd4a800",
        ("source", "market", "symbol", "start_date", "end_date", "max_records", "max_requests"),
    ),
    (
        "ImfEconomicIndicator",
        ImfEconomicIndicatorQuery,
        {"symbol": "WEO::NGDP_RPCH"},
        {"symbol": ["WEO::NGDP_RPCH", "WEO::NGDPD"]},
        "8ecc3f9a0b3169d0824a5643151fb22914487fa6891bb3af44c49c0e1a4d925e",
        "dab8d435067dded1255106d5381d8884b90688963b99ef89df3e1b45a3a6ceba",
        "9ecca9940bc972dde167a4803a3ec6aaefa71763775731105110b055e979a4a9",
        (
            "source",
            "market",
            "symbol",
            "start_date",
            "end_date",
            "country",
            "frequency",
            "transform",
            "dimension_values",
            "limit",
        ),
    ),
    (
        "DeribitFuturesHistorical",
        FuturesHistoricalQuery,
        {
            "symbol": "BTC-PERPETUAL",
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
        },
        {
            "symbol": ["BTC-PERPETUAL", "ETH-PERPETUAL"],
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
        },
        "936329b9d5c6ad5e539f4c56a270f622637446cc3be1dc959b028f9bec82830c",
        "b36fa0a4f8c02599eb4807f18bbf62ea2131eb8f01e05bdf256a520c31ec214f",
        "bcc94c5c136b5a19bf327b87c26363b0a3d95ba9aa1eee808d81182fc80525ae",
        ("source", "market", "symbol", "start_date", "end_date", "interval", "expiration"),
    ),
    (
        "DeribitFuturesInfo",
        FuturesInfoQuery,
        {"symbol": "BTC-PERPETUAL"},
        {"symbol": ["BTC-PERPETUAL", "ETH-PERPETUAL"]},
        "b10bce803a90a5de61a0fe24bf9069ab1daf1686bf159661f8c4327333c7b831",
        "8a28cf7f4a88d8cf3c8481233b50ab48373ee732afde33ab84038cc43fb288bf",
        "9fa00458554fff24cd24b99a8267397e674eb6c417ddd8e03fea087e9281d940",
        ("source", "market", "symbol", "start_date", "end_date"),
    ),
    (
        "IntrinioEtfPricePerformance",
        _EtfPricePerformanceQuery,
        {"symbol": "SPY"},
        {"symbol": ["SPY", "QQQ"]},
        "a6b6c2b405d34dc416e6d0144382593154267d2873f850e98010d4807cd38099",
        "b06dd0300455bfaa41f68f4c34ed18e7bbfafa6aa1e18402d92862b78871dbfa",
        "6c0d7750396ef8871bb581306cadb14bfcebd1c75f31a4494c4b3300243fceea",
        ("source", "market", "symbol", "start_date", "end_date", "return_type", "adjustment"),
    ),
)


def test_multisymbol_leaf_schemas_and_dumps_match_prechange_baseline() -> None:
    for (
        _name,
        model,
        string_query,
        list_query,
        schema_sha,
        string_dump_sha,
        list_dump_sha,
        field_order,
    ) in _QUERY_CASES:
        assert tuple(model.model_fields) == field_order
        assert _ordered_sha(model.model_json_schema(mode="validation")) == schema_sha
        for query, expected_sha in (
            (string_query, string_dump_sha),
            (list_query, list_dump_sha),
        ):
            instance = model.model_validate(query)
            assert tuple(instance.model_dump(mode="python")) == field_order
            assert tuple(instance.model_dump(mode="json")) == field_order
            assert _ordered_sha(instance.model_dump(mode="python")) == expected_sha
            assert _ordered_sha(instance.model_dump(mode="json")) == expected_sha
        with pytest.raises(ValidationError):
            model.model_validate({**string_query, "symbol": 17})


def test_remaining_multisymbol_leaf_schemas_and_dumps_match_prechange_baseline() -> None:
    remaining_cases = (
        (
            "YfinanceMultiSymbol",
            YfinanceMultiSymbolQuery,
            {"symbol": "AAPL"},
            {"symbol": ["AAPL", "MSFT"]},
            "264d2534aa8a5bdb2110a7b6d093d306e1a5caffc721f081523a15deea496343",
            "5d9c692f3ed9bef751fe3733f23163bf8fe9516c3e93759d8cf5ff36aa0ccaa1",
            "f1b8e9d72e3198d79b6adb3112ecfd9dfe078f5fdb08e5491ca0c5d0f7bbdb24",
            ("source", "market", "symbol", "start_date", "end_date"),
        ),
        (
            "EiaShortTermEnergyOutlook",
            ShortTermEnergyOutlookQuery,
            {"symbol": "PET.WCESTUS1.M"},
            {"symbol": ["SERIES.A", "SERIES.B"]},
            "f72b4912c4781b3d307c2de9f41dc32b840055e75d5b80c5a51454ebdb6a5d93",
            "3787851487d8b969a75e35353ce5eb88ddbb829e4cbee9a1e94ac6322d20b3fa",
            "fb643e2dd53ac1222471bdd6461b558f5e356611750ea07990381739fa44c376",
            (
                "source",
                "market",
                "symbol",
                "start_date",
                "end_date",
                "table",
                "release_date",
                "frequency",
            ),
        ),
    )
    for (
        _name,
        model,
        string_query,
        list_query,
        schema_sha,
        string_dump_sha,
        list_dump_sha,
        field_order,
    ) in remaining_cases:
        assert tuple(model.model_fields) == field_order
        assert _ordered_sha(model.model_json_schema(mode="validation")) == schema_sha
        for query, expected_sha in (
            (string_query, string_dump_sha),
            (list_query, list_dump_sha),
        ):
            instance = model.model_validate(query)
            assert tuple(instance.model_dump(mode="python")) == field_order
            assert tuple(instance.model_dump(mode="json")) == field_order
            assert _ordered_sha(instance.model_dump(mode="python")) == expected_sha
            assert _ordered_sha(instance.model_dump(mode="json")) == expected_sha
        with pytest.raises(ValidationError):
            model.model_validate({**string_query, "symbol": 17})


def test_query_schema_service_accepts_registered_multisymbol_query() -> None:
    registry = ProviderRegistry()
    registry.register_provider_models("cboe", ("IndexHistorical",), CboeIndexHistoricalFetcher())
    descriptor = find_provider_model(registry, "cboe", "IndexHistorical")

    schema = query_schema_for_model(registry, descriptor)

    assert schema == CboeIndexHistoricalQuery.model_json_schema(mode="validation")
    assert (
        schema["properties"]["symbol"]
        == CboeIndexHistoricalQuery.model_json_schema(mode="validation")["properties"]["symbol"]
    )


def test_three_reviewed_capture_query_hashes_and_field_order_are_unchanged() -> None:
    cases = (
        (
            "fred",
            "FredSeries",
            FredSeriesQuery,
            {
                "source": "fred",
                "market": "us",
                "series_id": "CPIAUCSL",
                "start_date": date(2024, 1, 1),
                "end_date": date(2024, 2, 1),
            },
            [
                {
                    "date": "2024-01-01",
                    "value": ".",
                    "realtime_start": "2024-01-01",
                    "realtime_end": "2024-02-01",
                },
                {
                    "date": "2024-01-01",
                    "value": "3.1",
                    "realtime_start": "2024-02-02",
                    "realtime_end": "9999-12-31",
                },
            ],
            "cb66cb188c40d274287f553a386966b61000a497e2be010e2e95f15a975a3947",
            "319ca744be091dd71cc2814bc26412ec7f7caf4eeceadb7dc8e5d9a7225e9f38",
            "4797f43402ce997ab3462410ecc9233f0d37c54586f6e7b33a2ce8a8a514c7b2",
        ),
        (
            "bls",
            "BlsSeries",
            BlsSeriesQuery,
            {
                "source": "bls",
                "market": "us",
                "series_ids": ["LNS14000000"],
                "start_year": 2025,
                "end_year": 2025,
                "max_requests": 2,
            },
            [
                {
                    "series_id": "LNS14000000",
                    "year": "2025",
                    "period": "M13",
                    "period_name": "Annual average",
                    "value": "4.1",
                    "footnotes": [{"code": "P", "text": "Preliminary."}],
                    "latest": None,
                    "api_version": "v2",
                }
            ],
            "c8aeed819556f814f77b0dca6efb703f77abb8aac6732e9bd6566f614939e4c6",
            "654f05d527043b05f618a7b51a450166bb49b3be5df0239fb0b8b5a39f9042f7",
            "a7e6eeade8ff6acbd4b70bfaff5a8b99639302f205286947edf9cdc34560776d",
        ),
        (
            "fmp",
            "EquityHistorical",
            EquityHistoricalQuery,
            {"source": "fmp", "market": "us", "symbol": "BRK.B"},
            [
                {
                    "symbol": "BRK.B",
                    "date": "2026-01-02",
                    "open": 100.0,
                    "high": 103.0,
                    "low": 99.5,
                    "close": 102.0,
                    "volume": 2**64 + 1,
                    "change": -0.0,
                    "changePercent": 0.25,
                    "vwap": 101.0,
                }
            ],
            "eb72781a579d052ef416c4237ff9ab291df1c25ba172e30a41845e0b21887ed9",
            "5e6c15e93f32fc2a4125d08e8cdbdbcbad0cb8fd9c79d240d77fd0a905cafd8d",
            "fe4749cd3d51ccf6d6030a7284b79d37355ff46a16b93c27de26c576db55de51",
        ),
    )
    for (
        source,
        model_name,
        query_model,
        query_values,
        raw,
        expected_query_sha,
        expected_raw_sha,
        expected_capture_sha,
    ) in cases:
        params = query_model.model_validate(query_values)
        capture = make_provider_model_capture(
            source=source,
            model=model_name,
            params=params,
            raw=raw,
        )
        assert capture.query_sha256 == expected_query_sha
        assert capture.raw_sha256 == expected_raw_sha
        assert (
            _ordered_sha(
                {
                    "source": capture.source,
                    "model": capture.model,
                    "raw_json": capture.raw_json,
                    "query_json": capture.query_json,
                    "raw_sha256": capture.raw_sha256,
                    "query_sha256": capture.query_sha256,
                    "raw_row_count": capture.raw_row_count,
                    "raw_scope": capture.raw_scope,
                    "schema_version": capture.schema_version,
                }
            )
            == expected_capture_sha
        )
