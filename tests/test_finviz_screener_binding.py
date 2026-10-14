"""Guarded offline checks for Finviz's partial screener central binding."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from collections import Counter
    from collections.abc import Iterator, Mapping

    from opendata.data.domains import DomainSpec
    from opendata.data.models import FinvizEquityScreener
    from opendata.data.providers.catalog import Provider

GUARD_DIR = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/C100/acceptance-federal-reserve-clean-extra"
)
_GUARD_REQUIRED = os.environ.get("OPENDATA_REQUIRE_FINVIZ_PREIMPORT_GUARD") == "1"


class _HardIOGuard(Protocol):
    """Describe only the guard state inspected by this binding test."""

    blocked_attempts: Counter[str]
    saved_original_dispatches: Counter[str]

    def install(self) -> None: ...

    def summary(self) -> dict[str, object]: ...


IO_GUARD: _HardIOGuard | None = None
_BOOTSTRAP_DBAPI_MODULES: object | None = None
_BOOTSTRAP_BLOCKED_ATTEMPTS: Mapping[str, int] | None = None


def _accepted_bootstrap_blocked_attempts(value: object) -> dict[str, int] | None:
    """Copy only an empty bootstrap counter or the known single bind denial."""
    if type(value) is not dict:
        return None
    attempts: dict[object, object] = value
    if not attempts:
        return {}
    if len(attempts) != 1:
        return None
    surface, count = next(iter(attempts.items()))
    if type(surface) is not str or surface != "socket.bind":
        return None
    if type(count) is not int or count != 1:
        return None
    return {"socket.bind": 1}


def _matches_bootstrap_blocked_attempts(current: object, bootstrap: Mapping[str, int]) -> bool:
    """Require the post-import counter to equal the saved bootstrap prefix."""
    attempts = _accepted_bootstrap_blocked_attempts(current)
    return attempts is not None and attempts == bootstrap


if _GUARD_REQUIRED:
    os.environ["TESTING"] = "true"
    sys.path.insert(0, str(GUARD_DIR))
    # Ordinary pytest collection must not install process-wide I/O patches.
    from hard_io_guard import HardIOGuard

    IO_GUARD = HardIOGuard()
    IO_GUARD.install()
    _BOOTSTRAP_SUMMARY = IO_GUARD.summary()
    if _BOOTSTRAP_SUMMARY["installed"] is not True:
        raise RuntimeError("the required Finviz binding I/O guard did not install")
    if _BOOTSTRAP_SUMMARY["opendata_modules_imported"] != []:
        raise RuntimeError("opendata modules were imported before the Finviz binding guard")
    bootstrap_blocked_attempts = _accepted_bootstrap_blocked_attempts(
        _BOOTSTRAP_SUMMARY["blocked_attempts"]
    )
    if bootstrap_blocked_attempts is None:
        raise RuntimeError("the Finviz binding guard recorded an unapproved bootstrap attempt")
    _BOOTSTRAP_BLOCKED_ATTEMPTS = MappingProxyType(bootstrap_blocked_attempts)
    if _BOOTSTRAP_SUMMARY["saved_original_dispatches"] != {}:
        raise RuntimeError("the Finviz binding guard saved a dispatch during installation")
    if _BOOTSTRAP_SUMMARY["saved_original_dispatch_total"] != 0:
        raise RuntimeError("the Finviz binding guard dispatched during installation")
    if not isinstance(_BOOTSTRAP_SUMMARY["loaded_dbapi_modules"], list):
        raise RuntimeError("the Finviz binding guard did not report its DBAPI baseline")
    _BOOTSTRAP_DBAPI_MODULES = _BOOTSTRAP_SUMMARY["loaded_dbapi_modules"]
    if "opendata.main" in sys.modules:
        raise RuntimeError("opendata.main was imported before the Finviz binding guard")

    from opendata.data.domains import load_domains
    from opendata.data.models import FinvizEquityScreener
    from opendata.data.providers.catalog import get_provider

pytestmark = pytest.mark.skipif(
    not _GUARD_REQUIRED,
    reason="run only with OPENDATA_REQUIRE_FINVIZ_PREIMPORT_GUARD=1 in isolation",
)


def test_finviz_screener_has_one_lazy_partial_canonical_binding() -> None:
    """The central descriptor exposes exactly one typed first-page subset."""
    provider: Provider = get_provider("finviz")
    bindings = [
        binding
        for binding in provider.fetcher_bindings
        if "EquityScreener" in binding.canonical_model_ids
    ]

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.module == "opendata.data.providers.finviz.models.fetchers"
    assert binding.class_name == "FinvizEquityScreenerFetcher"
    assert binding.canonical_model_ids == ("EquityScreener",)
    assert binding.scenario is not None
    for phrase in ("第一页", "20 行", "11 字段", "子集", "非完整 61 字段"):
        assert phrase in binding.scenario
    assert provider.fetcher_dict["EquityScreener"] is binding
    assert binding.module not in sys.modules

    fetcher = binding.load("finviz")
    assert binding.module in sys.modules
    capability = fetcher.capability
    assert type(fetcher).__name__ == "FinvizEquityScreenerFetcher"
    assert fetcher.canonical_model == "EquityScreener"
    assert capability.source == "finviz"
    assert capability.market == "us"
    assert capability.domain == "finviz_equity_screener"
    assert capability.period == "snapshot"
    assert capability.verified is False
    assert "at most 20 source rows" in capability.notes
    assert "11-column subset" in capability.notes
    assert "full 61-field screener contract" in capability.notes

    model: type[FinvizEquityScreener] = FinvizEquityScreener
    domain: DomainSpec = load_domains()["finviz_equity_screener"]
    assert domain.contract == model.__name__
    assert domain.temporal_kind == "snapshot"
    assert domain.time_field is None
    assert domain.natural_key == ("rank", "symbol")
    assert domain.filter_dims == ()
    assert domain.storage_mode == "transient"
    assert domain.permissions == ("query",)


@pytest.fixture(autouse=True)
def _assert_no_io_attempts() -> Iterator[None]:
    """Require import and test execution to leave the isolated guard clean."""
    guard = IO_GUARD
    if guard is None:
        yield
        return

    summary = guard.summary()
    bootstrap_blocked_attempts = _BOOTSTRAP_BLOCKED_ATTEMPTS
    assert bootstrap_blocked_attempts is not None
    assert _matches_bootstrap_blocked_attempts(
        summary["blocked_attempts"], bootstrap_blocked_attempts
    )
    assert summary["saved_original_dispatches"] == {}
    assert summary["saved_original_dispatch_total"] == 0
    assert summary["loaded_dbapi_modules"] == _BOOTSTRAP_DBAPI_MODULES
    assert "opendata.main" not in sys.modules
    yield
    summary = guard.summary()
    assert _matches_bootstrap_blocked_attempts(
        summary["blocked_attempts"], bootstrap_blocked_attempts
    )
    assert summary["saved_original_dispatches"] == {}
    assert summary["saved_original_dispatch_total"] == 0
    assert summary["loaded_dbapi_modules"] == _BOOTSTRAP_DBAPI_MODULES
    assert "opendata.main" not in sys.modules
