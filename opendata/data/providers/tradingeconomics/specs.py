"""The Trading Economics EconomicCalendar wire and output declaration.

Wire facts come from Trading Economics' official Economic Calendar REST docs:
``/economic_calendar/country/``, ``/economic_calendar/group/``,
``/economic_calendar/snapshot/`` and ``/economic_calendar/schema/``. The request router in the
model module selects only documented endpoint forms. OpenBB's EconomicCalendar documentation is
used solely to define the output field names and accepted query vocabulary.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec

TRADINGECONOMICS_BASE_URL: Final = "https://api.tradingeconomics.com"
TRADINGECONOMICS_CREDENTIAL: Final = "TRADINGECONOMICS_API_KEY"
TRADINGECONOMICS_MAX_URL_CHARS: Final = 260
TRADINGECONOMICS_CALENDAR_MAX_ROWS: Final = 1000

GROUP_VALUES: Final[tuple[tuple[str, str], ...]] = (
    ("interest_rate", "interest rates"),
    ("inflation", "inflation"),
    ("bonds", "bonds"),
    ("consumer", "consumer"),
    ("gdp", "gdp"),
    ("government", "government"),
    ("housing", "housing"),
    ("labour", "labor"),
    ("markets", "markets"),
    ("money", "money"),
    ("prices", "prices"),
    ("trade", "trade"),
    ("business", "business"),
)

IMPORTANCE_VALUES: Final[tuple[tuple[str, str], ...]] = (
    ("low", "1"),
    ("medium", "2"),
    ("high", "3"),
)

ECONOMIC_CALENDAR = ModelSpec(
    model="EconomicCalendar",
    domain="economic_calendar",
    asset_class="macro",
    period="d",
    market="all",
    base_url=TRADINGECONOMICS_BASE_URL,
    path="/calendar",
    rows_pointer="",
    params=(
        ParamSpec(
            "start_date",
            "date",
            note=(
                "First date in a two-ended calendar window; the official route requires both dates."
            ),
        ),
        ParamSpec(
            "end_date",
            "date",
            note=(
                "Last date in a two-ended calendar window; the official route requires both dates."
            ),
        ),
        ParamSpec(
            "country",
            "str",
            note=(
                "Country name(s), including comma-separated lists, moved into the documented "
                "country route."
            ),
        ),
        ParamSpec(
            "importance",
            "enum",
            enum=("low", "medium", "high"),
            value_map=IMPORTANCE_VALUES,
            note="TE's country endpoint maps importance low/medium/high to 1/2/3.",
        ),
        ParamSpec(
            "group",
            "enum",
            enum=tuple(name for name, _ in GROUP_VALUES),
            value_map=GROUP_VALUES,
            note="OpenBB's underscore spelling is mapped to the official group path spelling.",
        ),
        ParamSpec(
            "calendar_id",
            "str",
            note="One numeric ID or a comma-separated list, placed in /calendar/calendarid/{ids}.",
        ),
    ),
    columns=(
        ColumnSpec("date", "str", source_key="Date", required=True, nullable=False),
        ColumnSpec("country", "str", source_key="Country"),
        ColumnSpec("category", "str", source_key="Category"),
        ColumnSpec("event", "str", source_key="Event"),
        ColumnSpec("importance", "str", source_key="Importance"),
        ColumnSpec("source", "str", source_key="Source"),
        ColumnSpec("currency", "str", source_key="Currency"),
        ColumnSpec("unit", "str", source_key="Unit"),
        ColumnSpec("consensus", "str", source_key="Forecast"),
        ColumnSpec("previous", "str", source_key="Previous"),
        ColumnSpec("revised", "str", source_key="Revised"),
        ColumnSpec("actual", "str", source_key="Actual"),
        ColumnSpec("forecast", "str", source_key="TEForecast"),
        ColumnSpec("reference", "str", source_key="Reference"),
        ColumnSpec("reference_date", "date", source_key="ReferenceDate"),
        ColumnSpec("calendar_id", "int", source_key="CalendarId", required=True, nullable=False),
        ColumnSpec("date_span", "int", source_key="DateSpan"),
        ColumnSpec("symbol", "str", source_key="Symbol"),
        ColumnSpec("ticker", "str", source_key="Ticker"),
        ColumnSpec("te_url", "str", source_key="URL"),
        ColumnSpec("source_url", "str", source_key="SourceURL"),
        ColumnSpec("last_updated", "str", source_key="LastUpdate"),
    ),
    static_query=(("f", "json"),),
    credential=TRADINGECONOMICS_CREDENTIAL,
    credential_query_key="c",
    scenario="Trading Economics 经济日历：按文档支持的日历过滤器返回事件行",
    error_prefix="TRADINGECONOMICS",
    notes=(
        "Official API docs: docs.tradingeconomics.com/economic_calendar/country/ (country, date, "
        "importance, country+group and country+group+date routes); /group/ (group and group+date); "
        "/snapshot/ (calendar snapshot and calendarid route); /schema/ (source field names and "
        "types). The response is a bare JSON list. OpenBB EconomicCalendar model docs define the "
        "row field names and input enum. The API adapter converts TE's numeric importance to the "
        "documented low/medium/high labels, numeric strings to integer IDs/date_span, and ISO "
        "reference dates to date values. Date is UTC per the source schema; DateSpan is limited to "
        "0 (known timing) and 1 (estimated timing). The official calendar ceiling is 1,000 rows "
        "and 260 URL characters; oversized calendar-ID lists are split into bounded requests. "
        "Unsupported filter combinations are refused pre-I/O."
    ),
)

DECLARED_MODELS: Final[tuple[str, ...]] = (ECONOMIC_CALENDAR.model,)
