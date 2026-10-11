"""Fan-out and join declarations for engine models that need more than one request.

The engine's fetch loop builds one URL from one ``ModelSpec`` and pages it
(``http_json.fetch_pages``, line 549's single ``url = ...``); the capability census measured 238
model rows and found ten whose upstream answer is not one request. Five fan a single declaration
out over a list of symbols or over the rows of a first response (``flow.fan_out``), and five fetch
a second endpoint keyed off the first response's rows and merge it onto them (``flow.join``). The
registry's own words for the seam: "fetch_pages builds one URL per declaration and paging it;
there is no per-symbol or per-item request set to enumerate and concatenate" and "the same
single-URL loop is the proof no second response is fetched and joined onto the first model's
rows."

This module is the declaration plus the PURE parts of both capabilities: it turns a declared
variant source into an ordered plan, concatenates fetched rows in the declared order, and performs
the key-matched merge. It never performs I/O. The caller injects a "fetch one plan step" callable
as the transport seam, and the contract suite proves it is handed exactly the steps this module
built. Every hazard that would otherwise come back as a silently short result -- a variant that
yielded nothing where all variants are required, an unmatched row under ``inner``, a duplicate
right-hand key under a uniqueness declaration, a str-vs-int key mix, a write-over-a-left-column
collision -- raises :class:`MultiRequestError`; a malformed declaration never reads back as an
empty result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

__all__ = [
    "FanOutPlan",
    "FanOutSpec",
    "JoinFetchSpec",
    "JoinSpec",
    "MultiRequestError",
    "PlanStep",
    "build_fan_out_plan",
    "build_join_plan",
    "execute_plan",
    "merge_rows",
    "variants_from_rows",
]

#: One decoded record as the engine's row normalizer receives it: raw keys to raw values.
Record = Mapping[str, object]

#: The injectable transport seam. It is called once per plan step with the step this module built
#: and returns that step's decoded rows; nothing here performs the fetch itself.
StepFetcher = Callable[["PlanStep"], Iterable[Record]]

#: The ceiling a declared variant/request cap may sit under. A cap large enough to enumerate more
#: requests than any reviewed model publishes is an unbounded loop wearing a bound, so the bound
#: itself is bounded -- the same reading ``PaginationSpec.max_pages`` takes.
_MAX_VARIANT_CEILING = 1_000


class MultiRequestError(RuntimeError):
    """A stable, attributable refusal of a fan-out or join plan.

    Codes are bare tokens (``FANOUT_VARIANT_CAP``, ``JOIN_DUP_RIGHT_KEY``, ...) so the integration
    that owns ``ModelSpec`` can re-raise them under a declaration's own ``error_prefix`` without
    this module needing to know it.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        """Store the refusal code and the detail that names the offending row or variant."""
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _check_name(value: object, label: str) -> str:
    """Return ``value`` when it is a usable non-blank key or field name."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-blank string, got {value!r}")
    return value


def _check_cap(value: object, label: str) -> int:
    """Return ``value`` when it is a bound this module can actually enforce."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an int, got {value!r}")
    if not 0 < value <= _MAX_VARIANT_CEILING:
        raise ValueError(f"{label} must be between 1 and {_MAX_VARIANT_CEILING}, got {value!r}")
    return value


