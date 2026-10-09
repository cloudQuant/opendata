"""What only the oecd package can state: the accounting over its nine upstream rows.

The engine's promise is that a model is either declared or recorded as inexpressible, and this file
judges the second half for oecd. The package must account for exactly the rows
``模型级任务清单.csv`` binds under ``OBB2-oecd-<model>``, refuse a row that is neither declared nor
recorded, refuse one that is both, and refuse a recorded row whose blocker is a claim about effort
rather than a capability the declaration format lacks.

Two of the nine were declared last round -- ``ConsumerPriceIndex`` and ``Unemployment`` -- and
:mod:`opendata.data.providers.oecd.specs` retracts them:
``scripts/quality/declaration_provenance.py`` read the pinned models and found the declarations
described this repository's own clean-room reader (:mod:`...models._client`) rather than OpenBB's
models, whose published columns are ``country``/``date``/``value``. So the faces below assert the
retraction is *complete* -- no ``ModelSpec``, no generated fetcher module, no domain, no registry
entry, no ``resolve_model`` answer -- and then re-measure the refusals on the live engine instead
of trusting the prose.

The re-measurement is the file's second half: each capability a blocker names is probed by
declaring the request the pinned upstream actually builds and watching the engine refuse it or send
it wrong. Every probe ships a positive control, because a probe that only ever fails proves nothing
about the instrument. All cases are offline: an autouse guard below replaces the engine's only I/O
seam with a raiser, so "no network" is a property of the run rather than of the fixtures, and no
probe claims a live OECD response.

Nothing here re-derives a column list or a dataflow id: those facts are cited in the blockers, and
what is checked is the bookkeeping only this package can be trusted with.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from opendata.data.domains import load_domains, require_domain
from opendata.data.provider import LazyFetcherBinding
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    build_row_model,
    normalize_record,
    render_path,
)
from opendata.data.providers._engine.spec import (
    DELIMITED_DECODERS,
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    ParamSpec,
    RowFilterOp,
    RowFilterSpec,
)
from opendata.data.providers.catalog import (
    engine_declared_models,
    get_provider,
    register_provider,
)
from opendata.data.providers.oecd import specs
from opendata.data.registry import ProviderRegistry
from scripts.quality.provider_model_inventory import LEDGER_RELATIVE_PATH, load_task_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The per-model survey oecd's nine rows were read into, and the denominator the blockers answer to.
CENSUS_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-imf-oecd-famafrench-misc.json"
)

#: The capability names the blockers may use, each one defined -- with the engine line that shows it
#: missing -- in the specs module's docstring. A blocker naming an undefined capability would be an
#: assertion rather than a measurement.
CAPABILITY_VOCABULARY = (
    "params.value_map",
    "path.list_segment",
    "columns.compose",
    "row_filter.comparison",
    "fixed query keys",
    "more than one request",
)

#: The HICP dataflow as ``consumer_price_index.py:204`` spells it, commas and ``@`` included.
UPSTREAM_DATAFLOW = "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0"


def ledger_oecd_rows() -> list[str]:
    """Return the upstream model names the iteration-2 ledger fixes for oecd."""
    return [
        row["upstream_model"]
        for row in load_task_ledger(REPO_ROOT / LEDGER_RELATIVE_PATH)
        if row["provider"] == "oecd"
    ]


def census_oecd_rows() -> list[dict[str, Any]]:
    """Return the census rows recorded for oecd, in the file's own order."""
    document = json.loads((REPO_ROOT / CENSUS_RELATIVE_PATH).read_text(encoding="utf-8"))
    return [row for row in document["rows"] if row["provider"] == "oecd"]


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove offline-ness structurally: the real HTTP read path raises the moment it is reached."""

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("an oecd accounting case reached the network")

    monkeypatch.setattr(http_json, "_http_get_json", forbidden)


def _key_spec(**overrides: Any) -> ModelSpec:
    """Return the declaration the pinned CPI model would need: one mapped key in the path."""
    fields: dict[str, Any] = {
        "model": "ConsumerPriceIndex",
        "domain": "oecd_cpi_probe",
        "asset_class": "macro",
        "period": "M",
        "market": "all",
        "base_url": "https://sdmx.oecd.org/public/rest",
        "path": f"/data/{UPSTREAM_DATAFLOW}/{{series_key}}",
        "columns": (ColumnSpec("country"),),
        "params": (ParamSpec("series_key", "str", required=True),),
        "scenario": "宏观通胀观测探针：按 SDMX 序列键取回消费价格指数观测值",
        "error_prefix": "OECD",
    }
    fields.update(overrides)
    return ModelSpec(**fields)


def _query(**values: Any) -> Any:
    """A validated query object: ``render_path`` reads attributes, not dictionaries."""
    return type("Query", (), values)()


class TestTheNineRowAccounting:
    """Declared plus recorded equals the nine ledger rows, and nothing is declared."""

    def test_package_rows_are_the_ledger_rows(self) -> None:
        assert sorted(specs.LEDGER_ROWS) == sorted(ledger_oecd_rows())
        assert len(specs.LEDGER_ROWS) == 9
        assert len(set(specs.LEDGER_ROWS)) == 9

    def test_the_census_asked_about_the_same_nine_rows(self) -> None:
        rows = census_oecd_rows()
        assert len(rows) == 9
        assert {row["upstream_model"] for row in rows} == set(specs.LEDGER_ROWS)
        # The survey's own readings the module docstring reports, re-read rather than recalled.
        assert {row["http_method"] for row in rows} == {"GET"}
        assert {row["response_format"] for row in rows} == {"csv"}
        assert {row["row_extraction"] for row in rows} == {"flat"}
        assert {row["paged"] for row in rows} == {False}
        assert {bool(row["expressible_today"]) for row in rows} == {False}

    def test_nothing_is_declared(self) -> None:
        assert specs.DECLARED_MODELS == ()

    def test_every_row_is_recorded_exactly_once(self) -> None:
        declared = set(specs.DECLARED_MODELS)
        recorded = set(specs.NOT_DECLARABLE)
        assert declared.isdisjoint(recorded), "a row is both declared and recorded"
        assert declared | recorded == set(specs.LEDGER_ROWS), (
            "the package does not account for every ledger row: "
            f"{sorted(set(specs.LEDGER_ROWS) - declared - recorded)}"
        )
        assert len(recorded) == 9

    def test_a_blocker_names_a_defined_capability_and_cites_its_source(self) -> None:
        for model, reason in specs.NOT_DECLARABLE.items():
            named = [token for token in CAPABILITY_VOCABULARY if token in reason]
            assert named, f"{model}: a blocker with no capability name is an effort claim"
            assert re.search(r"[a-z_]+\.py:\d", reason), f"{model}: no file-anchored cite"
            assert re.search(r"\(:\d", reason), f"{model}: a cite that never repeats its file"
            assert len(reason) <= 320, (
                f"{model}: a blocker a reviewer cannot hold is a design note ({len(reason)})"
            )

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
        assert used == set(CAPABILITY_VOCABULARY), f"unused capability name: {used!r} vs defined"

    @pytest.mark.parametrize("retracted", ["consumer_price_index", "unemployment_engine"])
    def test_a_retracted_declaration_leaves_no_importable_module(self, retracted: str) -> None:
        module = f"opendata.data.providers.oecd.models.{retracted}"
        assert importlib.util.find_spec(module) is None
        assert not hasattr(specs, "CONSUMER_PRICE_INDEX")
        assert not hasattr(specs, "UNEMPLOYMENT")

    def test_the_refusals_are_about_capability_not_this_package_being_behind(self) -> None:
        # An effort claim ("not written yet") would satisfy every check above and teach nothing, so
        # each blocker has to carry at least one mechanism word from the engine's own vocabulary.
        for model, reason in specs.NOT_DECLARABLE.items():
            assert re.search(
                r"segment|literal|lookup|joined|renamed|scaled|converted|window|retried|request"
                r"|dict|composed",
                reason,
            ), f"{model}: no mechanism named"
            assert not re.search(r"not implemented|todo|later|effort|manual", reason, re.I), model


class TestProviderDescriptor:
    """The retraction reached the descriptor, the registry and the domain table."""

    def test_oecd_binds_only_its_two_hand_written_fetchers(self) -> None:
        provider = get_provider("oecd")
        assert len(provider.fetcher_bindings) == 2
        for binding in provider.fetcher_bindings:
            assert isinstance(binding, LazyFetcherBinding)
            # A binding without canonical model ids is a legacy fetcher, not an engine declaration.
            assert binding.canonical_model_ids == ()
            assert binding.scenario is None
        assert {binding.class_name for binding in provider.fetcher_bindings} == {
            "OecdCpiFetcher",
            "OecdUnemploymentFetcher",
        }

    def test_oecd_publishes_no_engine_declaration(self) -> None:
        declared = [model for source, model in engine_declared_models() if source == "oecd"]
        assert declared == []

    def test_the_two_shipped_legs_stay_registered_and_verified(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("oecd", registry)
        by_domain = {capability.domain: capability for capability in capabilities}
        assert set(by_domain) == {"economy_cpi", "economy_unemployment"}
        assert all(capability.source == "oecd" for capability in capabilities)
        # These two are hand-written fetchers verified in iteration 1; retracting a declaration must
        # not silently un-verify what was already standing.
        assert [by_domain[domain].verified for domain in sorted(by_domain)] == [True, True]

    def test_the_retracted_model_identities_resolve_to_nothing(self) -> None:
        registry = ProviderRegistry()
        register_provider("oecd", registry)
        for model in ("ConsumerPriceIndex", "Unemployment"):
            with pytest.raises(LookupError) as caught:
                registry.resolve_model("oecd", model)
            assert f"oecd/{model}" in str(caught.value)

    def test_no_oecd_domain_was_registered_for_a_retracted_declaration(self) -> None:
        domains = load_domains()
        for domain in ("oecd_consumer_price_index", "oecd_unemployment"):
            assert domain not in domains
            with pytest.raises(LookupError):
                require_domain(domain)
        # The retraction removed oecd's two entries and nothing else; the table keeps its 36
        # after the two federal_reserve engine domains joined the 34.
        assert len(domains) == 36
        assert sorted(d for d in domains if d.startswith("cboe_")) == [
            "cboe_available_indices",
            "cboe_index_constituent_quotes",
            "cboe_index_search",
        ]


class TestTheComposedKeyIsNotDeclarable:
    """Each capability a blocker names, re-measured by declaring the request the upstream builds.

    These are the probes that make a refusal a finding rather than a claim: the pinned CPI model's
    key is ``{country}.{frequency}.{methodology}.CPI.{units}.{expenditure}.N.`` with every part read
    out of a code table (consumer_price_index.py:203-205), so the questions are whether the engine
    can map a value, carry a ``+``, send a key whose positions are empty, or derive a column.
    """

    def test_positive_control_a_single_segment_value_is_carried_verbatim(self) -> None:
        spec = _key_spec()
        assert spec.path_placeholders == ("series_key",)
        # A whole key with no illegal character renders -- the instrument works.
        assert render_path(spec, _query(series_key="USA.M.HICP.CPI.PC..N.")) == (
            f"/data/{UPSTREAM_DATAFLOW}/USA.M.HICP.CPI.PC..N."
        )

    def test_params_cannot_be_mapped_through_a_code_table(self) -> None:
        spec = _key_spec()
        rendered = render_path(spec, _query(series_key="united_states.M.HICP"))
        # Upstream would send USA.M.HICP here; the engine has no field to say so and passes the
        # caller's text through, which addresses the wrong resource rather than raising an error.
        assert rendered.endswith("/united_states.M.HICP")
        assert "USA" not in rendered
        assert "value_map" not in {field.name for field in dataclasses.fields(ParamSpec)}

    def test_a_joined_multi_country_key_is_refused_before_any_send(self) -> None:
        spec = _key_spec()
        with pytest.raises(ProviderEngineError) as caught:
            render_path(spec, _query(series_key="USA+CAN.M.HICP"))
        assert caught.value.code == "OECD_PARAM_UNENCODABLE"
        # The validator is a character class, not a blacklist: the separator upstream joins codes
        # with is the one character a segment cannot carry, while its neighbours can.
        assert http_json._PATH_SEGMENT.match("USA+CAN") is None
        assert http_json._PATH_SEGMENT.match("USA-CAN.M.HICP") is not None

    def test_the_dataflow_literal_is_not_the_blocker_the_mapped_key_is(self) -> None:
        # The claim this module retracts about itself: a `FLOW,DSD@DF_X,1.0` template renders,
        # because only a substituted *value* is validated as one segment. A refusal that rested on
        # the comma would be wrong, so the refusals rest on the mapped key that follows the literal.
        spec = _key_spec()
        assert render_path(spec, _query(series_key="G")) == f"/data/{UPSTREAM_DATAFLOW}/G"

    def test_the_fixed_query_keys_cannot_ride_in_the_path(self) -> None:
        # Upstream appends `dimensionAtObservation=...&detail=dataonly` after a `?` in the literal.
        with pytest.raises(ValueError) as caught:
            _key_spec(path="/data/G?dimensionAtObservation=TIME_PERIOD&detail=dataonly")
        assert "bare absolute path" in str(caught.value)
        # And a declaration has no field for a key no upstream parameter backs:
        assert "static_params" not in {field.name for field in dataclasses.fields(ModelSpec)}

    def test_a_comparison_row_filter_cannot_window_the_dates(self) -> None:
        # consumer_price_index.py:246-248 keeps rows by `date >= start_date and date <= end_date`.
        with pytest.raises(ValueError) as caught:
            RowFilterSpec(op="gte", columns=["date"], value="2020")
        assert "not one of" in str(caught.value)
        assert RowFilterSpec(op="equals", columns=["date"], value="2020").op == "equals"
        assert {"gte", "lte", "between"}.isdisjoint(set(get_args(RowFilterOp)))

    def test_a_column_can_be_renamed_and_cast_but_never_derived(self) -> None:
        spec = _key_spec(columns=(ColumnSpec("country", "str", source_key="REF_AREA"),))
        row = normalize_record({"REF_AREA": "USA"}, None, spec, build_row_model(spec))
        # The one thing a column can do -- read under another key -- works (that is the control)...
        assert row.country == "USA"
        # ...and it is all it can do: upstream maps that code back to a country name and divides the
        # value by 100 (consumer_price_index.py:242-250), which has no field to declare.
        assert {"map", "scale", "derive", "formula"}.isdisjoint(
            {field.name for field in dataclasses.fields(ColumnSpec)}
        )

    def test_the_census_needs_that_do_ship_are_not_what_blocks_these_rows(self) -> None:
        # Two of the needs the census names are shipped capabilities, so a refusal that rested on
        # them would be stale: the delimited decoder and the client-side filter both exist now.
        assert "csv" in DELIMITED_DECODERS
        assert _key_spec(decoder=DecoderSpec(kind="csv")).decoder.delimited
        assert RowFilterSpec(op="contains", columns=["REF_AREA"], value="USA").op == "contains"
        shipped = {"csv_decoder", "client_side_row_filter"}
        for record in census_oecd_rows():
            remaining = set(record["needs_engine_capability"]) - shipped
            assert remaining, f"{record['upstream_model']}: nothing is left to block it"
            # What is left is the key composition every blocker names, row after row.
            assert remaining == {"sdmx_dotted_key_path"}, record["upstream_model"]
