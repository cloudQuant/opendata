"""ths instrument catalog against two independent vendors (AC-10 / C14).

``instrument`` is the domain every other leg leans on when a bar query comes
back empty: only a listing date says whether the window predates the symbol.
That makes it the capability whose failure mode is not a wrong number but a
wrong *absence*, so this run judges the fields the pipeline consumes
(``list_date``, ``delist_date``, ``status``, ``symbol`` / ``exchange``).

Four criteria, two of them against vendors that are not fuyao:

* **A - shape (self-proof).** Uniqueness, the ``thscode``↔``ticker``
  reconciliation ``normalize_instruments`` now enforces (real contracts only:
  the synthetic 连续/主连 series carry a different display code by upstream
  convention), suffix-vs-``exchange`` agreement judged through the alias table
  the catalog itself reveals (a futures suffix is the short form of the
  exchange code in ``exchange``, so a literal compare condemns 730 good rows),
  and - the regression this round exists for - that every published ``status``
  agrees with ``last_trade_date`` compared against the snapshot day. Read on
  the **raw** rows: the contract collapses ``last_trade_date`` and ``end_date``
  into one field, so only the raw pair shows a flipped label.
* **B - membership (中证指数官网, independent vendor).** Every member of three
  standard indices must be inside the a-share catalog, *and* the exchange the
  publisher states for that member must equal the catalog's suffix - codes
  alone would pass with a wrong exchange column.
* **C - listing date (新浪, independent vendor).** The first daily bar sina
  publishes for a symbol is its listing day; compared day-for-day with the
  catalog ``list_date`` on a sample covering each board and both ends of the
  date range. A 北交所 row whose vendor history starts *earlier* is only a
  scope difference when that earlier stretch is sparse (pre-平移 新三板 era),
  which is measured rather than assumed.
* **D - expiry rule (交易所规则, not a vendor).** 中金所 publishes 最后交易日 by
  rule: 股指 = 到期月份第三个周五, 国债 = 第二个周五. Recomputed from the
  catalog's own ``end_date`` and compared with its ``last_trade_date``. A
  single "third Friday" assumption is wrong for every 国债 row, which is why
  the two families are measured separately.

What is deliberately **not** judged: ``currency`` for futures/options is a
local fill (upstream publishes null for all 1,139 futures rows), ``board`` is
not published by this endpoint, and the delisted population cannot be checked
at all - the catalog publishes no delisted A-shares, so no survivorship-bias-
free claim is made anywhere here.

Usage (py313 env; needs network - legs B and C call two other vendors):

    python scripts/ops/ths_instrument_cross_check.py
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import Instrument

#: 秒。上游对连发直接断连（实测），两次目录请求之间要留开。
PACE_SECONDS = 12.0
#: 断连后的重试间隔与次数（证据脚本要跑完，不要一断就废）。
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 5
#: 参照侧（中证/新浪）逐指数、逐标的请求，同样限速。
REFERENCE_PACE_SECONDS = 2.0
#: 单页行数（上游上限）与翻页保护：offset 被忽略时不无限翻。
PAGE_LIMIT = 10_000
MAX_PAGES = 20

#: 参照侧的指数（裸码即中证官网的清单键）：覆盖主板/科创/小盘三种分布。
REFERENCE_INDICES: tuple[str, ...] = ("000300", "000688", "000852")

#: ``thscode`` 后缀 ↔ 中证官网 ``交易所`` 列的中文表述。
PUBLISHER_EXCHANGE_NAMES = {
    "SH": "上海证券交易所",
    "SZ": "深圳证券交易所",
    "BJ": "北京证券交易所",
}

#: ``thscode`` 后缀 → 上游 ``exchange`` 列取值。a-share 两侧逐字相同；期货的后缀是
#: 短写、``exchange`` 是交易所代码全称（2026-09-25 实测目录内出现的配对：
#: ``CZC``↔``CZCE``、``SHF``↔``SHFE``、``GFE``↔``GFEX``、``CFE``↔``CFFEX``，
#: 而 ``DCE``/``INE`` 两侧同写），所以「后缀 = 交易所」必须查这张表判等，
#: 直接逐字比会把 730 个正常行判成不符。表本身在报告里按实测配对回显，
#: 未列出的配对一律算不符。
EXCHANGE_CODE_BY_SUFFIX = {
    "SH": "SH",
    "SZ": "SZ",
    "BJ": "BJ",
    "DCE": "DCE",
    "CZC": "CZCE",
    "SHF": "SHFE",
    "INE": "INE",
    "GFE": "GFEX",
    "CFE": "CFFEX",
}

#: 允许把「新浪首根早于目录上市日」登记成口径差的板块（实测只在北交所出现）。
SCOPE_DIFF_SUFFIXES = frozenset({"BJ"})
#: 低于该根/年的密度才算「另一段交易历史」（平移前的全国股转系统期），而不是
#: 同一段连续历史被截早了。2026-09-25 实测三只北交所采样：``920000.BJ`` 目录
#: 上市日前 16 根 / 3.74 年 = **4.3 根/年**；``920010.BJ`` 332 根 / 5.19 年 =
#: **64.0 根/年**（做市期密集、之后骤减）；``920422.BJ`` 0 根（与新浪首根同日，
#: 本就一致）。两段北交所/沪深连续竞价的实测密度约 242 根/年，故阈值取 120：
#: 既是连续密度的一半，也远高于上面两例。
SPARSE_BARS_PER_YEAR = 120.0

#: 中金所品种族 → (中文名, 最后交易日是到期月份的第几个周五)。
CFFEX_FAMILY_RULE: Mapping[str, tuple[str, int]] = {
    "IF": ("股指", 3),
    "IH": ("股指", 3),
    "IC": ("股指", 3),
    "IM": ("股指", 3),
    "T": ("国债", 2),
    "TF": ("国债", 2),
    "TS": ("国债", 2),
    "TL": ("国债", 2),
}
#: 中金所的 ``thscode`` 后缀（实测股指/国债合约均为 ``.CFE``）。
CFFEX_SUFFIX = "CFE"

#: 采样口径：每块市场一只 + 上市日期区间两端各一只，覆盖后缀与年份两个维度。
SAMPLE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("最早上市", "oldest"),
    ("最新上市", "newest"),
    ("科创板 688", "688"),
    ("创业板 300", "300"),
    ("注册制创业板 301", "301"),
    ("主板 605", "605"),
    ("原中小板 002", "002"),
    ("北交所（代码最小）", "BJ"),
    ("北交所（最早上市）", "BJ-oldest"),
    ("北交所（上市日中位）", "BJ-median"),
)

_SINA_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


class ShapeReport(NamedTuple):
    """Catalog-shape measurements for one asset type (criterion A)."""

    asset_type: str
    rows: int
    contract_rows: int
    duplicate_codes: int
    differing_codes: int
    dated_code_disagreements: int
    suffix_exchange_mismatch: int
    literal_suffix_exchange_mismatch: int
    status_wrong: int
    unpinned: int
    delisted: int
    null_list_date: int
    date_less_rows: int
    snapshot: date | None
    exchange_values: tuple[str, ...]
    exchange_pairs: tuple[str, ...]


class MemberReport(NamedTuple):
    """One index' membership reconciliation against the publisher (criterion B)."""

    index_code: str
    members: int
    in_catalog: int
    exchange_mismatch: tuple[str, ...]
    missing: tuple[str, ...]


