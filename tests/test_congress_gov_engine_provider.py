"""What only the congress_gov package can state: the accounting over its 8 upstream rows.

The engine's promise is that a model is either declared or recorded as inexpressible, and this file
judges the second half. congress_gov's package must account for exactly the rows the pinned upstream
provider dictionary binds
(``providers/congress_gov/openbb_congress_gov/__init__.py:23-31``, mirrored by the
``OBB2-congress_gov-*`` ids in the iteration-2 ledger), refuse a row that is neither declared
nor recorded, refuse one that is both, and refuse a recorded row whose blocker is a claim about
effort rather than a capability the declaration format lacks.

No URL, column or parameter is re-derived here -- those claims live in
:data:`opendata.data.providers.congress_gov.specs.UPSTREAM_EVIDENCE`, which every blocker cites.
What is checked is the bookkeeping only this package can be trusted with, plus three faces that
cannot be kept green by editing a list: each blocker's capability ids are re-folded from the census
row it belongs to and re-read against the roadmap registry's own ``present`` flag, the capabilities
the refusals claim are absent are executed against the live engine on recorded synthetic fixtures,
and the package's data-rights statement is re-read from the two in-tree rights documents.

Every fixture below is a SYNTHETIC capability probe written by this file. It claims no Congress.gov
endpoint: the origin is the RFC 2606 reserved ``.test`` TLD, which cannot resolve, and no column
name in it is offered as a published congress_gov field. A probe exists to be executed, so a
blocker recorded in prose cannot go stale unnoticed.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
)
from opendata.data.providers._engine.testing import SyntheticTransport, fixture_context
from opendata.data.providers.catalog import (
    PROVIDERS,
    engine_declared_models,
    get_provider,
    health_check,
    register_provider,
)
from opendata.data.providers.congress_gov import specs
from opendata.data.providers.congress_gov._source import SOURCE
from opendata.data.providers.congress_gov.provider import PROVIDER
from opendata.data.registry import ProviderRegistry
from scripts.quality.model_capability_census import map_needs
from scripts.quality.provider_model_inventory import LEDGER_RELATIVE_PATH, load_task_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The per-model survey the refusals must obey: one row per provider×model task, carrying the
#: endpoint shape, the capability the engine would need and the ``expressible_today`` flag.
CENSUS_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-congress_gov.json"
)

#: The roadmap artifact whose ``capability_registry`` says which capability the engine ships today.
ROADMAP_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/capability-roadmap.json"
)

#: The sentence ``catalog._reserved_provider()`` writes for a source with no local implementation.
RESERVED_SENTENCE = "Catalog identity reserved; no local fetcher is implemented."

#: The congress_gov denominator: the ledger rows and the upstream dictionary's 8 keys.
UPSTREAM_ROW_COUNT = 8

#: The capability ids the package's blockers are allowed to name, each one read ``absent`` by the
#: roadmap registry. A blocker that names nothing from here is an effort claim; a token used in a
#: blocker and not defined in :mod:`...congress_gov.specs`' docstring is an undefined term.
CAPABILITY_VOCABULARY: tuple[str, ...] = (
    "decode.html_text",
    "decode.opaque_bytes",
    "flow.branch",
    "flow.fan_out",
    "flow.join",
    "flow.preflight_lookup",
    "outside.anti_bot",
    "outside.sdk",
    "outside.stateful",
    "path.assemble_single_row",
)

#: The ids the registry marks as shipped that this package still refuses to count as coverage, and
#: the reason each one falls short -- checked against ``present`` below, so a capability that later
#: grows the missing half moves this file with it.
SHIPPED_BUT_INSUFFICIENT: tuple[str, ...] = ("rows.order_limit", "columns.derive", "rows.filter")

#: ---- synthetic probe plumbing (reserved origin, no endpoint claim) ----------------------------
PROBE_BASE_URL = "https://congress-refusal-probe.test"
PROBE_ERROR_PREFIX = "CONGRESSPROBE"
PROBE_LIST_URL = f"{PROBE_BASE_URL}/probe/list"
PROBE_BILL_URL = f"{PROBE_BASE_URL}/probe/bill"
PROBE_DOCUMENT_URL = f"{PROBE_BASE_URL}/probe/document"
PROBE_DETAIL_URL = f"{PROBE_BASE_URL}/probe/detail"

#: One flat record of a made-up list document: two keys, both declared as columns.
PROBE_RECORDS = (
    {"bill_number": "hr-1", "title": "First made-up bill"},
    {"bill_number": "hr-2", "title": "Second made-up bill"},
    {"bill_number": "hr-3", "title": "Third made-up bill"},
)


@pytest.fixture(autouse=True)
def _no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the engine's single I/O seam raise, so no case can reach a network."""

    def forbidden(*args: Any, **kwargs: Any) -> HttpResponse:
        raise AssertionError("a congress_gov probe must not reach the real transport")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def ledger_congress_rows() -> list[str]:
    """Return the upstream model names the iteration-2 ledger fixes for congress_gov."""
    return [
        row["upstream_model"]
        for row in load_task_ledger(REPO_ROOT / LEDGER_RELATIVE_PATH)
        if row["provider"] == "congress_gov"
    ]


