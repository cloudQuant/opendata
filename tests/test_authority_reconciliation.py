"""``authority.json`` 与**活注册表**的全库对账（AC-3 / AC-10，任务 #30）.

C22 补了四条宏观权威行，也留了一条明确的覆盖面边界：它的守卫只遍历 11 条
**硬编码**的宏观腿，所以"某个域压根没进表""某行列了一个根本不存在的源"这类
缺陷它一个也发现不了。而 ``GET /api/v1/data/sources`` 是把这张表**原样**回给
调用方的（``opendata/api/data.py:59``），于是表里的假腿就成了对外声明。本轮实测
表内有 7 对 (域, 源) 在注册表里没有任何能力：

* ``stock_adjust`` / ``futures_fundamentals`` 两行整行是空的（这两个域一条能力都没
  注册过：复权因子走 market-dumps + ``FactorBuilder`` 的派生层，不经路由层；期货
  F10 端点是 ``client_only``）；
* ``financial_indicator`` 里的 ``ths``（端点 available 但 C10 实测契约不匹配，故意
  没接）、``instrument`` 与 ``trading_calendar`` 里的 ``akshare``（C13/C14 只落了
  ths 侧）。

对账的判据是两条，缺一不可：**表里不许有注册表没有的腿**（对外声明不夸大），
**注册表里有多条 auto 腿的域不许没有行**（否则又回到"谁先注册谁答"，即 C22 的
宏观缺陷）。第三条只在有实际后果时才成立：单一源的域**不需要**行——没有东西要
排序，硬要它写一行等于让权威表去认证一个它表达不出来的排序，这正是 AC-10 对
``fund_action`` 与海外日线写的"登记成权威序属口径造假"。表里仍然留着的 4 条单源行
是各自有理由的（C22 实测过 ``economy_rate``；另外三条是本轮去假腿之后剩下的真实
单腿），所以本模块把它们钉成显式清单，而不是让"要不要写行"重新变成随手的事。

这里全部按活注册表（``register_providers()`` 之后的 44 条能力）判，而不是按某个
手写的腿清单判——那正是 C22 遗留、本轮要收掉的那条边界。谁排在前面不在本模块的
判据里：那是真机跨 vendor 对照的事（``tests/test_macro_routing.py`` 与
``docs/evidence/C22``）。本模块只保证"表说的腿确实存在，存在的腿确实归表管"。
"""

from typing import Any

import pytest

from opendata.data.capability import Capability
from opendata.data.domains import load_domains
from opendata.data.providers import register_providers
from opendata.data.registry import authority_baseline, get_registry, reconcile_authority

#: The seven ``(domain, source)`` pairs C23 removed: each was listed while no
#: capability registered it. Re-adding any one must be a violation - these are
#: the defects this round fixed, not hypotheticals.
PHANTOM_PAIRS: tuple[tuple[str, str], ...] = (
    ("stock_adjust", "ths"),
    ("stock_adjust", "akshare"),
    ("futures_fundamentals", "ths"),
    ("futures_fundamentals", "akshare"),
    ("financial_indicator", "ths"),
    ("instrument", "akshare"),
    ("trading_calendar", "akshare"),
)

#: Domains served by exactly one source and left out of the table on purpose
#: (AC-10): nothing to rank. ``bond_daily`` is the unverified akshare leg,
#: ``fund_action`` is ths-only, ``stock_daily_overseas`` is yfinance-only.
SINGLE_SOURCE_EXEMPTIONS: frozenset[str] = frozenset(
    {"bond_daily", "fund_action", "stock_daily_overseas"}
)

# Query-native domains are absent from the authority table while their model
# bindings remain unverified and outside the legacy warehouse table plan.
NATIVE_MODEL_DOMAINS: frozenset[str] = frozenset(
    {
        "bls_search",
        "bls_series",
        "currency_reference_rates",
        "equity_historical",
        "equity_quote",
        "fred_search",
        "fred_series",
        "sofr",
        "sonia",
        "balance_of_payments",
        "yield_curve",
    }
)

#: Domains served by one source but still holding a row. Each is a real leg, not
#: a phantom: ``economy_rate`` ranks a single macro source because C22 measured
#: that the others cannot serve the request shape, and the three remaining after
#: this round's thinning keep the pre-existing declaration honest. Anything else
#: appearing here has to be justified before it lands.
SINGLE_SOURCE_ROWS: frozenset[str] = frozenset(
    {"economy_rate", "financial_indicator", "instrument", "trading_calendar"}
)

#: Declared in ``domains.yaml`` but served by no capability, and why: the factor
#: layer is derived outside routing (market-dumps -> ods -> FactorBuilder), and the
#: futures F10 endpoint is ``client_only``. Pinned so a new silently unserved domain
#: fails here instead of quietly widening the gap.
UNSERVED_DOMAINS: frozenset[str] = frozenset({"stock_adjust", "futures_fundamentals"})


