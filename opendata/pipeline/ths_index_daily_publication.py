"""Fail-closed publication jobs for four separate THS daily index series.

The default builders remain disabled and unreviewed. A separate CSI 500
one-shot builder accepts a fixed, user-declared internal-use scope and an
already-provisioned persistent source gate. It can only fetch one explicit
window of at most ten years, with one physical transport attempt. The source
history boundary and cross-execution attempt ceiling remain UNKNOWN.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
)
from opendata.data.models.market import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.endpoints import INDEX_PRICES_ENDPOINT, MAX_WINDOW
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_DATASET_ID_BY_SYMBOL,
    INDEX_JOB_ID_BY_SYMBOL,
    INDEX_SCHEMA_VERSION,
    INDEX_SEMANTIC_VERSION,
    SSE50_INDEX_SYMBOL,
    build_index_daily_contract,
)
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import SourceAttemptGate
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
_CSI500_SYMBOL = "000905.SH"
_CSI500_DATASET_ID = INDEX_DATASET_ID_BY_SYMBOL[_CSI500_SYMBOL]
# The provider receives an inclusive caller range and converts its end to a
# half-open endpoint range. Its inclusive day count equals that resulting span.
_MAX_WINDOW_CALENDAR_DAYS = MAX_WINDOW.days
_MAX_TRANSPORT_ATTEMPTS = 1
_SOURCE_RIGHTS_EVIDENCE = "docs/data-rights-registry.md#0.1"
_HISTORY_BOUNDARY_UNKNOWN: Final[Literal["UNKNOWN"]] = "UNKNOWN"
_ALLOWED_INTERNAL_PURPOSES = (
    "automatic_collection",
    "storage_in_opendata",
    "internal_opendata_to_afi_api",
)
_QUALITY = {"status": "unknown", "report_id": None, "issue_count": 0}

__all__ = [
    "SSE50_INDEX_JOB_ID",
    "SSE50_INDEX_JOB_VERSION",
    "Csi500FuyaoSourceAccess",
    "build_authorized_csi500_index_daily_job",
    "build_csi500_manual_backfill_payload",
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


@dataclass(frozen=True, slots=True, repr=False)
class Csi500FuyaoSourceAccess:
    """Narrow CSI 500 binding to the user's declared Fuyao internal use.

    The rights basis is ``USER_DECLARED`` and is not independent supplier
    verification. The gate and binding must refer to an existing persistent
    THS ``index_daily`` QUERY grant for the Fuyao host; each physical send is
    still reserved by that gate. Its exact expected evidence reference is
    carried into the transport attempt and compared with the persisted grant
    in the reservation transaction. This value grants no external
    redistribution or completeness claim.
    """

    source_attempt_gate: SourceAttemptGate = field(repr=False, compare=False)
    source_attempt_binding: SourceAttemptBinding = field(repr=False, compare=False)
    operator_confirms_user_declared_internal_use: bool

    def __post_init__(self) -> None:
        """Require explicit confirmation and a trusted, unbound run identity."""
        if type(self.source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be an exact SourceAttemptGate")
        if type(self.source_attempt_binding) is not SourceAttemptBinding:
            raise TypeError("source_attempt_binding must be an exact SourceAttemptBinding")
        if self.source_attempt_binding.run_key is not None:
            raise ValueError("source_attempt_binding must leave run_key unset")
        if type(self.operator_confirms_user_declared_internal_use) is not bool:
            raise TypeError("operator confirmation must be an explicit boolean")
        if not self.operator_confirms_user_declared_internal_use:
            raise ValueError("Fuyao CSI 500 internal-use authorization was not confirmed")

    @property
    def evidence_reference(self) -> str:
        """Return the registry reference for the user's documented declaration."""
        return _SOURCE_RIGHTS_EVIDENCE

    @property
    def authorization_basis(self) -> Literal["USER_DECLARED"]:
        """Identify this as a user declaration, not verified supplier terms."""
        return "USER_DECLARED"

    @property
    def symbol(self) -> str:
        """Return the one index symbol bound by this access record."""
        return _CSI500_SYMBOL

    @property
    def dataset_id(self) -> str:
        """Return the one dataset bound by this access record."""
        return _CSI500_DATASET_ID

    @property
    def endpoint_path(self) -> str:
        """Return the one Fuyao endpoint bound by this access record."""
        return INDEX_PRICES_ENDPOINT

    @property
    def operation(self) -> Literal["query"]:
        """Return the only permitted source transport operation."""
        return "query"

    @property
    def purposes(self) -> tuple[str, ...]:
        """Return the three internal uses already declared by the user."""
        return _ALLOWED_INTERNAL_PURPOSES

    @property
    def maximum_window_calendar_days(self) -> int:
        """Return the endpoint's per-request technical ceiling, not a rights quota."""
        return _MAX_WINDOW_CALENDAR_DAYS

    @property
    def source_history_boundary(self) -> Literal["UNKNOWN"]:
        """Keep the earliest source-available date explicitly unknown."""
        return _HISTORY_BOUNDARY_UNKNOWN

    @property
    def external_distribution_allowed(self) -> Literal[False]:
        """The user's declaration does not authorize external redistribution."""
        return False

    def binding_for_run(self, run_key: str) -> SourceAttemptBinding:
        """Attach the trusted grant identity to this exact one-shot run."""
        if type(run_key) is not str or not run_key.strip():
            raise ValueError("run_key must be a non-empty string")
        return SourceAttemptBinding(
            grant_id=self.source_attempt_binding.grant_id,
            expected_revision=self.source_attempt_binding.expected_revision,
            principal_scope=self.source_attempt_binding.principal_scope,
            run_key=run_key,
            expected_rights_evidence_reference=self.evidence_reference,
        )

    def __repr__(self) -> str:
        """Keep grant identifiers out of routine logs and tracebacks."""
        return "Csi500FuyaoSourceAccess(<redacted>)"


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
    _validate_date_order(start_date, end_date)
    return RunRequest(
        run_type="backfill",
        query={},
        partition={"symbol": symbol},
        idempotency_key=idempotency_key,
        backfill_start=start_date,
        backfill_end=end_date,
    )