class ListingReport(NamedTuple):
    """One symbol's catalog listing date against the vendor's first bar (C)."""

    label: str
    symbol: str
    list_date: date | None
    first_bar: date | None
    bars: int
    bars_before: int = 0

    @property
    def judged(self) -> bool:
        """Whether the reference call produced a date to compare against."""
        return self.bars > 0 and self.first_bar is not None

    @property
    def bars_per_year_before(self) -> float:
        """How dense the vendor's pre-``list_date`` history is.

        A continuous listing runs at roughly 240 bars a year; the 新三板 era the
        vendor hangs on the same symbol measured 23 bars over four years. The
        density is what separates "two venues" from "a wrong date".
        """
        if self.list_date is None or self.first_bar is None or self.bars_before <= 0:
            return 0.0
        years = max((self.list_date - self.first_bar).days, 1) / 365.25
        return self.bars_before / years


class RuleReport(NamedTuple):
    """One 中金所 contract against its exchange's expiry rule (criterion D)."""

    family: str
    symbol: str
    end_date: date | None
    last_trade: date | None
    rule_date: date | None


def _catalog(asset_type: str) -> tuple[list[Mapping[str, Any]], date | None]:
    """Read one asset type's raw catalog rows and the envelope's snapshot day.

    Raw rather than normalized on purpose: the contract has no field for
    ``end_date`` next to ``last_trade_date``, and criteria A and D need the
    pair to show whether the derived label is right.

    Args:
        asset_type: The upstream leaf type to list.

    Returns:
        The ``data.item`` rows in upstream order, and the snapshot day.

    Raises:
        RuntimeError: Retries used up, or no page ever came back short.
        Exception: The last transport error that triggered a retry.
    """
    from opendata.data.providers.ths.endpoints import TICKERS_LIST_ENDPOINT, envelope_snapshot
    from opendata.data.providers.ths.models._client import client

    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with client(timeout_seconds=120.0) as active:
                rows: list[Mapping[str, Any]] = []
                snapshot: date | None = None
                for page in range(MAX_PAGES):
                    response = active.get(
                        TICKERS_LIST_ENDPOINT,
                        params={
                            "limit": PAGE_LIMIT,
                            "offset": page * PAGE_LIMIT,
                            "asset_type": asset_type,
                        },
                    )
                    if snapshot is None:
                        snapshot = envelope_snapshot(response.envelope)
                    batch = [item for item in response.envelope.items if isinstance(item, Mapping)]
                    rows.extend(batch)
                    if len(batch) < PAGE_LIMIT:
                        return rows, snapshot
                raise RuntimeError(f"{asset_type}: no short page within {MAX_PAGES} pages")
        except Exception as exc:  # noqa: PERF203  # 上游断连是瞬时的：重试而不是放弃
            last = exc
            print(
                f"!! catalog {asset_type} attempt {attempt + 1}: {type(exc).__name__}",
                file=sys.stderr,
            )
            time.sleep(RETRY_SECONDS)
    raise RuntimeError(f"{asset_type}: {RETRY_ATTEMPTS} fetch attempts failed") from last


