"""停牌语义与复权基准的真机测量：口径映射表两个域级声明的取值来源.

口径映射表要声明每个源「停牌日长什么样」（``suspension``）和「价格以什么基准交
付」（``adjust``），这两个事实不能靠猜：有的源把停牌日整行不发
（``absent_row``），有的源照发一行而价格为 0（``zero_price_row``，fuyao ETF 腿
已实测到，见 ``tests/test_fuyao_endpoints.py`` 的 0 价行用例）。差别直接决定双
源校对的 ``missing_count`` 里有多少是停牌，也决定查询层的复权合成能不能对某个域
动手，所以声明之前必须先量。

列名一律取自口径映射表本身（``source_key`` 与字段的 ``from``），这张表因此不只
是声明：一个把 ods 列名写错的声明会在这条 SELECT 上当场失败。逐标的按主键取行
（ods 没有二级索引，只有全表一次的市场日分布例外）。停牌候选这样认定：**该腿自
己的市场日集合里、落在该标的自身 [first, last] 区间内、而该标的没有行的那些天**。
市场日由该腿 ``COUNT(*) group by trade_date`` 的分布推出（阈值 = 中位覆盖率的
50%），所以既不需要日历表，也不会把节假日读成停牌。

只读：全程 SELECT，不写任何表。用法（py313 环境，数据库可达）：

    python scripts/ops/suspension_shape_check.py --symbols 200
    python scripts/ops/suspension_shape_check.py --domain futures_daily --sources ths
"""

from __future__ import annotations

import argparse
import statistics
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy import Engine

    from opendata.data.mapping import DomainMapping

#: 默认量 A 股日线：它是唯一双源校对的价格域，口径不一致会让停牌日被读成分歧。
DEFAULT_DOMAIN = "stock_daily"
#: 一张表一条腿：源自己的行才是源语义的证据，合并后的 dwd 已把两腿抹平。
DEFAULT_LEGS = ("ths", "akshare")
DEFAULT_SYMBOLS = 200
#: 当日行数 >= 中位行数的这个比例，才算该腿眼里市场在开盘。
MARKET_DAY_RATIO = 0.5
#: 防住「只有一个标的在发行也算市场日」的假阳性下限。
MIN_MARKET_DAY_ROWS = 20
#: 打印多少个缺行样本。
EXAMPLE_LIMIT = 5
EXCHANGES = (".SH", ".SZ", ".BJ")
#: 源在自己的行里声明复权基准的那个列（ths 的 dump 有，akshare 没有）。
BASIS_COLUMN = "adjusted"


