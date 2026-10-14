"""Clean-room Federal Reserve FOMC documents and H.15 yield curve fetchers.

Only official Federal Reserve HTML and H.15 DDP CSV responses are read. The
public FOMC documents query is covered from 1959 onward. Yield-curve requests
select the latest H.15 observation on or before each requested date; that is
an explicit local selection rule, not a claim about a public "nearest date"
rule. The H.15 rate is an additive source extension to the public date/maturity
row contract.
"""

from __future__ import annotations

import csv
import io
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date as date_type
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from opendata.data.capability import Capability
from opendata.data.http_client import GovernedHttpClient, get_shared_http_client
from opendata.data.models.federal_reserve_clean_documents import (
    FederalReserveFomcDocument,
    FederalReserveYieldCurveObservation,
)
from opendata.data.protocol import FetchContext, Fetcher

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SOURCE = "federal_reserve"
MARKET = "us"
FEDERAL_RESERVE_HOST = "www.federalreserve.gov"
FED_DDP_OUTPUT_URL = "https://www.federalreserve.gov/datadownload/Output.aspx"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_HISTORICAL_URL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
H15_TREASURY_PACKAGE_ID = "bf17364827e38702b42a58cf8eaa3f78"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_FOMC_PAGES = 64
MAX_YIELD_CURVE_DATE_SELECTORS = 256
FIRST_CURRENT_CALENDAR_YEAR = 2021

_H15_SERIES = (
    ("RIFLGFCM01_N.B", "1-month"),
    ("RIFLGFCM03_N.B", "3-month"),
    ("RIFLGFCM06_N.B", "6-month"),
    ("RIFLGFCY01_N.B", "1-year"),
    ("RIFLGFCY02_N.B", "2-year"),
    ("RIFLGFCY03_N.B", "3-year"),
    ("RIFLGFCY05_N.B", "5-year"),
    ("RIFLGFCY07_N.B", "7-year"),
    ("RIFLGFCY10_N.B", "10-year"),
    ("RIFLGFCY20_N.B", "20-year"),
    ("RIFLGFCY30_N.B", "30-year"),
)
_H15_MISSING = frozenset({"", "ND"})
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_NAMES = (
    "January(?:\\s*/\\s*February)?|February|March|April(?:\\s*/\\s*May)?|May|June|July|August|"
    "September|October(?:\\s*/\\s*November)?|November|December|"
    "Jan(?:uary)?(?:\\s*/\\s*Feb(?:ruary)?)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?(?:\\s*/\\s*May)?|Jun(?:e)?|Jul(?:y)?|"
    "Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?(?:\\s*/\\s*Nov(?:ember)?)?|"
    "Nov(?:ember)?|Dec(?:ember)?"
)
_MEETING_DATE_RE = re.compile(
    rf"(?P<month>{_MONTH_NAMES})\s+(?P<day>\d{{1,2}})"
    rf"(?:\s*[-–—]\s*(?P<last_day>\d{{1,2}}))?"
    rf"(?P<note>\s*\([^)]*(?:meeting|notation vote|unscheduled|cancelled)[^)]*\))?"
    rf"(?:\s+(?P<meeting>Meeting)\b)?(?:\s*[-–—]\s*(?P<explicit_year>(?:19|20)\d{{2}}))?",
    re.IGNORECASE,
)
_YEAR_SECTION_RE = re.compile(r"(?P<year>(?:19|20)\d{2})\s+FOMC\s+Meetings", re.I)
_RELEASE_RE = re.compile(
    rf"Released\s+(?P<month>{_MONTH_NAMES})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>(?:19|20)\d{{2}})",
    re.IGNORECASE,
)
_DOC_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("speaker_key", re.compile(r"(?:SEP\s+)?(?:participant|speaker)\s+key", re.I)),
    ("beige_book", re.compile(r"beige\s+book", re.I)),
    ("teal_book", re.compile(r"teal\s*book", re.I)),
    ("green_book", re.compile(r"green\s*book", re.I)),
    ("blue_book", re.compile(r"blue\s*book", re.I)),
    ("red_book", re.compile(r"red\s*book", re.I)),
    ("minutes", re.compile(r"\bminutes\b", re.I)),
    ("press_conference", re.compile(r"press\s+conference", re.I)),
    ("press_release", re.compile(r"press\s+release", re.I)),
    (
        "projections",
        re.compile(
            r"(?:projection\s+materials|SEP\s*:\s*individual\s+projections|"
            r"summary\s+of\s+economic\s+projections)",
            re.I,
        ),
    ),
    ("agenda", re.compile(r"\bagenda\b", re.I)),
    ("transcript", re.compile(r"\btranscript\b", re.I)),
    (
        "monetary_policy",
        re.compile(
            r"statement(?:\s+on\s+longer[- ]run\s+goals\s+and\s+monetary\s+policy\s+strategy)?",
            re.I,
        ),
    ),
    (
        "materials",
        re.compile(
            r"(?:presentation\s+materials|implementation\s+note|\b\d{4}\s+memos\b)",
            re.I,
        ),
    ),
)