def build_csi500_manual_backfill_payload(
    *,
    source_access: Csi500FuyaoSourceAccess,
    start_date: date,
    end_date: date,
    idempotency_key: str,
) -> dict[str, JsonValue]:
    """Build one dispatcher payload for a CSI 500 one-shot historical window.

    The inclusive window is capped so the provider's half-open conversion fits
    within one THS endpoint chunk. The cap is not evidence of the source's
    earliest available date, a retention promise, or a cross-execution quota.
    """
    _require_csi500_source_access(source_access)
    _validate_window(start_date, end_date)
    request = RunRequest(
        run_type="manual",
        query={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        partition={"symbol": _CSI500_SYMBOL},
        idempotency_key=idempotency_key,
    )
    return {
        "query": request.query,
        "partition": request.partition,
        "shard": request.shard,
        "idempotency_key": request.idempotency_key,
    }


def build_sse50_index_daily_job(
    contract_registry: DatasetContractRegistry,
) -> ReviewedJob:
    """Build the disabled, unreviewed THS SSE 50 publication job."""
    return build_index_daily_job(SSE50_INDEX_SYMBOL, contract_registry)


def build_authorized_csi500_index_daily_job(
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoSourceAccess,
) -> ReviewedJob:
    """Build an explicitly injected, unscheduled CSI 500 one-shot job.

    The returned job is enabled only for the dedicated one-shot dispatcher; it
    stays disabled and unscheduled, and cannot enter ordinary backfill or
    scheduled dispatch. The caller must inject an existing persistent source
    gate binding. Its send-time reservations, not this builder, authorize
    physical requests.
    """
    _require_csi500_source_access(source_access)
    return _build_index_daily_job(
        _CSI500_SYMBOL,
        contract_registry,
        source_access=source_access,
    )


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
    return _build_index_daily_job(symbol, contract_registry, source_access=None)


def _build_index_daily_job(
    symbol: str,
    contract_registry: DatasetContractRegistry,
    *,
    source_access: Csi500FuyaoSourceAccess | None,
) -> ReviewedJob:
    """Build the shared per-symbol job, with opt-in one-shot access if bound."""
    dataset_id = _validate_symbol(symbol)
    if source_access is not None:
        _require_csi500_source_access(source_access)
        if symbol != _CSI500_SYMBOL or dataset_id != source_access.dataset_id:
            raise ValueError("source access is bound only to the CSI 500 dataset")
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
        version=_job_version_for(source_access),
        dataset_id=dataset_id,
        provider=_PROVIDER,
        model=_MODEL,
        timezone="Asia/Shanghai",
        calendar=None,
        schedule=None,
        enabled=False,
    )
    fetcher = ThsIndexDailyFetcher(
        source_attempt_gate=(
            source_access.source_attempt_gate if source_access is not None else None
        ),
        max_attempts=_MAX_TRANSPORT_ATTEMPTS if source_access is not None else None,
    )
    writer = ValidatedPublicationWriter(contract_registry)
    one_shot_only = source_access is not None

    def fetch(request: RunRequest, context: RunExecutionContext) -> object:
        """Fetch one explicitly requested range through the registered THS provider."""
        start, end = _validate_request(request, symbol, one_shot_only=one_shot_only)
        context.checkpoint()
        if source_access is None:
            _require_ths_source_authorization()
        else:
            budget = _csi500_one_attempt_budget()
            binding = source_access.binding_for_run(context.run_key)
            with request_execution_scope(
                source=_PROVIDER,
                canonical_model=_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=binding,
            ):
                result = fetcher.fetch(
                    ctx=FetchContext(
                        timeout=_FETCH_TIMEOUT_SECONDS,
                        request_budget=budget,
                    ),
                    symbol=symbol,
                    start_date=start,
                    end_date=end,
                )
        if source_access is None:
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
        start, end = _validate_request(request, symbol, one_shot_only=one_shot_only)
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
                "source_rights_status": (
                    "USER_DECLARED_INTERNAL_USE_UNVERIFIED"
                    if source_access is not None
                    else "UNKNOWN"
                ),
                "source_rights_basis": (
                    source_access.authorization_basis if source_access is not None else "UNKNOWN"
                ),
                "source_rights_evidence_reference": (
                    source_access.evidence_reference if source_access is not None else None
                ),
                "source_endpoint_path": (
                    source_access.endpoint_path if source_access is not None else None
                ),
                "source_operation": (
                    source_access.operation if source_access is not None else "UNKNOWN"
                ),
                "source_allowed_uses": (
                    list(source_access.purposes) if source_access is not None else []
                ),
                "source_external_distribution_allowed": False,
                "source_comparison_status": "unknown",
                "source_data_origin_status": "unknown",
                "coverage_status": "unknown",
                "source_history_boundary": _HISTORY_BOUNDARY_UNKNOWN,
                "source_history_boundary_evidence": None,
                "source_cross_execution_attempt_limit": "UNKNOWN",
                "source_per_request_calendar_day_limit": _MAX_WINDOW_CALENDAR_DAYS,
                "source_per_run_transport_attempt_limit": (
                    _MAX_TRANSPORT_ATTEMPTS if source_access is not None else None
                ),
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
        _validate_request(request, symbol, one_shot_only=one_shot_only)
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
        reviewed=one_shot_only,
        one_shot_only=one_shot_only,
    )


