"""Financial point-in-time version primitives (AC2-19).

The legacy financial DWD remains a latest-value table.  This module defines
the separate immutable version/coverage records used by the opt-in PIT path.
No timestamp is inferred from ``_as_of``, merge time, or local observation
time: callers must supply source publication facts explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opendata.data.models.financial import FinancialIndicator, FinancialStatement

if TYPE_CHECKING:
    from collections.abc import Mapping

FINANCIAL_DOMAINS = frozenset({"financial_statement", "financial_indicator"})
PUBLIC_TIME_PRECISIONS = frozenset({"timestamp", "date", "unknown"})
MAX_VERSION_BATCH = 2_000
MAX_VERSION_PAYLOAD_BYTES = 32_768
MAX_PIT_SYMBOLS = 20
MAX_PIT_CANDIDATES = 4_096
MAX_PIT_WINDOW_DAYS = 3_660
MAX_PIT_OFFSET = 100_000

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MARKET = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class FinancialPitError(ValueError):
    """Base error carrying a stable API/service error code."""

    code = "PIT_INVALID"


class FinancialPitUnsupported(FinancialPitError):  # noqa: N818 - stable public error code name
    """The available evidence cannot support a PIT answer."""

    code = "PIT_UNSUPPORTED"


class FinancialPitCoverageGap(FinancialPitUnsupported):
    """No explicit complete-history declaration covers the requested slice."""

    code = "PIT_COVERAGE_GAP"


class FinancialPitConflict(FinancialPitError):  # noqa: N818 - stable public error code name
    """A source revision has conflicting normalized contents."""

    code = "PIT_VERSION_CONFLICT"


class FinancialPitQueryTooBroad(FinancialPitError):  # noqa: N818 - stable public error code name
    """A query exceeded a fixed row/symbol/window bound."""

    code = "PIT_QUERY_TOO_BROAD"


@dataclass(frozen=True)
class FinancialVersionInput:
    """One source-normalized financial row and its proven source metadata.

    ``payload`` must be an exact FinancialStatement or FinancialIndicator
    contract row.  A caller has to provide a stable source revision and an
    explicit order; ``revision=1`` is never synthesized here.  ``unknown``
    publication precision stores an observation that is deliberately
    invisible to PIT queries.
    """

    domain: str
    source: str
    market: str
    payload: Mapping[str, object]
    source_revision: str
    revision_order: int
    public_time_precision: Literal["timestamp", "date", "unknown"]
    source_timezone: str
    observed_at: datetime
    provenance: str
    published_at: datetime | None = None
    published_on: date | None = None


@dataclass(frozen=True)
class FinancialCoverageInput:
    """An explicit complete-history declaration for one source/symbol."""

    domain: str
    source: str
    market: str
    symbol: str
    report_period_start: date
    report_period_end: date
    public_history_from: datetime
    complete_through: datetime
    source_timezone: str
    evidence_uri: str


@dataclass(frozen=True)
class PreparedFinancialVersion:
    """Validated and serialized row ready for a single SQL transaction."""

    values: Mapping[str, object]


@dataclass(frozen=True)
class IngestResult:
    """A committed ingest outcome; conflicts are persisted and reported."""

    inserted: int
    idempotent: int
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class ParsedAsOf:
    """An ``as_of`` selector preserving date versus exact timestamp precision."""

    kind: Literal["date", "timestamp"]
    value: date | datetime


def parse_as_of(value: str | date | datetime) -> ParsedAsOf:
    """Parse an ISO date or timezone-aware ISO timestamp.

    A date is interpreted as the end of that date in the selected source's
    declared timezone.  A timestamp must carry an explicit UTC offset.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise FinancialPitError("as_of timestamp must include a timezone offset")
        return ParsedAsOf("timestamp", value.astimezone(timezone.utc))
    if isinstance(value, date):
        return ParsedAsOf("date", value)
    if not isinstance(value, str) or not value:
        raise FinancialPitError("as_of must be an ISO date or timezone-aware timestamp")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return ParsedAsOf("date", date.fromisoformat(value))
        except ValueError as exc:
            raise FinancialPitError("as_of date must be a valid YYYY-MM-DD") from exc
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise FinancialPitError("as_of timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinancialPitError("as_of timestamp must include a timezone offset")
    return ParsedAsOf("timestamp", parsed.astimezone(timezone.utc))


def prepare_financial_version(record: FinancialVersionInput) -> PreparedFinancialVersion:
    """Validate a normalized financial row and make its immutable identity."""
    if record.domain not in FINANCIAL_DOMAINS:
        raise FinancialPitError(f"unsupported financial domain {record.domain!r}")
    if not _IDENTIFIER.fullmatch(record.source):
        raise FinancialPitError("source must be a lowercase provider identifier")
    if not _MARKET.fullmatch(record.market):
        raise FinancialPitError("market must be 1-32 letters, digits, '_' or '-'")
    if not isinstance(record.source_revision, str) or not record.source_revision.strip():
        raise FinancialPitError("source_revision must be explicit and non-empty")
    if len(record.source_revision) > 255:
        raise FinancialPitError("source_revision exceeds 255 characters")
    if type(record.revision_order) is not int or record.revision_order < 1:
        raise FinancialPitError("revision_order must be a positive source-proven integer")
    if record.public_time_precision not in PUBLIC_TIME_PRECISIONS:
        raise FinancialPitError("public_time_precision must be timestamp, date, or unknown")
    if not isinstance(record.provenance, str) or not record.provenance.strip():
        raise FinancialPitError("provenance must identify the source evidence")
    if len(record.provenance) > 4096:
        raise FinancialPitError("provenance exceeds 4096 characters")
    if not isinstance(record.observed_at, datetime) or record.observed_at.tzinfo is None:
        raise FinancialPitError("observed_at must be a timezone-aware timestamp")
    observed_at = _utc_naive(record.observed_at)
    try:
        source_zone = ZoneInfo(record.source_timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise FinancialPitError("source_timezone must be a valid IANA timezone") from exc

    model = FinancialStatement if record.domain == "financial_statement" else FinancialIndicator
    if "revision" not in record.payload:
        raise FinancialPitError(
            "payload revision must be explicit; implicit revision=1 is forbidden"
        )
    try:
        typed = model.model_validate(dict(record.payload))
    except Exception as exc:
        raise FinancialPitError(f"payload does not satisfy {model.__name__}: {exc}") from exc
    payload = typed.model_dump(mode="python")
    symbol = payload["symbol"]
    report_period = payload["report_period"]
    announce_date = payload["announce_date"]
    if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 128:
        raise FinancialPitError("payload symbol must be 1-128 non-empty characters")
    if record.revision_order != payload["revision"]:
        raise FinancialPitError("revision_order must equal the explicit contract revision")

    published_at_utc: datetime | None = None
    published_on: date | None = None
    visible_at_utc: datetime | None = None
    if record.public_time_precision == "timestamp":
        if (
            not isinstance(record.published_at, datetime)
            or record.published_at.tzinfo is None
            or record.published_at.utcoffset() is None
            or record.published_on is not None
        ):
            raise FinancialPitError("timestamp precision requires only an aware published_at value")
        published_at_utc = _utc_naive(record.published_at)
        published_on = record.published_at.astimezone(source_zone).date()
        visible_at_utc = published_at_utc
    elif record.public_time_precision == "date":
        if record.published_at is not None or not isinstance(record.published_on, date):
            raise FinancialPitError("date precision requires only a published_on date")
        published_on = record.published_on
        visible_at_utc = _source_day_start_after_utc(published_on, source_zone)
    elif record.published_at is not None or record.published_on is not None:
        raise FinancialPitError("unknown precision must not carry an unproven publication time")

    if published_on is not None and announce_date != published_on:
        raise FinancialPitError(
            "contract announce_date must equal the source-proven publication calendar date"
        )

    payload_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )
    payload_bytes = payload_json.encode("utf-8")
    if len(payload_bytes) > MAX_VERSION_PAYLOAD_BYTES:
        raise FinancialPitError(
            f"normalized financial payload exceeds {MAX_VERSION_PAYLOAD_BYTES} bytes"
        )

    key = _business_key(record.domain, payload)
    identity = {
        "domain": record.domain,
        "source": record.source,
        "market": record.market.upper(),
        "business_key": key,
        "source_revision": record.source_revision,
    }
    version_id = _sha256(identity)
    digest_material = {
        "payload": payload,
        "source_revision": record.source_revision,
        "revision_order": record.revision_order,
        "public_time_precision": record.public_time_precision,
        "published_on": published_on,
        "published_at_utc": published_at_utc,
        "source_timezone": record.source_timezone,
    }
    payload_digest = _sha256(digest_material)
    values = {
        "version_id": version_id,
        "domain": record.domain,
        "source": record.source,
        "market": record.market.upper(),
        "symbol": symbol,
        "report_period": report_period,
        "announce_date": announce_date,
        "statement_type": payload.get("statement_type"),
        "item": payload.get("item"),
        "indicator": payload.get("indicator"),
        "source_revision": record.source_revision,
        "revision_order": record.revision_order,
        "public_time_precision": record.public_time_precision,
        "published_at_utc": published_at_utc,
        "published_on": published_on,
        "visible_at_utc": visible_at_utc,
        "source_timezone": record.source_timezone,
        "observed_at_utc": observed_at,
        "provenance": record.provenance.strip(),
        "payload_json": payload_json,
        "payload_digest": payload_digest,
    }
    return PreparedFinancialVersion(values)


def prepare_financial_coverage(record: FinancialCoverageInput) -> dict[str, object]:
    """Validate and identify one source's certified PIT coverage interval."""
    if record.domain not in FINANCIAL_DOMAINS:
        raise FinancialPitError(f"unsupported financial domain {record.domain!r}")
    if not _IDENTIFIER.fullmatch(record.source):
        raise FinancialPitError("source must be a lowercase provider identifier")
    if not _MARKET.fullmatch(record.market):
        raise FinancialPitError("market must be 1-32 letters, digits, '_' or '-'")
    if not isinstance(record.symbol, str) or not record.symbol.strip() or len(record.symbol) > 128:
        raise FinancialPitError("coverage symbol must be 1-128 non-empty characters")
    if record.report_period_start > record.report_period_end:
        raise FinancialPitError("report_period_start must not be after report_period_end")
    start = _utc_naive(record.public_history_from)
    through = _utc_naive(record.complete_through)
    if start > through:
        raise FinancialPitError("public_history_from must not be after complete_through")
    if not isinstance(record.evidence_uri, str) or not record.evidence_uri.strip():
        raise FinancialPitError("coverage evidence_uri must be explicit")
    if len(record.evidence_uri) > 4096:
        raise FinancialPitError("coverage evidence_uri exceeds 4096 characters")
    try:
        ZoneInfo(record.source_timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise FinancialPitError("source_timezone must be a valid IANA timezone") from exc
    identity = {
        "domain": record.domain,
        "source": record.source,
        "market": record.market.upper(),
        "symbol": record.symbol,
        "report_period_start": record.report_period_start,
        "report_period_end": record.report_period_end,
        "public_history_from": start,
        "complete_through": through,
        "source_timezone": record.source_timezone,
        "evidence_uri": record.evidence_uri.strip(),
    }
    return {
        "coverage_id": _sha256(identity),
        "domain": record.domain,
        "source": record.source,
        "market": record.market.upper(),
        "symbol": record.symbol,
        "report_period_start": record.report_period_start,
        "report_period_end": record.report_period_end,
        "public_history_from_utc": start,
        "complete_through_utc": through,
        "source_timezone": record.source_timezone,
        "evidence_uri": record.evidence_uri.strip(),
        "evidence_digest": hashlib.sha256(record.evidence_uri.strip().encode("utf-8")).hexdigest(),
    }


def _business_key(domain: str, payload: Mapping[str, object]) -> dict[str, object]:
    if domain == "financial_statement":
        return {
            "symbol": payload["symbol"],
            "statement_type": payload["statement_type"],
            "report_period": payload["report_period"],
            "item": payload["item"],
        }
    return {
        "symbol": payload["symbol"],
        "report_period": payload["report_period"],
        "indicator": payload["indicator"],
    }


def _source_day_start_after_utc(value: date, zone: ZoneInfo) -> datetime:
    """Return the first source-local instant after a date-only publication.

    A public date identifies a calendar day, not a publication instant within
    that day.  The conservative availability boundary is therefore midnight
    at the start of the next source-local day (including DST transitions).
    """
    try:
        next_day = value + timedelta(days=1)
    except OverflowError as exc:
        raise FinancialPitError("date-only publication day is outside supported range") from exc
    return (
        datetime.combine(next_day, time.min, tzinfo=zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def _utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise FinancialPitError("timestamps must be datetime values")
    if value.tzinfo is None or value.utcoffset() is None:
        raise FinancialPitError("timestamps must include a timezone offset")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value {type(value).__name__}")