@dataclass(frozen=True)
class FanOutSpec:
    """One request enumerated per variant of a declared list-valued field.

    The variants come either from a list-valued query parameter (the comma-separated ``symbols``
    the intrinio and deribit info models split and re-request, ``source="param"``) or from the rows
    of a first response (the fundamentals ids hop one publishes, ``source="rows"``). Each step's
    overlay binds the variant text under ``overlay_key`` so the caller's path template or query
    encodes it; concatenation follows plan order, optionally re-sorted by ``order_by``.

    Attributes:
        source: Where the variant list comes from.
        field: Query parameter name (``param``) or first-response row key (``rows``).
        request_param: Name the overlay binds under; defaults to ``field``.
        max_variants: Hard cap on the distinct variant count; exceeding it refuses at plan-build
            time, before a single request is scheduled.
        all_required: Whether a variant that yields no rows fails the whole plan (fail closed)
            instead of being skipped.
        order_by: Row key the concatenated result is stably re-sorted by; empty keeps plan order.
    """

    source: Literal["param", "rows"]
    field: str
    request_param: str = ""
    max_variants: int = 64
    all_required: bool = True
    order_by: str = ""

    def __post_init__(self) -> None:
        """Reject a fan-out declaration the plan builder could not honour."""
        if self.source not in {"param", "rows"}:
            raise ValueError(f"fan-out source must be 'param' or 'rows', got {self.source!r}")
        _check_name(self.field, "fan-out field")
        if self.request_param:
            _check_name(self.request_param, "fan-out request_param")
        _check_cap(self.max_variants, "fan-out max_variants")
        if not isinstance(self.all_required, bool):
            raise ValueError("fan-out all_required must be a bool")
        if self.order_by:
            _check_name(self.order_by, "fan-out order_by")

    @property
    def overlay_key(self) -> str:
        """Return the request parameter name each step binds its variant under."""
        return self.request_param or self.field


@dataclass(frozen=True)
class JoinFetchSpec:
    """The second request of a join, keyed off the first response's rows.

    This is the fan-out declaration for the right-hand side: one request per distinct value of
    ``source_key`` found in the left rows. ``build_join_plan`` compiles it onto the same
    :class:`FanOutPlan` the fan-out capability runs, so one seam enumerates both capabilities.

    Attributes:
        source_key: Left-row key whose values enumerate the right-hand requests.
        request_param: Name the overlay binds under; defaults to ``source_key``.
        max_requests: Cap on the distinct key count, enforced at plan-build time.
        all_required: Whether a key whose right-hand request yields no rows fails the whole plan.
    """

    source_key: str
    request_param: str = ""
    max_requests: int = 64
    all_required: bool = True

    def __post_init__(self) -> None:
        """Reject a join-fetch declaration the plan builder could not honour."""
        _check_name(self.source_key, "join source_key")
        if self.request_param:
            _check_name(self.request_param, "join request_param")
        _check_cap(self.max_requests, "join max_requests")
        if not isinstance(self.all_required, bool):
            raise ValueError("join all_required must be a bool")

    @property
    def overlay_key(self) -> str:
        """Return the request parameter name each step binds its key under."""
        return self.request_param or self.source_key


