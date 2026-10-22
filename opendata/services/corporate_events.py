"""Persistent, reviewable corporate-event version and conflict storage."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

from opendata.pipeline.factors import FactorEvent
from opendata.pipeline.notify import NotifyResult, build_notify_hook
from opendata.pipeline.runner import PipelineContext, Window

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection, Row

    from opendata.pipeline.corporate_events import (
        CorporateEventVersion,
        RawCorporateEventObservation,
    )
    from opendata.pipeline.factor_builder import FactorBuilder, FactorBuildResult, FactorWriteScope


EVENT_VERSIONS_TABLE = "dwd_corporate_event_versions"
EVENT_CONFLICTS_TABLE = "dwd_corporate_event_conflicts"
EVENT_CANDIDATES_TABLE = "dwd_corporate_event_candidates"
EVENT_RECOMPUTE_OUTBOX_TABLE = "dwd_corporate_event_recompute_outbox"
MAX_EVENT_RECOMPUTE_SYMBOLS = 50
MAX_EVENT_RECOMPUTE_JOBS = 500
_FACTOR_DERIVED_SOURCE = "corporate-action-affine-v1"


@dataclass(frozen=True)
class EventVersionWrite:
    """Outcome of persisting one version, including recomputation keys."""

    status: Literal["inserted", "idempotent", "conflict"]
    content_digest: str
    affected_keys: tuple[tuple[str, date], ...]
    outbox_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventRecomputeJob:
    """One claimed, durable factor-recompute and notification request."""

    job_id: str
    batch_id: str
    source: str
    event_identity: str
    revision_order: int
    content_digest: str
    symbol: str
    ex_date: date
    created_at: datetime
    lease_token: str
    attempt_count: int


@dataclass(frozen=True)
class EventDrainResult:
    """Result of one bounded outbox pass."""

    claimed_jobs: int
    completed_jobs: int
    failed_jobs: int
    rows_written: int
    failures: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CorporateEventIngestResult:
    """Version persistence and its immediately attempted outbox work."""

    version_write: EventVersionWrite
    drain: EventDrainResult


class CorporateEventConflictError(ValueError):
    """Raised when the latest revision of an event has conflicting payloads."""


class CorporateEventVersionStore:
    """Store immutable event versions and reject ambiguous current revisions.

    The service expects the three tables from migration 0009. It does not
    create or migrate production tables. Unknown identities are stored in the
    candidate table with a storage observation UUID, never an event ID.
    """

    def __init__(self, engine: Engine) -> None:
        """Bind the store to an engine with migration-0009 tables."""
        self.engine = engine

    def record_version(self, version: CorporateEventVersion) -> EventVersionWrite:
        """Persist a version idempotently and record same-revision conflicts."""
        digest = version.content_digest
        identity = version.identity
        where_identity = {
            "source": identity.source,
            "event_identity": identity.event_identity,
        }
        with self.engine.begin() as connection:
            existing_rows = connection.execute(
                text(
                    # B608: fixed table identifier; values are bound.
                    f"SELECT `revision_order`, `source_revision`, `content_digest`, "  # noqa: S608 # nosec B608
                    f"`symbol`, `ex_date` FROM `{EVENT_VERSIONS_TABLE}` "
                    "WHERE `source` = :source AND `event_identity` = :event_identity"
                ),
                where_identity,
            ).fetchall()
            for row in existing_rows:
                if int(row[0]) == version.revision_order and str(row[1]) != version.source_revision:
                    raise ValueError(
                        "one revision_order cannot name multiple source_revision values"
                    )

            prior_latest_order = max((int(row[0]) for row in existing_rows), default=None)
            prior_latest = [row for row in existing_rows if int(row[0]) == prior_latest_order]
            revision_rows = [row for row in existing_rows if int(row[0]) == version.revision_order]
            same_version_exists = any(str(row[2]) == digest for row in revision_rows)
            if not same_version_exists:
                connection.execute(
                    text(
                        # B608: fixed table identifier; row values are bound.
                        f"INSERT INTO `{EVENT_VERSIONS_TABLE}` ("  # noqa: S608 # nosec B608
                        "`source`, `event_identity`, `identity_basis`, `economic_event_id`, "
                        "`symbol`, `ex_date`, `revision_order`, `source_revision`, "
                        "`content_digest`, `pre_event_share_basis`, `cash_dividend`, "
                        "`stock_dividend`, `rights_shares`, `rights_price`, "
                        "`published_date`, `published_timestamp`, `publication_precision`, "
                        "`source_timezone`, `observed_at`) VALUES ("
                        ":source, :event_identity, :identity_basis, :economic_event_id, "
                        ":symbol, :ex_date, :revision_order, :source_revision, "
                        ":content_digest, :pre_event_share_basis, :cash_dividend, "
                        ":stock_dividend, :rights_shares, :rights_price, :published_date, "
                        ":published_timestamp, :publication_precision, :source_timezone, "
                        ":observed_at)"
                    ),
                    _version_parameters(version, digest),
                )

            peer_digests = sorted({str(row[2]) for row in revision_rows if str(row[2]) != digest})
            if not same_version_exists:
                for other_digest in peer_digests:
                    digest_a, digest_b = sorted((digest, other_digest))
                    connection.execute(
                        text(
                            # B608: fixed table identifier; conflict values are bound.
                            f"INSERT INTO `{EVENT_CONFLICTS_TABLE}` ("  # noqa: S608 # nosec B608
                            "`source`, `event_identity`, `revision_order`, `digest_a`, `digest_b`, "
                            "`detected_at`) VALUES (:source, :event_identity, :revision_order, "
                            ":digest_a, :digest_b, :detected_at)"
                        ),
                        {
                            **where_identity,
                            "revision_order": version.revision_order,
                            "digest_a": digest_a,
                            "digest_b": digest_b,
                            "detected_at": _utc_naive(version.observed_at),
                        },
                    )

            current_order = max([int(row[0]) for row in existing_rows] + [version.revision_order])
            if same_version_exists:
                rank_digests = {str(row[2]) for row in revision_rows}
                conflict = len(rank_digests) > 1
                status: Literal["inserted", "idempotent", "conflict"] = (
                    "conflict" if conflict else "idempotent"
                )
                # Replaying one side of a live conflict is still visibly a
                # conflict. It does not add new evidence or new work.
                changed_current = False
            else:
                rank_digests = {str(row[2]) for row in revision_rows} | {digest}
                conflict = len(rank_digests) > 1 and version.revision_order == current_order
                status = "conflict" if conflict else "inserted"
                changed_current = version.revision_order >= (prior_latest_order or -1)

            affected = set()
            if changed_current:
                for row in prior_latest:
                    affected.add((_factor_symbol(_row_text(row[3])), _row_date(row[4])))
                affected.add((_factor_symbol(identity.symbol), identity.ex_date))
            affected_keys = tuple(sorted(affected, key=lambda key: (key[0], key[1])))
            outbox_job_ids = (
                _persist_recompute_jobs(connection, version, digest, affected_keys)
                if changed_current
                else ()
            )
            return EventVersionWrite(
                status=status,
                content_digest=digest,
                affected_keys=affected_keys,
                outbox_job_ids=outbox_job_ids,
            )

    def preserve_unresolved(self, observation: RawCorporateEventObservation) -> str:
        """Persist raw evidence when no defensible event identity is known."""
        observation_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    # B608: fixed table identifier; observation values are bound.
                    f"INSERT INTO `{EVENT_CANDIDATES_TABLE}` ("  # noqa: S608 # nosec B608
                    "`observation_id`, `source`, `content_digest`, `raw_payload_json`, "
                    "`symbol`, `ex_date`, `observed_at`, `identity_status`) VALUES ("
                    ":observation_id, :source, :content_digest, :raw_payload_json, "
                    ":symbol, :ex_date, :observed_at, 'identity_unresolved')"
                ),
                {
                    "observation_id": observation_id,
                    "source": observation.source,
                    "content_digest": observation.content_digest,
                    "raw_payload_json": observation.raw_payload_json,
                    "symbol": observation.symbol,
                    "ex_date": observation.ex_date,
                    "observed_at": _utc_naive(observation.observed_at),
                },
            )
        return observation_id

    def load_current_events(self, *, symbols: Sequence[str] | None = None) -> list[FactorEvent]:
        """Load each unambiguous latest revision as a typed factor event.

        A conflict at the highest revision raises instead of falling back to
        an older version or selecting a payload arbitrarily.
        """
        normalized_symbols = None
        if symbols is not None:
            normalized_symbols = tuple(sorted({_factor_symbol(symbol) for symbol in symbols}))
            if not normalized_symbols:
                return []
        query = (
            # B608: fixed table identifier; filters are bound.
            f"SELECT `v`.`source`, `v`.`event_identity`, `v`.`economic_event_id`, "  # noqa: S608 # nosec B608
            "`v`.`symbol`, `v`.`ex_date`, `v`.`revision_order`, `v`.`source_revision`, "
            "`v`.`content_digest`, `v`.`pre_event_share_basis`, `v`.`cash_dividend`, "
            "`v`.`stock_dividend`, `v`.`rights_shares`, `v`.`rights_price` "
            f"FROM `{EVENT_VERSIONS_TABLE}` AS `v` "
        )
        params: dict[str, object] = {}
        if normalized_symbols is not None:
            symbol_terms: list[str] = []
            for index, symbol in enumerate(normalized_symbols):
                exact = f"symbol_{index}"
                prefix = f"symbol_prefix_{index}"
                symbol_terms.append(f"(`s`.`symbol` = :{exact} OR `s`.`symbol` LIKE :{prefix})")
                params[exact] = symbol
                params[prefix] = f"{symbol}.%"
            query += (
                # B608: fixed SQL and indexed binds; symbols are bound.
                "WHERE EXISTS ("  # noqa: S608 # nosec B608
                f"SELECT 1 FROM `{EVENT_VERSIONS_TABLE}` AS `s` "
                "WHERE `s`.`source` = `v`.`source` "
                "AND `s`.`event_identity` = `v`.`event_identity` "
                f"AND ({' OR '.join(symbol_terms)})"
                ") "
            )
        query += "ORDER BY `v`.`source`, `v`.`event_identity`, "
        query += "`v`.`revision_order`, `v`.`content_digest`"
        statement = text(query)
        with self.engine.connect() as connection:
            rows = connection.execute(statement, params).fetchall()

        by_identity: dict[tuple[str, str], list[Row]] = {}
        for source_row in rows:
            by_identity.setdefault((str(source_row[0]), str(source_row[1])), []).append(source_row)

        current: list[FactorEvent] = []
        conflicted: list[str] = []
        for (source, event_identity), versions in sorted(by_identity.items()):
            latest_order = max(int(row[5]) for row in versions)
            latest = [row for row in versions if int(row[5]) == latest_order]
            if len({str(row[7]) for row in latest}) != 1:
                conflicted.append(f"{source}:{event_identity}@{latest_order}")
                continue
            latest_row = latest[0]
            current_symbol = _factor_symbol(_row_text(latest_row[3]))
            if normalized_symbols is not None and current_symbol not in normalized_symbols:
                continue
            current.append(
                FactorEvent(
                    symbol=current_symbol,
                    ex_date=_row_date(latest_row[4]),
                    cash_dividend=float(latest_row[9]),
                    bonus=float(latest_row[10]),
                    allotment_ratio=float(latest_row[11]),
                    allotment_price=float(latest_row[12]),
                    source=source,
                    event_identity=event_identity,
                    economic_event_id=(None if latest_row[2] is None else str(latest_row[2])),
                    pre_event_share_basis=(None if latest_row[8] is None else str(latest_row[8])),
                    source_revision=str(latest_row[6]),
                    content_digest=str(latest_row[7]),
                )
            )
        if conflicted:
            raise CorporateEventConflictError(
                "latest corporate-event revision has unresolved payload conflict(s): "
                + ", ".join(conflicted)
            )
        return sorted(current, key=lambda event: (event.symbol, event.ex_date, event.source or ""))

    def claim_pending_jobs(
        self,
        *,
        limit: int,
        max_symbols: int,
        lease_seconds: int,
        identity_scope: tuple[str, str] | None = None,
    ) -> tuple[EventRecomputeJob, ...]:
        """Claim a bounded set of pending jobs with compare-and-swap leases."""
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_RECOMPUTE_JOBS:
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_RECOMPUTE_JOBS}")
        if type(max_symbols) is not int or not 1 <= max_symbols <= MAX_EVENT_RECOMPUTE_SYMBOLS:
            raise ValueError(f"max_symbols must be between 1 and {MAX_EVENT_RECOMPUTE_SYMBOLS}")
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        now = _utc_naive(datetime.now(timezone.utc))
        lease_until = now + timedelta(seconds=lease_seconds)
        identity_where = ""
        params: dict[str, object] = {"now": now, "limit": limit}
        if identity_scope is not None:
            identity_where = (
                " AND `source` = :identity_source AND `event_identity` = :event_identity"
            )
            params["identity_source"], params["event_identity"] = identity_scope
        with self.engine.begin() as connection:
            rows = connection.execute(
                text(
                    # B608: fixed table and clause; filter values are bound.
                    f"SELECT `job_id`, `batch_id`, `source`, `event_identity`, "  # noqa: S608 # nosec B608
                    "`revision_order`, `content_digest`, `symbol`, `ex_date`, `attempt_count`, "
                    "`created_at` "
                    f"FROM `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "
                    "WHERE ((`status` = 'pending' AND `available_at` <= :now) "
                    "OR (`status` = 'processing' AND `lease_until` <= :now))"
                    f"{identity_where} ORDER BY `created_at`, `job_id` LIMIT :limit"
                ),
                params,
            ).fetchall()
            selected: list[Row] = []
            selected_symbols: set[str] = set()
            for candidate_row in rows:
                symbol = _factor_symbol(_row_text(candidate_row[6]))
                if symbol not in selected_symbols and len(selected_symbols) >= max_symbols:
                    break
                selected_symbols.add(symbol)
                selected.append(candidate_row)

            claimed: list[EventRecomputeJob] = []
            for selected_row in selected:
                lease_token = str(uuid.uuid4())
                result = connection.execute(
                    text(
                        # B608: fixed table; lease values are bound.
                        f"UPDATE `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "  # noqa: S608 # nosec B608
                        "SET `status` = 'processing', `attempt_count` = `attempt_count` + 1, "
                        "`lease_token` = :lease_token, `lease_until` = :lease_until "
                        "WHERE `job_id` = :job_id AND "
                        "((`status` = 'pending' AND `available_at` <= :now) OR "
                        "(`status` = 'processing' AND `lease_until` <= :now))"
                    ),
                    {
                        "lease_token": lease_token,
                        "lease_until": lease_until,
                        "job_id": str(selected_row[0]),
                        "now": now,
                    },
                )
                if result.rowcount != 1:
                    continue
                claimed.append(
                    EventRecomputeJob(
                        job_id=str(selected_row[0]),
                        batch_id=str(selected_row[1]),
                        source=str(selected_row[2]),
                        event_identity=str(selected_row[3]),
                        revision_order=int(selected_row[4]),
                        content_digest=str(selected_row[5]),
                        symbol=_factor_symbol(_row_text(selected_row[6])),
                        ex_date=_row_date(selected_row[7]),
                        created_at=_row_datetime(selected_row[9]),
                        lease_token=lease_token,
                        attempt_count=int(selected_row[8]) + 1,
                    )
                )
            return tuple(claimed)

    def complete_jobs(
        self,
        jobs: Sequence[EventRecomputeJob],
        scope: FactorWriteScope,
    ) -> None:
        """Complete only the jobs still owned by their claim tokens."""
        completed_at = _utc_naive(datetime.now(timezone.utc))
        with self.engine.begin() as connection:
            for job in jobs:
                result = connection.execute(
                    text(
                        # B608: fixed table; completion values are bound.
                        f"UPDATE `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "  # noqa: S608 # nosec B608
                        "SET `status` = 'done', `completed_at` = :completed_at, "
                        "`factor_row_count` = :factor_row_count, "
                        "`factor_window_start` = :factor_window_start, "
                        "`factor_window_end` = :factor_window_end, "
                        "`lease_token` = NULL, `lease_until` = NULL, `last_error` = NULL "
                        "WHERE `job_id` = :job_id AND `status` = 'processing' "
                        "AND `lease_token` = :lease_token"
                    ),
                    {
                        "completed_at": completed_at,
                        "factor_row_count": scope.row_count,
                        "factor_window_start": scope.window_start,
                        "factor_window_end": scope.window_end,
                        "job_id": job.job_id,
                        "lease_token": job.lease_token,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError(f"corporate-event outbox lease lost for {job.job_id}")

    def retry_jobs(self, jobs: Sequence[EventRecomputeJob], error: Exception) -> None:
        """Return owned jobs to pending with bounded exponential backoff."""
        now = _utc_naive(datetime.now(timezone.utc))
        message = f"{type(error).__name__}: {error}"[:4_000]
        with self.engine.begin() as connection:
            for job in jobs:
                delay = min(60, 2 ** min(job.attempt_count - 1, 6))
                connection.execute(
                    text(
                        # B608: fixed table; error value is bound.
                        f"UPDATE `{EVENT_RECOMPUTE_OUTBOX_TABLE}` "  # noqa: S608 # nosec B608
                        "SET `status` = 'pending', `available_at` = :available_at, "
                        "`lease_token` = NULL, `lease_until` = NULL, `last_error` = :last_error "
                        "WHERE `job_id` = :job_id AND `status` = 'processing' "
                        "AND `lease_token` = :lease_token"
                    ),
                    {
                        "available_at": now + timedelta(seconds=delay),
                        "last_error": message,
                        "job_id": job.job_id,
                        "lease_token": job.lease_token,
                    },
                )


class CorporateEventIngestService:
    """Persist event revisions, rebuild factors, and publish durable updates."""

    def __init__(
        self,
        version_store: CorporateEventVersionStore,
        factor_builder: FactorBuilder,
        *,
        max_jobs_per_batch: int = MAX_EVENT_RECOMPUTE_JOBS,
        max_symbols_per_batch: int = MAX_EVENT_RECOMPUTE_SYMBOLS,
        lease_seconds: int = 900,
        notify_hook_factory: Callable[
            [str, int, datetime], Callable[[PipelineContext], Awaitable[object]]
        ]
        | None = None,
    ) -> None:
        """Bind a single version store to its factor builder and notifier."""
        if factor_builder.event_store is not version_store:
            raise ValueError("factor_builder must read from the supplied event version store")
        if factor_builder.engine is not version_store.engine:
            raise ValueError("factor builder and version store must share one engine")
        if (
            type(max_jobs_per_batch) is not int
            or not 1 <= max_jobs_per_batch <= MAX_EVENT_RECOMPUTE_JOBS
        ):
            raise ValueError(f"max_jobs_per_batch must be between 1 and {MAX_EVENT_RECOMPUTE_JOBS}")
        if (
            type(max_symbols_per_batch) is not int
            or not 1 <= max_symbols_per_batch <= MAX_EVENT_RECOMPUTE_SYMBOLS
        ):
            raise ValueError(
                f"max_symbols_per_batch must be between 1 and {MAX_EVENT_RECOMPUTE_SYMBOLS}"
            )
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        self.version_store = version_store
        self.factor_builder = factor_builder
        self.max_jobs_per_batch = max_jobs_per_batch
        self.max_symbols_per_batch = max_symbols_per_batch
        self.lease_seconds = lease_seconds
        self.notify_hook_factory = notify_hook_factory

    async def ingest(self, version: CorporateEventVersion) -> CorporateEventIngestResult:
        """Atomically persist a version/outbox row, then drain its pending work."""
        written = self.version_store.record_version(version)
        drain = await self.drain_pending(
            identity_scope=(version.identity.source, version.identity.event_identity)
        )
        return CorporateEventIngestResult(version_write=written, drain=drain)

    def preserve_unresolved(self, observation: RawCorporateEventObservation) -> str:
        """Retain raw source evidence without inventing an event identity."""
        return self.version_store.preserve_unresolved(observation)

    async def drain_pending(
        self,
        *,
        identity_scope: tuple[str, str] | None = None,
    ) -> EventDrainResult:
        """Process a bounded, lease-claimed set of recomputes and notifications."""
        jobs = self.version_store.claim_pending_jobs(
            limit=self.max_jobs_per_batch,
            max_symbols=self.max_symbols_per_batch,
            lease_seconds=self.lease_seconds,
            identity_scope=identity_scope,
        )
        if not jobs:
            return EventDrainResult(0, 0, 0, 0)

        by_symbol: dict[str, list[EventRecomputeJob]] = {}
        for job in jobs:
            by_symbol.setdefault(job.symbol, []).append(job)
        completed_jobs = 0
        failed_jobs = 0
        rows_written = 0
        failures: list[tuple[str, str]] = []
        for symbol, symbol_jobs in by_symbol.items():
            try:
                keys = tuple(
                    sorted(
                        {(job.symbol, job.ex_date) for job in symbol_jobs},
                        key=lambda key: key[1],
                    )
                )
                result = await asyncio.to_thread(
                    self.factor_builder.build,
                    affected_keys=keys,
                )
                scope = _scope_for_symbol(result, symbol)
                for job in symbol_jobs:
                    context = PipelineContext(
                        domain="stock_adjust",
                        source=_FACTOR_DERIVED_SOURCE,
                        window=Window(scope.window_start, scope.window_end),
                        affected_keys=((job.symbol, job.ex_date),),
                        symbols=(job.symbol,),
                    )
                    hook = self._notify_hook(job, scope.row_count)
                    notification = await hook(context)
                    _validate_notification(notification, job, scope)
                self.version_store.complete_jobs(symbol_jobs, scope)
                completed_jobs += len(symbol_jobs)
                rows_written += scope.row_count
            except Exception as exc:  # noqa: PERF203  # isolate and persist each symbol failure
                self.version_store.retry_jobs(symbol_jobs, exc)
                failed_jobs += len(symbol_jobs)
                failures.append((symbol, f"{type(exc).__name__}: {exc}"))
        return EventDrainResult(
            claimed_jobs=len(jobs),
            completed_jobs=completed_jobs,
            failed_jobs=failed_jobs,
            rows_written=rows_written,
            failures=tuple(failures),
        )

    def _notify_hook(
        self, job: EventRecomputeJob, row_count: int
    ) -> Callable[[PipelineContext], Awaitable[object]]:
        if self.notify_hook_factory is not None:
            return self.notify_hook_factory(job.batch_id, row_count, job.created_at)
        return build_notify_hook(
            self.version_store.engine,
            batch_id=job.batch_id,
            layer="dwd",
            table=self.factor_builder.target_table,
            payload_mode="meta",
            row_count=row_count,
            created_at=job.created_at,
        )


def _persist_recompute_jobs(
    connection: Connection,
    version: CorporateEventVersion,
    digest: str,
    affected_keys: Sequence[tuple[str, date]],
) -> tuple[str, ...]:
    """Insert deterministic outbox jobs in the event-version transaction."""
    if not affected_keys:
        return ()
    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        conflict_clause = "ON CONFLICT(`job_id`) DO NOTHING"
    elif dialect_name in {"mysql", "mariadb"}:
        conflict_clause = "ON DUPLICATE KEY UPDATE `job_id` = VALUES(`job_id`)"
    else:
        raise RuntimeError(f"unsupported corporate-event outbox dialect {dialect_name!r}")

    created_at = _utc_naive(datetime.now(timezone.utc))
    identity = version.identity
    job_ids: list[str] = []
    for symbol, ex_date in affected_keys:
        name = "|".join(
            (
                identity.source,
                identity.event_identity,
                str(version.revision_order),
                digest,
                symbol,
                ex_date.isoformat(),
            )
        )
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"opendata:corporate-event-recompute:{name}"))
        batch_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"opendata:corporate-event-watermark:{job_id}")
        )
        connection.execute(
            text(
                # B608: fixed table and dialect clause; row values are bound.
                f"INSERT INTO `{EVENT_RECOMPUTE_OUTBOX_TABLE}` ("  # noqa: S608 # nosec B608
                "`job_id`, `batch_id`, `source`, `event_identity`, `revision_order`, "
                "`content_digest`, `symbol`, `ex_date`, `status`, `attempt_count`, "
                "`available_at`, `created_at`) VALUES ("
                ":job_id, :batch_id, :source, :event_identity, :revision_order, "
                ":content_digest, :symbol, :ex_date, 'pending', 0, :available_at, :created_at) "
                f"{conflict_clause}"
            ),
            {
                "job_id": job_id,
                "batch_id": batch_id,
                "source": identity.source,
                "event_identity": identity.event_identity,
                "revision_order": version.revision_order,
                "content_digest": digest,
                "symbol": symbol,
                "ex_date": ex_date,
                "available_at": created_at,
                "created_at": created_at,
            },
        )
        job_ids.append(job_id)
    return tuple(job_ids)


def _factor_symbol(value: str) -> str:
    """Normalize the source exchange suffix to the factor table symbol."""
    return value.split(".", 1)[0]


def _scope_for_symbol(result: FactorBuildResult, symbol: str) -> FactorWriteScope:
    """Require a committed, complete factor row scope for a recomputed symbol."""
    scopes = {scope.symbol: scope for scope in result.write_scopes}
    if sum(scope.row_count for scope in result.write_scopes) != result.rows_written:
        raise RuntimeError("factor builder row scopes do not match its committed row count")
    scope = scopes.get(symbol)
    if scope is None or scope.row_count < 1:
        raise RuntimeError(f"factor builder committed no rows for affected symbol {symbol!r}")
    return scope


def _validate_notification(
    notification: object,
    job: EventRecomputeJob,
    scope: FactorWriteScope,
) -> None:
    """Verify the durable watermark matches the factor write and stable job."""
    if not isinstance(notification, NotifyResult):
        raise TypeError("notify hook must return NotifyResult after recording its watermark")
    watermark = notification.watermark
    expected = (
        job.batch_id,
        "stock_adjust",
        _FACTOR_DERIVED_SOURCE,
        "dwd",
        scope.window_start,
        scope.window_end,
        scope.row_count,
        job.created_at,
    )
    actual = (
        watermark.batch_id,
        watermark.domain,
        watermark.source,
        watermark.layer,
        watermark.window_start,
        watermark.window_end,
        watermark.rows,
        watermark.created_at,
    )
    if actual != expected:
        raise ValueError(f"notify hook watermark does not match factor outbox job {job.job_id}")


def _version_parameters(version: CorporateEventVersion, digest: str) -> dict[str, object]:
    """Convert the immutable version into database-native scalar values."""
    identity = version.identity
    timestamp = version.published_timestamp
    return {
        "source": identity.source,
        "event_identity": identity.event_identity,
        "identity_basis": identity.identity_basis,
        "economic_event_id": identity.economic_event_id,
        "symbol": identity.symbol,
        "ex_date": identity.ex_date,
        "revision_order": version.revision_order,
        "source_revision": version.source_revision,
        "content_digest": digest,
        "pre_event_share_basis": version.pre_event_share_basis,
        "cash_dividend": float(version.cash_dividend),
        "stock_dividend": float(version.stock_dividend),
        "rights_shares": float(version.rights_shares),
        "rights_price": float(version.rights_price),
        "published_date": version.published_date,
        "published_timestamp": None if timestamp is None else _utc_naive(timestamp),
        "publication_precision": version.publication_precision,
        "source_timezone": version.source_timezone,
        "observed_at": _utc_naive(version.observed_at),
    }


def _utc_naive(value: datetime) -> datetime:
    """Store timezone-aware instants in UTC without a driver-dependent tz."""
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _row_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"database returned invalid corporate-event text {value!r}")
    return value


def _row_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"database returned invalid corporate-event date {value!r}") from exc
    raise ValueError(f"database returned invalid corporate-event date {value!r}")


def _row_datetime(value: object) -> datetime:
    """Normalize the UTC-naive timestamp stored in the durable outbox."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"database returned invalid outbox timestamp {value!r}") from exc
    raise ValueError(f"database returned invalid outbox timestamp {value!r}")


__all__ = [
    "CorporateEventIngestResult",
    "CorporateEventIngestService",
    "CorporateEventConflictError",
    "CorporateEventVersionStore",
    "EventDrainResult",
    "EventRecomputeJob",
    "EventVersionWrite",
]
