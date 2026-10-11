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
from datetime import date
from pathlib import Path
from typing import Any, Final, cast, get_args

import pytest

from opendata.data.domains import load_domains, require_domain
from opendata.data.provider import LazyFetcherBinding
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    build_row_model,
    encode_query,
    normalize_record,
    render_path,
)
from opendata.data.providers._engine.key_composition import (
    CompositeKeySpec,
    KeyComponent,
    KeyCompositionError,
    render_composite_key,
)
from opendata.data.providers._engine.spec import (
    DELIMITED_DECODERS,
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    ParamSpec,
    RowFilterOp,
    RowFilterSpec,
    ValueKind,
)
from opendata.data.providers._engine.value_transform import (
    STAGE_SEQUENCE,
    CastSpec,
    DerivePlan,
    DeriveStage,
    DeriveStageKind,
    ScaleSpec,
    ValueTransformError,
    apply_cast,
)
from opendata.data.providers.catalog import (
    PROVIDERS,
    engine_declared_models,
    get_provider,
    register_provider,
)
from opendata.data.providers.oecd import specs
from opendata.data.registry import ProviderRegistry
from scripts.quality import model_capability_census as mcc
from scripts.quality.provider_model_inventory import LEDGER_RELATIVE_PATH, load_task_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The per-model survey oecd's nine rows were read into, and the denominator the blockers answer to.
CENSUS_RELATIVE_PATH = Path(
    "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-imf-oecd-famafrench-misc.json"
)