def census_rows() -> dict[str, dict[str, Any]]:
    """Index the congress_gov rows of the per-model census by ledger task id."""
    document = json.loads((REPO_ROOT / CENSUS_RELATIVE_PATH).read_text(encoding="utf-8"))
    by_task = {row["task_id"]: row for row in document["rows"]}
    return {key: row for key, row in by_task.items() if row.get("provider") == "congress_gov"}


def roadmap_registry() -> dict[str, dict[str, Any]]:
    """The capability registry as committed: one read, so every face below shares the same bytes."""
    return json.loads((REPO_ROOT / ROADMAP_RELATIVE_PATH).read_text(encoding="utf-8"))[
        "capability_registry"
    ]


def folded_capabilities(task_id: str) -> tuple[set[str], set[str]]:
    """Split one census row's folded capability set into the absent and shipped halves."""
    row = census_rows()[task_id]
    folded = set(map_needs(tuple(row["needs_engine_capability"]), task_id))
    registry = roadmap_registry()
    absent = {cap for cap in folded if registry[cap]["present"] is not True}
    shipped = folded - absent
    return absent, shipped


def probe_spec(**overrides: Any) -> ModelSpec:
    """One throwaway declaration, used only to exercise a capability the engine does or lacks."""
    fields: dict[str, Any] = {
        "model": "Probe",
        "domain": "probe",
        "asset_class": "equity",
        "period": "snapshot",
        "market": "all",
        "base_url": PROBE_BASE_URL,
        "path": "/probe/list",
        "rows_pointer": "bills",
        "columns": (
            ColumnSpec("bill_number", "str", required=True),
            ColumnSpec("title", "str"),
        ),
        "scenario": "能力探针：离线验证被记录的阻塞能力确实不存在",
        "error_prefix": PROBE_ERROR_PREFIX,
    }
    fields.update(overrides)
    return ModelSpec(**fields)


def probe_fetcher(spec: ModelSpec, transport: SyntheticTransport) -> Any:
    """Instantiate a generated probe fetcher against a recorded transport, never a socket."""
    cls = make_http_json_fetcher(SOURCE, spec)
    return type(cls.__name__, (cls,), {"http_transport": transport})()


class TestCongressGovIsARealProvider:
    """congress_gov is a locally authored descriptor, not a catalog placeholder."""

    def test_descriptor_is_not_the_reserved_placeholder(self) -> None:
        provider = get_provider("congress_gov")
        assert provider is PROVIDER
        assert provider.description != RESERVED_SENTENCE
        assert "reserved" not in provider.description.lower()
        assert provider.website == "https://api.congress.gov"

    def test_the_routing_label_is_the_package_name(self) -> None:
        assert SOURCE == "congress_gov" == PROVIDER.source
        assert any(provider.source == "congress_gov" for provider in PROVIDERS)

    def test_the_credential_face_is_the_upstream_dictionary(self) -> None:
        """``credentials=["api_key"]`` (``__init__.py:22``) is what the descriptor declares."""
        assert len(PROVIDER.credentials) == 1
        credential = PROVIDER.credentials[0]
        assert credential.name == specs.UPSTREAM_CREDENTIAL_NAME == "api_key"
        assert credential.environment_variable == "CONGRESS_GOV_API_KEY"
        # No binding can ask for a key yet, so health must not either.
        assert credential.required_for_health is False

    def test_a_provider_with_hand_written_bindings_is_implemented(self) -> None:
        """This is the face the catalog arithmetic reads: eight fetchers, so implemented.

        The rows are served by hand-written adapters; the eight *engine declarations* stay
        retracted (``specs.DECLARED_MODELS == ()``), which is a different claim from "no rows".
        """
        assert PROVIDER.is_implemented is True
        assert len(PROVIDER.fetchers) == len(specs.UPSTREAM_ROWS) == 8
        assert health_check("congress_gov")["congress_gov"].status == "ready"