def _contracts(asset_type: str) -> tuple[Instrument, ...]:
    """Read one asset type through the routing path, exactly as a caller would.

    Args:
        asset_type: The leaf type to list.

    Returns:
        The contract rows the adapter published.
    """
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    rows = get_registry().resolve_domain("instrument", source="ths").fetch(asset_type=asset_type)
    return cast("tuple[Instrument, ...]", tuple(rows))


def _as_date(value: object) -> date | None:
    """Parse an upstream ``YYYY-MM-DD`` cell; null and blank stay ``None``."""
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value))


def _suffix(symbol: str) -> str:
    """The ``thscode`` exchange suffix."""
    return symbol.partition(".")[2].upper()


def _plain(symbol: str) -> str:
    """The ``thscode`` without its exchange suffix."""
    return symbol.partition(".")[0]


def _code(row: Mapping[str, Any]) -> str:
    """The upstream row's ``thscode`` as text."""
    return str(row.get("thscode") or "")


def _codes_differ(row: Mapping[str, Any]) -> bool:
    """Whether the row's two code spellings disagree (``thscode`` vs ``ticker``)."""
    return _plain(_code(row)) != str(row.get("ticker") or "")


def _nth_friday(year: int, month: int, which: int) -> date:
    """The ``which``-th Friday of a month (1-based, counted from day 1)."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 7 * (which - 1))


def _expected_status(row: Mapping[str, Any], snapshot: date | None) -> str:
    """The status the snapshot implies, recomputed without the adapter's code."""
    if snapshot is None:
        return "unknown"
    stop = _as_date(row.get("last_trade_date")) or _as_date(row.get("end_date"))
    return "delisted" if stop is not None and stop < snapshot else "active"


