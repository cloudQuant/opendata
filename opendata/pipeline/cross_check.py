"""Cross-check engine (design §8.2, milestone A4.5).

Two sources for one domain are normalized through their mapping
tables onto the contract shape and compared key by key:

```
ods(源A) → field_mapping → 契约模型 ┐
                                    ├→ 按业务 key 逐字段比对 → dq_diff_report
ods(源B) → field_mapping → 契约模型 ┘
```

Rules from the design: numeric fields pass within their relative
tolerance (default 1e-6, price fields 1e-4 on the mapping), dates and
strings compare exactly, and a key present on only one side is a
``missing`` difference (both sides missing the key cannot happen: the
key universe is the union of the two sides). The diff rate uses the
union of keys as its denominator, and only aggregate counts plus a
sampled detail list are produced - the full detail is exported
separately so a million-row diff cannot flood the warehouse.

The verdicts feed two consumers: :mod:`opendata.pipeline.diff_report`
(detail rows in ``dq_diff_report``) and the alert policy.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from opendata.data.mapping import normalize_frame

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import pandas as pd

    from opendata.data.mapping import DomainMapping

#: Default cap on sampled detail rows per comparison.
DEFAULT_SAMPLE_LIMIT = 100

#: Pseudo field name of a whole-row verdict (single-side missing key).
ROW_FIELD = "*"


class Verdict(str, enum.Enum):
    """Outcome of one comparison."""

    CONSISTENT = "consistent"
    DEVIATION = "deviation"
    MISSING = "missing"


@dataclass(frozen=True)
class FieldDiff:
    """One non-consistent comparison result.

    Attributes:
        biz_key: Business key values of the row.
        field: Contract field, or ``*`` for a whole-row verdict.
        value_a: Value from source A (None when the row is missing).
        value_b: Value from source B (None when the row is missing).
        deviation: Relative deviation for numeric fields, else None.
        verdict: Deviation or missing.
    """

    biz_key: tuple[object, ...]
    field: str
    value_a: object
    value_b: object
    deviation: float | None
    verdict: Verdict


@dataclass(frozen=True)
class DiffSummary:
    """Aggregate result of one cross-check.

    Attributes:
        domain: Domain identifier.
        source_a: First source identifier.
        source_b: Second source identifier.
        checked_at: Comparison timestamp.
        batch_id: Batch identifier of the run.
        compared_keys: Size of the key union (rate denominator).
        deviation_count: Rows with an over-tolerance field value.
        missing_count: Keys present on only one side.
        per_field: Deviation counts per contract field.
        samples: Sampled non-consistent details (capped).
    """

    domain: str
    source_a: str
    source_b: str
    checked_at: datetime
    batch_id: str
    compared_keys: int
    deviation_count: int
    missing_count: int
    per_field: dict[str, int] = field(default_factory=dict)
    samples: list[FieldDiff] = field(default_factory=list)

    @property
    def diff_rate(self) -> float:
        """Share of compared keys with at least one difference."""
        if self.compared_keys == 0:
            return 0.0
        return (self.deviation_count + self.missing_count) / self.compared_keys

    @property
    def verdict(self) -> Verdict:
        """Overall verdict of the comparison."""
        if self.deviation_count:
            return Verdict.DEVIATION
        if self.missing_count:
            return Verdict.MISSING
        return Verdict.CONSISTENT

    @property
    def has_diffs(self) -> bool:
        """Whether anything differed."""
        return bool(self.deviation_count or self.missing_count)


def compare_source_frames(
    domain: str,
    frame_a: pd.DataFrame,
    mapping_a: DomainMapping,
    frame_b: pd.DataFrame,
    mapping_b: DomainMapping,
    *,
    source_a: str,
    source_b: str,
    batch_id: str,
    checked_at: datetime | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> DiffSummary:
    """Normalize two source frames and compare them key by key.

    Args:
        domain: Domain identifier (both mappings must be for it).
        frame_a: Raw frame of source A.
        mapping_a: Mapping of source A.
        frame_b: Raw frame of source B.
        mapping_b: Mapping of source B.
        source_a: Source identifier of A.
        source_b: Source identifier of B.
        batch_id: Batch identifier of the run.
        checked_at: Comparison timestamp; defaults to now (UTC).
        sample_limit: Maximum sampled details to keep.

    Returns:
        The aggregate summary with sampled details.

    Raises:
        ValueError: If a mapping belongs to another domain, a frame has
            duplicate business keys, or the key columns disagree.
    """
    if mapping_a.domain != domain or mapping_b.domain != domain:
        raise ValueError(
            f"mappings must both belong to domain {domain!r} "
            f"(got {mapping_a.domain!r}/{mapping_b.domain!r})"
        )
    return compare_normalized(
        domain,
        normalize_frame(frame_a, mapping_a),
        normalize_frame(frame_b, mapping_b),
        key=mapping_a.key,
        tolerances=mapping_b.tolerances or mapping_a.tolerances,
        source_a=source_a,
        source_b=source_b,
        batch_id=batch_id,
        checked_at=checked_at,
        sample_limit=sample_limit,
    )


def compare_normalized(
    domain: str,
    frame_a: pd.DataFrame,
    frame_b: pd.DataFrame,
    *,
    key: Sequence[str],
    tolerances: Mapping[str, float] | None = None,
    source_a: str,
    source_b: str,
    batch_id: str,
    checked_at: datetime | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> DiffSummary:
    """Compare two contract-shaped frames key by key.

    Args:
        domain: Domain identifier.
        frame_a: Normalized frame of source A.
        frame_b: Normalized frame of source B.
        key: Business-key columns.
        tolerances: Optional per-field relative tolerances.
        source_a: Source identifier of A.
        source_b: Source identifier of B.
        batch_id: Batch identifier of the run.
        checked_at: Comparison timestamp; defaults to now (UTC).
        sample_limit: Maximum sampled details to keep.

    Returns:
        The aggregate summary with sampled details.

    Raises:
        ValueError: If a key column is missing from a frame or a frame
            holds duplicate keys (ambiguous comparison, fail closed).
    """
    from opendata.data.mapping import DEFAULT_TOLERANCE

    thresholds = tolerances or {}
    key_set = set(key)
    fields = [name for name in frame_a.columns if name not in key_set]
    other = {name for name in frame_b.columns if name not in key_set}
    if set(fields) != other:
        raise ValueError(
            f"sources {source_a!r} and {source_b!r} expose different contract fields "
            f"after normalization (only in {source_a!r}: {sorted(set(fields) - other)}, "
            f"only in {source_b!r}: {sorted(other - set(fields))}); "
            "the field mappings disagree (fail closed)"
        )
    rows_a = _index_by_key(frame_a, key, source_a)
    rows_b = _index_by_key(frame_b, key, source_b)
    universe = sorted(set(rows_a) | set(rows_b), key=lambda item: tuple(str(part) for part in item))
    deviations = 0
    missing = 0
    per_field: dict[str, int] = {}
    samples: list[FieldDiff] = []
    for biz_key in universe:
        row_a = rows_a.get(biz_key)
        row_b = rows_b.get(biz_key)
        if row_a is None or row_b is None:
            missing += 1
            _sample(
                samples,
                sample_limit,
                FieldDiff(
                    biz_key=biz_key,
                    field=ROW_FIELD,
                    value_a=None if row_a is None else "present",
                    value_b=None if row_b is None else "present",
                    deviation=None,
                    verdict=Verdict.MISSING,
                ),
            )
            continue
        for field_name in fields:
            deviation = _deviation(row_a.get(field_name), row_b.get(field_name))
            if deviation is None:  # strings: exact compare
                if str(row_a.get(field_name)) != str(row_b.get(field_name)):
                    deviations += 1
                    per_field[field_name] = per_field.get(field_name, 0) + 1
                    _sample(
                        samples,
                        sample_limit,
                        FieldDiff(
                            biz_key=biz_key,
                            field=field_name,
                            value_a=row_a.get(field_name),
                            value_b=row_b.get(field_name),
                            deviation=None,
                            verdict=Verdict.DEVIATION,
                        ),
                    )
                continue
            if deviation > thresholds.get(field_name, DEFAULT_TOLERANCE):
                deviations += 1
                per_field[field_name] = per_field.get(field_name, 0) + 1
                _sample(
                    samples,
                    sample_limit,
                    FieldDiff(
                        biz_key=biz_key,
                        field=field_name,
                        value_a=row_a.get(field_name),
                        value_b=row_b.get(field_name),
                        deviation=deviation,
                        verdict=Verdict.DEVIATION,
                    ),
                )
    return DiffSummary(
        domain=domain,
        source_a=source_a,
        source_b=source_b,
        checked_at=checked_at or datetime.now(timezone.utc),
        batch_id=batch_id,
        compared_keys=len(universe),
        deviation_count=deviations,
        missing_count=missing,
        per_field=per_field,
        samples=samples,
    )


def _index_by_key(frame: pd.DataFrame, key: Sequence[str], source: str) -> dict[tuple, dict]:
    """Index a frame by its business key, failing closed on duplicates."""
    missing = [column for column in key if column not in frame.columns]
    if missing:
        raise ValueError(f"source {source!r} frame lacks key columns {missing}")
    indexed: dict[tuple, dict] = {}
    for record in frame.to_dict("records"):
        biz_key = tuple(record[column] for column in key)
        if biz_key in indexed:
            raise ValueError(
                f"source {source!r} has duplicate business key {biz_key!r}; "
                "the comparison needs unique keys (fail closed)"
            )
        indexed[biz_key] = record
    return indexed


def _deviation(value_a: object, value_b: object) -> float | None:
    """Relative deviation of two values, None when not numeric.

    Args:
        value_a: Value from source A.
        value_b: Value from source B.

    Returns:
        ``|a - b| / max(|a|, |b|)`` for numeric pairs (0.0 when both
        are zero and equal, 1.0 when exactly one is zero), or None when
        either value is not numeric.
    """
    import pandas as pd

    numbers: list[float] = []
    for value in (value_a, value_b):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        try:
            numbers.append(float(value))  # type: ignore[arg-type]  # numeric-cell coercion
        except (TypeError, ValueError):
            return None
    left, right = numbers
    denominator = max(abs(left), abs(right))
    if denominator == 0.0:
        return 0.0
    return abs(left - right) / denominator


def _sample(samples: list[FieldDiff], limit: int, diff: FieldDiff) -> None:
    """Append a detail while the sample cap allows it."""
    if limit > 0 and len(samples) < limit:
        samples.append(diff)