@dataclass(frozen=True)
class JoinSpec:
    """How the second response's rows merge onto the first model's rows.

    ``how`` has no default on purpose: left-preserve is a declaration, not a silent fallback. A
    merged row is a copy of the left row overlaid with the matched right row's copy columns, so
    the caller's rows are never mutated.

    Attributes:
        left_key: Row key on the first response's rows the join reads.
        right_key: Row key on the second response's rows the join reads.
        how: ``"inner"`` refuses an unmatched row on either side; ``"left"`` preserves unmatched
            left rows with their right columns nulled.
        right_unique: Whether a duplicate join key on the right is a refusal. When False, the
            first right row in right order wins a multi-matched left row, deterministically.
        right_columns: The right keys copied onto the left row; empty means every right key
            except ``right_key`` itself. A declared list also fixes the nulled schema under
            ``how="left"`` when no right rows were published at all.
    """

    left_key: str
    right_key: str
    how: Literal["inner", "left"]
    right_unique: bool = True
    right_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a join declaration whose merge could only ever misfire."""
        _check_name(self.left_key, "join left_key")
        _check_name(self.right_key, "join right_key")
        if self.how not in {"inner", "left"}:
            raise ValueError(f"join how must be 'inner' or 'left', got {self.how!r}")
        if not isinstance(self.right_unique, bool):
            raise ValueError("join right_unique must be a bool")
        if not isinstance(self.right_columns, tuple) or any(
            not isinstance(name, str) or not name.strip() for name in self.right_columns
        ):
            raise ValueError("join right_columns must be a tuple of non-blank keys")
        if len(self.right_columns) != len(set(self.right_columns)):
            raise ValueError("join right_columns declares the same key twice")


@dataclass(frozen=True)
class PlanStep:
    """One request in a built plan, handed verbatim to the injected fetcher.

    Attributes:
        index: The step's position in the plan; concatenation follows it.
        variant: The rendered variant text this step enumerates.
        params: The overlay the caller merges into the declaration's encoded query; exactly
            ``{overlay_key: variant}``.
    """

    index: int
    variant: str
    params: Mapping[str, str]


@dataclass(frozen=True)
class FanOutPlan:
    """The ordered request set a fan-out or join declaration compiled to.

    Attributes:
        spec: The :class:`FanOutSpec` this plan was built from.
        steps: One step per distinct variant, in declared order.
    """

    spec: FanOutSpec
    steps: tuple[PlanStep, ...]

    @property
    def variants(self) -> tuple[str, ...]:
        """Return the variant texts in the order the plan enumerates them."""
        return tuple(step.variant for step in self.steps)


def _render_variant(value: object, position: int) -> str:
    """Return the URL text of one variant, refusing what cannot address a request."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MultiRequestError(
            "FANOUT_VARIANT_TYPE",
            f"variant {position} must be a str or int, got {value!r}",
        )
    text = str(value)
    if not text.strip():
        raise MultiRequestError("FANOUT_VARIANT_BLANK", f"variant {position} is blank")
    return text


def variants_from_rows(rows: Sequence[Record], key: str) -> tuple[object, ...]:
    """Return ``key``'s value from every row, refusing a row that does not publish it.

    A variant source that silently skips the row missing its key would fan out over a shorter
    request set than the first response declared, and the result would look like a legitimate
    smaller answer.
    """
    values: list[object] = []
    for position, row in enumerate(rows):
        if not isinstance(row, Mapping) or key not in row:
            raise MultiRequestError(
                "FANOUT_SOURCE_KEY_MISSING",
                f"row {position} does not publish variant source {key!r}",
            )
        values.append(row[key])
    return tuple(values)


def build_fan_out_plan(spec: FanOutSpec, variants: Sequence[object]) -> FanOutPlan:
    """Compile ``variants`` into an ordered plan, capped and de-duplicated.

    Duplicates collapse to their first occurrence so one comma-list cannot address the same URL
    twice, and the cap is measured on the distinct variants -- a caller who passed the same symbol
    forty times did not declare forty requests.

    Raises:
        MultiRequestError: The variant list is empty, holds a value that cannot render into a
            request, or exceeds ``spec.max_variants``.
    """
    if isinstance(variants, (str, bytes, bytearray)):
        raise MultiRequestError(
            "FANOUT_VARIANTS_NOT_A_LIST",
            "a fan-out variant list cannot itself be one string; pass the split values",
        )
    rendered: list[str] = []
    seen: set[str] = set()
    for position, value in enumerate(variants):
        text = _render_variant(value, position)
        if text in seen:
            continue
        seen.add(text)
        rendered.append(text)
    if not rendered:
        raise MultiRequestError("FANOUT_NO_VARIANTS", "the declared variant list is empty")
    if len(rendered) > spec.max_variants:
        raise MultiRequestError(
            "FANOUT_VARIANT_CAP",
            f"{len(rendered)} distinct variants exceed the declared cap of {spec.max_variants}",
        )
    steps = tuple(
        PlanStep(index=index, variant=text, params={spec.overlay_key: text})
        for index, text in enumerate(rendered)
    )
    return FanOutPlan(spec=spec, steps=steps)