class TestTheEightRowAccounting:
    """Declared plus recorded equals the upstream rows, exactly once each."""

    def test_package_rows_are_the_ledger_rows(self) -> None:
        from_ledger = ledger_congress_rows()
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

    def test_provider_bindings_and_declarations_stay_in_step(self) -> None:
        """Every upstream row is bound once by a hand-written fetcher; no engine declaration exists."""
        assert specs.DECLARED_MODELS == ()
        assert len(PROVIDER.fetcher_bindings) == len(specs.UPSTREAM_ROWS) == 8
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert bound == set(specs.UPSTREAM_ROWS)
        assert not (bound & set(specs.DECLARED_MODELS))

    def test_a_blocker_names_a_defined_capability_and_cites_its_source(self) -> None:
        for model, reason in specs.NOT_DECLARABLE.items():
            named = [token for token in CAPABILITY_VOCABULARY if token in reason]
            assert named, f"{model}: a blocker with no capability name is an effort claim"
            assert re.search(r"[a-z_]+\.py:\d", reason), f"{model}: no file-anchored cite"
            assert re.search(r"\(:\d", reason), f"{model}: a cite that never repeats its file"
            assert len(reason) <= 320, (
                f"{model}: a blocker a reviewer cannot hold is a design note ({len(reason)})"
            )
            for capability in specs.BLOCKING_CAPABILITIES[model]:
                assert capability in reason, f"{model}: never names its own blocker {capability}"

    def test_the_vocabulary_is_defined_by_the_module_it_is_used_in(self) -> None:
        docstring = specs.__doc__ or ""
        for token in CAPABILITY_VOCABULARY:
            assert token in docstring, f"{token}: used in a blocker but defined nowhere"
        used = {
            token
            for reason in specs.NOT_DECLARABLE.values()
            for token in CAPABILITY_VOCABULARY
            if token in reason
        }
        assert used == set(CAPABILITY_VOCABULARY), f"unused capability name: {sorted(used)!r}"
        for token in SHIPPED_BUT_INSUFFICIENT:
            assert token in docstring, f"{token}: shipped half never explained"

    def test_the_supporting_tables_answer_for_the_same_rows(self) -> None:
        assert set(specs.BLOCKING_CAPABILITIES) == set(specs.UPSTREAM_ROWS)
        assert set(specs.UPSTREAM_EVIDENCE) == set(specs.UPSTREAM_ROWS)
        assert set(specs.SHIPPED_NEEDS_WITHIN) <= set(specs.UPSTREAM_ROWS)
        for model, evidence in specs.UPSTREAM_EVIDENCE.items():
            assert re.search(r"[a-z_]+\.py:\d", evidence), model
            assert Path(evidence.split(",")[0].split(" ")[0]).name, model

    def test_the_refusals_are_about_capability_not_this_package_being_behind(self) -> None:
        # An effort claim ("not written yet") would satisfy every check above and teach nothing, so
        # each blocker has to carry at least one mechanism word from the engine's own vocabulary.
        for model, reason in specs.NOT_DECLARABLE.items():
            assert re.search(
                r"branch|fan|paginat|loop|normaliz|merged|payload|record list|pointer|mapping|"
                r"bytes|cache|backoff|pipeline|caller text|markdown",
                reason,
            ), f"{model}: no mechanism named"
            assert not re.search(r"not implemented|todo|later|effort|manual", reason, re.I), model


