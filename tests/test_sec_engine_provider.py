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

The file's second half re-checks the three rows ``capability-roadmap.json`` lists as covered by
already-shipped capabilities, and refuses them from the census rather than from prose: every
blocker has to name the need its census row records and cite that row's own proof line, and every
recorded need is then re-measured against the live engine offline. So a blocker that stops being
true fails here instead of quietly becoming a fabricated declaration. Those probes are synthetic
fixtures on a reserved, non-resolving origin -- they claim no SEC endpoint and no published column.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from opendata.data.providers._engine.decoders import ResponseDecodeError
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import ColumnSpec, DecoderSpec, ModelSpec
from opendata.data.providers._engine.testing import SyntheticTransport, fixture_context
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

#: The per-model survey the declarations must obey: one row per provider×model task, carrying the
#: endpoint shape, the capability the engine would need and the ``expressible_today`` flag.
CENSUS_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-sec-tmx-fed-gov-finra.json"
)

#: The roadmap artifact listing rows whose residual engine need labels name shipped capabilities.
ROADMAP_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json"
)

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
        from_ledger = ledger_sec_rows()
        assert sorted(specs.UPSTREAM_ROWS) == sorted(from_ledger)
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


#: ---- the three rows ``capability-roadmap.json`` lists as already covered, re-measured here -----
#:
#: Every fixture below is a SYNTHETIC capability probe written by this file. It claims no SEC
#: endpoint: the origin is the RFC 2606 reserved ``.test`` TLD, which cannot resolve, and no column
#: name in it is offered as a published sec field. A probe exists to be executed against the live
#: engine, so a blocker recorded in prose cannot go stale unnoticed.
PROBE_BASE_URL = "https://sec-refusal-probe.test"
PROBE_ERROR_PREFIX = "SECPROBE"
PROBE_DOCUMENT_URL = f"{PROBE_BASE_URL}/probe/document"
PROBE_FILER_LIST_URL = f"{PROBE_BASE_URL}/probe/filer-list"

#: An XBRL-style nesting: the observations sit in a list keyed by the *unit*, so the pointer that
#: addresses the tagged concept lands on a mapping. That is what ``xbrl_tag_assembly`` means.
UNIT_NESTED_DOCUMENT = {
    "units": {"usd": [{"value": 1.5}, {"value": 2.5}]},
}

#: A colon-separated body with no header row, exactly the shape the recorded need names: the
#: engine's shipped ``decode.delimited`` keys its columns off the first line, which this body lacks.
HEADERLESS_COLON_BODY = b"1040175:ACME CAPITAL LLC\n1193054:BETA ADVISERS INC\n"

#: The need set the roadmap records for each re-checked row now that the labels are split, and
#: the shipped half of it -- the part that used to be mistaken for the whole row.
#: ``tag_column_selection`` no longer folds onto ``columns.select`` (one column copies one fixed
#: source key, so a tag-chosen column is a different capability), and the filer list's old
#: ``delimited_text_decoder`` label no longer folds onto ``decode.delimited`` (a body with no
#: published header is a different shape).
#: Re-collapsing either label in ``LABEL_TO_CAPABILITY`` moves the derived face and breaks the
#: equality below, so this is the pin that keeps the split from quietly reverting.
ROADMAP_NEED_SETS_AFTER_THE_SPLIT: dict[str, set[str]] = {
    "CashFlowStatement": {"columns.response_dependent_selection", "columns.select"},
    "IncomeStatement": {"columns.response_dependent_selection", "columns.select"},
    "InstitutionsSearch": {"decode.delimited_headerless"},
}
SHIPPED_NEEDS_WITHIN: dict[str, set[str]] = {
    "CashFlowStatement": {"columns.select"},
    "IncomeStatement": {"columns.select"},
    "InstitutionsSearch": set(),
}


def census_rows() -> dict[str, dict[str, Any]]:
    """Index the sec rows of the per-model census by ledger task id."""
    document = json.loads((REPO_ROOT / CENSUS_RELATIVE_PATH).read_text(encoding="utf-8"))
    return {row["task_id"]: row for row in document["rows"] if row.get("provider") == "sec"}