def _fetch(engine: Engine, sql: str, **params: object) -> list[dict[str, Any]]:
    """Run a read-only SELECT and return dict rows.

    Args:
        engine: Warehouse engine.
        sql: Statement text (module literal; values are bound).
        **params: Bound parameters.

    Returns:
        One dict per row, keyed by column name.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        cursor = conn.execute(text(sql), params)
        columns = list(cursor.keys())
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _mapping(source: str, domain: str) -> DomainMapping:
    """Return one leg's domain mapping (it names the ods columns).

    Args:
        source: Leg identifier.
        domain: Registered domain identifier.

    Returns:
        The domain mapping.
    """
    from opendata.data.mapping import require_domain_mapping

    return require_domain_mapping(source, domain)


def ods_name(source: str, domain: str) -> str:
    """The ods table one leg writes, named by the registry not by hand.

    Args:
        source: Leg identifier.
        domain: Registered domain identifier.

    Returns:
        The table name.
    """
    from opendata.data.domains import ods_table

    return ods_table(domain, source)


def sample_symbols(engine: Engine, source: str, domain: str, limit: int) -> list[str]:
    """Pick symbols off one leg's primary key (ordered, stops at ``limit``).

    Args:
        engine: Warehouse engine.
        source: Leg identifier.
        domain: Registered domain identifier.
        limit: How many symbols to return.

    Returns:
        Contract-shaped symbols - the mapping's own key normalizer strips
        the exchange suffix where it declares one, exactly as the
        pipeline does.
    """
    mapping = _mapping(source, domain)
    key_symbol = mapping.source_key[0]
    rows = _fetch(
        engine,
        f"SELECT DISTINCT `{key_symbol}` AS s FROM `{ods_name(source, domain)}` "  # noqa: S608
        f"ORDER BY `{key_symbol}` LIMIT :limit",
        limit=limit,
    )
    return [str(mapping.to_contract_key((row["s"],))[0]) for row in rows]


def spellings(symbol: str) -> list[str]:
    """Every exchange-suffixed spelling one bare code can arrive under.

    The ods tables keep the source's own value (design §8.1), so a query
    has to ask for the suffixed forms and let the mapping's
    ``normalize: plain`` turn them back into contract symbols.

    Args:
        symbol: Bare contract symbol.

    Returns:
        The bare code plus each suffixed variant.
    """
    return [symbol, *[f"{symbol}{suffix}" for suffix in EXCHANGES]]


def leg_rows(engine: Engine, source: str, domain: str, symbol: str) -> list[dict[str, Any]]:
    """Read one leg's rows for one symbol, naming columns from the mapping.

    Args:
        engine: Warehouse engine.
        source: Leg identifier.
        domain: Registered domain identifier.
        symbol: Contract-shaped symbol.

    Returns:
        Rows keyed by contract names (``trade_date``, ``close``,
        ``volume``, ``amount``).
    """
    mapping = _mapping(source, domain)
    key_symbol, key_date = mapping.source_key
    fields = mapping.fields
    measured = ("close", "volume", "amount")
    selected = ", ".join(f"`{fields[name].source_column}` AS `{name}`" for name in measured)
    bindings: dict[str, object] = {
        f"k{index}": value for index, value in enumerate(spellings(symbol))
    }
    placeholders = ", ".join(f":k{index}" for index in range(len(bindings)))
    sql = (
        f"SELECT `{key_date}` AS trade_date, {selected} "  # noqa: S608
        f"FROM `{ods_name(source, domain)}` WHERE `{key_symbol}` IN ({placeholders})"
    )
    return _fetch(engine, sql, **bindings)


def market_days(engine: Engine, source: str, domain: str) -> set[date]:
    """Derive this leg's own trading-day set from its row distribution.

    A day the source published bars for was a trading day as far as this
    source is concerned. No calendar table is involved, so a holiday can
    never be read as a suspension.

    Args:
        engine: Warehouse engine.
        source: Leg identifier.
        domain: Registered domain identifier.

    Returns:
        The trading days this leg observes.

    Raises:
        RuntimeError: If the leg holds no rows at all.
    """
    key_date = _mapping(source, domain).source_key[1]
    rows = _fetch(
        engine,
        f"SELECT `{key_date}` AS d, COUNT(*) AS n FROM `{ods_name(source, domain)}` "  # noqa: S608
        f"GROUP BY `{key_date}`",
    )
    if not rows:
        raise RuntimeError(f"{ods_name(source, domain)} holds no rows to derive trading days from")
    counts = [int(row["n"]) for row in rows]
    threshold = max(MIN_MARKET_DAY_ROWS, statistics.median(counts) * MARKET_DAY_RATIO)
    return {row["d"] for row in rows if int(row["n"]) >= threshold}


def declared_basis(engine: Engine, source: str, domain: str) -> list[str]:
    """Read the adjust basis this source states in its own rows.

    Args:
        engine: Warehouse engine.
        source: Leg identifier.
        domain: Registered domain identifier.

    Returns:
        The distinct non-null values of the basis column, or
        ``["<no column>"]`` when the leg stores none.
    """
    from sqlalchemy import inspect

    table = ods_name(source, domain)
    columns = {str(column["name"]) for column in inspect(engine).get_columns(table)}
    if BASIS_COLUMN not in columns:
        return ["<no column>"]
    rows = _fetch(
        engine,
        f"SELECT DISTINCT `{BASIS_COLUMN}` AS v FROM `{table}` LIMIT 20",  # noqa: S608
    )
    return sorted({str(row["v"]) for row in rows if row["v"] is not None})


def shape_of(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Reduce one symbol's rows to the counts the verdict needs.

    Args:
        rows: Rows read from the leg.

    Returns:
        ``rows``, ``zero_close`` (price not positive) and ``no_trade``
        (volume and amount both zero while the price is positive).
    """
    zero_close = sum(1 for row in rows if float(row["close"] or 0) <= 0.0)
    no_trade = sum(
        1
        for row in rows
        if float(row["close"] or 0) > 0.0
        and float(row["volume"] or 0) == 0.0
        and float(row["amount"] or 0) == 0.0
    )
    return {"rows": len(rows), "zero_close": zero_close, "no_trade": no_trade}