class TestTheCensusIsWhatTheRefusalsAnswerTo:
    """Each blocker's capability set is re-folded from its census row and read back off the disk."""

    @pytest.mark.parametrize(
        "model",
        sorted(specs.UPSTREAM_ROWS),
        ids=sorted(specs.UPSTREAM_ROWS),
    )
    def test_census_denies_each_row_and_the_blocker_quotes_its_absent_half(
        self, model: str
    ) -> None:
        task_id = f"OBB2-congress_gov-{model}"
        row = census_rows()[task_id]
        assert row["upstream_model"] == model
        assert row["expressible_today"] is False, f"{model}: the census denies the refusal"
        assert row["needs_engine_capability"], model
        absent, shipped = folded_capabilities(task_id)
        assert shipped == set(specs.SHIPPED_NEEDS_WITHIN.get(model, ())), model
        reason = specs.NOT_DECLARABLE[model]
        named = specs.BLOCKING_CAPABILITIES[model]
        assert absent == set(named), (
            f"{model}: the blocker names {sorted(named)} but the census folded to {sorted(absent)}"
        )
        for capability in sorted(absent):
            assert capability in reason, (
                f"{model}: the census folds {capability} onto this row as absent, but the refusal "
                f"never spells it: {reason!r}"
            )
        evidence_file, _, evidence_line = Path(row["evidence"]).name.partition(":")
        assert evidence_line in specs.UPSTREAM_EVIDENCE[model], (
            f"{model}: UPSTREAM_EVIDENCE drops the census's own line {evidence_line}"
        )

    @pytest.mark.parametrize("capability", sorted(CAPABILITY_VOCABULARY))
    def test_the_named_capabilities_are_read_absent_by_the_registry(self, capability: str) -> None:
        entry = roadmap_registry()[capability]
        assert entry["present"] is False, f"{capability}: shipped now, so the blocker is stale"

    @pytest.mark.parametrize("capability", sorted(SHIPPED_BUT_INSUFFICIENT))
    def test_the_credited_capabilities_are_read_present(self, capability: str) -> None:
        assert roadmap_registry()[capability]["present"] is True, capability

    def test_the_three_outside_capabilities_are_not_engine_work(self) -> None:
        """``outside.*`` rows cannot be unblocked by a kernel, so their refusal is final.

        The registry's own flag says the engine has no route to them; a future
        CongressCommitteeDocuments declaration therefore has to come with something other than an
        engine change.
        """
        registry = roadmap_registry()
        for capability in ("outside.sdk", "outside.stateful", "outside.anti_bot"):
            assert registry[capability]["present"] is False
            assert registry[capability]["engine_work"] is False, capability