class FederalReserveSourceError(ValueError):
    """Invalid or incomplete Federal Reserve source response."""


def _current_year() -> int:
    return date_type.today().year


class _IdentityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: Literal["federal_reserve"] = "federal_reserve"
    market: Literal["us"] = "us"


class FomcDocumentsQuery(_IdentityQuery):
    """Public FOMC document filters, with the documented 1959 start year."""

    year: int | None = None
    document_type: Literal[
        "all",
        "monetary_policy",
        "minutes",
        "projections",
        "materials",
        "press_release",
        "press_conference",
        "agenda",
        "transcript",
        "speaker_key",
        "beige_book",
        "teal_book",
        "green_book",
        "blue_book",
        "red_book",
    ] = "all"

    @field_validator("year")
    @classmethod
    def _validate_year(cls, value: int | None) -> int | None:
        if value is not None and (value < 1959 or value > _current_year()):
            raise ValueError("year must be between 1959 and the current year")
        return value


def _normalize_date(value: object) -> date_type | None:
    if value is None:
        return None
    if type(value) is date_type:
        return value
    if isinstance(value, str):
        try:
            return date_type.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("date values must use YYYY-MM-DD") from exc
    raise ValueError("date values must be date, ISO date string, or null")


class YieldCurveQuery(_IdentityQuery):
    """The Fed-specific YieldCurve query accepts date selectors only."""

    date: date_type | None | list[date_type | None] = None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> object:
        if value is None:
            return None
        raw_values: Sequence[object]
        if isinstance(value, str) and "," in value:
            raw_values = value.split(",")
            values_are_list = True
        elif isinstance(value, list):
            raw_values = value
            values_are_list = True
        else:
            raw_values = [value]
            values_are_list = False
        if len(raw_values) > MAX_YIELD_CURVE_DATE_SELECTORS:
            raise ValueError(f"date accepts at most {MAX_YIELD_CURVE_DATE_SELECTORS} selectors")
        normalized = [_normalize_date(item) for item in raw_values]
        return normalized if values_are_list else normalized[0]


class _FederalReserveFetcher(Fetcher[Any, object]):
    """Shared lazy client access without query-error/source-error conflation."""

    async_mode: ClassVar[str] = "bounded_thread"
    capability: ClassVar[Capability]
    canonical_model: ClassVar[str]
    query_type: ClassVar[type[BaseModel]]

    def __init__(self, http_client: GovernedHttpClient | None = None) -> None:
        self._http_client = http_client

    def transform_query(self, **kwargs: object) -> BaseModel:
        return self.query_type.model_validate(kwargs)

    def _get(
        self,
        url: str,
        ctx: FetchContext,
        *,
        params: Mapping[str, str] | None = None,
        cache_context: Mapping[str, object] | None = None,
    ) -> bytes:
        client = self._http_client or get_shared_http_client()
        response = client.get(
            url,
            params=params,
            headers={"Accept": "text/html, text/csv;q=0.9, */*;q=0.1"},
            timeout=min(ctx.timeout or DEFAULT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS),
            source=SOURCE,
            cache_context={"model": self.canonical_model, **dict(cache_context or {})},
        )
        content = getattr(response, "content", None)
        if not isinstance(content, bytes):
            raise FederalReserveSourceError(f"{self.canonical_model}: expected byte response")
        return content


