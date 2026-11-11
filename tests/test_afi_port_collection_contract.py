"""Contract tests for the ported AFI collection-script tree.

These pin the things the killable worker and script service rely on at
runtime, without making any network or warehouse call:

* every ported collection module imports under its ``opendata.*`` name;
* the two AFI entrypoint shapes (module-level ``main`` and pure-class
  ``fetch_data``/``run``) both resolve to a callable via the same helper
  the runtime uses;
* the resolved instance exposes the ``table_name`` the writer targets.
"""

from __future__ import annotations

import importlib

import pytest

from opendata.afi_port.services.script import AkshareScriptService
from opendata.data_fetch.providers.akshare_to_mysql import AkshareToMySql


@pytest.fixture(scope="module")
def service() -> AkshareScriptService:
    """A service instance with no DB session, for the pure resolver helpers."""
    return AkshareScriptService.__new__(AkshareScriptService)


def test_ported_modules_import_under_opendata_namespace() -> None:
    """Representative leaves from each family import with no app.* residue."""
    modules = (
        "opendata.data_fetch.scripts.stocks.daily.stock_zh_a_hist_legacy",
        "opendata.data_fetch.scripts.funds.weekly.money_fund_hist_em",
        "opendata.data_fetch.scripts.common.dict_result",
    )
    for name in modules:
        module = importlib.import_module(name)
        assert module.__name__ == name


def test_module_main_entrypoint_resolves(service: AkshareScriptService) -> None:
    """A module-level main() dispatching to fetch_data resolves to a callable."""
    module = importlib.import_module(
        "opendata.data_fetch.scripts.stocks.daily.stock_zh_a_hist_legacy"
    )
    resolved = service._resolve_module_callable(module, "main")
    assert callable(resolved)
    assert resolved.__qualname__ == "StockZhAHist.fetch_data"
    instance = service._legacy_script_instance(module)
    assert isinstance(instance, AkshareToMySql)
    assert getattr(instance, "table_name", None)


def test_pure_class_run_entrypoint_resolves(service: AkshareScriptService) -> None:
    """A no-main, pure-class module resolves its run() entrypoint."""
    module = importlib.import_module("opendata.data_fetch.scripts.funds.weekly.money_fund_hist_em")
    assert not callable(getattr(module, "main", None))
    resolved = service._resolve_module_callable(module, "main")
    assert callable(resolved)
    assert resolved.__qualname__ == "MoneyFundHistEm.run"


def test_worker_descriptor_builder_handles_a_ported_entrypoint() -> None:
    """The killable worker's descriptor builder accepts a ported entrypoint."""
    from opendata.afi_port.runtime import script_worker_runtime as runtime

    module = importlib.import_module(
        "opendata.data_fetch.scripts.stocks.daily.stock_zh_a_hist_legacy"
    )
    descriptor = runtime._callable_descriptor(module.main)
    assert descriptor["module"].startswith("opendata.data_fetch.scripts.")
    assert descriptor["qualname"] == "main"
