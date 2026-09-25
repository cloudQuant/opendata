"""宏观域的路由判据（AC-3 / AC-10）：market 维度要透传，权威序要说了算.

C21 把这件事登记成遗留：``authority.json`` 从不列宏观域，而
``resolve_domain`` 又把 ``period`` / ``market`` 丢掉，于是 ``source=auto`` 在
``economy_cpi`` 上退化成"谁先注册谁答"。同一棵树换一种文件收集顺序就有 2 条
注册用例翻转（``test_ecb_provider`` / ``test_imf_provider``），也就是说那两条
绿灯是顺序巧合，不是路由判据。

本模块钉住修好后的三件事，全部离线（用真实 provider 的能力声明，不发请求）：

1. 跨 market 的歧义**必须失败**：市场没指定时，宁可报错也不替调用方猜一个市场
   （问了美国 CPI 却答欧元区 HICP 是错数据，不是"源换了"）；
2. ``resolve_domain`` 透传 ``period`` / ``market``，与 ``resolve`` 同判据；
3. 同一 (domain, period, market) 上有多条 verified 腿时，赢家由权威表决定，
   **与注册顺序无关**——这里把同一批腿按正序、逆序、以及每条候选腿各自打头的
   顺序各注册一遍，赢家必须始终是同一个，且等于权威表里第一个列出的源。

这里不写"我觉得谁该赢"：谁在前是 ``authority.json`` 的责任，其依据是真机跨 vendor
对照（``scripts/ops/macro_authority_cross_check.py``，留档 ``docs/evidence/C22``）；
本模块只证明"决定赢家的是权威表，不是收集顺序"。
"""

from collections.abc import Sequence
from typing import Any

import pytest

from opendata.data.protocol import Fetcher
from opendata.data.providers.ecb.models.cpi import EcbCpiFetcher
from opendata.data.providers.ecb.models.gdp import EcbGdpFetcher
from opendata.data.providers.ecb.models.rate import EcbRateFetcher
from opendata.data.providers.fred.models.cpi import FredCpiFetcher
from opendata.data.providers.fred.models.gdp import FredGdpFetcher
from opendata.data.providers.fred.models.unemployment import FredUnemploymentFetcher
from opendata.data.providers.imf.models.cpi import ImfCpiFetcher
from opendata.data.providers.imf.models.gdp import ImfGdpFetcher
from opendata.data.providers.imf.models.unemployment import ImfUnemploymentFetcher
from opendata.data.providers.oecd.models.cpi import OecdCpiFetcher
from opendata.data.providers.oecd.models.unemployment import OecdUnemploymentFetcher
from opendata.data.registry import ProviderRegistry, authority_baseline

#: Every macro leg the deployment registers, whatever the import order is.
MACRO_LEGS: tuple[type[Fetcher[Any, Any]], ...] = (
    EcbCpiFetcher,
    EcbGdpFetcher,
    EcbRateFetcher,
    FredCpiFetcher,
    FredGdpFetcher,
    FredUnemploymentFetcher,
    ImfCpiFetcher,
    ImfGdpFetcher,
    ImfUnemploymentFetcher,
    OecdCpiFetcher,
    OecdUnemploymentFetcher,
)

#: One ``(domain, period, market)`` request shape plus the sources serving it.
Group = tuple[str, str, str, list[str]]