def roadmap_document() -> dict[str, Any]:
    """The roadmap artifact as committed: one read, so every face below shares the same bytes."""
    return json.loads((REPO_ROOT / ROADMAP_RELATIVE_PATH).read_text(encoding="utf-8"))


def roadmap_shipped_cover_needs() -> dict[str, dict[str, Any]]:
    """Return the roadmap's ``shipped_cover_rows`` entries for sec, by upstream model."""
    return {
        entry["upstream_model"]: entry
        for entry in roadmap_document()["roadmap"]["shipped_cover_rows"]
        if entry.get("provider") == "sec"
    }


def roadmap_build_order_needs() -> dict[str, set[str]]:
    """Return the need set the roadmap itself records for each sec row in the engine build pool."""
    found: dict[str, set[str]] = {}
    for step in roadmap_document()["roadmap"]["greedy_build_order"]:
        for row in step["newly_unlocked_rows"]:
            if row.get("provider") == "sec":
                assert row["upstream_model"] not in found, row["upstream_model"]
                found[row["upstream_model"]] = set(row["needs"])
    return found


def probe_spec(**overrides: Any) -> ModelSpec:
    """One throwaway declaration, used only to exercise a capability the engine does or lacks."""
    fields: dict[str, Any] = {
        "model": "Probe",
        "domain": "probe",
        "asset_class": "equity",
        "period": "snapshot",
        "market": "all",
        "base_url": PROBE_BASE_URL,
        "path": "/probe/document",
        "rows_pointer": "units",
        "columns": (ColumnSpec("value", "float", required=True),),
        "scenario": "能力探针：离线验证被记录的阻塞能力确实不存在",
        "error_prefix": PROBE_ERROR_PREFIX,
    }
    fields.update(overrides)
    return ModelSpec(**fields)


def probe_fetcher(spec: ModelSpec, transport: SyntheticTransport) -> Any:
    """Instantiate a generated probe fetcher against a recorded transport, never a socket."""
    cls = make_http_json_fetcher(SOURCE, spec)
    return type(cls.__name__, (cls,), {"http_transport": transport})()


class TestTheRoadmapThreeStayRefused:
    """The census, not the roadmap's label mapping, is what a sec declaration has to answer to."""

    def test_the_package_constructs_no_declaration(self) -> None:
        source_text = (REPO_ROOT / "opendata/data/providers/sec/specs.py").read_text(
            encoding="utf-8"
        )
        assert source_text.count("ModelSpec(") == 0
        assert specs.DECLARED_MODELS == ()
        assert PROVIDER.fetcher_bindings == ()
        assert {spec.model for source, spec in engine_declared_models() if source == "sec"} == set()

    def test_the_recheck_only_names_recorded_rows(self) -> None:
        assert set(specs.RECHECKED_THIS_ROUND) <= set(specs.NOT_DECLARABLE)
        assert set(specs.RECHECKED_THIS_ROUND).isdisjoint(specs.DECLARED_MODELS)

    @pytest.mark.parametrize(
        ("model", "task_id"),
        sorted(specs.RECHECKED_THIS_ROUND.items()),
        ids=sorted(specs.RECHECKED_THIS_ROUND),
    )
    def test_census_denies_each_and_the_blocker_quotes_it(self, model: str, task_id: str) -> None:
        """A refusal is honest only if it names the recorded need and the recorded proof line."""
        row = census_rows()[task_id]
        assert row["upstream_model"] == model
        assert row["expressible_today"] is False, f"{model}: the census denies the declaration"
        assert row["needs_engine_capability"], model
        reason = specs.NOT_DECLARABLE[model]
        for need in row["needs_engine_capability"]:
            assert need in reason, f"{model}: the blocker never names {need}"
        assert Path(row["evidence"]).name in reason, f"{model}: no engine proof cited"

    def test_the_roadmap_no_longer_claims_any_sec_row_is_shipped(self) -> None:
        """What the split cost: these three left ``shipped_cover_rows`` and joined the build pool.

        They were the roadmap's "already covered" rows only because two need labels folded onto
        shipped capabilities. ``scripts/quality/model_capability_census.py`` now maps them to
        ``columns.response_dependent_selection`` and ``decode.delimited_headerless``, both of which
        the registry reads absent, so the roadmap's own face for sec measures no shipped
        coverage at all -- the same 17 -> 3 drop that
        ``docs/evidence/C75/label-split-counterfact.py`` proves against ``greedy_cover``. The
        census rows keep ``expressible_today: false``.
        """
        registry = roadmap_document()["capability_registry"]
        rows = census_rows()
        assert roadmap_shipped_cover_needs() == {}, "a sec row is back in the shipped-coverage list"

        build_needs = roadmap_build_order_needs()
        for model, task_id in specs.RECHECKED_THIS_ROUND.items():
            needs = build_needs[model]
            assert needs == ROADMAP_NEED_SETS_AFTER_THE_SPLIT[model], model
            shipped = {need for need in needs if registry[need]["present"] is True}
            assert shipped == SHIPPED_NEEDS_WITHIN[model], model
            blocked = needs - shipped
            assert blocked, f"{model}: nothing blocks it, so the refusal is stale"
            assert rows[task_id]["expressible_today"] is False, model


