"""OpenBB compatibility map tests (FR-7 / AC-10).

Schema and fail-closed loading, the acceptance rule that every enabled
provider appears with a consuming scenario, and the domain rule that only
pending rows may name domains the registry does not know yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opendata.data.openbb_map import (
    OpenBBMapError,
    covered_capabilities,
    load_openbb_map,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_MAP_PATH = _REPO_ROOT / "opendata/data/openbb_map.yaml"
_INVENTORY_PATH = _REPO_ROOT / "docs/proposals/openbb-migration/provider-inventory.yaml"


def _confirmed_name_universe() -> set[str]:
    """Names traceable to a real source carrier: legacy v1 map, inventory model lists,
    and the registry projection for inventory `local_providers:` rows (ths/akshare)."""
    universe: set[str] = set()

    legacy = yaml.safe_load(_LEGACY_MAP_PATH.read_text(encoding="utf-8"))
    universe.update(row["openbb"]["model"] for row in legacy["entries"])

    inventory = yaml.safe_load(_INVENTORY_PATH.read_text(encoding="utf-8"))
    for row in inventory["providers"]:
        universe.update(row.get("models") or [])

    local_sources = {row["provider"] for row in inventory["local_providers"]}

    from opendata.data.providers.catalog import list_providers

    for provider in list_providers():
        if provider.source in local_sources:
            for binding in provider.fetcher_bindings:
                universe.update(binding.canonical_model_ids or (binding.model_key,))
    universe.discard("TBD")
    return universe


def _untraceable(confirmed: set[str], universe: set[str]) -> list[str]:
    return sorted(confirmed - universe)


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

        universe = _confirmed_name_universe()
        print(f"\nconfirmed={len(confirmed)} derived-universe={len(universe)}")
        assert _untraceable(confirmed, universe) == []

        coined = confirmed | {"ZzConfirmedModelThatDoesNotExist"}
        assert _untraceable(coined, universe) == ["ZzConfirmedModelThatDoesNotExist"]

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

        # The seven reviewed rows keep their exact statuses; the iter2 projection wave (360c61d)
        # only added registered fred rows on top of them.
        assert fred_rows >= {
            ("economy_cpi", "verified"),
            ("economy_gdp", "verified"),
            ("economy_unemployment", "verified"),
            ("fred_search", "registered"),
            ("fred_series", "registered"),
            ("sofr", "registered"),
            ("sonia", "registered"),
        }
        # Nothing added by the wave may self-certify: only the three officially checked
        # domains stay verified, every other fred row must be registered.
        assert {domain for domain, status in fred_rows if status == "verified"} == {
            "economy_cpi",
            "economy_gdp",
            "economy_unemployment",
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