def ambiguous_groups() -> list[Group]:
    """Group the auto-eligible macro legs by what a caller can ask for.

    Returns:
        Only the shapes served by more than one leg - the ones where an
        authority order means something at all.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for leg in MACRO_LEGS:
        capability = leg.capability
        if not capability.participates_in_auto():
            continue
        groups.setdefault((capability.domain, capability.period, capability.market), []).append(
            capability.source
        )
    return [(*key, sorted(sources)) for key, sources in sorted(groups.items()) if len(sources) > 1]


def build_registry(legs: Sequence[type[Fetcher[Any, Any]]]) -> ProviderRegistry:
    """Register the given macro legs into a fresh registry, in the given order."""
    registry = ProviderRegistry()
    for leg in legs:
        registry.register(leg())
    return registry


class TestMarketDimension:
    def test_ambiguity_detection_is_not_vacuous(self) -> None:
        """If no group were served by two legs, the order tests below prove nothing."""
        assert [(item[0], item[1], item[2]) for item in ambiguous_groups()] == [
            ("economy_cpi", "1M", "eu"),
            ("economy_unemployment", "1A", "global"),
        ]

    def test_market_blind_auto_fails_on_a_multi_market_domain(self) -> None:
        registry = build_registry(MACRO_LEGS)
        with pytest.raises(LookupError, match="does not pick across markets") as excinfo:
            registry.resolve_domain("economy_cpi")
        message = str(excinfo.value)
        assert "eu" in message
        assert "global" in message

    def test_narrowing_the_market_answers_without_a_guess(self) -> None:
        registry = build_registry(MACRO_LEGS)
        euro = registry.resolve_domain("economy_cpi", period="1M", market="eu")
        assert euro.capability.market == "eu"
        global_leg = registry.resolve_domain("economy_cpi", period="1A", market="global")
        assert global_leg.capability.market == "global"

    def test_resolve_domain_matches_resolve(self) -> None:
        """The catalog-facing entry must be ``resolve`` with the same filters."""
        registry = build_registry(MACRO_LEGS)
        for leg in MACRO_LEGS:
            capability = leg.capability
            by_domain = registry.resolve_domain(
                capability.domain,
                period=capability.period,
                market=capability.market,
                source=capability.source,
            )
            by_fields = registry.resolve(
                capability.asset_class,
                capability.domain,
                period=capability.period,
                market=capability.market,
                source=capability.source,
            )
            assert by_domain is by_fields

    def test_single_market_domains_stay_routable(self) -> None:
        """The guard must not break the case that never had two markets."""
        registry = build_registry(MACRO_LEGS)
        assert registry.resolve_domain("economy_rate").capability.source == "ecb"

    def test_unverified_leg_still_never_answers_auto(self) -> None:
        registry = build_registry(MACRO_LEGS)
        with pytest.raises(LookupError, match="no verified capability"):
            registry.resolve_domain("economy_cpi", period="1M", market="us")


class TestAuthorityDecidesNotRegistrationOrder:
    @pytest.mark.parametrize("group", ambiguous_groups(), ids=lambda g: "/".join(g[:3]))
    def test_winner_is_the_same_whatever_the_registration_order(self, group: Group) -> None:
        """Every order that could make the other leg win is tried.

        Each candidate is put first in turn (the only positions that can
        change the answer while the baseline is silent), plus the natural
        and the reversed registration order.
        """
        domain, period, market, sources = group
        orders: list[Sequence[type[Fetcher[Any, Any]]]] = [
            MACRO_LEGS,
            tuple(reversed(MACRO_LEGS)),
        ]
        for source in sources:
            first = [leg for leg in MACRO_LEGS if leg.capability.source == source]
            rest = [leg for leg in MACRO_LEGS if leg.capability.source != source]
            orders.append(tuple(first + rest))
        winners = set()
        for order in orders:
            resolved = build_registry(order).resolve_domain(domain, period=period, market=market)
            winners.add(resolved.capability.source)
        assert len(winners) == 1, f"{domain}/{period}/{market} answered {winners} across orders"
        assert winners.pop() in sources

    @pytest.mark.parametrize("group", ambiguous_groups(), ids=lambda g: "/".join(g[:3]))
    def test_winner_is_the_highest_listed_source(self, group: Group) -> None:
        """The declared baseline, not the loop above, is what picks the winner."""
        domain, period, market, sources = group
        listed = [source for source in authority_baseline().get(domain, ()) if source in sources]
        assert listed, f"{domain} lists none of {sources}, so auto falls back to luck"
        registry = build_registry(MACRO_LEGS)
        resolved = registry.resolve_domain(domain, period=period, market=market)
        assert resolved.capability.source == listed[0]

    def test_baseline_is_what_wins_after_a_degradation(self) -> None:
        """Ranking must survive the first candidate going unhealthy."""
        registry = build_registry(MACRO_LEGS)
        top = registry.resolve_domain("economy_unemployment")
        registry.mark_unavailable(top)
        fallen = registry.resolve_domain("economy_unemployment")
        assert fallen.capability.source != top.capability.source
        assert fallen.capability.domain == "economy_unemployment"
        registry.mark_available(top)
        assert registry.resolve_domain("economy_unemployment") is top


class TestBaselineCoversMacro:
    def test_every_macro_domain_is_listed(self) -> None:
        baseline = authority_baseline()
        served = {leg.capability.domain for leg in MACRO_LEGS}
        missing = sorted(served - set(baseline))
        assert not missing, f"macro domains routing on registration order: {missing}"

    def test_baseline_lists_only_sources_that_serve_the_domain(self) -> None:
        """A row naming a source with no leg there is a typo waiting to mis-route."""
        baseline = authority_baseline()
        served = {(leg.capability.domain, leg.capability.source) for leg in MACRO_LEGS}
        for domain in sorted({leg.capability.domain for leg in MACRO_LEGS}):
            for source in baseline[domain]:
                assert (domain, source) in served, f"{domain} lists {source}, which serves nothing"