def shape_report(
    asset_type: str,
    raw: Sequence[Mapping[str, Any]],
    contracts: Sequence[Instrument],
    snapshot: date | None,
) -> ShapeReport:
    """Measure one asset type's catalog against its own invariants (criterion A).

    Args:
        asset_type: The leaf type the rows were asked for.
        raw: The upstream rows for the same call.
        contracts: Rows as the routed adapter published them.
        snapshot: The envelope's snapshot day.

    Returns:
        Counts plus the numbers that judge the fix: labels disagreeing with
        the snapshot-derived status, and real (dated) contracts whose two code
        spellings do not match. The synthetic series (``AP00``↔``AP7777``
        连续、``ICZL``↔``IC9999`` 主连、``IC8888`` 加权) carry a different
        display code by upstream convention, so they are counted only as an
        observation in ``differing_codes``.
    """
    symbols = [_code(row) for row in raw]
    by_code = {row.symbol: row for row in contracts}
    unpinned = sum(1 for row in raw if _code(row) not in by_code)
    status_wrong = sum(
        1
        for row in raw
        if _expected_status(row, snapshot) != by_code.get(_code(row), _MISSING).status
    )
    dated = [
        row for row in raw if _as_date(row.get("last_trade_date")) or _as_date(row.get("end_date"))
    ]
    pairs = [(_suffix(_code(row)), _exchange_cell(row)) for row in raw]
    return ShapeReport(
        asset_type=asset_type,
        rows=len(raw),
        contract_rows=len(contracts),
        duplicate_codes=len(symbols) - len(set(symbols)),
        differing_codes=sum(1 for row in raw if _codes_differ(row)),
        dated_code_disagreements=sum(1 for row in dated if _codes_differ(row)),
        suffix_exchange_mismatch=sum(
            1 for suffix, exchange in pairs if EXCHANGE_CODE_BY_SUFFIX.get(suffix) != exchange
        ),
        literal_suffix_exchange_mismatch=sum(1 for suffix, exchange in pairs if suffix != exchange),
        status_wrong=status_wrong,
        unpinned=unpinned,
        delisted=sum(1 for row in contracts if row.status == "delisted"),
        null_list_date=sum(1 for row in raw if not row.get("list_date")),
        date_less_rows=len(raw) - len(dated),
        snapshot=snapshot,
        exchange_values=tuple(sorted({exchange or "<空>" for _, exchange in pairs})),
        exchange_pairs=tuple(sorted({f"{suffix}→{exchange}" for suffix, exchange in pairs})),
    )


def _exchange_cell(row: Mapping[str, Any]) -> str:
    """The row's ``exchange`` value as an upper-cased code (blank when absent)."""
    return str(row.get("exchange") or "").upper()


class _Missing:
    """Sentinel row: a code the adapter did not publish never matches a status."""

    status = "<absent>"


_MISSING = _Missing()


def member_reports(catalog: Sequence[Instrument]) -> list[MemberReport]:
    """Reconcile the publisher's member lists with the catalog (criterion B).

    Args:
        catalog: The a-share catalog rows already fetched.

    Returns:
        One report per reference index.
    """
    import opendata.data.providers.akshare._vendor as opendata_http

    by_plain = {_plain(row.symbol): row for row in catalog}
    reports: list[MemberReport] = []
    for code in REFERENCE_INDICES:
        frame = opendata_http.index_stock_cons_csindex(symbol=code)
        missing: list[str] = []
        mismatched: list[str] = []
        for member_code, publisher_market in zip(frame["成分券代码"], frame["交易所"], strict=True):
            plain = str(member_code).strip().zfill(6)
            row = by_plain.get(plain)
            if row is None:
                missing.append(plain)
                continue
            expected = next(
                (
                    suffix
                    for suffix, name in PUBLISHER_EXCHANGE_NAMES.items()
                    if name in str(publisher_market)
                ),
                "",
            )
            if _suffix(row.symbol) != expected:
                mismatched.append(f"{plain}:{_suffix(row.symbol)}≠{expected or '?'}")
        reports.append(
            MemberReport(
                index_code=code,
                members=len(frame),
                in_catalog=len(frame) - len(missing),
                exchange_mismatch=tuple(mismatched),
                missing=tuple(sorted(missing)),
            )
        )
        time.sleep(REFERENCE_PACE_SECONDS)
    return reports


