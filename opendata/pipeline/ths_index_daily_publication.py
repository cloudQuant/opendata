"""Fail-closed publication jobs for four separate THS daily index series.

Each job is bound to one exact whitelisted symbol and dataset and is returned
disabled and unreviewed. THS source authorization is UNKNOWN, so production
fetches stop before constructing or calling the provider. Offline tests may
replace that private guard while supplying fixture transport responses; the
job exposes no production source-injection switch.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING

from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
)
from opendata.data.models.market import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_DATASET_ID_BY_SYMBOL,
    INDEX_JOB_ID_BY_SYMBOL,
    INDEX_SCHEMA_VERSION,
    INDEX_SEMANTIC_VERSION,
    SSE50_INDEX_SYMBOL,
    build_index_daily_contract,
)
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from pydantic import JsonValue
    from sqlalchemy.engine import Connection

SSE50_INDEX_JOB_ID = INDEX_JOB_ID_BY_SYMBOL[SSE50_INDEX_SYMBOL]
SSE50_INDEX_JOB_VERSION = "publication-v1"
_PROVIDER = "ths"
_MODEL = "index_daily"
_FETCH_TIMEOUT_SECONDS = 30
_QUALITY = {"status": "unknown", "report_id": None, "issue_count": 0}

__all__ = [
    "SSE50_INDEX_JOB_ID",
    "SSE50_INDEX_JOB_VERSION",
    "build_index_daily_backfill_request",
    "build_index_daily_job",
    "build_sse50_index_daily_backfill_request",
    "build_sse50_index_daily_job",
]


class ThsIndexSourceAuthorizationUnknownError(PermissionError):
    """Raised while THS rights for production acquisition remain unreviewed."""

    def __init__(self) -> None:
        """Use a fixed error message that cannot disclose provider credentials."""
        super().__init__("THS index source authorization is UNKNOWN")


def build_sse50_index_daily_backfill_request(
    *,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> RunRequest:
    """Build the first-series SSE 50 backfill request."""
    return build_index_daily_backfill_request(
        symbol=SSE50_INDEX_SYMBOL,
        start_date=start_date,
        end_date=end_date,
        idempotency_key=idempotency_key,
    )


def build_index_daily_backfill_request(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> RunRequest:
    """Build an inclusive backfill request for exactly one whitelisted index.

    Args:
        symbol: Exact supported symbol including its market suffix.
        start_date: First requested trading date, inclusive.
        end_date: Last requested trading date, inclusive.
        idempotency_key: Caller-supplied stable identifier for this backfill.

    Returns:
        A validated request pinned to the requested single-index dataset.
    """
    _validate_symbol(symbol)
    if type(start_date) is not date:
        raise TypeError("start_date must be a date")
    if type(end_date) is not date:
        raise TypeError("end_date must be a date")
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")
    return RunRequest(
        run_type="backfill",
        query={},
        partition={"symbol": symbol},
        idempotency_key=idempotency_key,
        backfill_start=start_date,
        backfill_end=end_date,
    )


def build_sse50_index_daily_job(
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled, unreviewed THS SSE 50 publication job."""
    return build_index_daily_job(SSE50_INDEX_SYMBOL, contract_registry)