class TestNothingHereIsLiveVerified:
    """No congress_gov row claims a source-verified response, and no row claims acceptance."""

    def test_the_package_constructs_no_declaration(self) -> None:
        source_text = (REPO_ROOT / "opendata/data/providers/congress_gov/specs.py").read_text(
            encoding="utf-8"
        )
        assert source_text.count("ModelSpec(") == 0
        assert specs.DECLARED_MODELS == ()
        declared = {model for source, model in engine_declared_models() if source == "congress_gov"}
        assert declared == set()

    def test_models_ships_the_hand_written_fetcher_module(self) -> None:
        """The rows ship as hand-written adapters; no generated fetcher module exists."""
        shipped = sorted(
            path.name
            for path in (REPO_ROOT / "opendata/data/providers/congress_gov/models").glob("*.py")
        )
        assert shipped == ["__init__.py", "_client.py", "iter2.py"]

    def test_discovery_through_the_catalog_matches_the_package(self) -> None:
        found = {
            spec.model for source, spec in engine_declared_models() if source == "congress_gov"
        }
        assert found == set(specs.DECLARED_MODELS) == set()

    def test_registration_verifies_nothing(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("congress_gov", registry)
        assert len(capabilities) == len(specs.UPSTREAM_ROWS) == 8
        assert not any(capability.verified for capability in capabilities)

    def test_no_reviewed_clause_basis_is_registered_for_congress_gov(self) -> None:
        """The package's rights statement is re-read from the tree every run, not recalled.

        ``specs`` says no reviewed Congress.gov clause basis exists in-tree and that no row is
        registered here. Both halves are measured: a data-rights row naming congress would make
        this fail, and that is the point -- the row is a human artifact with verbatim quotes, so it
        has to be written by a person, and then this package has to re-read it and re-judge
        acceptance.
        """
        registry = (REPO_ROOT / "docs/data-rights-registry.md").read_text(encoding="utf-8")
        review = (REPO_ROOT / "docs/evidence/C65/data-rights-review.md").read_text(encoding="utf-8")
        assert "congress" not in registry.lower()
        assert "congress" not in review.lower()
        docstring = specs.__doc__ or ""
        assert "no reviewed clause basis for Congress.gov in this repo" in docstring
        for path in sorted((REPO_ROOT / "opendata/data/providers/congress_gov").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            assert "DEV_DONE" not in text, path
            assert "已复核" not in text, path


#: ---- the recorded capabilities, executed against the live engine on recorded fixtures ---------


class TestFlowBranchIsAbsent:
    """``flow.branch``: one declaration addresses one template, chosen before any response."""

    def test_no_field_of_the_declaration_carries_a_path_choice(self) -> None:
        fields = set()
        for cls in (ModelSpec, ColumnSpec, DecoderSpec, PaginationSpec, ParamSpec):
            fields |= {field.name for field in dataclasses.fields(cls)}
        for knob in ("path_branches", "branch", "endpoint", "endpoints", "alternate_paths"):
            assert knob not in fields, knob

    def test_an_optional_undefaulted_placeholder_is_refused_when_written(self) -> None:
        """``bill``/``bill/{congress}``/``bill/{congress}/{bill_type}`` needs a settable-or-absent
        segment, and the engine refuses exactly that (spec.py:706-709): a declaration that cannot
        address a resource on its own is not a branch, it is an incomplete record."""
        with pytest.raises(ValueError) as raised:
            probe_spec(
                path="/bill/{congress}",
                params=(ParamSpec("congress", "int", note="国会届次"),),
            )
        assert "must be required or defaulted" in str(raised.value)

    def test_a_defaulted_placeholder_freezes_one_branch(self) -> None:
        """The only spelling the engine accepts addresses one resource, always."""
        spec = probe_spec(
            path="/bill/{congress}",
            params=(ParamSpec("congress", "int", default=118),),
            columns=(ColumnSpec("bill_number", "str", required=True),),
            rows_pointer="bills",
        )
        transport = SyntheticTransport(
            {(f"{PROBE_BASE_URL}/bill/118", frozenset()): HttpResponse(200, {"bills": []})}
        )
        rows = probe_fetcher(spec, transport).fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert rows == ()
        assert [call["url"] for call in transport.calls] == [f"{PROBE_BASE_URL}/bill/118"]


class TestFlowFanOutIsAbsent:
    """``flow.fan_out``: a URL published by a response is never the address of the next request."""

    def test_the_address_is_built_once_and_re_sent_with_a_larger_offset(self) -> None:
        """CongressBills' ``limit=0`` diversion and CongressAmendments' per-item detail URLs both
        arrive as URLs in a body (helpers.py:286-287, congress_amendments.py:280). Paging here can
        only re-send the declared path: the second call below is the same path with ``offset=2``,
        while the ``next`` URL the first body published is never asked for."""
        spec = probe_spec(
            pagination=PaginationSpec(kind="offset", limit_key="limit", offset_key="offset"),
            params=(ParamSpec("limit", "int", default=2),),
        )
        first = {
            "bills": list(PROBE_RECORDS[:2]),
            "pagination": {
                "count": 3,
                "next": f"{PROBE_BASE_URL}/probe/published-page?offset=2",
            },
        }
        second = {"bills": list(PROBE_RECORDS[2:]), "pagination": {"count": 3, "next": None}}
        transport = SyntheticTransport(
            {
                (PROBE_LIST_URL, frozenset({("limit", "2"), ("offset", "0")})): HttpResponse(
                    200, first
                ),
                (PROBE_LIST_URL, frozenset({("limit", "2"), ("offset", "2")})): HttpResponse(
                    200, second
                ),
            }
        )
        rows = probe_fetcher(spec, transport).fetch(ctx=fixture_context(SOURCE, spec, sends=2))
        assert len(rows) == 3
        assert [call["url"] for call in transport.calls] == [PROBE_LIST_URL, PROBE_LIST_URL]
        assert [call["params"]["offset"] for call in transport.calls] == ["0", "2"]
        assert all("published-page" not in str(call) for call in transport.calls)


class TestFlowJoinIsAbsent:
    """``flow.join``: normalization sees one document, so a column from a second one is absent."""

    def test_a_column_only_the_second_request_would_supply_is_a_shape_failure(self) -> None:
        """``CongressBillInfo`` reads ``cosponsors.url`` out of the base payload and asks for it
        (bill_info.py:111-189). Declaring the merged-in column is legal; fetching it is not, and the
        engine says so after the one request it is allowed to make."""
        spec = probe_spec(
            path="/probe/detail",
            columns=(
                ColumnSpec("bill_number", "str", required=True),
                ColumnSpec("cosponsor_count", "int", required=True),
            ),
        )
        # The base document publishes the URL of the answer, not the answer itself.
        base = {
            "bills": [
                {
                    "bill_number": "hr-1",
                    "cosponsors": {
                        "count": 12,
                        "url": "https://congress-refusal-probe.test/probe/x",
                    },
                }
            ]
        }
        transport = SyntheticTransport({(PROBE_DETAIL_URL, frozenset()): HttpResponse(200, base)})
        with pytest.raises(ProviderEngineError) as raised:
            probe_fetcher(spec, transport).fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_SHAPE_INVALID"
        assert "cosponsor_count" in raised.value.missing
        assert len(transport.calls) == 1


class TestPreflightLookupIsAbsent:
    """``flow.preflight_lookup``: nothing reads a prior response to decide what to ask next."""

    def test_a_caller_supplied_address_cannot_become_a_path_segment(self) -> None:
        """``CongressBillText``/``CongressAmendmentText`` request URLs the caller hands over
        (bill_text.py:79-101). A placeholder may carry one segment of a fixed template; a value
        that spells a whole address is refused before anything is asked of I/O."""
        spec = probe_spec(
            path="/probe/{document_url}",
            params=(ParamSpec("document_url", "str", required=True),),
            columns=(ColumnSpec("filename", "str", required=True),),
            rows_pointer="",
        )
        transport = SyntheticTransport({})
        with pytest.raises(ProviderEngineError) as raised:
            probe_fetcher(spec, transport).fetch(
                ctx=fixture_context(SOURCE, spec, sends=1),
                document_url="https://www.congress.gov/118/plaws/publ1/plaws/publ1.pdf",
            )
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_PARAM_UNENCODABLE"
        assert transport.calls == []


class TestAssembleSingleRowIsAbsent:
    """``path.assemble_single_row``: a pointer on one object is a shape failure, not a row."""

    def test_a_mapping_under_the_pointer_is_refused(self) -> None:
        """``bill_info.py:107`` reads ``response["bill"]``, a dict, and assembles one record.

        ``resolve_rows`` requires a list (http_json.py:410-435)."""
        spec = probe_spec(path="/probe/document", rows_pointer="bill")
        transport = SyntheticTransport(
            {
                (
                    PROBE_DOCUMENT_URL,
                    frozenset(),
                ): HttpResponse(200, {"bill": {"bill_number": "hr-1", "title": "Made up"}})
            }
        )
        with pytest.raises(ProviderEngineError) as raised:
            probe_fetcher(spec, transport).fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_SHAPE_INVALID"
        assert len(transport.calls) == 1


class TestHtmlAndOpaqueByteDecodingAreAbsent:
    """``decode.html_text``/``decode.opaque_bytes``: the decoder set is closed, none is JSON."""

    def test_no_decoder_kind_spells_html_or_pdf(self) -> None:
        with pytest.raises(ValueError) as raised:
            DecoderSpec(kind="html")
        assert "unknown decoder kind" in str(raised.value)
        with pytest.raises(ValueError) as raised:
            DecoderSpec(kind="pdf")
        assert "unknown decoder kind" in str(raised.value)

    def test_a_pdf_body_handed_to_a_json_declaration_is_a_typed_refusal(self) -> None:
        """``bill_text.py:106-108`` base64-encodes ``response.content``; nothing here can declare
        that, and the bytes are refused rather than guessed at."""
        spec = probe_spec(path="/probe/document", rows_pointer="bills")
        transport = SyntheticTransport(
            {
                (PROBE_DOCUMENT_URL, frozenset()): HttpResponse(
                    200, None, payload=b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
                )
            }
        )
        with pytest.raises(ProviderEngineError) as raised:
            probe_fetcher(spec, transport).fetch(ctx=fixture_context(SOURCE, spec, sends=1))
        assert raised.value.code == f"{PROBE_ERROR_PREFIX}_SHAPE_INVALID"
        assert len(transport.calls) == 1