def live_capabilities() -> list[Capability]:
    """Return the fully registered deployment capabilities."""
    register_providers()
    return get_registry().capabilities()


def sources_per_domain() -> dict[str, set[str]]:
    """Group the live registry into ``domain -> sources serving it``."""
    grouped: dict[str, set[str]] = {}
    for capability in live_capabilities():
        grouped.setdefault(capability.domain, set()).add(capability.source)
    return grouped


def _cap(
    domain: str,
    source: str,
    *,
    verified: bool = True,
    asset_class: str = "equity",
    period: str = "1D",
    market: str = "cn",
) -> Capability:
    """Build a capability declaration for the rule-level tests."""
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period=period,
        market=market,
        source=source,
        verified=verified,
    )


def _with_pair(domain: str, source: str) -> dict[str, tuple[str, ...]]:
    """Return the shipped authority table with one phantom pair added back."""
    table = dict(authority_baseline())
    table[domain] = (*table.get(domain, ()), source)
    return table


class TestLiveTableMatchesLiveRegistry:
    """The shipped table and the shipped registry describe the same legs."""

    def test_reconciliation_is_clean(self):
        assert reconcile_authority(live_capabilities()) == ()

    def test_no_row_names_a_source_that_does_not_serve_the_domain(self):
        """The direction ``/sources`` over-claims in, checked leg by leg.

        ``reconcile_authority`` already reports this, but a bare empty-tuple
        assertion says only "something is off"; this one names the pair.
        """
        served = sources_per_domain()

        for domain, row in authority_baseline().items():
            for source in row:
                assert source in served.get(domain, set()), f"{domain}/{source} is a phantom"

    def test_table_is_required_rows_plus_the_explained_single_source_ones(self):
        """Every row is either needed to rank two legs or listed as an exemption.

        A domain served by several sources without a row falls back to
        registration order (the C22 defect), and a single-source row has to be
        one of the four this round inherited on purpose.
        """
        served = sources_per_domain()
        multi_source = {domain for domain, sources in served.items() if len(sources) > 1}

        assert set(authority_baseline()) == multi_source | SINGLE_SOURCE_ROWS

    def test_domains_without_a_row_are_exactly_the_single_source_ones(self):
        """Separate deliberate single-source gaps from native model domains."""
        served = sources_per_domain()
        unlisted = set(served) - set(authority_baseline())

        assert SINGLE_SOURCE_EXEMPTIONS.isdisjoint(NATIVE_MODEL_DOMAINS)
        assert unlisted == SINGLE_SOURCE_EXEMPTIONS | NATIVE_MODEL_DOMAINS
        for domain in SINGLE_SOURCE_EXEMPTIONS:
            assert len(served[domain]) == 1

        specs = load_domains()
        registry = get_registry()
        native_descriptors = {
            domain: [
                descriptor
                for descriptor in registry.list_model_descriptors()
                if descriptor.domain == domain
            ]
            for domain in NATIVE_MODEL_DOMAINS
        }
        assert set(native_descriptors) == NATIVE_MODEL_DOMAINS
        assert all(len(descriptors) == 1 for descriptors in native_descriptors.values())
        for domain, descriptors in native_descriptors.items():
            spec = specs[domain]
            descriptor = descriptors[0]
            fetcher = registry.resolve_model(descriptor.source, descriptor.model)
            capability = fetcher.capability

            assert spec.semantics_declared is True
            assert "query" in spec.permissions
            assert len(served[domain]) == 1
            assert descriptor.source in served[domain]
            assert descriptor.model == fetcher.canonical_model
            assert descriptor.full_capability_identity == (
                capability.asset_class,
                capability.domain,
                capability.period,
                capability.market,
                capability.source,
            )
            assert capability.domain == domain
            assert descriptor.verified is False
            assert capability.verified is False

        # This is the registry-derived legacy table plan only; it does not
        # query a warehouse or assert that any planned table exists there.
        from opendata.pipeline.alert_matrix import warehouse_tables

        planned_tables = warehouse_tables()
        assert all(f"dwd_{domain}" not in planned_tables for domain in NATIVE_MODEL_DOMAINS)
        assert all(
            not any(table.startswith(f"ods_{domain}_") for table in planned_tables)
            for domain in NATIVE_MODEL_DOMAINS
        )

    def test_declared_domains_are_either_served_or_named_as_unserved(self):
        served = sources_per_domain()

        assert set(load_domains()) - set(served) == UNSERVED_DOMAINS
        assert set(served) - set(load_domains()) == set()

    def test_guard_is_not_limited_to_a_hand_listed_set_of_legs(self):
        """C22's guard walked 11 named macro legs; this one walks the registry.

        Drop a domestic row and a macro row and both must be named in the same
        call - nothing here lists legs, so a domain added next round is covered
        the day it registers its second source.
        """
        table = {
            domain: sources
            for domain, sources in authority_baseline().items()
            if domain not in {"stock_daily", "economy_gdp"}
        }

        assert reconcile_authority(live_capabilities(), baseline=table) == (
            # The count names every source serving the domain, whether or not it
            # has the credential required by auto routing.
            "unlisted-domain: economy_gdp is served by 3 sources but has no row",
            "unlisted-domain: stock_daily is served by 2 sources but has no row",
        )

    @pytest.mark.parametrize(("domain", "source"), PHANTOM_PAIRS)
    def test_each_removed_phantom_pair_is_caught(self, domain: str, source: str):
        """The claim under test is "those seven pairs were phantoms", individually.

        The shipped table differs from the pre-round table by exactly one pair
        per case, so adding one back has to raise exactly that one violation.
        """
        violations = reconcile_authority(live_capabilities(), baseline=_with_pair(domain, source))

        assert violations == (
            f"phantom-leg: {domain}/{source} is listed but registers no capability",
        )