def build_index_daily_job(
    symbol: str,
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled publication job for one whitelisted THS index.

    The job uses the registered THS provider, normalizer, and immutable v2
    publication writer. Its fixed task is intentionally not scheduled or
    enabled until source rights and dataset-specific validation are reviewed.
    Each symbol maps to its own dataset and job identity, so the records route
    never combines index series that lack a request-time symbol filter.

    Args:
        symbol: Exact supported index symbol including its market suffix.
        contract_registry: Explicit registry containing the exact per-symbol
            OHLCV contract produced by :func:`build_index_daily_contract`.

    Returns:
        A disabled and unreviewed job bound to one exact symbol and dataset.

    Raises:
        ValueError: If the symbol is not whitelisted or its contract mismatches.
        TypeError: If the supplied dependency is not a contract registry.
    """
    dataset_id = _validate_symbol(symbol)
    if not isinstance(contract_registry, DatasetContractRegistry):
        raise TypeError("contract_registry must be a DatasetContractRegistry")
    expected_contract = build_index_daily_contract(symbol)
    try:
        registered_contract = contract_registry.resolve(
            dataset_id,
            INDEX_SCHEMA_VERSION,
            INDEX_SEMANTIC_VERSION,
        )
    except DatasetContractRegistryError as exc:
        raise ValueError("the exact single-index contract is not registered") from exc
    if canonical_sha256(registered_contract) != canonical_sha256(expected_contract):
        raise ValueError("the registered single-index contract does not match this job")

    job_task = TaskDefinition(
        job_id=INDEX_JOB_ID_BY_SYMBOL[symbol],
        version=SSE50_INDEX_JOB_VERSION,
        dataset_id=dataset_id,
        provider=_PROVIDER,
        model=_MODEL,
        timezone="Asia/Shanghai",
        calendar=None,
        schedule=None,
        enabled=False,
    )
    fetcher = ThsIndexDailyFetcher()
    writer = ValidatedPublicationWriter(contract_registry)

    def fetch(request: RunRequest, context: RunExecutionContext) -> object:
        """Fetch one explicitly requested range through the registered THS provider."""
        start, end = _validate_request(request, symbol)
        context.checkpoint()
        _require_ths_source_authorization()
        result = fetcher.fetch(
            ctx=FetchContext(timeout=_FETCH_TIMEOUT_SECONDS),
            symbol=symbol,
            start_date=start,
            end_date=end,
        )
        context.checkpoint()
        return result

    def normalize(
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        """Validate one provider result and serialize only this index's OHLCV."""
        start, end = _validate_request(request, symbol)
        context.checkpoint()
        if type(raw) is not tuple or not raw:
            raise ValueError("THS index daily result must be a non-empty tuple")
        bars: list[Bar] = []
        for row in raw:
            if not isinstance(row, Bar):
                raise ValueError("THS index daily result contains an invalid row")
            _validate_bar(row, start, end, symbol)
            bars.append(row)
        trade_dates = [bar.trade_date for bar in bars]
        if trade_dates != sorted(set(trade_dates)):
            raise ValueError("THS index daily rows must be unique and date ordered")

        records = tuple(_record_for_bar(bar, symbol) for bar in bars)
        return NormalizedBatch(
            records=records,
            schema_version=INDEX_SCHEMA_VERSION,
            semantic_version=INDEX_SEMANTIC_VERSION,
            unit="per-field",
            adjustment_basis="not_applicable",
            quality=dict(_QUALITY),
            provenance={
                "provider": _PROVIDER,
                "model": _MODEL,
                "dataset_id": dataset_id,
                "source_rights_status": "unknown",
                "source_comparison_status": "unknown",
                "source_data_origin_status": "unknown",
                "coverage_status": "unknown",
                "source_history_boundary": "unknown",
                "requested_start_inclusive": start.isoformat(),
                "requested_end_inclusive": end.isoformat(),
            },
        )

    def publish(
        connection: Connection,
        task: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        idempotency_key: str,
    ) -> PublicationReceipt:
        """Enforce the fixed dataset/symbol boundary before the shared writer."""
        _validate_request(request, symbol)
        if (
            task.job_id != job_task.job_id
            or task.version != job_task.version
            or task.dataset_id != dataset_id
            or task.provider != _PROVIDER
            or task.model != _MODEL
        ):
            raise ValueError("publication task does not match this single-index job")
        if not batch.records:
            raise ValueError("empty index batches cannot be published")
        for record in batch.records:
            if not isinstance(record, NormalizedRecord) or not isinstance(record.payload, Mapping):
                raise ValueError("publication batch contains an invalid index record")
            if record.payload.get("symbol") != symbol:
                raise ValueError("publication batch contains a different index symbol")
        if (
            batch.schema_version != INDEX_SCHEMA_VERSION
            or batch.semantic_version != INDEX_SEMANTIC_VERSION
        ):
            raise ValueError("publication batch does not match this single-index contract")
        return writer(connection, task, request, batch, idempotency_key)

    return ReviewedJob(
        task=job_task,
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=False,
    )


def _require_ths_source_authorization() -> None:
    """Refuse real source acquisition until a reviewed rights decision exists."""
    raise ThsIndexSourceAuthorizationUnknownError


def _validate_symbol(symbol: str) -> str:
    """Resolve one exact whitelisted symbol to its dedicated dataset identity."""
    if not isinstance(symbol, str) or symbol not in INDEX_DATASET_ID_BY_SYMBOL:
        raise ValueError("symbol must be one of the four reviewed benchmark codes")
    return INDEX_DATASET_ID_BY_SYMBOL[symbol]


def _validate_request(request: RunRequest, symbol: str) -> tuple[date, date]:
    """Require the exact fixed-symbol backfill request shape."""
    if not isinstance(request, RunRequest):
        raise TypeError("request must be a RunRequest")
    if (
        request.run_type != "backfill"
        or request.query != {}
        or request.partition != {"symbol": symbol}
        or request.shard is not None
        or request.backfill_start is None
        or request.backfill_end is None
    ):
        raise ValueError("request must be a fixed-symbol index backfill")
    return request.backfill_start, request.backfill_end


def _validate_bar(bar: Bar, start: date, end: date, symbol: str) -> None:
    """Reject rows outside the requested index or with invalid numeric values."""
    if bar.symbol != symbol:
        raise ValueError("THS provider returned a different index symbol")
    if not start <= bar.trade_date <= end:
        raise ValueError("THS provider returned a date outside the requested window")
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise ValueError("THS provider returned a non-finite OHLCV value")
    if any(value < 0 for value in values):
        raise ValueError("THS provider returned a negative OHLCV value")
    if (
        bar.high < bar.low
        or bar.high < bar.open
        or bar.high < bar.close
        or bar.low > bar.open
        or bar.low > bar.close
    ):
        raise ValueError("THS provider returned inconsistent OHLC values")


def _record_for_bar(bar: Bar, symbol: str) -> NormalizedRecord:
    """Create a business-date row without inventing source timestamps."""
    trade_date = bar.trade_date.isoformat()
    payload: dict[str, JsonValue] = {
        "symbol": symbol,
        "trade_date": trade_date,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }
    return NormalizedRecord(
        business_key=json.dumps(
            [symbol, trade_date],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        payload=payload,
        business_date=bar.trade_date,
    )
