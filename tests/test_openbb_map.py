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


@pytest.fixture(autouse=True)
def _clear_cache():
    load_openbb_map.cache_clear()
    yield
    load_openbb_map.cache_clear()


class TestShippedMap:
    def test_loads_and_validates(self):
        entries = load_openbb_map()

        assert entries
        assert all(entry.scenario for entry in entries)

    def test_confirmed_models_are_never_guesses(self):
        confirmed = {entry.model for entry in load_openbb_map() if entry.model != "TBD"}

        # 这些名字来自对上游 fetcher_dict 声明的机械解析（provider-inventory.yaml）
        assert confirmed <= {"EquityHistorical", "ConsumerPriceIndex", "FuturesHistorical"}

    def test_enabled_capabilities_are_covered(self):
        """AC-10 准入：每个已启用 provider×domain 必须在对照表中。"""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        registered = {
            (capability.source, capability.domain) for capability in get_registry().capabilities()
        }

        assert registered <= covered_capabilities()

    def test_tbd_entries_are_explicit(self):
        tbd = [entry for entry in load_openbb_map() if entry.model == "TBD"]

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
