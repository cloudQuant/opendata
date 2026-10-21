"""WSJ ETF mover declarations.

The three published tables (leaders, laggards, most active) share one JSON envelope:
``data.instruments`` is a list of 25 rows keyed by the contract's column names. The endpoint
requires no credential; it answers only to a browser User-Agent (default clients receive
403/500). The endpoint and the mover spellings come from the OpenBB reference under
REFERENCE-EXCEPTION-01; the row fields are the ETFPerformance output contract.
"""

from __future__ import annotations

import json
from typing import Final

from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec
from opendata.data.providers._engine.value_transform import CastSpec, DerivePlan, DeriveStage

WSJ_BASE_URL: Final = "https://www.wsj.com"
WSJ_MOVERS_PATH: Final = "/market-data/mutualfunds-etfs/etfmovers"
WSJ_USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

COLUMNS: Final[tuple[ColumnSpec, ...]] = (
    ColumnSpec("symbol", "str", source_key="ticker", required=True, nullable=False),
    ColumnSpec("name", "str", source_key="name", required=True, nullable=False),
    ColumnSpec("last_price", "float", source_key="lastPrice", required=True, nullable=False),
    ColumnSpec(
        "percent_change", "float", source_key="percentChange", required=True, nullable=False
    ),
    ColumnSpec("net_change", "float", source_key="priceChange", required=True, nullable=False),
    ColumnSpec("volume", "float", source_key="volume", required=True, nullable=False),
    ColumnSpec("date", "date", source_key="timestamp", required=True, nullable=False),
    ColumnSpec("country", "str", source_key="country", required=True, nullable=False),
    ColumnSpec("mantissa", "int", source_key="mantissa", required=True, nullable=False),
    ColumnSpec("type", "str", source_key="type", required=True, nullable=False),
    ColumnSpec(
        "formatted_price", "str", source_key="formattedPrice", required=True, nullable=False
    ),
    ColumnSpec(
        "formatted_volume", "str", source_key="formattedVolume", required=True, nullable=False
    ),
    ColumnSpec(
        "formatted_price_change",
        "str",
        source_key="formattedPriceChange",
        required=True,
        nullable=False,
    ),
    ColumnSpec(
        "formatted_percent_change",
        "str",
        source_key="formattedPercentChange",
        required=True,
        nullable=False,
    ),
    ColumnSpec("url", "str", source_key="url", required=True, nullable=False),
)


def _mover_spec(model: str, mover: str, scenario: str, domain: str) -> ModelSpec:
    """Declare one WSJ mover table over the shared row shape."""
    mover_id = json.dumps(
        {"application": "WSJ", "etfMover": mover, "count": 25}, separators=(",", ":")
    )
    return ModelSpec(
        model=model,
        domain=domain,
        asset_class="etf",
        period="d",
        market="us",
        base_url=WSJ_BASE_URL,
        path=WSJ_MOVERS_PATH,
        rows_pointer="data.instruments",
        columns=COLUMNS,
        static_headers=(("User-Agent", WSJ_USER_AGENT),),
        static_query=(("id", mover_id), ("type", "mdc_etfmovers")),
        column_derives=(
            DerivePlan(
                column="date",
                stages=(
                    DeriveStage(
                        kind="cast",
                        cast=CastSpec(
                            kind="date_string_to_iso",
                            input_format="%Y-%m-%dT%H:%M:%S.%f%z",
                        ),
                    ),
                ),
                note=(
                    "WSJ publishes each row's timestamp as an ISO instant; the contract's "
                    "date column is that instant's calendar day."
                ),
            ),
        ),
        scenario=scenario,
        error_prefix="WSJ",
        notes=(
            "WSJ publishes the mover tables as unauthenticated JSON behind a browser "
            "User-Agent; the page's default client is refused. Rows are returned in the "
            "source's published order (25 per table)."
        ),
    )


ETF_GAINERS: Final = _mover_spec(
    "ETFGainers",
    "leaders",
    "WSJ ETF leaders：公开 JSON 表中的涨幅居前 ETF 行",
    "wsj_etf_gainers",
)
ETF_LOSERS: Final = _mover_spec(
    "ETFLosers",
    "laggards",
    "WSJ ETF laggards：公开 JSON 表中的跌幅居前 ETF 行",
    "wsj_etf_losers",
)
ETF_ACTIVE: Final = _mover_spec(
    "ETFActive",
    "most_active",
    "WSJ ETF most active：公开 JSON 表中的成交活跃 ETF 行",
    "wsj_etf_active",
)

DECLARED_MODELS: Final[tuple[str, ...]] = ("ETFGainers", "ETFLosers", "ETFActive")
NOT_DECLARED: Final[dict[str, str]] = {}