def build_join_plan(spec: JoinFetchSpec, left_rows: Sequence[Record]) -> FanOutPlan:
    """Compile one right-hand request per distinct join key found in ``left_rows``.

    Raises:
        MultiRequestError: A left row misses ``source_key``, the keys are empty or uncappable, or
            a key cannot render into a request.
    """
    fan_out = FanOutSpec(
        source="rows",
        field=spec.source_key,
        request_param=spec.overlay_key,
        max_variants=spec.max_requests,
        all_required=spec.all_required,
    )
    return build_fan_out_plan(fan_out, variants_from_rows(left_rows, spec.source_key))


def _materialize_rows(step: PlanStep, rows: object) -> tuple[Record, ...]:
    """Return one step's fetched rows, refusing what is not a row sequence."""
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Iterable):
        raise MultiRequestError(
            "FANOUT_STEP_ROWS",
            f"step {step.index} returned {rows!r}, which is not a sequence of records",
        )
    materialized: list[Record] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MultiRequestError(
                "FANOUT_STEP_ROWS",
                f"step {step.index} yielded a non-record row {row!r}",
            )
        materialized.append(row)
    return tuple(materialized)


def _ordered(rows: list[Record], order_by: str) -> tuple[Record, ...]:
    """Return ``rows`` in plan order, or stably re-sorted by the declared ``order_by`` key.

    The sort is refused rather than approximated when a row lacks the key or the key's values mix
    str and int: a half-sorted or comparison-crashing result reported as ordered is the silent
    wrongness this guard exists to stop.
    """
    if not order_by:
        return tuple(rows)
    kinds: set[type] = set()
    for row in rows:
        if order_by not in row:
            raise MultiRequestError(
                "FANOUT_ORDER_KEY_MISSING", f"a concatenated row does not publish {order_by!r}"
            )
        value = row[order_by]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise MultiRequestError(
                "FANOUT_ORDER_KEY_TYPE", f"sort value {value!r} under {order_by!r} is not str/int"
            )
        kinds.add(type(value))
    if len(kinds) > 1:
        raise MultiRequestError(
            "FANOUT_ORDER_KEY_TYPE", f"{order_by!r} mixes str and int sort values"
        )
    # The loop above has already refused any value that is not str/int, so the sort key is total --
    # the cast only tells mypy what the guard established.
    return tuple(sorted(rows, key=lambda row: cast("str | int", row[order_by])))


def execute_plan(plan: FanOutPlan, fetch: StepFetcher) -> tuple[Record, ...]:
    """Fetch every step through the injected transport and concatenate the rows.

    Steps are executed in plan order and their rows appended in the same order, so the result is
    a deterministic function of the plan; ``spec.order_by`` then applies the declared re-sort.

    Raises:
        MultiRequestError: A step's fetcher returned something that is not a record sequence, or
            a step yielded no rows while the declaration says all variants are required.
    """
    collected: list[Record] = []
    for step in plan.steps:
        rows = _materialize_rows(step, fetch(step))
        if not rows and plan.spec.all_required:
            raise MultiRequestError(
                "FANOUT_EMPTY_VARIANT",
                f"variant {step.variant!r} (step {step.index}) yielded no rows "
                "and the plan declares all variants required",
            )
        collected.extend(rows)
    return _ordered(collected, plan.spec.order_by)


