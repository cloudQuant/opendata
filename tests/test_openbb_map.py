"""OpenBB compatibility map tests (FR-7 / AC-10).

Schema and fail-closed loading, the acceptance rule that every enabled
provider appears with a consuming scenario, and the domain rule that only
pending rows may name domains the registry does not know yet.
"""

from __future__ import annotations

import pytest

from opendata.data.openbb_map import (
    OpenBBMapError,
    covered_capabilities,
    load_openbb_map,
)


def _write(tmp_path, body: str):
    path = tmp_path / "openbb_map.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _load_shipped_map():
    """Load the registry projection after explicitly registering local providers."""
    from opendata.data.providers import register_providers

    register_providers()
    return load_openbb_map()


@pytest.fixture(autouse=True)
def _clear_cache():
    load_openbb_map.cache_clear()
    yield
    load_openbb_map.cache_clear()


class TestShippedMap:
    def test_loads_and_validates(self):
        entries = _load_shipped_map()

        assert entries
        assert all(entry.scenario for entry in entries)

    def test_confirmed_models_are_never_guesses(self):
        confirmed = {entry.model for entry in _load_shipped_map() if entry.model != "TBD"}

        # 除新接入的 ECB 本地 binding 外，这些名字来自上游 fetcher_dict 的机械解析
        # （provider-inventory.yaml）：
        # EquityHistorical/ConsumerPriceIndex/FuturesHistorical/EtfHistorical
        # （后者由 alpha_vantage、tiingo、cboe 三个 provider 声明）
        # 三个 ECB 名称则由本地受控 binding 明确注册。
        # AvailableIndices/IndexConstituents 同样出自该清单的 cboe 行
        # （`models: [AvailableIndices, …, IndexConstituents, …]`），本轮把这两条做成了
        # 声明式引擎声明；两条能力 verified=False，不进 auto 路由。
        # IndexSearch 与两条 federal_reserve 名称没有上游 fetcher_dict 行可抄：它们由
        # registry 投影（list_model_descriptors）带进 map，出处是本地 provider.py/specs.py
        # 的 scenario 声明，同样 verified=False。
        assert confirmed == {
            "EquityHistorical",
            "ConsumerPriceIndex",
            "FuturesHistorical",
            "EtfHistorical",
            "FredSearch",
            "FredSeries",
            "SOFR",
            "BlsSearch",
            "BlsSeries",
            "EquityQuote",
            "CurrencyReferenceRates",
            "YieldCurve",
            "BalanceOfPayments",
            "SONIA",
            "AvailableIndices",
            "IndexConstituents",
            "IndexSearch",
            "MoneyMeasures",
            "TreasuryRates",
        }

    def test_enabled_capabilities_are_covered(self):
        """AC-10 准入：每个已启用 provider×domain 必须在对照表中。"""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        registered = {
            (capability.source, capability.domain) for capability in get_registry().capabilities()
        }

        assert registered == covered_capabilities()

    def test_fred_officially_checked_domains_are_marked_verified(self):
        fred_rows = {
            (ours.domain, ours.status)
            for entry in _load_shipped_map()
            for ours in entry.ours
            if ours.provider == "fred"
        }

        assert fred_rows == {
            ("economy_cpi", "verified"),
            ("economy_gdp", "verified"),
            ("economy_unemployment", "verified"),
            ("fred_search", "registered"),
            ("fred_series", "registered"),
            ("sofr", "registered"),
            ("sonia", "registered"),
        }

    def test_tbd_entries_are_explicit(self):
        tbd = [entry for entry in _load_shipped_map() if entry.model == "TBD"]

        assert tbd  # 财务/指数成分的 OpenBB 模型名尚未从上游确认
        assert all(entry.scenario for entry in tbd)


class TestFailClosed:
    def test_missing_file(self, tmp_path):
        with pytest.raises(OpenBBMapError, match="unreadable"):
            load_openbb_map(str(tmp_path / "absent.yaml"))

    def test_bad_version(self, tmp_path):
        path = _write(tmp_path, "version: 2\nentries: []\n")

        with pytest.raises(OpenBBMapError, match="version 1"):
            load_openbb_map(path)

    def test_empty_entries(self, tmp_path):
        path = _write(tmp_path, "version: 1\nentries: []\n")

        with pytest.raises(OpenBBMapError, match="non-empty entries"):
            load_openbb_map(path)

    def test_entry_without_scenario(self, tmp_path):
        path = _write(
            tmp_path,
            "version: 1\n"
            "entries:\n"
            "  - openbb: {model: EquityHistorical}\n"
            "    ours:\n"
            "      - {provider: ths, domain: stock_daily, contract: Bar, status: verified}\n",
        )

        with pytest.raises(OpenBBMapError, match="scenario"):
            load_openbb_map(path)

    def test_unknown_status(self, tmp_path):
        path = _write(
            tmp_path,
            "version: 1\n"
            "entries:\n"
            "  - openbb: {model: EquityHistorical}\n"
            "    scenario: 行情\n"
            "    ours:\n"
            "      - {provider: ths, domain: stock_daily, contract: Bar, status: live}\n",
        )

        with pytest.raises(OpenBBMapError, match="unknown status"):
            load_openbb_map(path)

    def test_registered_row_needs_a_registered_domain(self, tmp_path):
        path = _write(
            tmp_path,
            "version: 1\n"
            "entries:\n"
            "  - openbb: {model: EquityHistorical}\n"
            "    scenario: 行情\n"
            "    ours:\n"
            "      - {provider: ths, domain: not_a_domain, contract: Bar, status: registered}\n",
        )

        with pytest.raises(OpenBBMapError, match="unregistered domain"):
            load_openbb_map(path)

    def test_pending_row_may_name_an_unregistered_domain(self, tmp_path):
        path = _write(
            tmp_path,
            "version: 1\n"
            "entries:\n"
            "  - openbb: {model: ConsumerPriceIndex}\n"
            "    scenario: 宏观\n"
            "    ours:\n"
            "      - {provider: fred, domain: economy.cpi, contract: MacroSeries, "
            "status: pending, batch: P0}\n",
        )

        entries = load_openbb_map(path)

        assert entries[0].ours[0].batch == "P0"

    def test_batch_is_only_for_pending_rows(self, tmp_path):
        path = _write(
            tmp_path,
            "version: 1\n"
            "entries:\n"
            "  - openbb: {model: EquityHistorical}\n"
            "    scenario: 行情\n"
            "    ours:\n"
            "      - {provider: ths, domain: stock_daily, contract: Bar, "
            "status: verified, batch: P0}\n",
        )

        with pytest.raises(OpenBBMapError, match="batch"):
            load_openbb_map(path)