class TestReconciliationRules:
    """Each rule bites on its own, and only in the direction it claims to."""

    def test_phantom_leg_is_named(self):
        caps = [_cap("stock_daily", "ths")]

        assert reconcile_authority(caps, baseline={"stock_daily": ("ths", "akshare")}) == (
            "phantom-leg: stock_daily/akshare is listed but registers no capability",
        )

    def test_auto_leg_outside_the_table_is_named(self):
        """A verified leg the table does not rank is the C22 defect, whole-registry.

        This is the shape a second leg lands in: the domain is already listed,
        so nothing about the old rows looks wrong - only the new source is.
        """
        caps = [_cap("stock_daily", "ths"), _cap("stock_daily", "akshare")]

        assert reconcile_authority(caps, baseline={"stock_daily": ("ths",)}) == (
            "unranked-leg: stock_daily/akshare serves auto requests but is not listed",
        )

    def test_two_sources_without_any_row_is_named(self):
        caps = [_cap("stock_daily", "ths"), _cap("stock_daily", "akshare")]

        assert reconcile_authority(caps, baseline={}) == (
            "unlisted-domain: stock_daily is served by 2 sources but has no row",
        )

    def test_single_source_domain_needs_no_row(self):
        """AC-10's position, as a rule rather than an absence of tests."""
        caps = [_cap("fund_action", "ths")]

        assert reconcile_authority(caps, baseline={}) == ()

    def test_unverified_leg_may_stay_out_of_the_table(self):
        """A synthetic unverified leg does not need ranking in the authority table."""
        caps = [
            _cap("economy_cpi", "ecb", asset_class="macro", period="1M", market="eu"),
            _cap(
                "economy_cpi",
                "fred",
                verified=False,
                asset_class="macro",
                period="1M",
                market="us",
            ),
        ]

        assert reconcile_authority(caps, baseline={"economy_cpi": ("ecb",)}) == ()

    def test_empty_row_is_named(self):
        """A row with no sources ranks nothing and must not read as listed."""
        caps = [_cap("stock_daily", "ths")]

        assert reconcile_authority(caps, baseline={"stock_daily": ()}) == (
            "empty-row: stock_daily is listed with no source",
        )

    def test_verdict_does_not_depend_on_registration_order(self):
        caps = [_cap("stock_daily", "ths"), _cap("stock_daily", "akshare")]
        baseline = {"stock_daily": ("ths",)}

        assert (
            reconcile_authority(caps, baseline=baseline)
            == reconcile_authority(
                list(reversed(caps)),
                baseline=baseline,
            )
            == ("unranked-leg: stock_daily/akshare serves auto requests but is not listed",)
        )


class TestSourcesApiAgreesWithRegistry:
    """``GET /sources`` must not advertise a leg the registry cannot serve."""

    async def _get_sources(self, test_client, test_user_token) -> dict[str, Any]:
        register_providers()

        response = await test_client.get(
            "/api/v1/data/sources",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        return response.json()

    async def test_authority_is_bounded_by_the_registered_view(self, test_client, test_user_token):
        payload = await self._get_sources(test_client, test_user_token)
        registered = {
            (domain, source)
            for source, domains in payload["registered"].items()
            for domain in domains
        }

        # A subset, not an equality: the route also renders single-source domains
        # that carry no row, and the row side must never leave the registered one.
        for domain, sources in payload["authority"].items():
            assert domain in {d for d, _ in registered}, f"{domain} is not served at all"
            for source in sources:
                assert (domain, source) in registered, f"{domain}/{source} is not routable"

    async def test_phantom_rows_are_gone_from_the_public_claim(self, test_client, test_user_token):
        payload = await self._get_sources(test_client, test_user_token)
        authority: dict[str, list[str]] = payload["authority"]

        assert "stock_adjust" not in authority
        assert "futures_fundamentals" not in authority
        assert authority["financial_indicator"] == ["akshare"]
        assert authority["instrument"] == ["ths"]
        assert authority["trading_calendar"] == ["ths"]