def _parse_source_date(value: str, *, model: str) -> date_type:
    try:
        return date_type.fromisoformat(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise FederalReserveSourceError(f"{model}: invalid observation date") from exc


def _parse_h15_csv(text: str) -> dict[date_type, tuple[float | None, ...]]:
    """Parse the official H.15 date-by-series table, including ``ND`` cells."""
    reader = csv.reader(io.StringIO(text, newline=""))
    header: tuple[str, ...] | None = None
    observations: dict[date_type, tuple[float | None, ...]] = {}
    series_ids = tuple(series_id for series_id, _ in _H15_SERIES)
    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        first = row[0].strip().casefold()
        if header is None:
            if first not in {"time period", "observation date", "date"}:
                continue
            header = tuple(item.strip().rsplit("/", maxsplit=1)[-1].upper() for item in row[1:])
            if len(header) != len(set(header)):
                raise FederalReserveSourceError("YieldCurve: duplicate H.15 series columns")
            missing = set(series_ids) - set(header)
            if missing:
                raise FederalReserveSourceError(
                    f"YieldCurve: H.15 CSV is missing series {sorted(missing)}"
                )
            continue
        if len(row) != len(header) + 1:
            raise FederalReserveSourceError("YieldCurve: H.15 CSV row width changed")
        observation = _parse_source_date(row[0], model="YieldCurve")
        if observation in observations:
            raise FederalReserveSourceError("YieldCurve: duplicate H.15 observation date")
        by_series: dict[str, float | None] = {}
        for column, series_id in zip(row[1:], header, strict=True):
            raw = column.strip()
            if raw.upper() in _H15_MISSING:
                value = None
            else:
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise FederalReserveSourceError(
                        f"YieldCurve: invalid H.15 rate for {series_id}"
                    ) from exc
                if not math.isfinite(value):
                    raise FederalReserveSourceError("YieldCurve: non-finite H.15 rate")
            by_series[series_id] = value
        observations[observation] = tuple(by_series[series_id] for series_id in series_ids)
    if header is None:
        raise FederalReserveSourceError("YieldCurve: H.15 CSV has no observation header")
    if not observations:
        raise FederalReserveSourceError("YieldCurve: H.15 CSV has no observations")
    return observations


class YieldCurveFetcher(_FederalReserveFetcher):
    """Fetch nominal Treasury constant maturities from the official H.15 CSV."""

    capability = Capability(
        asset_class="fixed_income",
        domain="federal_reserve_yield_curve",
        period="daily",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes=(
            "Clean-room H.15 nominal CMT source; rate is a source extension in fraction/year; "
            "date selection is latest observation on or before the requested date."
        ),
    )
    canonical_model = "YieldCurve"
    query_type = YieldCurveQuery

    def extract_data(self, params: YieldCurveQuery, ctx: FetchContext) -> object:
        """Fetch the official all-observations nominal Treasury package."""
        if params.date == []:
            return None
        package_params = {
            "rel": "H15",
            "series": H15_TREASURY_PACKAGE_ID,
            "lastobs": "",
            "from": "",
            "to": "",
            "filetype": "csv",
            "label": "include",
            "layout": "seriescolumn",
            "type": "package",
        }
        return self._get(
            FED_DDP_OUTPUT_URL,
            ctx,
            params=package_params,
            cache_context={"package": "treasury_constant_maturities"},
        )

    def transform_data(
        self, raw: object, params: YieldCurveQuery
    ) -> list[FederalReserveYieldCurveObservation]:
        """Select one curve observation for each distinct resolved query date."""
        if params.date == []:
            return []
        if not isinstance(raw, bytes):
            raise FederalReserveSourceError("YieldCurve: expected H.15 CSV bytes")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FederalReserveSourceError("YieldCurve: H.15 CSV is not UTF-8") from exc
        observations = _parse_h15_csv(text)
        dates = sorted(observations)
        query_dates = params.date
        if query_dates is None:
            selectors: list[date_type | None] = [None]
        elif isinstance(query_dates, list):
            selectors = query_dates
        else:
            selectors = [query_dates]
        if not selectors:
            return []
        selected_dates: set[date_type] = set()
        for selector in selectors:
            if selector is None:
                selected_dates.add(dates[-1])
                continue
            index = bisect_right(dates, selector) - 1
            if index >= 0:
                selected_dates.add(dates[index])
        output: list[FederalReserveYieldCurveObservation] = []
        for observation in sorted(selected_dates):
            values = observations[observation]
            for (_, maturity), rate in zip(_H15_SERIES, values, strict=True):
                output.append(
                    FederalReserveYieldCurveObservation(
                        date=observation,
                        maturity=maturity,
                        rate=None if rate is None else rate / 100.0,
                    )
                )
        return output


@dataclass(frozen=True)
class _PageRequest:
    year: int | None
    is_calendar: bool
    url: str


@dataclass(frozen=True)
class _FomcPage:
    year: int | None
    is_calendar: bool
    body: bytes


@dataclass(frozen=True)
class _Anchor:
    index: int
    href: str
    text: str
    start: int
    end: int
    release_group_ids: tuple[int, ...]


@dataclass(frozen=True)
class _DomFrame:
    """Track a parsed element and whether it defines a release-date scope."""

    node_id: int
    tag: str
    classes: frozenset[str]
    is_release_group: bool


class _VisibleHtml(HTMLParser):
    """Collect visible text, anchors, and bounded document-group ancestry."""

    _BLOCK_TAGS = frozenset(
        {
            "article",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "p",
            "section",
            "td",
            "th",
            "tr",
        }
    )
    _RELEASE_GROUP_TAGS = frozenset({"li", "p", "td", "th"})
    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.anchors: list[_Anchor] = []
        self._length = 0
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._anchor_start = 0
        self._anchor_release_group_ids: tuple[int, ...] = ()
        self._ignore_depth = 0
        self._dom_stack: list[_DomFrame] = []
        self._next_node_id = 0
        self._release_group_ranges: dict[int, list[int]] = {}
        self._release_group_tags: dict[int, str] = {}
        self._release_group_classes: dict[int, frozenset[str]] = {}
        self._release_group_parent_ids: dict[int, tuple[int, ...]] = {}
        self._release_group_anchor_indices: dict[int, list[int]] = {}
        self.anchor_document_types: dict[int, str | None] = {}

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def release_group_ranges(self) -> dict[int, tuple[int, int]]:
        """Return source-text spans for each bounded release-date group."""
        return {
            node_id: (bounds[0], bounds[1])
            for node_id, bounds in self._release_group_ranges.items()
        }

    @property
    def release_group_classes(self) -> dict[int, frozenset[str]]:
        """Return stable classes for DOM groups with release-date scopes."""
        return self._release_group_classes

    def release_group_owner(self, offset: int) -> int | None:
        """Find the most specific release group containing a source-text offset."""
        containing_groups = [
            group_id
            for group_id, (start, end) in self.release_group_ranges.items()
            if start <= offset < end
        ]
        if not containing_groups:
            return None
        deepest = max(
            len(self._release_group_parent_ids[group_id]) for group_id in containing_groups
        )
        nearest_groups = [
            group_id
            for group_id in containing_groups
            if len(self._release_group_parent_ids[group_id]) == deepest
        ]
        return nearest_groups[0] if len(nearest_groups) == 1 else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK_TAGS and not self._ignore_depth:
            self._append_separator()
        attributes = dict(attrs)
        class_value = attributes.get("class")
        classes = frozenset(class_value.split()) if isinstance(class_value, str) else frozenset()
        parent_is_meeting_row = bool(
            self._dom_stack
            and self._dom_stack[-1].tag == "div"
            and "fomc-meeting" in self._dom_stack[-1].classes
        )
        is_release_group = tag in self._RELEASE_GROUP_TAGS or (
            tag == "div" and (parent_is_meeting_row or "fomc-meeting__minutes" in classes)
        )
        if tag not in self._VOID_TAGS:
            node_id = self._next_node_id
            self._next_node_id += 1
            parent_release_group_ids = tuple(
                frame.node_id for frame in reversed(self._dom_stack) if frame.is_release_group
            )
            self._dom_stack.append(
                _DomFrame(
                    node_id=node_id,
                    tag=tag,
                    classes=classes,
                    is_release_group=is_release_group,
                )
            )
            if is_release_group:
                self._release_group_ranges[node_id] = [self._length, self._length]
                self._release_group_tags[node_id] = tag
                self._release_group_classes[node_id] = classes
                self._release_group_parent_ids[node_id] = parent_release_group_ids
                self._release_group_anchor_indices[node_id] = []
        if tag in {"script", "style", "noscript"}:
            self._ignore_depth += 1
        if tag == "a" and self._anchor_href is None and not self._ignore_depth:
            href = attributes.get("href")
            if isinstance(href, str):
                self._anchor_href = href
                self._anchor_text = []
                self._anchor_start = self._length
                self._anchor_release_group_ids = tuple(
                    frame.node_id for frame in reversed(self._dom_stack) if frame.is_release_group
                )

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            anchor_index = len(self.anchors)
            self.anchors.append(
                _Anchor(
                    index=anchor_index,
                    href=self._anchor_href,
                    text="".join(self._anchor_text).strip(),
                    start=self._anchor_start,
                    end=self._length,
                    release_group_ids=self._anchor_release_group_ids,
                )
            )
            self.anchor_document_types[anchor_index] = None
            for group_id in self._anchor_release_group_ids:
                self._release_group_anchor_indices[group_id].append(anchor_index)
            self._anchor_href = None
            self._anchor_text = []
            self._anchor_release_group_ids = ()
        if tag in self._BLOCK_TAGS and not self._ignore_depth:
            self._append_separator()
        if tag in {"script", "style", "noscript"} and self._ignore_depth:
            self._ignore_depth -= 1
        self._pop_dom_frame(tag)

    def handle_data(self, data: str) -> None:
        if self._ignore_depth or not data:
            return
        self.text_parts.append(data)
        self._length += len(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)

    def _append_separator(self) -> None:
        if self._length and not self.text_parts[-1].endswith((" ", "\n", "\t")):
            self.text_parts.append(" ")
            self._length += 1

    def _pop_dom_frame(self, tag: str) -> None:
        for index in range(len(self._dom_stack) - 1, -1, -1):
            if self._dom_stack[index].tag == tag:
                for frame in self._dom_stack[index:]:
                    if frame.is_release_group:
                        self._release_group_ranges[frame.node_id][1] = self._length
                del self._dom_stack[index:]
                return

    def close(self) -> None:
        super().close()
        for frame in self._dom_stack:
            if frame.is_release_group:
                self._release_group_ranges[frame.node_id][1] = self._length
        self._dom_stack.clear()


def _month_number(value: str) -> int:
    token = re.split(r"[/\s]", value.strip().casefold(), maxsplit=1)[0].rstrip(".")
    month = _MONTHS.get(token)
    if month is None:
        raise FederalReserveSourceError("FomcDocuments: unknown meeting month")
    return month


def _meeting_date_before(
    text: str, anchor_offset: int, *, default_year: int | None
) -> date_type | None:
    prefix = text[:anchor_offset]
    candidates: list[tuple[re.Match[str], int]] = []
    for match in _MEETING_DATE_RE.finditer(prefix):
        # A plain single day is a meeting only when its source heading marks it as such.
        if (
            match.group("last_day") is None
            and not match.group("note")
            and not match.group("meeting")
        ):
            continue
        candidates.append((match, match.start()))
    if not candidates:
        return None
    match, _ = candidates[-1]
    year_text = match.group("explicit_year")
    if year_text is None:
        section_matches = list(_YEAR_SECTION_RE.finditer(prefix, 0, match.start()))
        if section_matches:
            year_text = section_matches[-1].group("year")
    year = int(year_text) if year_text is not None else default_year
    if year is None:
        return None
    try:
        return date_type(year, _month_number(match.group("month")), int(match.group("day")))
    except (ValueError, OverflowError) as exc:
        raise FederalReserveSourceError("FomcDocuments: invalid meeting heading date") from exc


def _document_type(prefix: str, anchor_text: str) -> str | None:
    context = f"{prefix} {anchor_text}"
    matches: list[tuple[int, int, str]] = []
    for kind, pattern in _DOC_LABEL_PATTERNS:
        matches.extend((match.start(), match.end(), kind) for match in pattern.finditer(context))
    if not matches:
        return None
    _, _, kind = max(matches, key=lambda item: (item[0], item[1]))
    return kind


def _document_format(anchor: _Anchor, url: str) -> str | None:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix:
        return suffix.lstrip(".")
    text = anchor.text.casefold()
    if "pdf" in text:
        return "pdf"
    if "html" in text:
        return "htm"
    return None


def _release_date(
    parser: _VisibleHtml,
    anchor: _Anchor,
    *,
    doc_type: str,
) -> date_type | None:
    text = parser.text
    group_ranges = parser.release_group_ranges
    group_classes = parser.release_group_classes
    selected_group_id: int | None = None
    allow_sibling_child_groups = False
    if doc_type == "minutes":
        selected_group_id = next(
            (
                group_id
                for group_id in anchor.release_group_ids
                if "fomc-meeting__minutes" in group_classes.get(group_id, frozenset())
            ),
            None,
        )
        allow_sibling_child_groups = selected_group_id is not None
    if selected_group_id is None and anchor.release_group_ids:
        selected_group_id = anchor.release_group_ids[0]
    if selected_group_id is None:
        return None

    member_indices = parser._release_group_anchor_indices.get(selected_group_id, [])
    if anchor.index not in member_indices:
        return None
    member_types = {
        parser.anchor_document_types[index]
        for index in member_indices
        if parser.anchor_document_types.get(index) is not None
    }
    if member_types != {doc_type}:
        return None
    if not allow_sibling_child_groups:
        for index in member_indices:
            member = parser.anchors[index]
            if member.release_group_ids and member.release_group_ids[0] != selected_group_id:
                return None

    bounds = group_ranges.get(selected_group_id)
    if bounds is None:
        return None
    group_start, group_end = bounds
    group_text = text[group_start:group_end]
    release_dates: set[date_type] = set()
    for match in _RELEASE_RE.finditer(group_text):
        absolute_start = group_start + match.start()
        caption_group_id = parser.release_group_owner(absolute_start)
        if caption_group_id is None:
            continue
        if allow_sibling_child_groups:
            caption_parent_ids = parser._release_group_parent_ids.get(caption_group_id, ())
            if (
                caption_group_id != selected_group_id
                and selected_group_id not in caption_parent_ids
            ):
                continue
        elif caption_group_id != selected_group_id:
            continue
        if doc_type == "minutes":
            if "minutes" not in group_text[: match.start()].casefold():
                continue
        elif absolute_start < anchor.end or absolute_start - anchor.end > 100:
            continue
        try:
            release_dates.add(
                date_type(
                    int(match.group("year")),
                    _month_number(match.group("month")),
                    int(match.group("day")),
                )
            )
        except (ValueError, OverflowError) as exc:
            raise FederalReserveSourceError("FomcDocuments: invalid explicit release date") from exc
    return next(iter(release_dates)) if len(release_dates) == 1 else None


def _parse_fomc_page(
    body: bytes,
    *,
    page_year: int | None,
    is_calendar: bool,
    required_calendar_years: tuple[int, ...] = (),
) -> list[FederalReserveFomcDocument]:
    try:
        html = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FederalReserveSourceError("FomcDocuments: source page is not UTF-8") from exc
    parser = _VisibleHtml()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise FederalReserveSourceError("FomcDocuments: malformed FOMC HTML") from exc
    text = parser.text
    folded_text = text.casefold()
    if "fomc" not in folded_text and "federal open market committee" not in folded_text:
        raise FederalReserveSourceError("FomcDocuments: source page is not an FOMC page")
    if is_calendar:
        calendar_years = {int(match.group("year")) for match in _YEAR_SECTION_RE.finditer(text)}
        if page_year is None or page_year not in calendar_years:
            raise FederalReserveSourceError("FomcDocuments: current calendar lacks year marker")
        missing_calendar_years = sorted(set(required_calendar_years) - calendar_years)
        if missing_calendar_years:
            missing = ", ".join(str(year) for year in missing_calendar_years)
            raise FederalReserveSourceError(
                f"FomcDocuments: current calendar is missing year headings: {missing}"
            )
    else:
        if page_year is None or str(page_year) not in text:
            raise FederalReserveSourceError("FomcDocuments: historical page lacks year marker")
    pending: list[tuple[_Anchor, date_type, str, str, str]] = []
    previous_anchor_end = 0
    previous_document_type: str | None = None
    for anchor in parser.anchors:
        between_anchors = text[previous_anchor_end : anchor.start]
        meeting_date = _meeting_date_before(
            text,
            anchor.start,
            default_year=None if is_calendar else page_year,
        )
        base_url = FOMC_CALENDAR_URL if is_calendar else FOMC_HISTORICAL_URL.format(year=page_year)
        absolute_url = urljoin(base_url, anchor.href)
        parts = urlsplit(absolute_url)
        if parts.scheme != "https" or parts.hostname != FEDERAL_RESERVE_HOST:
            previous_anchor_end = anchor.end
            previous_document_type = None
            continue
        doc_type = _document_type(between_anchors, anchor.text)
        separator_only = re.fullmatch(r"[\s|,;/]*", between_anchors) is not None
        if doc_type is None and separator_only and anchor.text.casefold() in {"pdf", "html"}:
            doc_type = previous_document_type
        if meeting_date is None:
            previous_anchor_end = anchor.end
            previous_document_type = None
            continue
        if is_calendar and meeting_date.year > (page_year or meeting_date.year):
            previous_anchor_end = anchor.end
            previous_document_type = None
            continue
        if doc_type is None:
            if "/monetarypolicy/files/" in parts.path.casefold():
                raise FederalReserveSourceError(
                    "FomcDocuments: official meeting page has an unrecognized document label"
                )
            previous_anchor_end = anchor.end
            previous_document_type = None
            continue
        doc_format = _document_format(anchor, absolute_url)
        if doc_format is None:
            raise FederalReserveSourceError(
                f"FomcDocuments: linked {doc_type} document has no declared format"
            )
        parser.anchor_document_types[anchor.index] = doc_type
        pending.append((anchor, meeting_date, doc_type, doc_format, absolute_url))
        previous_anchor_end = anchor.end
        previous_document_type = doc_type
    output: list[FederalReserveFomcDocument] = []
    for anchor, meeting_date, doc_type, doc_format, absolute_url in pending:
        release_date = _release_date(parser, anchor, doc_type=doc_type)
        output.append(
            FederalReserveFomcDocument(
                date=meeting_date,
                doc_type=doc_type,
                doc_format=doc_format,
                url=absolute_url,
                release_date=release_date,
            )
        )
    return output


def _page_requests(query: FomcDocumentsQuery) -> tuple[_PageRequest, ...]:
    current_year = _current_year()
    if query.year is not None:
        if query.year <= 2020:
            return (
                _PageRequest(
                    year=query.year,
                    is_calendar=False,
                    url=FOMC_HISTORICAL_URL.format(year=query.year),
                ),
            )
        return (_PageRequest(year=query.year, is_calendar=True, url=FOMC_CALENDAR_URL),)
    last_historical_year = min(current_year, 2020)
    requests = [
        _PageRequest(
            year=year,
            is_calendar=False,
            url=FOMC_HISTORICAL_URL.format(year=year),
        )
        for year in range(1959, last_historical_year + 1)
    ]
    if current_year > 2020:
        requests.append(_PageRequest(year=current_year, is_calendar=True, url=FOMC_CALENDAR_URL))
    if len(requests) > MAX_FOMC_PAGES:
        raise FederalReserveSourceError("FomcDocuments: full-year request exceeds page budget")
    return tuple(requests)


class FomcDocumentsFetcher(_FederalReserveFetcher):
    """List official FOMC materials without downloading their attachments."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_fomc_documents",
        period="event",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes=(
            "Clean-room Federal Reserve meeting-page links; year=None traverses 1959 through "
            "current year, all pages must succeed, and attachments are never fetched."
        ),
    )
    canonical_model = "FomcDocuments"
    query_type = FomcDocumentsQuery

    def extract_data(self, params: FomcDocumentsQuery, ctx: FetchContext) -> object:
        """Fetch every source page required by the query before returning raw data."""
        pages: list[_FomcPage] = []
        for request in _page_requests(params):
            body = self._get(
                request.url,
                ctx,
                cache_context={
                    "page_year": request.year,
                    "calendar": request.is_calendar,
                },
            )
            pages.append(_FomcPage(year=request.year, is_calendar=request.is_calendar, body=body))
        return tuple(pages)

    def transform_data(
        self, raw: object, params: FomcDocumentsQuery
    ) -> list[FederalReserveFomcDocument]:
        """Normalize linked documents and reject incomplete whole-year responses."""
        if not isinstance(raw, tuple) or any(not isinstance(page, _FomcPage) for page in raw):
            raise FederalReserveSourceError("FomcDocuments: invalid fetched page collection")
        results: dict[tuple[date_type, str, str, str], FederalReserveFomcDocument] = {}
        for page in raw:
            documents = _parse_fomc_page(
                page.body,
                page_year=page.year,
                is_calendar=page.is_calendar,
                required_calendar_years=(
                    tuple(range(FIRST_CURRENT_CALENDAR_YEAR, _current_year() + 1))
                    if page.is_calendar and params.year is None
                    else ()
                ),
            )
            for document in documents:
                if params.year is not None and document.date.year != params.year:
                    continue
                if params.year is None and document.date.year > _current_year():
                    continue
                if params.document_type != "all" and document.doc_type != params.document_type:
                    continue
                key = (document.date, document.doc_type, document.doc_format, document.url)
                results.setdefault(key, document)
        return sorted(
            results.values(),
            key=lambda item: (item.date, item.doc_type, item.url),
        )


__all__ = [
    "FederalReserveSourceError",
    "FomcDocumentsFetcher",
    "FomcDocumentsQuery",
    "YieldCurveFetcher",
    "YieldCurveQuery",
]
