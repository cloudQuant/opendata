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
# The two cboe snapshots joined when their domains entered ``domains.yaml``, followed by
# ``cboe_index_search``, the three ``federal_reserve_*`` engine domains (the last one
# ``row_expansion``) and the four ``intrinio_*`` ones (the last one ``intrinio_calendar_ipo``,
# also ``row_expansion``): each is ``verified=False``, single-source and
# ``storage_mode: transient``, so there is still nothing to rank and no legacy table to watch.
# This round's three (``federal_reserve_federal_funds_rate``, ``federal_reserve_sofr`` and
# ``intrinio_insider_trading``) joined on the same reading: their domains are now registered with
# reviewed contracts, so they leave the authority table for the same reason the other 22 did --
# one unverified source each, nothing to rank.
# C99's four (``benzinga_price_target``, ``econdb_available_indicators``,
# ``federal_reserve_overnight_bank_funding_rate`` and ``tiingo_crypto_historical``) join for the
# same reason and with the same measured shape: each domain is now declared with its reviewed
# contract,
# each is served by exactly one leg, that leg is ``verified=False``, and ``storage_mode: transient``
# keeps it out of both the legacy dwd/ods plan and the authority table (25 -> 29 names here).
# C100's ten initial ready model domains and the subsequent Alpha Vantage trio also remain outside
# the warehouse authority plan. Some history domains permit upsert, but none has a warehouse table.
# Wave 37 added ``cboe_index_snapshots`` and wave 38 adds ``seeking_alpha_calendar_earnings``: both
# landed a single-leg ``verified=False``/``transient`` model domain. The cboe name was missed when
# wave 37 shipped -- its test plane covered nine files and this one was not among them -- so the
# omission is corrected here rather than presented as never having existed. Wave 39 adds
# ``cboe_futures_curve`` on the same shape: one leg off Cboe's public settlement CSV, transient,
# unverified, and named here in the same wave that registers it rather than in a later correction.
NATIVE_MODEL_DOMAINS: frozenset[str] = frozenset(
    {
        "alpha_vantage_equity_historical",
        "alpha_vantage_etf_historical",
        "alpha_vantage_historical_eps",
        "balance_of_payments",
        "benzinga_analyst_search",
        "benzinga_company_news",
        "benzinga_price_target",
        "benzinga_world_news",
        "bls_search",
        "bls_series",
        "bond_indices",
        "cboe_available_indices",
        "cboe_equity_historical",
        "cboe_equity_quote",
        "cboe_equity_search",
        "cboe_etf_historical",
        "cboe_futures_curve",
        "cboe_index_constituent_quotes",
        "cboe_index_historical",
        "cboe_index_search",
        "cboe_index_snapshots",
        "cboe_options_chains",
        "cftc_commitment_of_traders",
        "cftc_market_directory",
        "commercial_paper",
        "commodity_spot_prices",
        "congress_amendment_info",
        "congress_amendment_text",
        "congress_amendments",
        "congress_bill_info",
        "congress_bill_text",
        "congress_bills",
        "congress_committee_documents",
        "congress_committee_info",
        "currency_reference_rates",
        "deribit_futures_curve",
        "deribit_futures_historical",
        "deribit_futures_info",
        "deribit_futures_instruments",
        "deribit_options_chains",
        "econdb_available_indicators",
        "econdb_economic_indicators",
        "econdb_gdp_nominal",
        "econdb_gdp_real",
        "econdb_port_volume",
        "econdb_yield_curve",
        "economic_calendar",
        "economy_imf_available_indicators",
        "economy_imf_cpi",
        "economy_imf_direction_of_trade",
        "economy_imf_indicators",
        "economy_oecd_cli",
        "economy_oecd_cpi",
        "economy_oecd_gdp_forecast",
        "economy_oecd_gdp_nominal",
        "economy_oecd_gdp_real",
        "economy_oecd_house_price_index",
        "economy_oecd_interest_rates",
        "economy_oecd_share_price_index",
        "economy_oecd_unemployment",
        "energy_petroleum_status_report",
        "energy_short_term_energy_outlook",
        "equity_historical",
        "equity_quote",
        "euro_short_term_rate",
        "famafrench_breakpoints",
        "famafrench_country_portfolio_returns",
        "famafrench_factors",
        "famafrench_international_index_returns",
        "famafrench_regional_portfolio_returns",
        "famafrench_us_portfolio_returns",
        "federal_reserve_central_bank_holdings",
        "federal_reserve_federal_funds_rate",
        "federal_reserve_fomc_documents",
        "federal_reserve_inflation_expectations",
        "federal_reserve_money_measures",
        "federal_reserve_overnight_bank_funding_rate",
        "federal_reserve_primary_dealer_fails",
        "federal_reserve_primary_dealer_positioning",
        "federal_reserve_sofr",
        "federal_reserve_svensson_yield_curve",
        "federal_reserve_total_factor_productivity",
        "federal_reserve_treasury_rates",
        "federal_reserve_yield_curve",
        "finra_equity_short_interest",
        "finra_otc_aggregate",
        "finviz_compare_groups",
        "finviz_equity_info",
        "finviz_equity_screener",
        "finviz_etf_price_performance",
        "finviz_key_metrics",
        "finviz_price_performance",
        "finviz_price_target",
        "fmp_analyst_estimates",
        "fmp_balance_sheet",
        "fmp_balance_sheet_growth",
        "fmp_calendar_events",
        "fmp_cash_flow_statement",
        "fmp_cash_flow_statement_growth",
        "fmp_company_filings",
        "fmp_company_news",
        "fmp_crypto_historical",
        "fmp_crypto_search",
        "fmp_currency_historical",
        "fmp_currency_pairs",
        "fmp_currency_snapshots",
        "fmp_discovery_filings",
        "fmp_dividend_calendar",
        "fmp_earnings_calendar",
        "fmp_earnings_call_transcript",
        "fmp_economic_calendar",
        "fmp_equity_active",
        "fmp_equity_gainers",
        "fmp_equity_info",
        "fmp_equity_losers",
        "fmp_equity_ownership",
        "fmp_equity_peers",
        "fmp_equity_screener",
        "fmp_esg_score",
        "fmp_etf_countries",
        "fmp_etf_equity_exposure",
        "fmp_etf_historical",
        "fmp_etf_holdings",
        "fmp_etf_info",
        "fmp_etf_price_performance",
        "fmp_etf_search",
        "fmp_etf_sectors",
        "fmp_executive_compensation",
        "fmp_financial_ratios",
        "fmp_forward_ebitda_estimates",
        "fmp_forward_eps_estimates",
        "fmp_government_trades",
        "fmp_historical_dividends",
        "fmp_historical_employees",
        "fmp_historical_eps",
        "fmp_historical_market_cap",
        "fmp_historical_splits",
        "fmp_income_statement",
        "fmp_income_statement_growth",
        "fmp_index_constituents",
        "fmp_index_directory",
        "fmp_index_historical",
        "fmp_insider_trading",
        "fmp_institutional_ownership",
        "fmp_ipo_calendar",
        "fmp_key_executives",
        "fmp_key_metrics",
        "fmp_market_risk_premium",
        "fmp_market_snapshots",
        "fmp_nport_disclosure",
        "fmp_price_performance",
        "fmp_price_target",
        "fmp_price_target_consensus",
        "fmp_revenue_business_line",
        "fmp_revenue_geographic",
        "fmp_share_statistics",
        "fmp_splits_calendar",
        "fmp_treasury_rates",
        "fmp_world_news",
        "fmp_yield_curve",
        "fred_ameribor",
        "fred_balance_of_payments",
        "fred_consumer_price_index",
        "fred_discount_window_primary_credit_rate",
        "fred_economic_calendar",
        "fred_european_central_bank_interest_rates",
        "fred_federal_funds_rate",
        "fred_manufacturing_outlook_ny",
        "fred_manufacturing_outlook_texas",
        "fred_mortgage_indices",
        "fred_nonfarm_payrolls",
        "fred_overnight_bank_funding_rate",
        "fred_personal_consumption_expenditures",
        "fred_projections",
        "fred_regional",
        "fred_release_table",
        "fred_retail_prices",
        "fred_search",
        "fred_selected_treasury_bill",
        "fred_selected_treasury_constant_maturity",
        "fred_series",
        "fred_survey_of_economic_conditions_chicago",
        "fred_tips_yields",
        "fred_treasury_constant_maturity",
        "fred_university_of_michigan",
        "fred_yield_curve",
        "government_us_commodity_psd_data",
        "government_us_commodity_psd_report",
        "government_us_treasury_auctions",
        "government_us_treasury_prices",
        "government_us_weather_bulletin",
        "government_us_weather_bulletin_download",
        "hqm_corporate_bond",
        "imf_maritime_chokepoint_info",
        "imf_maritime_chokepoint_volume",
        "imf_port_info",
        "imf_port_volume",
        "intrinio_balance_sheet",
        "intrinio_calendar_ipo",
        "intrinio_cash_flow_statement",
        "intrinio_company_filings",
        "intrinio_company_news",
        "intrinio_currency_pairs",
        "intrinio_equity_historical",
        "intrinio_equity_info",
        "intrinio_equity_quote",
        "intrinio_equity_search",
        "intrinio_etf_historical",
        "intrinio_etf_holdings",
        "intrinio_etf_info",
        "intrinio_etf_price_performance",
        "intrinio_etf_search",
        "intrinio_financial_ratios",
        "intrinio_forward_ebitda_estimates",
        "intrinio_forward_eps_estimates",
        "intrinio_forward_pe_estimates",
        "intrinio_forward_sales_estimates",
        "intrinio_fred_series",
        "intrinio_historical_attributes",
        "intrinio_historical_dividends",
        "intrinio_historical_market_cap",
        "intrinio_income_statement",
        "intrinio_index_historical",
        "intrinio_insider_trading",
        "intrinio_key_metrics",
        "intrinio_latest_attributes",
        "intrinio_market_snapshots",
        "intrinio_options_chains",
        "intrinio_options_unusual",
        "intrinio_price_target_consensus",
        "intrinio_reported_financials",
        "intrinio_search_attributes",
        "intrinio_share_statistics",
        "intrinio_world_news",
        "iorb",
        "nasdaq_calendar_dividend",
        "nasdaq_calendar_earnings",
        "nasdaq_calendar_ipo",
        "nasdaq_company_filings",
        "nasdaq_economic_calendar",
        "nasdaq_equity_screener",
        "nasdaq_equity_search",
        "nasdaq_historical_dividends",
        "nasdaq_top_retail",
        "sec_balance_sheet",
        "sec_balance_sheet_growth",
        "sec_cash_flow_statement",
        "sec_cash_flow_statement_growth",
        "sec_cik_map",
        "sec_company_filings",
        "sec_compare_company_facts",
        "sec_equity_ftd",
        "sec_equity_search",
        "sec_filing",
        "sec_filings",
        "sec_form_13f_hr",
        "sec_htm_file",
        "sec_income_statement",
        "sec_income_statement_growth",
        "sec_insider_trading",
        "sec_institutions_search",
        "sec_latest_financial_reports",
        "sec_management_discussion_analysis",
        "sec_nport_disclosure",
        "sec_rss_litigation",
        "sec_schema_files",
        "sec_sic_search",
        "sec_symbol_map",
        "seeking_alpha_calendar_earnings",
        "senior_loan_officer_survey",
        "sofr",
        "sonia",
        "sp500_multiples",
        "spot_rate",
        "tiingo_company_news",
        "tiingo_crypto_historical",
        "tiingo_currency_historical",
        "tiingo_equity_historical",
        "tiingo_etf_historical",
        "tiingo_trailing_dividend_yield",
        "tiingo_world_news",
        "tmx_available_indices",
        "tmx_bond_prices",
        "tmx_calendar_earnings",
        "tmx_company_filings",
        "tmx_company_news",
        "tmx_equity_gainers",
        "tmx_equity_historical",
        "tmx_equity_info",
        "tmx_equity_quote",
        "tmx_equity_search",
        "tmx_etf_countries",
        "tmx_etf_historical",
        "tmx_etf_holdings",
        "tmx_etf_info",
        "tmx_etf_search",
        "tmx_etf_sectors",
        "tmx_historical_dividends",
        "tmx_index_constituents",
        "tmx_index_sectors",
        "tmx_index_snapshots",
        "tmx_insider_trading",
        "tmx_options_chains",
        "tmx_price_target_consensus",
        "tmx_treasury_prices",
        "tradier_equity_historical",
        "tradier_equity_quote",
        "tradier_equity_search",
        "tradier_etf_historical",
        "tradier_options_chains",
        "yfinance_balance_sheet",
        "yfinance_cash_flow_statement",
        "yfinance_company_news",
        "yfinance_crypto_historical",
        "yfinance_currency_historical",
        "yfinance_dividend_history",
        "yfinance_equity_active",
        "yfinance_equity_aggressive_small_caps",
        "yfinance_equity_gainers",
        "yfinance_equity_historical",
        "yfinance_equity_info",
        "yfinance_equity_losers",
        "yfinance_equity_quote",
        "yfinance_equity_screener",
        "yfinance_equity_undervalued_growth",
        "yfinance_equity_undervalued_large_caps",
        "yfinance_etf_historical",
        "yfinance_etf_info",
        "yfinance_futures_curve",
        "yfinance_futures_historical",
        "yfinance_growth_tech_equities",
        "yfinance_income_statement",
        "yfinance_index_historical",
        "yfinance_key_executives",
        "yfinance_key_metrics",
        "yfinance_options_chains",
        "yfinance_price_target_consensus",
        "yfinance_share_statistics",
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
