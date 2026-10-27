"""Immutable publication contracts for four separately published THS indices.

Each supported symbol gets its own dataset identity. This keeps v2 record reads
single-series even though the API does not expose a general ``symbol`` filter.
The supported set is intentionally an exact whitelist, not an index catalog.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from opendata.data.platform_contracts import (
    DatasetContract,
    FieldContract,
    SourceIdentity,
)

INDEX_DATASET_ID_BY_SYMBOL: Mapping[str, str] = MappingProxyType(
    {
        "000016.SH": "cn.sse50.index.daily",
        "000300.SH": "cn.csi300.index.daily",
        "000905.SH": "cn.csi500.index.daily",
        "000906.SH": "cn.csi800.index.daily",
    }
)
INDEX_JOB_ID_BY_SYMBOL: Mapping[str, str] = MappingProxyType(
    {
        "000016.SH": "ths-sse50-index-daily",
        "000300.SH": "ths-csi300-index-daily",
        "000905.SH": "ths-csi500-index-daily",
        "000906.SH": "ths-csi800-index-daily",
    }
)
INDEX_SYMBOL_BY_DATASET_ID: Mapping[str, str] = MappingProxyType(
    {dataset_id: symbol for symbol, dataset_id in INDEX_DATASET_ID_BY_SYMBOL.items()}
)
INDEX_SCHEMA_VERSION = "index-ohlcv-v1"
INDEX_SEMANTIC_VERSION = "ths-index-points-and-shares-v1"
INDEX_SOURCE_ID = "ths.index_daily"
SSE50_INDEX_SYMBOL = "000016.SH"
SSE50_INDEX_DATASET_ID = INDEX_DATASET_ID_BY_SYMBOL[SSE50_INDEX_SYMBOL]
SSE50_INDEX_SOURCE_ID = INDEX_SOURCE_ID
SSE50_INDEX_SCHEMA_VERSION = INDEX_SCHEMA_VERSION
SSE50_INDEX_SEMANTIC_VERSION = INDEX_SEMANTIC_VERSION

__all__ = [
    "INDEX_DATASET_ID_BY_SYMBOL",
    "INDEX_JOB_ID_BY_SYMBOL",
    "INDEX_SCHEMA_VERSION",
    "INDEX_SEMANTIC_VERSION",
    "INDEX_SOURCE_ID",
    "INDEX_SYMBOL_BY_DATASET_ID",
    "SSE50_INDEX_DATASET_ID",
    "SSE50_INDEX_SCHEMA_VERSION",
    "SSE50_INDEX_SEMANTIC_VERSION",
    "SSE50_INDEX_SOURCE_ID",
    "SSE50_INDEX_SYMBOL",
    "build_index_daily_contract",
    "build_sse50_index_daily_contract",
]


def build_index_daily_contract(symbol: str) -> DatasetContract:
    """Build the OHLCV contract dedicated to one whitelisted index symbol.

    Open, high, low, and close are index points; the THS mapping labels index
    volume as shares. The mapping and independent OHLCV comparisons are
    recorded in ``opendata/data/mappings/ths.yaml`` and
    ``docs/evidence/C5/README.md``. C5 compared ``000300.SH`` and ``000905.SH``
    among other indices; it did not compare ``000016.SH`` or ``000906.SH``.
    Provider turnover is excluded: the available independent comparison does
    not cover index turnover, so this contract does not assign it a unit.

    Args:
        symbol: Exact supported THS index code including its market suffix.

    Returns:
        A strict business-date OHLCV contract for one symbol and dataset.

    Raises:
        ValueError: If ``symbol`` is not in the four-code whitelist.
    """
    if not isinstance(symbol, str) or symbol not in INDEX_DATASET_ID_BY_SYMBOL:
        raise ValueError("symbol must be one of the four reviewed benchmark codes")
    return DatasetContract(
        dataset_id=INDEX_DATASET_ID_BY_SYMBOL[symbol],
        schema_version=INDEX_SCHEMA_VERSION,
        semantic_version=INDEX_SEMANTIC_VERSION,
        asset_class="index",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
        ),
        business_key=("symbol", "trade_date"),
        ordering=("trade_date", "symbol"),
        temporal_semantics="business_date",
        time_field="trade_date",
        source=SourceIdentity(source_id=INDEX_SOURCE_ID, source="ths"),
        field_units={
            "open": "index_points",
            "high": "index_points",
            "low": "index_points",
            "close": "index_points",
            "volume": "shares",
        },
        adjustment_basis="not_applicable",
        field_adjustments={
            "open": "not_applicable",
            "high": "not_applicable",
            "low": "not_applicable",
            "close": "not_applicable",
            "volume": "not_applicable",
        },
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def build_sse50_index_daily_contract() -> DatasetContract:
    """Build the SSE 50 contract for callers of the original single-series API."""
    return build_index_daily_contract(SSE50_INDEX_SYMBOL)
