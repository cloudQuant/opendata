"""Deterministic, offline planning for registry-backed stock-daily history requests.

The planner turns reviewed lifecycle and source-alias evidence into candidate
``RunRequest`` objects that match the existing stock-daily job. It never loads a
universe, reads stored publications, contacts a provider, dispatches work, or
certifies that a candidate can be executed or that history is complete.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.platform_contracts import DatasetContract, canonical_sha256
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models.stock_daily import AkshareStockDailyFetcher
from opendata.data.providers.akshare.stock_daily_wire import MAX_STOCK_DAILY_WINDOW_DAYS
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.stock_daily_job import (
    _revalidate_task,
    _validate_stock_contract,
    _validated_query,
)

PLAN_VERSION: Final[str] = "stock-daily-backfill-plan-v1"
_BJ_PREFIXES: Final[tuple[str, ...]] = ("4", "8", "920")


class BackfillBlockReason(str, Enum):
    """A preserved reason that prevents a plan from claiming full readiness."""

    UNKNOWN_START_BOUNDARY = "unknown_start_boundary"
    UNKNOWN_LIFECYCLE_STATUS = "unknown_lifecycle_status"
    STALE_ACTIVE_STATUS = "stale_active_status"
    SOURCE_ALIAS_GAP = "source_alias_gap"
    AMBIGUOUS_SOURCE_ALIAS = "ambiguous_source_alias"
    INVALID_PROVIDER_SYMBOL = "invalid_provider_symbol"
    BJ_PROVIDER_MAPPING_UNVERIFIED = "bj_provider_mapping_unverified"
    DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED = "delisted_boundary_semantics_unverified"
    NOT_YET_IN_SCOPE = "not_yet_in_scope"
    UNIVERSE_SCOPE_UNATTESTED = "universe_scope_unattested"
    SOURCE_HISTORY_LIMIT_UNKNOWN = "source_history_limit_unknown"
    SOURCE_CAPABILITY_UNVERIFIED = "source_capability_unverified"
    COVERAGE_RECONCILIATION_REQUIRED = "coverage_reconciliation_required"
    DISPATCH_NOT_AUTHORIZED = "dispatch_not_authorized"


@dataclass(frozen=True, slots=True)
class BackfillBlocker:
    """One auditable blocker attached to a plan or a canonical instrument."""

    reason: BackfillBlockReason
    detail: str
    instrument_id: str | None = None
    range_start: date | None = None
    range_end: date | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StockDailyBackfillChunk:
    """One inclusive, source-alias-pinned candidate stock-daily request."""

    instrument_id: str
    lifecycle_version: str
    source_symbol: str
    provider_symbol: str
    alias_version: str
    alias_evidence_ref: str
    alias_valid_from: date | None
    alias_valid_through: date | None
    window_start: date
    window_end: date
    chunk_index: int
    idempotency_key: str
    request_payload_json: str
    request_payload_sha256: str

    @property
    def request(self) -> RunRequest:
        """Return a fresh validated RunRequest so nested JSON stays isolated."""
        return RunRequest.model_validate_json(self.request_payload_json, strict=True)

    @property
    def window_days(self) -> int:
        """Return the number of included calendar days in the closed window."""
        return (self.window_end - self.window_start).days + 1


@dataclass(frozen=True, slots=True)
class InstrumentBackfillResult:
    """Plan output for one registry lifecycle, including blocked instruments."""

    instrument_id: str
    lifecycle_version: str
    lifecycle_status: LifecycleStatus
    status_as_of: date
    status_evidence_ref: str
    known_start_date: date | None
    start_boundary_unknown: bool
    start_evidence_ref: str | None
    delisted_on: date | None
    delist_evidence_ref: str | None
    candidate_chunks: tuple[StockDailyBackfillChunk, ...]
    blockers: tuple[BackfillBlocker, ...]


@dataclass(frozen=True, slots=True)
class StockDailyBackfillPlan:
    """Immutable candidate plan pinned to the exact task, contract, and registry."""

    plan_version: str
    as_of: date
    dataset_id: str
    source_id: str
    provider: str
    job_id: str
    job_version: str
    model: str
    task_sha256: str
    contract_sha256: str
    registry_version: str
    registry_evidence_ref: str
    registry_sha256: str
    registry_lifecycle_count: int
    max_window_days: int
    universe_scope_complete: bool
    source_history_limit: str
    execution_authorized: bool
    instruments: tuple[InstrumentBackfillResult, ...]
    blockers: tuple[BackfillBlocker, ...]
    plan_sha256: str

    @property
    def candidate_chunks(self) -> tuple[StockDailyBackfillChunk, ...]:
        """Flatten candidate requests in deterministic instrument/chunk order."""
        return tuple(
            chunk for instrument in self.instruments for chunk in instrument.candidate_chunks
        )

    @property
    def full_history_ready(self) -> bool:
        """Return false because this plan does not attest universe or source completeness."""
        return False


def build_stock_daily_backfill_plan(
    registry: InstrumentReferenceRegistry,
    *,
    task: TaskDefinition,
    contract: DatasetContract,
    as_of: date,
    max_window_days: int = MAX_STOCK_DAILY_WINDOW_DAYS,
) -> StockDailyBackfillPlan:
    """Build a deterministic, non-dispatching plan from frozen registry evidence.

    The registry scope is selected by ``task.dataset_id`` and the exact
    ``contract.source.source_id``. Every lifecycle row in that scope is
    retained, including delisted and unknown rows. An independently evidenced
    start boundary and effective source alias are required before any candidate
    chunk is generated. All windows use inclusive business-date endpoints and
    are validated by the current stock-daily job adapter.

    Args:
        registry: Caller-supplied immutable lifecycle and alias registry.
        task: The exact stock-daily job declaration to which requests belong.
        contract: The exact dataset contract used by the stock-daily job.
        as_of: Explicit final business-date cutoff; no clock is consulted.
        max_window_days: Inclusive calendar-day cap, between 1 and 3660.

    Returns:
        A plan containing candidate requests, every scoped lifecycle, and
        blockers for unknown or unverified evidence. The plan is never
        execution-authorized.

    Raises:
        TypeError: If an input does not have its declared model type.
        ValueError: If the task/contract is not the current stock-daily shape,
            the requested date or limit is invalid, or the registry has no
            lifecycle rows for the exact dataset/source scope.
    """
    if not isinstance(registry, InstrumentReferenceRegistry):
        raise TypeError("registry must be an InstrumentReferenceRegistry")
    if not isinstance(contract, DatasetContract):
        raise TypeError("contract must be a DatasetContract")
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise ValueError("as_of must be a datetime.date, not a timestamp")
    if type(max_window_days) is not int or not 1 <= max_window_days <= MAX_STOCK_DAILY_WINDOW_DAYS:
        raise ValueError(
            f"max_window_days must be an integer from 1 to {MAX_STOCK_DAILY_WINDOW_DAYS}"
        )

    validated_task = _revalidate_task(task)
    if validated_task.provider != SOURCE or validated_task.model != "stock_daily":
        raise ValueError("task must identify the existing akshare stock_daily job")
    if validated_task.enabled:
        raise ValueError("the existing stock_daily fixture job must remain disabled")
    _validate_stock_contract(validated_task, contract)
    task_digest = _sha256(_canonical_json(validated_task.model_dump(mode="json")))
    contract_digest = canonical_sha256(contract)
    registry_digest = _registry_sha256(registry)
    source_id = contract.source.source_id
    scoped_lifecycles = tuple(
        sorted(
            (
                lifecycle
                for lifecycle in registry.lifecycles
                if lifecycle.dataset_id == validated_task.dataset_id
                and lifecycle.source_id == source_id
            ),
            key=lambda lifecycle: lifecycle.instrument_id,
        )
    )
    if not scoped_lifecycles:
        raise ValueError("registry has no lifecycle rows for the exact dataset/source scope")

    aliases_by_instrument: dict[str, list[InstrumentAlias]] = {}
    for alias in registry.aliases:
        if alias.dataset_id == validated_task.dataset_id and alias.source_id == source_id:
            aliases_by_instrument.setdefault(alias.instrument_id, []).append(alias)

    global_blockers = _global_blockers()
    fetcher = AkshareStockDailyFetcher()
    results = tuple(
        _plan_instrument(
            lifecycle,
            aliases=tuple(aliases_by_instrument.get(lifecycle.instrument_id, ())),
            task=validated_task,
            fetcher=fetcher,
            as_of=as_of,
            max_window_days=max_window_days,
            task_sha256=task_digest,
            contract_sha256=contract_digest,
            registry_sha256=registry_digest,
        )
        for lifecycle in scoped_lifecycles
    )
    plan_digest = _plan_sha256(
        plan_version=PLAN_VERSION,
        as_of=as_of,
        dataset_id=validated_task.dataset_id,
        source_id=source_id,
        provider=validated_task.provider,
        job_id=validated_task.job_id,
        job_version=validated_task.version,
        model=validated_task.model,
        task_sha256=task_digest,
        contract_sha256=contract_digest,
        registry_version=registry.registry_version,
        registry_evidence_ref=registry.evidence_ref,
        registry_sha256=registry_digest,
        registry_lifecycle_count=len(scoped_lifecycles),
        max_window_days=max_window_days,
        universe_scope_complete=False,
        source_history_limit="unknown",
        execution_authorized=False,
        instruments=results,
        blockers=global_blockers,
    )
    return StockDailyBackfillPlan(
        plan_version=PLAN_VERSION,
        as_of=as_of,
        dataset_id=validated_task.dataset_id,
        source_id=source_id,
        provider=validated_task.provider,
        job_id=validated_task.job_id,
        job_version=validated_task.version,
        model=validated_task.model,
        task_sha256=task_digest,
        contract_sha256=contract_digest,
        registry_version=registry.registry_version,
        registry_evidence_ref=registry.evidence_ref,
        registry_sha256=registry_digest,
        registry_lifecycle_count=len(scoped_lifecycles),
        max_window_days=max_window_days,
        universe_scope_complete=False,
        source_history_limit="unknown",
        execution_authorized=False,
        instruments=results,
        blockers=global_blockers,
        plan_sha256=plan_digest,
    )


def _plan_instrument(
    lifecycle: InstrumentLifecycle,
    *,
    aliases: tuple[InstrumentAlias, ...],
    task: TaskDefinition,
    fetcher: AkshareStockDailyFetcher,
    as_of: date,
    max_window_days: int,
    task_sha256: str,
    contract_sha256: str,
    registry_sha256: str,
) -> InstrumentBackfillResult:
    """Plan one lifecycle and retain all boundary, status, and alias blockers."""
    blockers: list[BackfillBlocker] = []
    candidate_end = _candidate_end(lifecycle, as_of)
    scope_start = lifecycle.known_start_date
    if lifecycle.start_boundary_unknown:
        blockers.append(
            BackfillBlocker(
                reason=BackfillBlockReason.UNKNOWN_START_BOUNDARY,
                detail="No independently evidenced lower business-date boundary is available.",
                instrument_id=lifecycle.instrument_id,
                range_start=None,
                range_end=candidate_end,
                evidence_refs=(),
            )
        )
    if lifecycle.status is LifecycleStatus.UNKNOWN:
        blockers.append(
            BackfillBlocker(
                reason=BackfillBlockReason.UNKNOWN_LIFECYCLE_STATUS,
                detail=(
                    "Lifecycle status is explicitly unknown; the status observation does not "
                    "establish an active or delisted endpoint."
                ),
                instrument_id=lifecycle.instrument_id,
                range_start=scope_start,
                range_end=as_of,
                evidence_refs=(lifecycle.status_evidence_ref,),
            )
        )
    if lifecycle.status is LifecycleStatus.ACTIVE and lifecycle.status_as_of < as_of:
        stale_start = lifecycle.status_as_of + timedelta(days=1)
        if scope_start is not None:
            stale_start = max(stale_start, scope_start)
        if stale_start <= as_of:
            blockers.append(
                BackfillBlocker(
                    reason=BackfillBlockReason.STALE_ACTIVE_STATUS,
                    detail="Active status is only evidenced through status_as_of.",
                    instrument_id=lifecycle.instrument_id,
                    range_start=stale_start,
                    range_end=as_of,
                    evidence_refs=(lifecycle.status_evidence_ref,),
                )
            )
    if (
        lifecycle.status is LifecycleStatus.DELISTED
        and lifecycle.delisted_on is not None
        and as_of >= lifecycle.delisted_on
        and scope_start is not None
        and scope_start <= lifecycle.delisted_on
    ):
        evidence_refs = (
            (lifecycle.delist_evidence_ref,) if lifecycle.delist_evidence_ref is not None else ()
        )
        blockers.append(
            BackfillBlocker(
                reason=BackfillBlockReason.DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED,
                detail=(
                    "The registry records delisted_on and evidence, but does not encode whether "
                    "that date is the last included business date or the first inactive date. "
                    "The candidate request includes it pending boundary review."
                ),
                instrument_id=lifecycle.instrument_id,
                range_start=lifecycle.delisted_on,
                range_end=lifecycle.delisted_on,
                evidence_refs=evidence_refs,
            )
        )

    chunks: list[StockDailyBackfillChunk] = []
    if scope_start is None:
        return _instrument_result(lifecycle, chunks, blockers)
    if scope_start > candidate_end:
        blockers.append(
            BackfillBlocker(
                reason=BackfillBlockReason.NOT_YET_IN_SCOPE,
                detail="The independently evidenced start boundary is after this plan cutoff.",
                instrument_id=lifecycle.instrument_id,
                range_start=scope_start,
                range_end=None,
                evidence_refs=(lifecycle.start_evidence_ref,)
                if lifecycle.start_evidence_ref is not None
                else (),
            )
        )
        return _instrument_result(lifecycle, chunks, blockers)

    segments = _alias_segments(scope_start, candidate_end, aliases)
    chunk_index = 0
    for segment_start, segment_end, active_aliases in segments:
        if not active_aliases:
            blockers.append(
                BackfillBlocker(
                    reason=BackfillBlockReason.SOURCE_ALIAS_GAP,
                    detail="No reviewed source alias covers this inclusive lifecycle interval.",
                    instrument_id=lifecycle.instrument_id,
                    range_start=segment_start,
                    range_end=segment_end,
                    evidence_refs=(),
                )
            )
            continue
        if len(active_aliases) > 1:
            blockers.append(
                BackfillBlocker(
                    reason=BackfillBlockReason.AMBIGUOUS_SOURCE_ALIAS,
                    detail=(
                        "Multiple source aliases for one canonical instrument are effective "
                        "over this interval; selecting one could duplicate or misroute bars."
                    ),
                    instrument_id=lifecycle.instrument_id,
                    range_start=segment_start,
                    range_end=segment_end,
                    evidence_refs=tuple(sorted(alias.evidence_ref for alias in active_aliases)),
                )
            )
            continue

        alias = active_aliases[0]
        try:
            alias_chunks = _chunks_for_alias(
                lifecycle,
                alias,
                task=task,
                fetcher=fetcher,
                start=segment_start,
                end=segment_end,
                first_chunk_index=chunk_index + 1,
                max_window_days=max_window_days,
                task_sha256=task_sha256,
                contract_sha256=contract_sha256,
                registry_sha256=registry_sha256,
            )
        except ValueError:
            blockers.append(
                BackfillBlocker(
                    reason=BackfillBlockReason.INVALID_PROVIDER_SYMBOL,
                    detail=(
                        "The reviewed alias cannot be represented by the existing stock_daily "
                        "query validator. No provider query was sent."
                    ),
                    instrument_id=lifecycle.instrument_id,
                    range_start=segment_start,
                    range_end=segment_end,
                    evidence_refs=(alias.evidence_ref,),
                )
            )
            continue
        chunks.extend(alias_chunks)
        chunk_index += len(alias_chunks)
        if any(_is_bj_symbol(chunk.provider_symbol, alias.alias) for chunk in alias_chunks):
            blockers.append(
                BackfillBlocker(
                    reason=BackfillBlockReason.BJ_PROVIDER_MAPPING_UNVERIFIED,
                    detail=(
                        "The existing query model accepts this Beijing code, but the current "
                        "Eastmoney job maps every non-6 prefix to market 0 and has no verified "
                        "BJ routing contract. Candidate chunks are not dispatchable."
                    ),
                    instrument_id=lifecycle.instrument_id,
                    range_start=segment_start,
                    range_end=segment_end,
                    evidence_refs=(alias.evidence_ref,),
                )
            )
    return _instrument_result(lifecycle, chunks, blockers)


def _instrument_result(
    lifecycle: InstrumentLifecycle,
    chunks: list[StockDailyBackfillChunk],
    blockers: list[BackfillBlocker],
) -> InstrumentBackfillResult:
    """Freeze a deterministically ordered lifecycle result."""
    return InstrumentBackfillResult(
        instrument_id=lifecycle.instrument_id,
        lifecycle_version=lifecycle.lifecycle_version,
        lifecycle_status=LifecycleStatus(lifecycle.status),
        status_as_of=lifecycle.status_as_of,
        status_evidence_ref=lifecycle.status_evidence_ref,
        known_start_date=lifecycle.known_start_date,
        start_boundary_unknown=lifecycle.start_boundary_unknown,
        start_evidence_ref=lifecycle.start_evidence_ref,
        delisted_on=lifecycle.delisted_on,
        delist_evidence_ref=lifecycle.delist_evidence_ref,
        candidate_chunks=tuple(chunks),
        blockers=tuple(blockers),
    )


def _candidate_end(lifecycle: InstrumentLifecycle, as_of: date) -> date:
    """Return the latest date supported for candidate generation, not completeness."""
    if lifecycle.status is LifecycleStatus.DELISTED:
        if lifecycle.delisted_on is None:
            raise ValueError("delisted lifecycle is missing its validated boundary")
        return min(as_of, lifecycle.delisted_on)
    return min(as_of, lifecycle.status_as_of)


def _alias_segments(
    start: date,
    end: date,
    aliases: tuple[InstrumentAlias, ...],
) -> tuple[tuple[date, date, tuple[InstrumentAlias, ...]], ...]:
    """Partition a closed range into alias-covered, uncovered, and ambiguous pieces."""
    starts: dict[date, list[InstrumentAlias]] = {}
    removals: dict[date, list[InstrumentAlias]] = {}
    for alias in aliases:
        alias_start = max(start, alias.valid_from or start)
        alias_end = min(end, alias.valid_through or end)
        if alias_start > alias_end:
            continue
        starts.setdefault(alias_start, []).append(alias)
        if alias_end < end:
            removals.setdefault(alias_end + timedelta(days=1), []).append(alias)

    boundaries = sorted({start, *starts, *removals})
    active: set[InstrumentAlias] = set()
    result: list[tuple[date, date, tuple[InstrumentAlias, ...]]] = []
    for index, boundary in enumerate(boundaries):
        for alias in removals.get(boundary, ()):
            active.discard(alias)
        for alias in starts.get(boundary, ()):
            active.add(alias)
        segment_end = (
            boundaries[index + 1] - timedelta(days=1) if index + 1 < len(boundaries) else end
        )
        if boundary <= end and segment_end >= boundary:
            effective_aliases = tuple(
                sorted(
                    active,
                    key=lambda item: (
                        item.alias,
                        item.alias_version,
                        item.evidence_ref,
                        item.valid_from or date.min,
                    ),
                )
            )
            result.append((boundary, segment_end, effective_aliases))
    return tuple(result)


def _chunks_for_alias(
    lifecycle: InstrumentLifecycle,
    alias: InstrumentAlias,
    *,
    task: TaskDefinition,
    fetcher: AkshareStockDailyFetcher,
    start: date,
    end: date,
    first_chunk_index: int,
    max_window_days: int,
    task_sha256: str,
    contract_sha256: str,
    registry_sha256: str,
) -> tuple[StockDailyBackfillChunk, ...]:
    """Create non-overlapping requests for one effective source-alias interval."""
    chunks: list[StockDailyBackfillChunk] = []
    cursor = start
    while cursor <= end:
        remaining_days = (end - cursor).days
        chunk_end = cursor + timedelta(days=min(max_window_days - 1, remaining_days))
        query = fetcher.transform_query(
            source=task.provider,
            market="cn",
            symbol=alias.alias,
            start_date=cursor.isoformat(),
            end_date=chunk_end.isoformat(),
            adjust="raw",
        )
        chunk_index = first_chunk_index + len(chunks)
        key_material = {
            "plan_version": PLAN_VERSION,
            "task": {
                "job_id": task.job_id,
                "version": task.version,
                "dataset_id": task.dataset_id,
                "provider": task.provider,
                "model": task.model,
            },
            "task_sha256": task_sha256,
            "contract_sha256": contract_sha256,
            "registry_sha256": registry_sha256,
            "instrument_id": lifecycle.instrument_id,
            "lifecycle_version": lifecycle.lifecycle_version,
            "alias": {
                "value": alias.alias,
                "version": alias.alias_version,
                "evidence_ref": alias.evidence_ref,
                "valid_from": _iso(alias.valid_from),
                "valid_through": _iso(alias.valid_through),
            },
            "window": {"start": cursor.isoformat(), "end": chunk_end.isoformat()},
            "query": {
                "source": query.source,
                "market": query.market,
                "symbol": query.symbol,
                "adjust": query.adjust,
            },
        }
        idempotency_key = f"bf-{_sha256(_canonical_json(key_material))}"
        request = RunRequest.model_validate(
            {
                "run_type": "backfill",
                "query": {
                    "source": task.provider,
                    "market": "cn",
                    "symbol": alias.alias,
                    "start_date": cursor.isoformat(),
                    "end_date": chunk_end.isoformat(),
                    "adjust": "raw",
                },
                "partition": {},
                "shard": query.symbol,
                "occurrence": None,
                "idempotency_key": idempotency_key,
                "backfill_start": cursor,
                "backfill_end": chunk_end,
            },
            strict=True,
        )
        validated_query = _validated_query(fetcher, request)
        if (
            validated_query.symbol != query.symbol
            or validated_query.start_date != cursor
            or validated_query.end_date != chunk_end
        ):
            raise ValueError("generated request differs from the stock_daily query contract")
        request_json = _canonical_json(request.model_dump(mode="json"))
        chunks.append(
            StockDailyBackfillChunk(
                instrument_id=lifecycle.instrument_id,
                lifecycle_version=lifecycle.lifecycle_version,
                source_symbol=alias.alias,
                provider_symbol=validated_query.symbol,
                alias_version=alias.alias_version,
                alias_evidence_ref=alias.evidence_ref,
                alias_valid_from=alias.valid_from,
                alias_valid_through=alias.valid_through,
                window_start=cursor,
                window_end=chunk_end,
                chunk_index=chunk_index,
                idempotency_key=idempotency_key,
                request_payload_json=request_json,
                request_payload_sha256=_sha256(request_json),
            )
        )
        if chunk_end == end:
            break
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _global_blockers() -> tuple[BackfillBlocker, ...]:
    """Return limitations that apply to every plan from current stock_daily wiring."""
    return (
        BackfillBlocker(
            reason=BackfillBlockReason.UNIVERSE_SCOPE_UNATTESTED,
            detail=(
                "InstrumentReferenceRegistry is a frozen row set and carries no complete "
                "all-A manifest attestation; this plan denominator is only the selected rows."
            ),
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.SOURCE_HISTORY_LIMIT_UNKNOWN,
            detail=(
                "The current stock_daily raw-evidence contract reports source_history_limit as "
                "unknown; earliest queryable history and retention are not certified."
            ),
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.SOURCE_CAPABILITY_UNVERIFIED,
            detail=(
                "The current AkshareStockDailyFetcher capability is unverified and its request "
                "budget does not authorize live calls."
            ),
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.COVERAGE_RECONCILIATION_REQUIRED,
            detail=(
                "No publication observations or complete business-calendar evidence were passed "
                "to this planner; candidates are full requested windows, not reconciled head, "
                "interior, and tail gaps."
            ),
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.DISPATCH_NOT_AUTHORIZED,
            detail=(
                "This planner emits offline candidate requests only; it does not review, enable, "
                "or dispatch the disabled/unreviewed stock_daily job."
            ),
        ),
    )


def _registry_sha256(registry: InstrumentReferenceRegistry) -> str:
    """Hash the actual complete registry contents independent of input tuple order."""
    ordered_lifecycles = sorted(
        registry.lifecycles,
        key=lambda row: (row.dataset_id, row.source_id, row.instrument_id),
    )
    ordered_aliases = sorted(
        registry.aliases,
        key=lambda row: (
            row.dataset_id,
            row.source_id,
            row.alias,
            row.instrument_id,
            row.valid_from or date.min,
            row.valid_through or date.max,
            row.alias_version,
        ),
    )
    lifecycle_payloads = [_lifecycle_payload(row) for row in ordered_lifecycles]
    alias_payloads = [_alias_payload(row) for row in ordered_aliases]
    return _sha256(
        _canonical_json(
            {
                "schema_version": registry.schema_version,
                "registry_version": registry.registry_version,
                "evidence_ref": registry.evidence_ref,
                "lifecycles": lifecycle_payloads,
                "aliases": alias_payloads,
            }
        )
    )


def _lifecycle_payload(lifecycle: InstrumentLifecycle) -> dict[str, object]:
    """Serialize every lifecycle field that can affect interpretation."""
    return {
        "dataset_id": lifecycle.dataset_id,
        "source_id": lifecycle.source_id,
        "instrument_id": lifecycle.instrument_id,
        "lifecycle_version": lifecycle.lifecycle_version,
        "status": LifecycleStatus(lifecycle.status).value,
        "status_as_of": lifecycle.status_as_of.isoformat(),
        "status_evidence_ref": lifecycle.status_evidence_ref,
        "known_start_date": _iso(lifecycle.known_start_date),
        "start_boundary_unknown": lifecycle.start_boundary_unknown,
        "start_evidence_ref": lifecycle.start_evidence_ref,
        "delisted_on": _iso(lifecycle.delisted_on),
        "delist_evidence_ref": lifecycle.delist_evidence_ref,
        "schema_version": lifecycle.schema_version,
    }


def _alias_payload(alias: InstrumentAlias) -> dict[str, object]:
    """Serialize every source-alias field that can affect routing or validity."""
    return {
        "dataset_id": alias.dataset_id,
        "source_id": alias.source_id,
        "alias": alias.alias,
        "instrument_id": alias.instrument_id,
        "alias_version": alias.alias_version,
        "evidence_ref": alias.evidence_ref,
        "valid_from": _iso(alias.valid_from),
        "valid_through": _iso(alias.valid_through),
        "schema_version": alias.schema_version,
    }


def _plan_sha256(
    *,
    plan_version: str,
    as_of: date,
    dataset_id: str,
    source_id: str,
    provider: str,
    job_id: str,
    job_version: str,
    model: str,
    task_sha256: str,
    contract_sha256: str,
    registry_version: str,
    registry_evidence_ref: str,
    registry_sha256: str,
    registry_lifecycle_count: int,
    max_window_days: int,
    universe_scope_complete: bool,
    source_history_limit: str,
    execution_authorized: bool,
    instruments: tuple[InstrumentBackfillResult, ...],
    blockers: tuple[BackfillBlocker, ...],
) -> str:
    """Hash the complete plan, including blockers and exact serialized requests."""
    normalized: dict[str, object] = {
        "plan_version": plan_version,
        "as_of": as_of.isoformat(),
        "dataset_id": dataset_id,
        "source_id": source_id,
        "provider": provider,
        "job_id": job_id,
        "job_version": job_version,
        "model": model,
        "task_sha256": task_sha256,
        "contract_sha256": contract_sha256,
        "registry_version": registry_version,
        "registry_evidence_ref": registry_evidence_ref,
        "registry_sha256": registry_sha256,
        "registry_lifecycle_count": registry_lifecycle_count,
        "max_window_days": max_window_days,
        "universe_scope_complete": universe_scope_complete,
        "source_history_limit": source_history_limit,
        "execution_authorized": execution_authorized,
        "instruments": [
            {
                "instrument_id": result.instrument_id,
                "lifecycle_version": result.lifecycle_version,
                "lifecycle_status": result.lifecycle_status.value,
                "status_as_of": result.status_as_of.isoformat(),
                "status_evidence_ref": result.status_evidence_ref,
                "known_start_date": _iso(result.known_start_date),
                "start_boundary_unknown": result.start_boundary_unknown,
                "start_evidence_ref": result.start_evidence_ref,
                "delisted_on": _iso(result.delisted_on),
                "delist_evidence_ref": result.delist_evidence_ref,
                "candidate_chunks": [_chunk_payload(chunk) for chunk in result.candidate_chunks],
                "blockers": [_blocker_payload(blocker) for blocker in result.blockers],
            }
            for result in instruments
        ],
        "blockers": [_blocker_payload(blocker) for blocker in blockers],
    }
    return _sha256(_canonical_json(normalized))


def _chunk_payload(chunk: StockDailyBackfillChunk) -> dict[str, object]:
    """Serialize one candidate request and its pinned lifecycle/alias evidence."""
    return {
        "instrument_id": chunk.instrument_id,
        "lifecycle_version": chunk.lifecycle_version,
        "source_symbol": chunk.source_symbol,
        "provider_symbol": chunk.provider_symbol,
        "alias_version": chunk.alias_version,
        "alias_evidence_ref": chunk.alias_evidence_ref,
        "alias_valid_from": _iso(chunk.alias_valid_from),
        "alias_valid_through": _iso(chunk.alias_valid_through),
        "window_start": chunk.window_start.isoformat(),
        "window_end": chunk.window_end.isoformat(),
        "chunk_index": chunk.chunk_index,
        "idempotency_key": chunk.idempotency_key,
        "request_payload_json": chunk.request_payload_json,
        "request_payload_sha256": chunk.request_payload_sha256,
    }


def _blocker_payload(blocker: BackfillBlocker) -> dict[str, object]:
    """Serialize blocker identity, interval, and evidence references."""
    return {
        "reason": blocker.reason.value,
        "detail": blocker.detail,
        "instrument_id": blocker.instrument_id,
        "range_start": _iso(blocker.range_start),
        "range_end": _iso(blocker.range_end),
        "evidence_refs": blocker.evidence_refs,
    }


def _is_bj_symbol(provider_symbol: str, source_symbol: str) -> bool:
    """Identify BSE query shapes that the current Eastmoney adapter cannot verify."""
    normalized_source = source_symbol.strip().upper()
    return (
        normalized_source.startswith("BJ")
        or normalized_source.endswith(".BJ")
        or provider_symbol.startswith(_BJ_PREFIXES)
    )


def _iso(value: date | None) -> str | None:
    """Return an ISO date or null for optional date fields."""
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("date serialization received a non-date boundary")
    return value.isoformat()


def _canonical_json(value: object) -> str:
    """Encode JSON-compatible plan data in a stable, compact representation."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    """Return lowercase SHA-256 for one canonical UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BackfillBlockReason",
    "BackfillBlocker",
    "InstrumentBackfillResult",
    "StockDailyBackfillChunk",
    "StockDailyBackfillPlan",
    "build_stock_daily_backfill_plan",
]