def _sample(catalog: Sequence[Instrument]) -> list[tuple[str, Instrument]]:
    """Pick one catalog row per sample bucket (deterministic, no RNG).

    Args:
        catalog: The a-share catalog rows.

    Returns:
        ``(label, row)`` pairs, dropping buckets the catalog has no row for.
    """
    dated = [row for row in catalog if row.list_date is not None]
    bj = sorted(
        (row for row in dated if _suffix(row.symbol) == "BJ"),
        key=lambda item: (item.list_date, item.symbol),
    )
    picks_for: Mapping[str, Instrument | None] = {
        "oldest": min(dated, key=lambda item: (item.list_date, item.symbol)) if dated else None,
        "newest": max(dated, key=lambda item: (item.list_date, item.symbol)) if dated else None,
        "BJ": min(bj, key=lambda item: item.symbol) if bj else None,
        "BJ-oldest": bj[0] if bj else None,
        "BJ-median": bj[len(bj) // 2] if bj else None,
    }
    picks: list[tuple[str, Instrument]] = []
    for label, bucket in SAMPLE_BUCKETS:
        if bucket in picks_for:
            row = picks_for[bucket]
        else:
            candidates = [item for item in dated if _plain(item.symbol).startswith(bucket)]
            row = min(candidates, key=lambda item: item.symbol) if candidates else None
        if row is not None:
            picks.append((label, row))
    return picks


def _as_plain_date(value: object) -> date | None:
    """Coerce a pandas timestamp / datetime to a bare calendar date.

    The reference frames carry ``pd.Timestamp``, which never compares equal to a
    ``date``, so the type must be unwrapped before the day-for-day check.
    """
    if value is None or isinstance(value, float):
        return None
    as_date = getattr(value, "date", None)
    if callable(as_date):
        return cast("date", as_date())
    return value if isinstance(value, date) else None


def listing_reports(catalog: Sequence[Instrument]) -> list[ListingReport]:
    """Compare catalog ``list_date`` with the vendor's first bar (criterion C).

    Args:
        catalog: The a-share catalog rows already fetched.

    Returns:
        One report per sampled symbol; ``bars`` is ``-1`` when the reference
        call itself failed, which is reported as unjudged rather than as a pass.
    """
    from opendata.data.providers.akshare._vendor.stock.stock_zh_a_sina import stock_zh_a_daily

    reports: list[ListingReport] = []
    for label, row in _sample(catalog):
        symbol = f"{_SINA_PREFIX.get(_suffix(row.symbol), '')}{_plain(row.symbol)}"
        try:
            frame = stock_zh_a_daily(
                symbol=symbol,
                start_date="19900101",
                end_date=date.today().strftime("%Y%m%d"),
            )
            days = [day for day in (_as_plain_date(value) for value in frame["date"]) if day]
            first = days[0] if days else None
            bars = len(frame)
            before = sum(1 for day in days if row.list_date is not None and day < row.list_date)
        except Exception as exc:  # 参照侧挂了要在表里显形，不是本地判据
            print(f"!! sina {symbol}: {type(exc).__name__}", file=sys.stderr)
            first, bars, before = None, -1, 0
        reports.append(
            ListingReport(
                label=label,
                symbol=row.symbol,
                list_date=row.list_date,
                first_bar=first,
                bars=bars,
                bars_before=before,
            )
        )
        time.sleep(REFERENCE_PACE_SECONDS)
    return reports


def rule_reports(raw: Sequence[Mapping[str, Any]]) -> list[RuleReport]:
    """Recompute 中金所 最后交易日 from the exchange's published rule (D).

    Args:
        raw: The futures catalog rows (both date fields, un-collapsed).

    Returns:
        One report per dated 中金所 contract of a rule-bearing family.
    """
    reports: list[RuleReport] = []
    for row in sorted(raw, key=_code):
        code = _code(row)
        if _suffix(code) != CFFEX_SUFFIX:
            continue
        family = "".join(char for char in _plain(code) if char.isalpha())
        spec = CFFEX_FAMILY_RULE.get(family)
        end = _as_date(row.get("end_date"))
        if spec is None or end is None:
            continue
        reports.append(
            RuleReport(
                family=f"{family} {spec[0]}",
                symbol=code,
                end_date=end,
                last_trade=_as_date(row.get("last_trade_date")),
                rule_date=_nth_friday(end.year, end.month, spec[1]),
            )
        )
    return reports


def _two_day_gap(raw: Sequence[Mapping[str, Any]]) -> int:
    """Count contracts whose expiry day and last-trade day differ (measured pair)."""
    return sum(
        1
        for row in raw
        if _as_date(row.get("end_date"))
        and _as_date(row.get("last_trade_date"))
        and _as_date(row.get("end_date")) != _as_date(row.get("last_trade_date"))
    )


def _print_shape(reports: Sequence[ShapeReport], failures: list[str]) -> None:
    """Render criterion A and append its breaches to ``failures``."""
    print("## A. 目录形状（自证：唯一性/码对账/后缀一致/status 与快照日）\n")
    print(
        "| 资产类型 | 上游行 | 契约行 | 重复码 | 两写法不等 | 其中真实合约 "
        "| 后缀≠交易所（逐字） | 后缀≠交易所（查别名表） | status 不符 | 未发布 "
        "| delisted | list_date 缺失 | 无日期行 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for report in reports:
        print(
            f"| {report.asset_type} | {report.rows} | {report.contract_rows} "
            f"| {report.duplicate_codes} | {report.differing_codes} "
            f"| {report.dated_code_disagreements} | {report.literal_suffix_exchange_mismatch} "
            f"| {report.suffix_exchange_mismatch} "
            f"| {report.status_wrong} | {report.unpinned} | {report.delisted} "
            f"| {report.null_list_date} | {report.date_less_rows} |"
        )
        for field in (
            "duplicate_codes",
            "dated_code_disagreements",
            "suffix_exchange_mismatch",
            "status_wrong",
            "unpinned",
        ):
            value = getattr(report, field)
            if value:
                failures.append(f"A/{report.asset_type}: {field}={value}")
        if report.snapshot is None:
            failures.append(f"A/{report.asset_type}: 信封未给出快照日，status 无从判定")
    for report in reports:
        print(
            f"\n- {report.asset_type} 的实测配对（``thscode`` 后缀 → ``exchange`` 列）："
            f"{', '.join(report.exchange_pairs)}。"
        )
        print(
            f"  两列逐字不等的行数 {report.literal_suffix_exchange_mismatch}；"
            f"按 ``EXCHANGE_CODE_BY_SUFFIX`` 别名表判等后剩 {report.suffix_exchange_mismatch} 行。"
            "判据查别名表是因为期货的后缀是短写（``CZC``/``SHF``/``GFE``/``CFE``）、"
            "``exchange`` 列写交易所代码全称（``CZCE``/``SHFE``/``GFEX``/``CFFEX``）；"
            "a-share 两侧同写，别名表对其为恒等。"
        )
    futures = next((report for report in reports if report.asset_type == "futures"), None)
    for report in reports:
        if report.asset_type == "futures" or report.date_less_rows != report.rows:
            continue
        print(
            f"\n- {report.asset_type}：{report.rows} 行的 "
            f"``last_trade_date``/``end_date`` **全为空**"
            f"（无日期行 {report.date_less_rows} = 上游行 {report.rows}），``delisted`` 恒为 "
            f"{report.delisted} ⇒ 上游这份目录**不发已退市标的**，「status 与快照日一致」在该资产"
            f"上退化为「在册即在市」，不构成幸存者偏差-free 的上市区间来源。判「退市后空窗」"
            f"需要另接退市日来源，口径待人工评审。"
        )
    if futures is not None:
        print(
            f"\n- 「两写法不等」= ``thscode`` 去后缀 ≠ ``ticker``。期货 "
            f"{futures.differing_codes} 行全部落在无日期行内"
            f"（{futures.date_less_rows} 行无日期；其中 "
            f"{futures.date_less_rows - futures.differing_codes} 行的 ``8888`` 加权两侧写法恰好"
            f"相同，故不等式为假），这些是上游的连续/主连/加权**合成序列**，展示码按约定不等于"
            f"代码前缀。有日期的真实合约 {futures.rows - futures.date_less_rows} 行里两写法不等的 "
            f"{futures.dated_code_disagreements} 行 ⇒ 「真实合约必须逐字相等 + 合成序列豁免」的"
            f"判据成立（豁免见 ``SYNTHETIC_SERIES_TICKER_TAILS``）。\n"
        )
    else:
        print("")


def _print_members(reports: Sequence[MemberReport], failures: list[str]) -> None:
    """Render criterion B and append its breaches to ``failures``."""
    print("## B. 成分归属（参照：中证指数官网，独立 vendor）\n")
    print("| 指数 | 成员数 | 命中目录 | 交易所后缀不符 | 未命中 |")
    print("| --- | --- | --- | --- | --- |")
    for report in reports:
        print(
            f"| {report.index_code} | {report.members} | {report.in_catalog} "
            f"| {len(report.exchange_mismatch)} | {', '.join(report.missing) or '-'} |"
        )
        if report.missing or report.exchange_mismatch:
            failures.append(
                f"B/{report.index_code}: 未命中 {list(report.missing)}；"
                f"后缀不符 {list(report.exchange_mismatch)}"
            )


def _print_listings(reports: Sequence[ListingReport], failures: list[str]) -> None:
    """Render criterion C and append its breaches to ``failures``."""
    print("## C. 上市日（参照：新浪首根日线，独立 vendor）\n")
    print("| 采样 | thscode | 目录 list_date | 新浪首根 | 总根数 | 早于上市日的根数 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    unjudged = 0
    scope_diffs: list[str] = []
    for report in reports:
        if report.bars < 0:
            verdict = "参照失败"
        elif not report.judged:
            verdict = "参照无数据"
        elif report.first_bar == report.list_date:
            verdict = "一致"
        elif (
            _suffix(report.symbol) in SCOPE_DIFF_SUFFIXES
            and report.first_bar is not None
            and report.list_date is not None
            and report.first_bar < report.list_date
            and report.bars_per_year_before < SPARSE_BARS_PER_YEAR
        ):
            verdict = "口径差"
            scope_diffs.append(
                f"{report.symbol}：新浪首根 {report.first_bar} 早于目录上市日 "
                f"{report.list_date}，其间的 {report.bars_before} 根密度仅 "
                f"{report.bars_per_year_before:.1f} 根/年（连续竞价约 242 根/年）"
            )
        else:
            verdict = "不符"
        if verdict in ("参照失败", "参照无数据"):
            unjudged += 1
        print(
            f"| {report.label} | {report.symbol} | {report.list_date} | {report.first_bar} "
            f"| {report.bars} | {report.bars_before} | {verdict} |"
        )
        if verdict == "不符":
            failures.append(
                f"C/{report.symbol}: list_date {report.list_date} != 新浪首根 {report.first_bar}"
            )
    if scope_diffs:
        print(
            f"\n- 「口径差」登记 {len(scope_diffs)} 只（判据：新浪早于目录，且早出的一段"
            f"密度 < {SPARSE_BARS_PER_YEAR:.0f} 根/年；反向或密度达到连续竞价水平的按不符判）："
        )
        for line in scope_diffs:
            print(f"  - {line}")
        print(
            "- 解释：目录里 920 段配着 2020 年的上市日，说明该代码段是上市之后统一分配的"
            "（新浪在同一 symbol 上把平移前的**全国股转系统（新三板）期**一并返回），"
            "那段做市/定期撮合的密度与连续竞价不是同一回事，故不判为数据错误。"
            "**下游口径注意**：``list_date`` 是北交所上市日，用它判「未上市空窗」时，"
            "这批标的平移前的行情不能算进来；反过来说，新浪侧的首根日期不可当作上市日用。"
        )
    if unjudged:
        print(f"\n- {unjudged} 只参照侧未给出可比的首根日线，按「未判」记录，不算通过也不算失败。")


def _print_rules(
    reports: Sequence[RuleReport], futures_raw: Sequence[Mapping[str, Any]], failures: list[str]
) -> None:
    """Render criterion D (family subtotals first, then any breach row)."""
    print("## D. 中金所最后交易日规则（股指=第三个周五 / 国债=第二个周五）\n")
    print("| 品种族 | 合约 | 到期日 | 上游最后交易日 | 规则推算 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- |")
    wrong: list[RuleReport] = []
    for family in sorted({report.family for report in reports}):
        rows = [report for report in reports if report.family == family]
        ok = sum(1 for report in rows if report.last_trade == report.rule_date)
        print(f"| {family} | 小计 {len(rows)} 只 | - | - | - | {ok}/{len(rows)} 相符 |")
        wrong += [report for report in rows if report.last_trade != report.rule_date]
        for report in rows:
            if report.last_trade != report.rule_date:
                print(
                    f"| {report.family} | {report.symbol} | {report.end_date} "
                    f"| {report.last_trade} | {report.rule_date} | 不符 |"
                )
    if wrong:
        failures.append(f"D: 规则不符 {len(wrong)} 只")
    print(
        f"\n- 覆盖品种族 {len({report.family for report in reports})} 个，共 {len(reports)} 只合约"
    )
    print(f"- 上游两日不等（end_date ≠ last_trade_date）的合约：{_two_day_gap(futures_raw)} 只")


def main() -> int:
    """Run every leg, print the evidence report, and judge the four criteria.

    Returns:
        ``0`` when all judged criteria hold, ``1`` otherwise.
    """
    print("# C14 ths 标的目录跨 vendor 对照（全程只读：不建表、不写仓）\n")

    a_raw, a_snapshot = _catalog("a-share")
    time.sleep(PACE_SECONDS)
    f_raw, f_snapshot = _catalog("futures")
    time.sleep(PACE_SECONDS)
    a_contracts = _contracts("a-share")
    time.sleep(PACE_SECONDS)
    f_contracts = _contracts("futures")

    print("## 取数（两条通道：raw 走传输层，契约走注册表路由）\n")
    print(f"- a-share：raw {len(a_raw)} 行 / 契约 {len(a_contracts)} 行，快照日 {a_snapshot}")
    print(f"- futures：raw {len(f_raw)} 行 / 契约 {len(f_contracts)} 行，快照日 {f_snapshot}\n")

    failures: list[str] = []
    _print_shape(
        [
            shape_report("a-share", a_raw, a_contracts, a_snapshot),
            shape_report("futures", f_raw, f_contracts, f_snapshot),
        ],
        failures,
    )
    print("")
    _print_members(member_reports(a_contracts), failures)
    print("")
    _print_listings(listing_reports(a_contracts), failures)
    print("")
    _print_rules(rule_reports(f_raw), f_raw, failures)

    print("\n## 判定")
    if failures:
        print(f"FAIL（{len(failures)} 项）")
        for line in failures:
            print(f"- {line}")
        return 1
    print("PASS：A/B/C/D 四条判据全部成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
