"""What only the sec package can state: the accounting over its 24 upstream rows.

The engine's promise is that a model is either declared or recorded as inexpressible, and this
file judges the second half. sec's package must account for exactly the rows the pinned upstream
provider dictionary binds (``providers/sec/openbb_sec/__init__.py:36-59``, mirrored by the
``OBB2-sec-*`` ids in the iteration-2 ledger), refuse a row that is neither declared nor recorded,
refuse one that is both, and refuse a recorded row whose blocker is a claim about effort rather
than a capability the declaration format lacks.

No URL, column or parameter is re-derived here -- those claims live in the declarations and in
:data:`opendata.data.providers.sec.specs.UPSTREAM_EVIDENCE`, which every blocker must cite. What
is checked is the bookkeeping only this package can be trusted with, plus the one fair-access
fact the SEC endpoints are always asked about: nothing in this package claims a live response.
"""

from __future__ import annotations

from pathlib import Path

from opendata.data.providers.catalog import (
    PROVIDERS,
    engine_declared_models,
    get_provider,
    register_provider,
)
from opendata.data.providers.sec import specs
from opendata.data.providers.sec._source import SOURCE
from opendata.data.providers.sec.provider import PROVIDER
from opendata.data.registry import ProviderRegistry
from scripts.quality.provider_model_inventory import LEDGER_RELATIVE_PATH, load_task_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The sentence ``catalog._reserved_provider()`` writes for a source with no local implementation.
RESERVED_SENTENCE = "Catalog identity reserved; no local fetcher is implemented."

#: The sec denominator: the ledger rows and the upstream dictionary's 24 keys.
UPSTREAM_ROW_COUNT = 24


def ledger_sec_rows() -> list[str]:
    """Return the upstream model names the iteration-2 ledger fixes for sec."""
    return [
        row["upstream_model"]
        for row in load_task_ledger(REPO_ROOT / LEDGER_RELATIVE_PATH)
        if row["provider"] == "sec"
    ]


class TestSecIsARealProvider:
    """sec is a locally authored descriptor, not a catalog placeholder."""

    def test_descriptor_is_not_the_reserved_placeholder(self) -> None:
        provider = get_provider("sec")
        assert provider is PROVIDER
        assert provider.description != RESERVED_SENTENCE
        assert "reserved" not in provider.description.lower()
        assert provider.website == "https://www.sec.gov/"

    def test_no_credential_is_declared(self) -> None:
        """EDGAR's public JSON needs no key, so health can never ask for one."""
        assert PROVIDER.credentials == ()

    def test_the_routing_label_is_the_package_name(self) -> None:
        assert SOURCE == "sec" == PROVIDER.source
        assert any(provider.source == "sec" for provider in PROVIDERS)


class TestTheTwentyFourRowAccounting:
    """Declared plus recorded equals the upstream rows, exactly once each."""

    def test_package_rows_are_the_ledger_rows(self) -> None:
        assert sorted(specs.UPSTREAM_ROWS) == sorted(ledger_sec_rows())
        assert len(specs.UPSTREAM_ROWS) == UPSTREAM_ROW_COUNT
        assert len(set(specs.UPSTREAM_ROWS)) == UPSTREAM_ROW_COUNT

    def test_every_row_is_declared_or_recorded_once(self) -> None:
        declared = set(specs.DECLARED_MODELS)
        recorded = set(specs.NOT_DECLARABLE)
        assert declared.isdisjoint(recorded), "a row is both declared and recorded"
        assert declared | recorded == set(specs.UPSTREAM_ROWS), (
            "the package does not account for every upstream row: "
            f"{sorted(set(specs.UPSTREAM_ROWS) - declared - recorded)}"
        )
        assert len(declared) + len(recorded) == UPSTREAM_ROW_COUNT

    def test_recorded_blockers_cite_upstream_code(self) -> None:
        """A blocker is a measurement, so it must point at the line that shows it."""
        assert set(specs.UPSTREAM_EVIDENCE) == set(specs.NOT_DECLARABLE)
        for model, reason in specs.NOT_DECLARABLE.items():
            assert reason.strip(), model
            assert ".py:" in reason, f"{model}: the blocker cites no upstream line"
            assert len(reason) <= 320, f"{model}: a blocker a reviewer cannot hold is a design note"

    def test_the_fair_access_header_is_not_the_blocker(self) -> None:
        """Upstream's identifying ``User-Agent`` is a literal default (utils/definitions.py:10).

        So ``static_headers`` can carry it verbatim and no row may be recorded as blocked by a
        header; the blocking capabilities are the document shapes and request counts instead.
        """
        assert "@" in specs.UPSTREAM_USER_AGENT
        for model, reason in specs.NOT_DECLARABLE.items():
            lowered = reason.lower()
            assert "user-agent" not in lowered, model
            assert "static_header" not in lowered, model
            assert "accept-encoding" not in lowered, model

    def test_provider_bindings_and_declarations_stay_in_step(self) -> None:
        """The descriptor binds one generated fetcher per declaration, never a hand-written one."""
        assert len(PROVIDER.fetcher_bindings) == len(specs.DECLARED_MODELS)
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert bound == set(specs.DECLARED_MODELS)


class TestNothingHereIsLiveVerified:
    """No sec declaration claims a source-verified response."""

    def test_discovery_through_the_catalog_matches_the_package(self) -> None:
        found = {spec.model for source, spec in engine_declared_models() if source == "sec"}
        assert found == set(specs.DECLARED_MODELS)

    def test_generated_fetchers_do_not_claim_verification(self) -> None:
        for fetcher in PROVIDER.fetchers:
            assert fetcher.capability.verified is False
            assert fetcher.capability.source == "sec"

    def test_registration_verifies_nothing(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("sec", registry)
        assert len(capabilities) == len(PROVIDER.fetcher_bindings)
        assert all(capability.source == "sec" for capability in capabilities)
        assert not any(capability.verified for capability in capabilities)
