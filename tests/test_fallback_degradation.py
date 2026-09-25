"""``authority.json`` 的排序**在路由上到底生了效没有**（AC-3 / AC-10，任务 #31）.

C23 证明表里每一条腿都真的注册了能力；它没能证明**退路真的能顶上**。这两件事
差别很大：一条腿注册了，不等于 ``source="auto"`` 在它前面那条失败时会换到它。
``resolve()`` 换腿要同时过三道条件，本模块逐道量，并把今天过不了的行列成钉死的清单：

1. **同形状才可换**：``_matches()`` 先按 ``(period, market)`` 过滤，所以一行里的两条
   腿只有共享一个形状才可能互为退路。本轮实测 ``stock_action`` 两腿一个写 ``1D``
   一个写 ``event``（同一市场同一契约，只是拼写不一致），那一行排的是一条
   ``resolve()`` 永远不会取用的退路——已把 akshare 侧对齐为 ``1D``。
   ``economy_gdp`` 是 C22 有意保留的例外：两腿本就不同频率不同市场，写进表只为
   让覆盖守卫不许悄悄退化。
2. **够资格才排得上**：只有 ``participates_in_auto()``（``verified`` 且 notes 不是
   on-demand / upstream-pending）的腿参与排序，所以"表里排第一"和"auto 用第一"
   是两码事：``fund_etf_daily`` 按 C20 把 akshare 排在 ths 前（ths 只答滚动 1827
   天），而 akshare 腿至今未转正（东财 K 线通道对本机拒答），这一行的顺序 auto
   并不认。
3. **摘掉前一条必须落到后一条**：把 ranked 里前 k 条 ``mark_unavailable`` 之后
   ``resolve(auto)`` 必须给出第 k+1 条。今天全注册表只有两个组做得到（钉在下面），
   国内 8 行（表里 11 行中 market=cn 的那些）一条也做不到——这正是"退路存在"必须
   从注释变成判据的原因。

谁排在前面是真机跨 vendor 对照的事（``scripts/ops/akshare_fallback_cross_check.py``
与 ``docs/evidence/C24/``），这里不判；这里只判"顺序能不能被路由执行"。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opendata.data.capability import Capability

from opendata.data.providers import register_providers
from opendata.data.registry import ProviderRegistry, authority_baseline, get_registry

#: A routing shape: two legs only compete when both fields match.
Shape = tuple[str, str, str, str]  # asset_class, domain, period, market

#: Rows whose ranked legs do not share any shape, and why that is on purpose:
#: C22 lists ``economy_gdp`` so the coverage guard cannot silently regress,
#: while its two legs answer different questions (1Q/eu vs 1A/global).
SHAPE_SPLIT_EXCEPTIONS: frozenset[str] = frozenset({"economy_gdp"})

#: Rows that advertise an order ``auto`` does not honor. ``fund_etf_daily``
#: ranks akshare first on C20's measured ground (the ths leg answers only a
#: rolling 1827 days, so it cannot serve a since-inception request), while the
#: akshare leg stays unverified because the eastmoney kline host refuses this
#: machine - see docs/evidence/C24/akshare-fallback-cross-check.txt.
ORDER_NOT_HONORED: frozenset[str] = frozenset({"fund_etf_daily"})

#: Rows with no auto-eligible leg at all: ``source="auto"`` cannot serve the
#: domain, which is what a P1 candidate registered ``verified=false`` means
#: (task #2). ``financial_indicator`` is the only one, and AC-11 flips it the
#: day the ``announce_date`` gap closes - not before.
NO_AUTO_LEG_ROWS: frozenset[str] = frozenset({"financial_indicator"})

#: The shapes with at least two auto-eligible legs today, i.e. the only places
#: where a published fallback can actually be taken. Pinned exactly: gaining a
#: member means a leg was flipped to ``verified`` (a measured decision needing
#: cross-vendor evidence), losing one means a leg was flipped back.
DEGRADABLE_SHAPES: frozenset[Shape] = frozenset(
    {
        ("macro", "economy_cpi", "1M", "eu"),
        ("macro", "economy_unemployment", "1A", "global"),
    }
)

#: The nine domestic rows that rank a second source.
DOMESTIC_MULTI_LEG_ROWS: frozenset[str] = frozenset(
    {
        "stock_daily",
        "stock_action",
        "financial_statement",
        "index_constituent",
        "index_daily",
        "fund_etf_daily",
        "futures_daily",
        "option_daily",
    }
)


def live_capabilities() -> list[Capability]:
    """Return the fully registered deployment capabilities."""
    register_providers()
    return get_registry().capabilities()


def leg_shapes(domain: str) -> set[tuple[str, str]]:
    """The ``(period, market)`` shapes the sources ranked in one row use."""
    row = authority_baseline().get(domain, ())
    return {
        (capability.period, capability.market)
        for capability in live_capabilities()
        if capability.domain == domain and capability.source in row
    }


def shape_census(domain: str) -> dict[tuple[str, str], list[str]]:
    """Count the ranked legs of one domain per shape, in row order."""
    row = authority_baseline().get(domain, ())
    census: dict[tuple[str, str], list[str]] = {}
    for source in row:
        for capability in live_capabilities():
            if capability.domain == domain and capability.source == source:
                census.setdefault((capability.period, capability.market), []).append(source)
    return census


def eligible_sources(domain: str) -> list[str]:
    """The ranked sources of one domain that ``auto`` may use, in row order."""
    eligible = {
        capability.source
        for capability in live_capabilities()
        if capability.domain == domain and capability.participates_in_auto()
    }
    return [source for source in authority_baseline().get(domain, ()) if source in eligible]


def auto_eligible_shapes() -> dict[Shape, list[str]]:
    """Group auto-eligible legs by shape, ordered as their row ranks them."""
    baseline = authority_baseline()
    groups: dict[Shape, list[str]] = {}
    for capability in live_capabilities():
        if not capability.participates_in_auto():
            continue
        row = baseline.get(capability.domain)
        if row is None or capability.source not in row:
            continue
        shape: Shape = (
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
        )
        members = groups.setdefault(shape, [])
        if capability.source not in members:
            members.append(capability.source)
    for shape, members in groups.items():
        members.sort(key=lambda source: authority_baseline()[shape[1]].index(source))
    return groups


@contextmanager
def _unavailable(registry: ProviderRegistry, *legs: tuple[str, str, str]) -> Iterator[None]:
    """Mark ``(asset_class, domain, source)`` legs unhealthy, restoring on exit.

    Args:
        registry: The live registry whose health flags are bent.
        *legs: The legs to take out of auto routing for the block.
    """
    fetchers = [registry.resolve(leg[0], leg[1], source=leg[2]) for leg in legs]
    for fetcher in fetchers:
        registry.mark_unavailable(fetcher)
    try:
        yield
    finally:
        for fetcher in fetchers:
            registry.mark_available(fetcher)


class TestRankedLegsCanCompete:
    """A row may only rank legs ``auto`` could actually choose between."""

    def test_only_the_documented_row_ranks_legs_that_never_meet(self):
        split = {
            domain
            for domain in authority_baseline()
            if len(shape_census(domain)) > 1
            and not any(len(legs) > 1 for legs in shape_census(domain).values())
        }

        assert split == SHAPE_SPLIT_EXCEPTIONS

    def test_stock_action_legs_share_the_verified_legs_shape(self):
        """The defect this round found and fixed, read back off the registry.

        ``authority.json`` ranks ``stock_action`` as ``[ths, akshare]``. While
        the akshare leg spelled its period ``event`` the row ranked a fallback
        no ``resolve()`` call could ever take, because ``_matches`` filters on
        ``period`` before it looks at health or order.
        """
        assert shape_census("stock_action") == {("1D", "cn"): ["ths", "akshare"]}

    def test_economy_gdp_exception_is_still_two_different_questions(self):
        """A pinned exception stays one only while the shapes stay disjoint."""
        assert shape_census("economy_gdp") == {("1Q", "eu"): ["ecb"], ("1A", "global"): ["imf"]}

    def test_economy_cpi_row_competes_where_it_claims_to(self):
        """Three ranked legs, but only the 1M/eu pair can replace each other."""
        assert shape_census("economy_cpi")[("1M", "eu")] == ["ecb", "oecd"]
        assert leg_shapes("economy_cpi") == {("1M", "eu"), ("1A", "global")}


class TestRowOrderIsTheOrderAutoUses:
    """Where a row is executable, its first entry is what ``auto`` answers with."""

    def test_rows_with_an_eligible_leg_start_with_an_eligible_leg(self):
        offenders = {
            domain
            for domain in authority_baseline()
            if (ranked := eligible_sources(domain)) and ranked[0] != authority_baseline()[domain][0]
        }

        assert offenders == ORDER_NOT_HONORED

    def test_fund_etf_daily_exception_holds_for_the_pinned_reason(self):
        """The exception has to stay true for the reason it was pinned."""
        akshare = next(
            capability
            for capability in live_capabilities()
            if (capability.domain, capability.source) == ("fund_etf_daily", "akshare")
        )

        assert akshare.verified is False
        assert akshare.participates_in_auto() is False
        assert eligible_sources("fund_etf_daily") == ["ths"]

    def test_rows_auto_cannot_serve_at_all_are_the_pinned_candidates(self):
        """A row with no eligible leg is a registered candidate, not a fallback."""
        unanswerable = {domain for domain in authority_baseline() if not eligible_sources(domain)}

        assert unanswerable == NO_AUTO_LEG_ROWS


class TestDegradationIsTaken:
    """Taking out the head leg hands the request to the next ranked leg."""

    def test_each_ranked_leg_loses_to_the_next_one_in_its_row(self):
        """Every multi-leg shape degrades along the row, one leg at a time."""
        registry = get_registry()
        for shape in sorted(DEGRADABLE_SHAPES):
            asset_class, domain, period, market = shape
            ordered = auto_eligible_shapes()[shape]
            for cut in range(len(ordered) - 1):
                dropped = ordered[: cut + 1]
                with _unavailable(registry, *[(asset_class, domain, s) for s in dropped]):
                    resolved = registry.resolve(
                        asset_class, domain, period=period, market=market, source="auto"
                    )
                assert resolved.capability.source == ordered[cut + 1], (
                    f"{shape}：摘掉 {dropped} 之后应落到 {ordered[cut + 1]}"
                )

    def test_the_last_leg_failing_leaves_a_lookup_miss_rather_than_a_guess(self):
        """Nothing bypasses the health flags to answer anyway."""
        registry = get_registry()
        asset_class, domain, period, market = "macro", "economy_cpi", "1M", "eu"
        ordered = auto_eligible_shapes()[(asset_class, domain, period, market)]

        with _unavailable(registry, *[(asset_class, domain, s) for s in ordered]):
            try:
                answer = registry.resolve(
                    asset_class, domain, period=period, market=market, source="auto"
                )
            except LookupError:
                return
            raise AssertionError(f"全部 {ordered} 都被摘掉，auto 仍给出 {answer.capability.source}")

    def test_health_flags_are_restored_after_each_chain(self):
        """The guard must not leave the shared registry bent for later tests."""
        registry = get_registry()
        shape = ("macro", "economy_cpi", "1M", "eu")
        head = auto_eligible_shapes()[shape][0]

        with _unavailable(registry, (shape[0], shape[1], head)):
            assert (
                registry.resolve(
                    shape[0], shape[1], period=shape[2], market=shape[3], source="auto"
                ).capability.source
                != head
            )

        assert (
            registry.resolve(
                shape[0], shape[1], period=shape[2], market=shape[3], source="auto"
            ).capability.source
            == head
        )


class TestDegradationCoverageIsMeasured:
    """How many of the advertised fallbacks exist in routing right now."""

    def test_degradable_shapes_are_exactly_the_measured_ones(self):
        """The round's headline number, pinned so it can only move on purpose."""
        multi = {shape for shape, legs in auto_eligible_shapes().items() if len(legs) >= 2}

        assert multi == DEGRADABLE_SHAPES

    def test_no_domestic_row_can_degrade_today(self):
        """AC-3's "自动退路" measured instead of assumed, for the domestic core.

        Eight domestic rows rank two sources; none has a second auto-eligible
        leg, so taking the head leg out does not move the request anywhere -
        the fallback is a comment. This fails the day a leg is flipped with
        cross-vendor evidence, which is when the acceptance wording can be
        upgraded.
        """
        domestic = {
            shape for shape in auto_eligible_shapes() if shape[1] in DOMESTIC_MULTI_LEG_ROWS
        }
        degraded = {shape for shape in domestic if len(auto_eligible_shapes()[shape]) >= 2}

        assert degraded == set()

    def test_the_domestic_gap_is_eligibility_not_a_missing_row(self):
        """Pinned so this conclusion cannot be reached by deleting rows instead.

        The table still advertises the second leg for all eight rows; what is
        missing is a measured reason to let ``auto`` use it.
        """
        multi_leg_domestic = {
            domain
            for domain, row in authority_baseline().items()
            if len(row) >= 2
            and any(
                capability.market == "cn"
                for capability in live_capabilities()
                if capability.domain == domain
            )
        }

        assert multi_leg_domestic == DOMESTIC_MULTI_LEG_ROWS
