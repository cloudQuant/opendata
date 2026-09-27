"""dwd merge layer (design §8.3, milestone A4.6).

``dwd_<domain>`` holds the contract-standardized rows plus the
traceability columns ``source`` / ``_merged_at`` / ``_diff_flag`` /
``_as_of``. The merge is deliberately conservative:

* the row of the **highest-authority source that has it** wins; a key
  the authority lacks degrades to the next source and the ``source``
  column records which one served it;
* a numeric disagreement between sources never changes the value - it
  only raises ``_diff_flag`` to 1 (the detail lives in
  ``dq_diff_report``);
* single-source domains run in **passthrough** mode so ``layer=dwd``
  stays contract-uniform (design §8.3 "恒启用");
* the merge unit is ``window ∪ affected keys``: a corrected old key
  (adjustment recompute, financial restatement) is recomputed along
  with the window, which is how revisions propagate;
* ``_as_of`` is the point-in-time version date that makes a backtest
  reproducible.

Writes reuse the ods writer's key-level upsert SQL builder, so a
re-merge of the same key updates in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd

from opendata.pipeline.ods_writer import build_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Engine

    from opendata.data.mapping import DomainMapping
    from opendata.pipeline.runner import PipelineContext

    #: Reads one source's rows for a window and a set of affected keys.
    Reader = Callable[[date, date, set], "pd.DataFrame"]
    #: Writes the merged frame into dwd; returns rows written.
    WriteDwd = Callable[["pd.DataFrame"], int]

#: Column carrying the serving source of a dwd row.
SOURCE_COLUMN = "source"
#: Columns the merge stamps on every row.
TRACE_COLUMNS = ("source", "_merged_at", "_diff_flag", "_as_of")


@dataclass(frozen=True)
class MergeStats:
    """Outcome of one merge.

    Attributes:
        rows: Rows in the merged frame.
        diff_flagged: Rows marked ``_diff_flag = 1``.
        degraded_rows: Rows served by a non-authority source.
        passthrough: True for single-source domains.
        colliding: One reason per business key a source carried twice and the
            merge therefore refused (see :func:`_index`); empty when nothing
            was ambiguous.
    """

    rows: int
    diff_flagged: int
    degraded_rows: int
    passthrough: bool
    colliding: tuple[str, ...] = ()


def merge_source_frames(
    domain: str,
    frames: Mapping[str, pd.DataFrame],
    *,
    authority: Sequence[str],
    key: Sequence[str],
    as_of: date,
    merged_at: datetime,
    extra_diff_keys: set[tuple] | frozenset[tuple] = frozenset(),
) -> tuple[pd.DataFrame, MergeStats]:
    """Merge the normalized frames of one domain.

    Args:
        domain: Domain identifier (for error messages).
        frames: Source identifier to its contract-normalized frame.
        authority: Source identifiers in authority order.
        key: Business-key columns.
        as_of: Point-in-time version date stamped on the rows.
        merged_at: Merge timestamp stamped on the rows.
        extra_diff_keys: Additional keys to flag (from the cross-check
            or a known-difference registry).

    Returns:
        The merged frame plus its stats.

    Raises:
        ValueError: If an authority source has no frame, the frames
            disagree on their value columns, or a frame lacks the key
            columns (fail closed: the two could be reconciled by no
            row-level decision). A key a frame carries twice is *not*
            raised: that key alone is refused and reported in
            ``stats.colliding`` (see :func:`_index`).
    """
    unknown = [source for source in authority if source not in frames]
    if unknown:
        raise ValueError(
            f"authority sources {unknown} have no frame for {domain!r}; "
            "the authority order must match the frames (fail closed)"
        )
    key_set = set(key)
    value_columns = _value_columns(frames, key_set, domain)
    indexed: dict[str, dict[tuple, dict]] = {}
    refused: list[str] = []
    for source, frame in frames.items():
        rows, collisions = _index(frame, key, source)
        indexed[source] = rows
        refused.extend(f"{source}: {biz_key!r}" for biz_key in sorted(collisions, key=repr))
    all_keys = sorted(
        {biz_key for rows in indexed.values() for biz_key in rows},
        key=lambda item: tuple(str(part) for part in item),
    )
    disagreeing = _disagreeing_keys(indexed, authority, value_columns, key)
    flagged = set(extra_diff_keys) | disagreeing
    stamped_at = merged_at.astimezone(timezone.utc).replace(tzinfo=None)
    records: list[dict] = []
    degraded = 0
    for biz_key in all_keys:
        for rank, source in enumerate(authority):
            row = indexed[source].get(biz_key)
            if row is None:
                continue
            if rank > 0:
                degraded += 1
            record = {column: row.get(column) for column in value_columns}
            record.update({column: row.get(column) for column in key})
            record[SOURCE_COLUMN] = source
            record["_merged_at"] = stamped_at
            record["_diff_flag"] = 1 if biz_key in flagged else 0
            record["_as_of"] = as_of
            records.append(record)
            break
    merged = pd.DataFrame(records, columns=[*key, *value_columns, *TRACE_COLUMNS])
    stats = MergeStats(
        rows=len(merged),
        diff_flagged=int(merged["_diff_flag"].sum()) if len(merged) else 0,
        degraded_rows=degraded,
        passthrough=len(frames) == 1,
        colliding=tuple(sorted(refused)),
    )
    return merged, stats


class DwdWriter:
    """Write merged frames into a dwd table (key-level upsert)."""

    def __init__(self, engine: Engine) -> None:
        """Bind the warehouse engine.

        Args:
            engine: Engine of the data-warehouse database.
        """
        self.engine = engine

    def write(self, frame: pd.DataFrame, *, table: str, key: Sequence[str]) -> int:
        """Upsert a merged frame on its business key.

        Args:
            frame: Merged frame (includes the trace columns).
            table: Target dwd table name.
            key: Business-key columns.

        Returns:
            Number of rows written.
        """
        from sqlalchemy import text

        if frame.empty:
            return 0
        columns = list(frame.columns)
        statement = text(build_upsert_sql(table, columns, key))
        records = frame.astype(object).where(frame.notna(), None).to_dict("records")
        with self.engine.begin() as connection:
            connection.execute(statement, records)
        return len(frame)


@dataclass
class DwdMergeService:
    """Merge a domain's sources for one window (pipeline step 4).

    Attributes:
        domain: Domain identifier.
        sources: Source identifiers to merge.
        authority: Authority order (subset of ``sources``).
        readers: Source identifier to its reader (window + affected keys).
        write_dwd: Write callable (``DwdWriter.write`` bound to the table).
        merged_at: Merge timestamp (tests pin it); None means now.
    """

    domain: str
    sources: tuple[str, ...]
    authority: tuple[str, ...]
    readers: Mapping[str, Reader]
    write_dwd: WriteDwd
    key: tuple[str, ...] | None = None
    mappings: Mapping[str, DomainMapping] = field(default_factory=dict)
    merged_at: datetime | None = None

    async def run(
        self,
        start: date,
        end: date,
        *,
        affected_keys: set[tuple] | frozenset[tuple] = frozenset(),
    ) -> MergeStats:
        """Merge the window plus the affected keys.

        Args:
            start: Window start date.
            end: Window end date.
            affected_keys: Keys to recompute even when outside the
                window (revision propagation).

        Returns:
            The merge stats.

        Raises:
            LookupError: If a source lacks a reader (fail closed).
        """
        frames = {
            source: self._reader(source)(start, end, set(affected_keys)) for source in self.sources
        }
        merged, stats = merge_source_frames(
            self.domain,
            frames,
            authority=self.authority,
            key=self._key(),
            as_of=end,
            merged_at=self.merged_at or datetime.now(timezone.utc),
            extra_diff_keys=frozenset(affected_keys),
        )
        self.write_dwd(merged)
        return stats

    async def run_hook(self, context: PipelineContext) -> MergeStats:
        """Pipeline hook entry point (step 4).

        Args:
            context: The pipeline context of the current run.

        Returns:
            The merge stats (ignored by the runner, useful in tests).
        """
        return await self.run(
            context.window.start,
            context.window.end,
            affected_keys=self._contract_keys(context),
        )

    def _contract_keys(self, context: PipelineContext) -> set[tuple]:
        """Re-spell the run's affected keys as the keys the merge compares.

        ``context.affected_keys`` is what step 2 just wrote, so it arrives in
        the source's own spelling (``600519.SH``, or ``股票代码`` ordering),
        while the merged frames are indexed by the contract key. Handing the
        source spelling straight to ``extra_diff_keys`` made that set
        unmatchable - the flagging it is supposed to add silently never
        landed, and only the keys the merge itself found disagreeing got
        marked.

        Args:
            context: The pipeline context of the current run.

        Returns:
            The affected keys in contract spelling. A source with no mapping
            here keeps its keys as given: with nothing to translate through,
            guessing would be the same silent error with more confidence.
        """
        raw = {tuple(key) for key in context.affected_keys if isinstance(key, tuple)}
        mapping = self.mappings.get(context.source)
        if mapping is None:
            return raw
        return {mapping.to_contract_key(key) for key in raw}

    def _key(self) -> tuple[str, ...]:
        """Business key: explicit, else derived from a source mapping.

        Returns:
            The business-key columns.

        Raises:
            ValueError: If neither a key nor a mapping was supplied
                (fail closed: the merge must not guess its key).
        """
        if self.key:
            return self.key
        for source in self.sources:
            mapping = self.mappings.get(source)
            if mapping is not None:
                return mapping.key
        raise ValueError(
            f"dwd merge of {self.domain!r} needs the business key: pass key= or the source mappings"
        )

    def _reader(self, source: str) -> Reader:
        """Return one source's reader or fail closed."""
        if source not in self.readers:
            raise LookupError(
                f"no reader registered for source {source!r} in domain {self.domain!r}"
            )
        return self.readers[source]