def measure_leg(
    engine: Engine, source: str, domain: str, symbols: list[str], market: set[date]
) -> dict[str, Any]:
    """Measure one leg: 0-price rows and trading days it omits.

    Args:
        engine: Warehouse engine.
        source: Leg identifier.
        domain: Registered domain identifier.
        symbols: Symbols sampled from this leg.
        market: This leg's own trading-day set.

    Returns:
        An aggregate reading (counts plus a few omission examples).
    """
    totals: Counter[str] = Counter()
    examples: list[str] = []
    for symbol in symbols:
        rows = leg_rows(engine, source, domain, symbol)
        for key, value in shape_of(rows).items():
            totals[key] += value
        days = {row["trade_date"] for row in rows}
        if len(days) < 2:
            totals["symbols_tiny_range"] += 1
            continue
        low, high = min(days), max(days)
        holes = sorted(day for day in market if low <= day <= high and day not in days)
        if holes:
            totals["symbols_with_omission"] += 1
            totals["omitted_days"] += len(holes)
            if len(examples) < EXAMPLE_LIMIT:
                examples.append(f"{symbol} {low}..{high} missing={len(holes)} first={holes[0]}")
    return {**dict(totals), "examples": examples}


def verdict(reading: dict[str, Any]) -> str:
    """Name the suspension convention this leg's shape supports.

    Args:
        reading: Output of :func:`measure_leg`.

    Returns:
        ``zero_price_row``, ``absent_row`` or ``undetermined``.
    """
    if not reading.get("rows"):
        return "undetermined"
    if reading.get("zero_close"):
        return "zero_price_row"
    if reading.get("omitted_days"):
        return "absent_row"
    return "undetermined"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, measure each leg and print the reading.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every leg got a decidable verdict, else ``1``.
    """
    from sqlalchemy import create_engine

    from opendata.core.config import get_settings

    parser = argparse.ArgumentParser(
        description="measure suspension shape and adjust basis from ods"
    )
    parser.add_argument(
        "--symbols", type=int, default=DEFAULT_SYMBOLS, help="symbols sampled per leg"
    )
    parser.add_argument(
        "--domain", default=DEFAULT_DOMAIN, help="registered domain to measure (stock_daily)"
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=list(DEFAULT_LEGS),
        help="legs to measure, one reading per leg (ths akshare)",
    )
    args = parser.parse_args(argv)

    engine = create_engine(get_settings().data_database_url)
    decided = True
    for source in args.sources:
        symbols = sample_symbols(engine, source, args.domain, args.symbols)
        market = market_days(engine, source, args.domain)
        reading = measure_leg(engine, source, args.domain, symbols, market)
        label = verdict(reading)
        decided = decided and label != "undetermined"
        print(
            f"{source} [{ods_name(source, args.domain)}] domain={args.domain} "
            f"symbols={len(symbols)} market_days={len(market)} "
            f"rows={reading.get('rows', 0)} zero_close={reading.get('zero_close', 0)} "
            f"no_trade={reading.get('no_trade', 0)} omitted_days={reading.get('omitted_days', 0)} "
            f"symbols_with_omission={reading.get('symbols_with_omission', 0)} "
            f"symbols_tiny_range={reading.get('symbols_tiny_range', 0)} "
            f"basis={declared_basis(engine, source, args.domain)} => suspension={label}",
            flush=True,
        )
        for example in reading.get("examples", []):
            print(f"    omission: {example}", flush=True)
    return 0 if decided else 1


if __name__ == "__main__":
    raise SystemExit(main())