class TestTheRefusedCapabilitiesAreReallyAbsent:
    """Executed offline witnesses: the engine still cannot carry what the census says it needs."""

    def test_no_declaration_field_exists_for_the_recorded_needs(self) -> None:
        """The blocker is a missing knob, so the knob's absence is what gets asserted."""
        declared_fields: set[str] = set()
        for cls in (ModelSpec, ColumnSpec, DecoderSpec):
            declared_fields |= {field.name for field in dataclasses.fields(cls)}
        for need in (
            "xbrl_tag_assembly",
            "tag_column_selection",
            "columns_derive",
            "columns_cast",
            "header_row",
            "skip_rows",
        ):
            assert need not in declared_fields, need
        assert {column.source_key for column in probe_spec().columns} == {None}

    def test_a_tag_nested_concept_is_a_shape_failure_not_a_row(self) -> None:
        """``CashFlowStatement``/``IncomeStatement``: no record list sits at the tagged address."""
        spec = probe_spec()
        canned = HttpResponse(200, UNIT_NESTED_DOCUMENT)
        transport = SyntheticTransport({(PROBE_DOCUMENT_URL, frozenset()): canned})
        fetcher = probe_fetcher(spec, transport)
        with pytest.raises(ProviderEngineError) as raised:
            fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_SHAPE_INVALID"
        assert len(transport.calls) == 1

    def test_a_headerless_delimited_body_refuses_instead_of_guessing_a_header(self) -> None:
        """``InstitutionsSearch``: the shipped delimited decoder needs a published header row."""
        spec = probe_spec(
            path="/probe/filer-list",
            rows_pointer="",
            columns=(
                ColumnSpec("identifier", "str", required=True),
                ColumnSpec("entity_name", "str"),
            ),
            decoder=DecoderSpec(kind="csv", delimiter=":"),
        )
        canned = HttpResponse(200, None, payload=HEADERLESS_COLON_BODY)
        transport = SyntheticTransport({(PROBE_FILER_LIST_URL, frozenset()): canned})
        fetcher = probe_fetcher(spec, transport)
        with pytest.raises(ResponseDecodeError) as raised:
            fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_HEADER_MISMATCH"
        assert raised.value.decoder == "csv"
        # The first data line was eaten as the header, so nothing was published: a typed refusal.
        assert len(transport.calls) == 1

    def test_an_undeclared_parameter_is_refused_before_any_send(self) -> None:
        """The offline guard: an undeclared key is refused before anything is asked of I/O."""
        spec = probe_spec()
        transport = SyntheticTransport({})
        fetcher = probe_fetcher(spec, transport)
        with pytest.raises(ProviderEngineError) as raised:
            fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), cik="1040175")
        assert raised.value.code.endswith("_QUERY_INVALID")
        assert transport.calls == []