def _value_columns(frames: Mapping[str, pd.DataFrame], key: set[str], domain: str) -> list[str]:
    """Contract value columns shared by every frame (fail closed)."""
    reference = None
    reference_source = None
    for source, frame in frames.items():
        columns = [column for column in frame.columns if column not in key]
        if reference is None:
            reference, reference_source = columns, source
            continue
        if set(columns) != set(reference):
            raise ValueError(
                f"sources disagree on the value columns of {domain!r} "
                f"({reference_source}: {sorted(reference)}, {source}: {sorted(columns)}); "
                "normalize through the field mappings first (fail closed)"
            )
    return reference or []


def _index(
    frame: pd.DataFrame, key: Sequence[str], source: str
) -> tuple[dict[tuple, dict], set[tuple]]:
    """Index a frame by business key, refusing every row of a colliding key.

    A key the leg carries twice is ambiguous: the dwd table is keyed on it, so
    only one of the two rows could ever be kept, and which one is not knowable
    here. Refusing that key (and reporting it) is the honest direction; the
    alternative this function took until C49 was to raise, which undelivered the
    whole window. The live shape: ``stock_action``/ths carries 55,072 keys, one
    of them twice (603883 published 0.16 cash and 0.5 cash + 0.3 bonus for the
    same 2024-06-27 ex-date) - and ``dwd_stock_action`` has been empty ever since.

    Returns:
        ``(rows by key, colliding keys)`` - a colliding key is in neither.

    Raises:
        ValueError: If a key column is absent (the mapping and the frame
            disagree, which no row-level decision can fix).
    """
    missing = [column for column in key if column not in frame.columns]
    if missing:
        raise ValueError(f"source {source!r} frame lacks key columns {missing}")
    indexed: dict[tuple, dict] = {}
    colliding: set[tuple] = set()
    for record in frame.to_dict("records"):
        biz_key = tuple(record[column] for column in key)
        if biz_key in colliding:
            continue
        if biz_key in indexed:
            colliding.add(biz_key)
            del indexed[biz_key]
            continue
        indexed[biz_key] = record
    return indexed, colliding


def _disagreeing_keys(
    indexed: Mapping[str, Mapping[tuple, dict]],
    authority: Sequence[str],
    value_columns: Sequence[str],
    key: Sequence[str],
) -> set[tuple]:
    """Keys where a lower-authority source disagrees with the winner."""
    from opendata.pipeline.cross_check import _deviation

    flagged: set[tuple] = set()
    for rank, source in enumerate(authority):
        rows = indexed[source]
        for biz_key, record in rows.items():
            winner = None
            for higher in authority[:rank]:
                if biz_key in indexed[higher]:
                    winner = indexed[higher][biz_key]
                    break
            if winner is None:
                continue
            for column in value_columns:
                if _differs(winner.get(column), record.get(column), _deviation):
                    flagged.add(biz_key)
                    break
    return flagged


def _differs(
    value_a: object,
    value_b: object,
    deviation_fn: Callable[[object, object], float | None],
) -> bool:
    """Whether two values disagree under the comparison rules."""
    deviation = deviation_fn(value_a, value_b)
    if deviation is None:
        return str(value_a) != str(value_b)
    return deviation > 1e-4