def _require_ths_source_authorization() -> None:
    """Refuse real source acquisition until a reviewed rights decision exists."""
    raise ThsIndexSourceAuthorizationUnknownError


def _require_csi500_source_access(value: object) -> Csi500FuyaoSourceAccess:
    """Require the exact CSI 500 user-declared source access binding."""
    if type(value) is not Csi500FuyaoSourceAccess:
        raise TypeError("source_access must be an exact Csi500FuyaoSourceAccess")
    return value


def _validate_window(start_date: object, end_date: object) -> tuple[date, date]:
    """Validate one inclusive date range against the endpoint's ten-year cap."""
    start, end = _validate_date_order(start_date, end_date)
    inclusive_days = (end - start).days + 1
    if inclusive_days > _MAX_WINDOW_CALENDAR_DAYS:
        raise ValueError("requested window exceeds the THS ten-year per-request limit")
    return start, end


def _validate_date_order(start_date: object, end_date: object) -> tuple[date, date]:
    """Validate date types and inclusive ordering without imposing a span cap."""
    if type(start_date) is not date:
        raise TypeError("start_date must be a date")
    if type(end_date) is not date:
        raise TypeError("end_date must be a date")
    if end_date < start_date:
        raise ValueError("end_date must not be before start_date")
    return start_date, end_date