def _classify_join_key(value: object, label: str) -> tuple[str, str]:
    """Return a ``(type-tag, text)`` pair that only ever matches its own kind.

    Int and str keys are kept in separate type tags on purpose: ``123`` and ``"123"`` are the
    upstream's two spellings of one identity as often as they are two different identities, and
    an equality that quietly flattens them drops whichever rows the other spelling addressed. A
    near-miss (same text, other tag) is reported as the ambiguity it is instead of as a miss.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MultiRequestError("JOIN_KEY_TYPE", f"{label} key {value!r} is not str/int")
    if isinstance(value, int):
        return ("int", str(value))
    if not value.strip():
        raise MultiRequestError("JOIN_KEY_BLANK", f"{label} key is blank")
    return ("str", value)


def merge_rows(
    left_rows: Sequence[Record],
    right_rows: Sequence[Record],
    spec: JoinSpec,
) -> tuple[Record, ...]:
    """Merge the second response's rows onto the first model's rows by the declared keys.

    Refusals (each a :class:`MultiRequestError`, never a short result):
        ``JOIN_DUP_RIGHT_KEY`` -- the right publishes one key twice under a uniqueness declaration.
        ``JOIN_INNER_UNMATCHED_LEFT`` / ``JOIN_INNER_UNMATCHED_RIGHT`` -- an ``inner`` join would
            drop a row on that side.
        ``JOIN_AMBIGUOUS_KEY_TYPE`` -- a left key has no exact match but a right key with the same
            text under the other type tag; the rows would silently fall out of each other.
        ``JOIN_COLUMN_CONFLICT`` -- a copied right key names a left column other than the join
            key, so the merge would silently overwrite source facts.

    Under ``how="left"`` an unmatched left row is preserved with every right copy column nulled
    (the declared ``right_columns`` fix that schema even when the right published nothing).
    """
    right_copy = (
        set(spec.right_columns)
        if spec.right_columns
        else {key for row in right_rows for key in row if key != spec.right_key}
    )
    left_space = {key for row in left_rows for key in row} - {spec.left_key}
    conflict = left_space & right_copy
    if conflict:
        raise MultiRequestError(
            "JOIN_COLUMN_CONFLICT",
            f"right columns {sorted(conflict)} would overwrite left columns; "
            "name the copy set in right_columns",
        )

    index: dict[tuple[str, str], Record] = {}
    tag_by_text: dict[str, str] = {}
    for position, row in enumerate(right_rows):
        tag, text = _classify_join_key(row.get(spec.right_key, None), "right")
        pair = (tag, text)
        if text in tag_by_text and tag_by_text[text] != tag:
            raise MultiRequestError(
                "JOIN_AMBIGUOUS_KEY_TYPE",
                f"the right publishes {text!r} as both {tag_by_text[text]} and {tag}; "
                "a str/int key mix would silently drop rows",
            )
        tag_by_text[text] = tag
        if pair in index:
            if spec.right_unique:
                raise MultiRequestError(
                    "JOIN_DUP_RIGHT_KEY",
                    f"right key {text!r} is published again by row {position} "
                    "under a uniqueness declaration",
                )
        else:
            index[pair] = row

    merged: list[Record] = []
    matched: set[tuple[str, str]] = set()
    for position, row in enumerate(left_rows):
        tag, text = _classify_join_key(row.get(spec.left_key, None), "left")
        pair = (tag, text)
        right = index.get(pair)
        if right is None and tag_by_text.get(text) not in (None, tag):
            raise MultiRequestError(
                "JOIN_AMBIGUOUS_KEY_TYPE",
                f"left key {text!r} ({tag}) has a right key spelled {tag_by_text[text]}; "
                "an exact-only match would silently drop these rows",
            )
        if right is None:
            if spec.how == "inner":
                raise MultiRequestError(
                    "JOIN_INNER_UNMATCHED_LEFT",
                    f"left row {position} (key {text!r}) matched no right row under an inner join",
                )
            merged.append({**dict(row), **dict.fromkeys(sorted(right_copy), None)})
            continue
        matched.add(pair)
        overlay = {key: right[key] for key in sorted(right_copy) if key in right}
        merged.append({**dict(row), **overlay})

    if spec.how == "inner" and set(index) - matched:
        unmatched = sorted(text for _tag, text in set(index) - matched)
        raise MultiRequestError(
            "JOIN_INNER_UNMATCHED_RIGHT",
            f"right keys {unmatched} matched no left row under an inner join",
        )
    return tuple(merged)