#: The capability names the blockers may use, each one defined -- with the engine line that shows it
#: missing -- in the specs module's docstring. A blocker naming an undefined capability would be an
#: assertion rather than a measurement.
#:
#: C89 re-read this list against the current engine. ``params.value_map`` and ``fixed query keys``
#: are gone from it because both shipped (:attr:`ParamSpec.value_map`,
#: :attr:`ModelSpec.static_query`) and a probe below runs each one's accepting arm, so leaving them
#: in the blockers would have been a false refusal; ``columns.period_cast``, ``derive.conditional``
#: and the lookup half of ``columns.compose`` joined it as the capability the derived-column seam
#: (``ModelSpec.column_derives``) still does not cover.
CAPABILITY_VOCABULARY = (
    "path.list_segment",
    "columns.compose",
    "columns.period_cast",
    "derive.conditional",
    "row_filter.comparison",
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


#: The CPI code table pinned upstream (consumer_price_index.py:203) as one parameter's map, so the
#: probe below can show the same key rendering with and without the field.
_SERIES_KEY_WITH_CODE_TABLE: Final = ParamSpec(
    "series_key", "str", required=True, value_map=(("united_states.M.HICP", "USA.M.HICP"),)
)


class TestTheNineRowAccounting:
    """Declared plus recorded equals the nine ledger rows, and nothing is declared."""

    def test_package_rows_are_the_ledger_rows(self) -> None:
        from_ledger = ledger_oecd_rows()
        assert sorted(specs.LEDGER_ROWS) == sorted(from_ledger)
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
        # The retraction's own scope, recomputed rather than remembered: oecd names no domain in the
        # table, and every domain a registered provider still names is in it -- so nothing else left
        # the table when oecd's two entries did. What stood here was a typed whole-table count that
        # read 41 while the registry carried 37 (42 since four intrinio legs and the Svensson leg
        # joined), so it could not have failed for a vanished or duplicated domain; the table is
        # guarded by name in tests/test_authority_reconciliation.py, which partitions it against the
        # served set.
        assert [domain for domain in domains if domain.startswith("oecd")] == []
        named_by_provider = {
            capability.domain
            for provider in PROVIDERS
            for capability in register_provider(provider.source, ProviderRegistry())
        }
        assert named_by_provider <= set(domains), sorted(named_by_provider - set(domains))
        assert sorted(d for d in domains if d.startswith("cboe_")) == [
            "cboe_available_indices",
            "cboe_equity_search",
            "cboe_index_constituent_quotes",
            "cboe_index_search",
        ]


class TestTheComposedKeyBlockersReMeasured:
    """Each capability a blocker names, re-measured by declaring the request the upstream builds.

    These are the probes that make a refusal a finding rather than a claim: the pinned CPI model's
    key is ``{country}.{frequency}.{methodology}.CPI.{units}.{expenditure}.N.`` with every part read
    out of a code table (consumer_price_index.py:203-205), so the questions are whether the engine
    can map a value, join the mapped positions into one segment, carry a ``+``, send a key whose
    positions are empty, or derive a column. Two of those six answers changed when
    :attr:`~ModelSpec.composite_keys` and :attr:`~ParamSpec.value_map` shipped: the probes below run
    the same declaration against the live engine either way, so what they report is the engine of
    this checkout rather than a remembered verdict.
    """

    def test_positive_control_a_single_segment_value_is_carried_verbatim(self) -> None:
        spec = _key_spec()
        assert spec.path_placeholders == ("series_key",)
        # A whole key with no illegal character renders -- the instrument works.
        assert render_path(spec, _query(series_key="USA.M.HICP.CPI.PC..N.")) == (
            f"/data/{UPSTREAM_DATAFLOW}/USA.M.HICP.CPI.PC..N."
        )

    def test_a_code_table_and_a_joined_key_are_both_declareable(self) -> None:
        """The two halves compose: a mapped value lands in the position that names its parameter."""
        spec = _key_spec(
            params=(
                ParamSpec("area", "str", required=True, value_map=(("united_states", "USA"),)),
                ParamSpec("freq", "str", required=True, value_map=(("monthly", "M"),)),
            ),
            composite_keys=(
                CompositeKeySpec(
                    name="series_key",
                    separator=".",
                    components=(
                        KeyComponent("area", source="area"),
                        KeyComponent("freq", source="freq"),
                        KeyComponent("measure", kind="literal", source="CPI"),
                    ),
                ),
            ),
        )
        # Upstream is addressed by ``USA.M.CPI``: the caller-facing spellings are what the record
        # accepts, and the code-table tokens are what the wire carries.
        assert render_path(spec, _query(area="united_states", freq="monthly")) == (
            f"/data/{UPSTREAM_DATAFLOW}/USA.M.CPI"
        )
        # A position is not also sent in the query string, and the arm that can differ: a value with
        # no declared upstream spelling is refused rather than passed through as the caller's text.
        assert encode_query(_query(area="united_states", freq="monthly"), spec) == {}
        with pytest.raises(ProviderEngineError) as caught:
            render_path(spec, _query(area="mexico", freq="monthly"))
        assert caught.value.code == "OECD_PARAM_VALUE_UNMAPPED"
        assert caught.value.rejected == ("area",)

    def test_the_mapped_value_is_what_ships_not_the_callers_text(self) -> None:
        # The probe as it read before the seam shipped: a parameter carried no field for the token
        # the endpoint expects, so ``united_states`` addressed the wrong resource silently. Both
        # sides run here, because the unmapped passthrough is the arm the new field has to differ
        # from -- a probe that only ever maps proves nothing about the map.
        assert "value_map" in {field.name for field in dataclasses.fields(ParamSpec)}
        unmapped = _key_spec(params=(ParamSpec("series_key", "str", required=True),))
        assert render_path(unmapped, _query(series_key="united_states.M.HICP")).endswith(
            "/united_states.M.HICP"
        )
        mapped = _key_spec(params=(_SERIES_KEY_WITH_CODE_TABLE,))
        assert render_path(mapped, _query(series_key="united_states.M.HICP")).endswith(
            "/USA.M.HICP"
        )

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

    def test_the_fixed_query_keys_cannot_ride_in_the_path_but_do_have_a_field(self) -> None:
        # Upstream appends `dimensionAtObservation=...&detail=dataonly` after a `?` in the literal.
        with pytest.raises(ValueError) as caught:
            _key_spec(path="/data/G?dimensionAtObservation=TIME_PERIOD&detail=dataonly")
        assert "bare absolute path" in str(caught.value)
        # And a key no upstream parameter backs does have a field: a constant on every request.
        spec = _key_spec(
            static_query=(("dimensionAtObservation", "TIME_PERIOD"), ("detail", "dataonly"))
        )
        sent = encode_query(_query(series_key="USA.M"), spec)
        assert sent == {"dimensionAtObservation": "TIME_PERIOD", "detail": "dataonly"}
        # A caller may not overwrite the addressing the model was reviewed against.
        assert encode_query(_query(series_key="USA.M", detail="everything"), spec) == sent

    def test_a_comparison_row_filter_cannot_window_the_dates(self) -> None:
        # consumer_price_index.py:246-248 keeps rows by `date >= start_date and date <= end_date`.
        with pytest.raises(ValueError) as caught:
            RowFilterSpec(op=cast("RowFilterOp", "gte"), columns=("date",), value="2020")
        assert "not one of" in str(caught.value)
        assert RowFilterSpec(op="equals", columns=("date",), value="2020").op == "equals"
        assert {"gte", "lte", "between"}.isdisjoint(set(get_args(RowFilterOp)))

    def test_a_column_can_be_renamed_rescaled_or_cast_but_never_looked_up(self) -> None:
        # The rename arm: a published dimension key read out under the ledger's column name.
        spec = _key_spec(columns=(ColumnSpec("country", "str", source_key="REF_AREA"),))
        row = normalize_record({"REF_AREA": "USA"}, None, spec, build_row_model(spec))
        assert row.model_dump()["country"] == "USA"
        # The two derive arms that shipped since C75, run as positive controls rather than recalled:
        # a percent-valued OBS_VALUE divided by 100 is a declaration, not a missing capability.
        scaled = _key_spec(
            columns=(ColumnSpec("value", "float", source_key="OBS_VALUE"),),
            column_derives=(
                DerivePlan(
                    column="value",
                    stages=(DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction()),),
                ),
            ),
        )
        scaled_row = normalize_record({"OBS_VALUE": "4.33"}, None, scaled, build_row_model(scaled))
        assert scaled_row.model_dump()["value"] == pytest.approx(0.0433)
        # And the arm the nine still need: upstream maps that code back to a country name and
        # rewrites it (unemployment.py:169, composite_leading_indicator.py:203-210). The kernel runs
        # a cast and a rescale and nothing else, so a code table has no stage to be declared with.
        with pytest.raises(ValueError, match="the kernel runs a cast and a rescale"):
            DeriveStage(kind=cast("DeriveStageKind", "map"))
        assert set(STAGE_SEQUENCE) == {"cast", "scale"}
        assert {"map", "scale", "derive", "formula", "value_map"}.isdisjoint(
            {field.name for field in dataclasses.fields(ColumnSpec)}
        )

    def test_the_census_asks_for_six_capabilities_the_engine_lacks(self) -> None:
        """Publish the nine rows' blockers as six absent engine fields, re-read on every run.

        Before C89 re-audited them line by line (docs/evidence/C89/oecd-blocker-recheck.md) this
        probe read the other way -- it subtracted the two capabilities that had just shipped and
        reported nothing missing, which is how nine undeclarable rows came to look ready. Both arms
        below move that reading, so what passes is the instrument's verdict on this checkout.
        """
        assert "csv" in DELIMITED_DECODERS
        assert _key_spec(decoder=DecoderSpec(kind="csv")).decoder.delimited
        assert RowFilterSpec(op="contains", columns=("REF_AREA",), value="USA").op == "contains"
        report = mcc.run(
            round_id="OECD-PROBE", root=REPO_ROOT, census_dir=REPO_ROOT / mcc.CENSUS_DIR_REL
        )
        present = set(report.statuses["present"])
        asked_by_row = {
            record["upstream_model"]: {
                mcc.LABEL_TO_CAPABILITY[label] for label in record["needs_engine_capability"]
            }
            for record in census_oecd_rows()
        }
        blocked = {model: caps - present for model, caps in asked_by_row.items()}
        missing = sorted({cap for caps in blocked.values() for cap in caps})
        assert missing == [
            "columns.compose",
            "columns.period_cast",
            "derive.conditional",
            "flow.branch",
            "path.list_segment",
            "rows.filter_comparison",
        ], blocked
        # Every one of the six reads ``absent``, not ``drifted`` or ``unanchored``: the registry's
        # own proof line is what refuses it, so the blocker names a capability and not a spelling.
        assert set(missing) <= set(report.statuses["absent"])
        # The part of the need set already satisfied, counted rather than assumed: all nine want a
        # delimited decoder and a composed key segment, and one -- CPI, the only leg with a date
        # window -- also a row filter.
        assert {cap for caps in asked_by_row.values() for cap in caps & present} == {
            "decode.delimited",
            "flow.composite_key_segment",
            "rows.filter",
        }
        assert [m for m, caps in asked_by_row.items() if "rows.filter" in caps] == [
            "ConsumerPriceIndex"
        ]
        # C89's split is visible in the same reading: eight legs cannot survive one date format, and
        # the one that can is the leg whose key pins ``M`` (the sibling probe shows both arms).
        assert sorted(m for m, caps in blocked.items() if "columns.period_cast" in caps) == [
            "ConsumerPriceIndex",
            "CountryInterestRates",
            "GdpForecast",
            "GdpNominal",
            "GdpReal",
            "HousePriceIndex",
            "SharePriceIndex",
            "Unemployment",
        ]
        assert [m for m, caps in blocked.items() if "columns.period_cast" not in caps] == [
            "CompositeLeadingIndicator"
        ]
        assert [m for m, caps in blocked.items() if "flow.branch" in caps] == ["HousePriceIndex"]
        # None of the nine is in the roadmap's ready set -- the face the false reading published.
        assert not [row for row in report.roadmap.shipped_cover if row.provider == "oecd"]
        # Arm one, the false-ready face itself: count the six as shipped and all nine ask nothing.
        collapsed = present | set(missing)
        assert all(not (caps - collapsed) for caps in asked_by_row.values())
        # Arm two, the reverse damage: take back the key-segment capability C88 wired and a seventh
        # name joins the blockers on all nine legs, so the pass reading is not a standing zero.
        unwired = present - {"flow.composite_key_segment"}
        relapsed = {model: caps - unwired for model, caps in asked_by_row.items()}
        assert {cap for caps in relapsed.values() for cap in caps} == set(missing) | {
            "flow.composite_key_segment"
        }
        assert len([m for m, caps in relapsed.items() if caps]) == 9