def _csi500_one_attempt_budget() -> RequestBudget:
    """Build the fixed in-memory grant for one exact CSI 500 source send."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    if not host:
        raise ValueError("Fuyao default API host is unavailable")
    grant = RequestGrant(
        source=_PROVIDER,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=_SOURCE_RIGHTS_EVIDENCE,
        task_attempts=_MAX_TRANSPORT_ATTEMPTS,
        source_attempts=_MAX_TRANSPORT_ATTEMPTS,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return RequestBudget(
        task_attempts=_MAX_TRANSPORT_ATTEMPTS,
        source_attempts=_MAX_TRANSPORT_ATTEMPTS,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )


def _job_version_for(source_access: Csi500FuyaoSourceAccess | None) -> str:
    """Pin one-shot run snapshots to the exact grant revision and fixed scope."""
    if source_access is None:
        return SSE50_INDEX_JOB_VERSION
    binding = source_access.source_attempt_binding
    scope = "\x00".join(
        (
            source_access.evidence_reference,
            source_access.dataset_id,
            source_access.symbol,
            source_access.endpoint_path,
            source_access.operation,
            *source_access.purposes,
            str(source_access.maximum_window_calendar_days),
            str(_MAX_TRANSPORT_ATTEMPTS),
            binding.grant_id,
            str(binding.expected_revision),
            binding.principal_scope,
        )
    )
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
    return f"{SSE50_INDEX_JOB_VERSION}.fuyao-{digest}"


def _validate_symbol(symbol: str) -> str:
    """Resolve one exact whitelisted symbol to its dedicated dataset identity."""
    if not isinstance(symbol, str) or symbol not in INDEX_DATASET_ID_BY_SYMBOL:
        raise ValueError("symbol must be one of the four reviewed benchmark codes")
    return INDEX_DATASET_ID_BY_SYMBOL[symbol]


def _validate_request(
    request: RunRequest,
    symbol: str,
    *,
    one_shot_only: bool = False,
) -> tuple[date, date]:
    """Require the exact fixed-symbol backfill or one-shot window shape."""
    if not isinstance(request, RunRequest):
        raise TypeError("request must be a RunRequest")
    if request.partition != {"symbol": symbol} or request.shard is not None:
        raise ValueError("request must be a fixed-symbol index backfill")
    if one_shot_only:
        if (
            request.run_type != "manual"
            or set(request.query) != {"start_date", "end_date"}
            or request.backfill_start is not None
            or request.backfill_end is not None
        ):
            raise ValueError("one-shot request must contain only one explicit date window")
        start = _parse_iso_date(request.query["start_date"], "start_date")
        end = _parse_iso_date(request.query["end_date"], "end_date")
        return _validate_window(start, end)
    if (
        request.run_type != "backfill"
        or request.query != {}
        or request.backfill_start is None
        or request.backfill_end is None
    ):
        raise ValueError("request must be a fixed-symbol index backfill")
    return _validate_date_order(request.backfill_start, request.backfill_end)


def _parse_iso_date(value: object, name: str) -> date:
    """Parse an exact canonical ISO calendar date from the manual query."""
    if type(value) is not str:
        raise ValueError(f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date string") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date string")
    return parsed


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