class TestTheC89BlockersReMeasured:
    """The four capability names C89 added to the blockers, each measured off the kernel.

    The probes above answer whether a mapped, composed, constant-keyed SDMX request can be declared
    -- it can, and three of C75's six blockers were wrong about that. What the nine rows still ask
    for is a value that is *restated* rather than copied: a code looked up in a table, a published
    period read under whichever width its frequency gives it, an arithmetic step one other parameter
    decides, and (for the house-price leg) a second request. Each probe below ships the accepting
    arm first, so what it reports is the boundary and not a typo.
    """

    def test_one_declared_date_format_widens_no_sdmx_period(self) -> None:
        # The accepting arm, which is the CLI case: its key pins frequency to ``M``
        # (composite_leading_indicator.py:181), so every observation is ``YYYY-MM``.
        monthly = CastSpec.date_string("%Y-%m")
        assert apply_cast(monthly, "2024-03") == date(2024, 3, 1)
        # The refusing arms, which are the other eight: oecd_date_to_python_date branches on the
        # published text's width -- ``YYYY``, ``YYYY-MM``, ``YYYY-Qn`` (utils/helpers.py:253-261) --
        # and a CastSpec carries exactly one format string, not a set.
        assert [field.name for field in dataclasses.fields(CastSpec)].count("input_format") == 1
        for published in ("2024", "2024-Q1", "2024-03-01"):
            with pytest.raises(ValueTransformError, match="does not match declared format"):
                apply_cast(monthly, published)

    def test_a_derive_plan_can_be_gated_on_nothing_but_a_row_filter_can(self) -> None:
        # Gating exists in the format -- for row filters, which is the control that keeps the next
        # assertion from reading as a blanket "no conditional anywhere".
        assert (
            RowFilterSpec(op="equals", columns=("REF_AREA",), value_param="area").value_param
            == "area"
        )
        plan = DerivePlan(
            column="value",
            stages=(DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction()),),
        )
        assert {field.name for field in dataclasses.fields(plan)} == {
            "column",
            "note",
            "source",
            "source_kind",
            "stages",
        }
        # Six rows stage on a parameter: ``/100`` only for a percent transform
        # (consumer_price_index.py:249-250), an ``int`` cast only for levels
        # (gdp_nominal.py:186-187), one or the other by units (gdp_forecast.py:235-240). The plan
        # binds a column, a source and stages, and nothing decides between two of them.
        assert {"when", "when_param", "gate", "unless", "branches"}.isdisjoint(
            {field.name for field in dataclasses.fields(DerivePlan)}
        )

    def test_a_record_drives_one_path_template_and_no_rewritten_second_request(self) -> None:
        # The control: the template itself is declarable and composes, so the blocker is the retry.
        spec = _key_spec(
            params=(
                ParamSpec("area", "str", required=True, value_map=(("united_states", "USA"),)),
            ),
            composite_keys=(
                CompositeKeySpec(
                    name="series_key",
                    components=(KeyComponent("area", source="area"),),
                ),
            ),
        )
        assert render_path(spec, _query(area="united_states")).endswith("/USA")
        # house_price_index.py:134-138 re-issues the same url with ``.M.`` replaced by ``.Q.`` after
        # a 404. ``path`` is one template, ``pagination`` repeats it with new page keys, and the
        # record has no field for a second addressing or a status to branch on.
        names = {field.name for field in dataclasses.fields(ModelSpec)}
        assert {"path", "pagination", "static_query"} <= names
        assert {"fallback_path", "retry_path", "alternative_path", "requests"}.isdisjoint(names)

    def test_the_joined_country_list_and_its_empty_sentinel_are_both_refused(self) -> None:
        # The control: one mapped code in one position composes, which is what C75 got wrong.
        key = CompositeKeySpec(
            name="dimension_key",
            error_code="OECD_KEY_COMPOSITION_INVALID",
            components=(
                KeyComponent("area", source="area", on_missing="default", default=""),
                KeyComponent("freq", kind="literal", source="M"),
            ),
        )
        assert render_composite_key(key, {"area": "USA"}) == "USA.M"
        # The omitted leg renders upstream's ``all`` wildcard, which is why the sentinel needs no
        # map entry -- but a caller who *says* ``all`` (unemployment.py:140-141) cannot be mapped to
        # it, and a bool flag cannot be mapped to ``Y``/``N`` at all (unemployment.py:137).
        assert render_composite_key(key, {}) == ".M"
        assert KeyComponent("wild", on_missing="default", default="").default == ""
        for entry, kind in ((("all", ""), "str"), (("true", "Y"), "bool")):
            with pytest.raises(ValueError):
                ParamSpec("area", cast("ValueKind", kind), value_map=(entry,))
        # And the joined list every multi-country query builds ("+".join, six of nine files): a
        # position is one value on one separator, and the charset refuses both spellings of a list.
        for joined in ("USA+CAN", ["USA", "CAN"]):
            with pytest.raises(KeyCompositionError) as caught:
                render_composite_key(key, {"area": joined})
            assert caught.value.reason == "charset_invalid"
            assert caught.value.component == "area"
