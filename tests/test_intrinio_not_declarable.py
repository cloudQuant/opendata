"""Intrinio's 38-row accounting: thirty-five routable models, three recorded refusals.

The provider's promise is that a censused model is either routable or written down with the
declaration-plane capability that blocks it. This file judges the second half for Intrinio, offline:
no socket, no API key, and no live call. It reads the three iteration-2 census artifacts as the
denominator, re-measures
:data:`opendata.data.providers.intrinio.specs.NOT_DECLARABLE` against the live ``ModelSpec`` field
surface, and replays one hand-written two-page cursor fixture through the engine's transport seam to
witness the one claim the ledger's whole shape rests on -- that a published ``next_page`` cursor is
declarable today, so no row may be refused "because of paging".

Why the cursor witness matters: the census instrument has no key for "the pages come from a cursor
the response publishes" and folds that need onto ``request.body_pagination`` for nine intrinio rows.
A refusal copied from that mapping would block rows the declaration plane can already page, and a
refusal that names a capability the engine ships is the same defect in the other direction -- so the
ledger's primary blockers are checked against the shipped vocabulary here rather than trusted.

That second check is not academic: ``param.relative_default`` arrived in the middle of this round
(``RelativeDefault``, and ``http_json`` performing it at query time), which is exactly the moment a
ledger written against the earlier plane turns dishonest. Two rows were withheld on it a round ago;
both are re-based here on the blocker that survives the new field, and the field's own shape is
frozen so the next widening of it -- a gate, say -- fails here before the ledger drifts.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.providers._engine.http_json import (
    HttpResponse,
    bound_request,
    encode_query,
    make_http_json_fetcher,
    render_path,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DerivePlan,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
    RelativeDefault,
    RowFilterSpec,
    RowOrderSpec,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SequencedResponseTransport,
    fixture_context,
)
from opendata.data.providers.catalog import engine_declared_models, get_provider
from opendata.data.providers.intrinio import specs
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.provider import PROVIDER
from scripts.quality.model_capability_census import LABEL_TO_CAPABILITY
from scripts.quality.model_capability_census import run as census_run

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The per-model survey intrinio's package must obey: one row per ``OBB2-intrinio-*`` task, carrying
#: the endpoint shape, the capability need labels and the ``expressible_today`` flag.
CENSUS_PATHS: tuple[Path, ...] = tuple(
    REPO_ROOT
    / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
    / f"census-intrinio-{part}of3.json"
    for part in (1, 2, 3)
)

#: A blocker is a measurement, so it has to point at the upstream line that shows it.
CITATION = re.compile(r"[A-Za-z0-9_./-]+\.py:\d+")


#: The canonical capability names the shared engine ships today -- read from the census judge rather
#: than restated here. ``verify_registry`` anchors every entry against ``ModelSpec``'s field
#: surface and the engine module that performs it, so a kernel that lands in ``_engine`` reopens the
#: refusals that named it instead of leaving them standing as stale prose. This set used to be typed
#: by hand and omitted ``columns.null_sentinel`` after C97 shipped that kernel, which is how
#: ``InsiderTrading`` stayed recorded on a capability the engine performs. The derivation is
#: and the control below is the witness that the reopened direction fails rather than mutes.
@functools.lru_cache(maxsize=1)
def shipped_canonical() -> frozenset[str]:
    """The registry entries the judge reads as present in this tree, live."""
    readings = census_run(round_id="refusal-guard").readings
    return frozenset(name for name, reading in readings.items() if reading.present)


#: Every canonical name :data:`specs.NOT_DECLARABLE` is allowed to block on. ``flow.fan_out`` and
#: ``flow.join`` name the two pure kernels in ``_engine/multi_request.py`` that no ``ModelSpec``
#: field reaches; ``request.body_pagination`` is deliberately absent -- the engine does page by
#: cursor, which is what the cursor witness below proves. ``param.relative_default`` left this list
#: the round it shipped: a default the engine performs cannot block a declaration.
BLOCKERS: tuple[str, ...] = (
    "flow.fan_out",
    "flow.branch",
    "flow.join",
    "flow.preflight_lookup",
    "param.value_transform",
    "derive.conditional",
    "columns.null_sentinel",
    "columns.response_dependent_selection",
    "columns.compose",
    "rows.cap_source_order",
    "path.object_keys",
    "path.assemble_single_row",
    "request.caller_url",
    "shape.columnar",
)

#: The vocabulary-gap marker: one row's need has no canonical name at all, and says so.
GAP_MARKER = "no canonical names it"


def census_rows() -> dict[str, dict[str, Any]]:
    """The 38 intrinio census rows, keyed by ``upstream_model``."""
    rows: list[dict[str, Any]] = []
    for path in CENSUS_PATHS:
        assert path.exists(), f"the census artifact moved: {path}"
        rows.extend(json.loads(path.read_text(encoding="utf-8"))["rows"])
    keyed = {row["upstream_model"]: row for row in rows}
    assert len(keyed) == len(rows) == 38
    return keyed


def primary_blocker(reason: str) -> str | None:
    """The capability a refusal names first, i.e. the one that alone stops the declaration."""
    found = [(reason.find(token), token) for token in BLOCKERS if token in reason]
    return min(found)[1] if found else None


def canonical_needs(row: dict[str, Any]) -> frozenset[str]:
    """One census row's need labels, in the canonical vocabulary the instrument itself uses."""
    labels: list[str] = list(row["needs_engine_capability"])
    return frozenset(LABEL_TO_CAPABILITY.get(label, label) for label in labels)


class TestTheThirtyEightRowAccounting:
    """Declared plus recorded equals the census denominator, exactly once each."""

    def test_the_package_rows_are_the_census_rows(self) -> None:
        assert sorted(specs.UPSTREAM_ROWS) == sorted(census_rows())
        assert len(specs.UPSTREAM_ROWS) == 38
        assert len(set(specs.UPSTREAM_ROWS)) == 38
        assert all(
            row["task_id"] == f"OBB2-intrinio-{model}" for model, row in census_rows().items()
        )

    def test_every_row_is_declared_or_recorded_once(self) -> None:
        declared = set(specs.DECLARED_MODELS)
        recorded = set(specs.NOT_DECLARABLE)
        assert declared.isdisjoint(recorded), "a row is both declared and recorded"
        assert declared | recorded == set(specs.UPSTREAM_ROWS), (
            "the package does not account for every census row: "
            f"{sorted(set(specs.UPSTREAM_ROWS) - declared - recorded)}"
        )
        assert len(declared) == 35
        assert len(recorded) == 3

    def test_declared_models_are_the_runtime_bindings(self) -> None:
        """The ledger's declared half is what the runtime can actually route -- no paper model."""
        engine_bound = {spec.model for source, spec in engine_declared_models() if source == SOURCE}
        assert engine_bound == set(specs.ENGINE_MODELS)
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert bound == set(specs.DECLARED_MODELS)
        assert len(PROVIDER.fetcher_bindings) == len(specs.DECLARED_MODELS)
        assert get_provider("intrinio").credentials[0].environment_variable == "INTRINIO_API_KEY"

    def test_clean_typed_module_is_present_and_quarantined_modules_are_absent(self) -> None:
        """Keep the accepted clean slice while ensuring exposed worker files leave the package."""
        modules = {
            path.name
            for path in (REPO_ROOT / "opendata/data/providers/intrinio/models").iterdir()
            if path.suffix == ".py"
        }
        assert {
            "__init__.py",
            "calendar_ipo.py",
            "currency_pairs.py",
            "equity_search.py",
            "historical_dividends.py",
            "insider_trading.py",
            "iter2_market_clean.py",
        } <= modules
        assert (
            not {
                "fixed.py",
                "equity_info.py",
                "equity_historical.py",
                "etf_info.py",
                "historical_market_cap.py",
                "index_historical.py",
                "iter2_market.py",
            }
            & modules
        )


class TestEveryRefusalIsAMeasurement:
    """A blocker names a missing capability, cites the line, and is not about effort."""

    @pytest.mark.parametrize("model", sorted(specs.NOT_DECLARABLE))
    def test_refusal_is_short_cited_and_names_a_missing_capability(self, model: str) -> None:
        reason = specs.NOT_DECLARABLE[model]
        assert reason.strip(), model
        assert len(reason) <= 320, f"{model}: a blocker a reviewer cannot hold is a design note"
        assert CITATION.search(reason), f"{model}: the blocker cites no upstream line"
        if GAP_MARKER in reason:
            assert "rows.order_limit" in reason, f"{model}: a gap must name its nearest capability"
            return
        primary = primary_blocker(reason)
        assert primary is not None, f"{model}: the blocker names no canonical capability"
        assert primary not in shipped_canonical(), (
            f"{model}: {primary} is shipped, so this row should be declared, not recorded"
        )

    def test_a_refusal_naming_a_shipped_capability_is_caught_not_muted(self) -> None:
        """The reopened direction has a witness: a shipped kernel must fail its old refusal.

        ``columns.null_sentinel`` shipped in C97 and the hand-typed set below this line never grew,
        so a blocker naming it passed the guard one round for free. The derived set carries it, and
        the same parse the parametrized test runs is pointed at a synthetic reason, so this fails if
        the derivation ever turns back into a constant -- or if a future kernel ships and its
        refusals are left standing.
        """
        shipped = shipped_canonical()
        assert "columns.null_sentinel" in shipped, "the derivation no longer reads the live engine"
        synthetic = "columns.null_sentinel: zeros nulled per row (models/probe.py:1)"
        assert primary_blocker(synthetic) in shipped

    @pytest.mark.parametrize("model", sorted(census_rows()))
    def test_the_census_denies_every_recorded_row(self, model: str) -> None:
        """``expressible_today`` and the ledger agree in both directions."""
        row = census_rows()[model]
        if model in specs.NOT_DECLARABLE:
            assert row["expressible_today"] is False, f"{model}: declared elsewhere, see below"
            assert row["needs_engine_capability"], model
        else:
            assert model in specs.DECLARED_MODELS, model

    def test_the_declared_half_is_now_credited_by_the_census_too(self) -> None:
        """HistoricalDividends' recorded divergence closed the round the engine grew the carrier.

        A round ago the census denied this row on ``param.relative_default`` while the package
        declared it: the upstream one-year default window (historical_dividends.py:74-78) had no
        carrier, so an omitted bound went unsent, and ``HISTORICAL_DIVIDENDS.notes`` wrote that
        down as a divergence instead of hiding it. ``RelativeDefault`` is the carrier that was
        missing, and the artifact credits a declared row only once its whole need set ships, so
        two halves are
        re-measured here rather than left as a claim. One divergence does still stand, and the
        notes still say so: there is no parameter-normalization facet, so a lowercase ``symbol``
        reaches a lowercase path.
        """
        credited = [
            model
            for model in specs.ENGINE_MODELS
            if census_rows()[model]["expressible_today"] is True
        ]
        assert credited == sorted(specs.ENGINE_MODELS)
        window = census_rows()["HistoricalDividends"]["needs_engine_capability"]
        assert "relative_date_default" in window
        bounds = sorted(
            parameter.name
            for parameter in specs.HISTORICAL_DIVIDENDS.params
            if parameter.relative_default is not None
        )
        assert bounds == ["end_date", "start_date"]
        notes = str(specs.HISTORICAL_DIVIDENDS.notes)
        assert "DIVERGENCE" in notes and "case-fold" in notes

    def test_relative_default_no_longer_blocks_a_row_anywhere(self) -> None:
        """The refusal vocabulary re-read against the field the engine grew mid-round.

        ``RelativeDefault`` carries an offset and nothing else -- no gate -- so a default
        conditioned on another parameter remains outside that declaration-plane field. The two
        rows withheld on ``param.relative_default`` a round ago are now routed: CompanyFilings has
        a typed fetcher, while InsiderTrading left the ledger when
        ``columns.null_sentinel`` shipped.
        """
        assert {field.name for field in dataclasses.fields(RelativeDefault)} == {
            "years",
            "months",
            "days",
        }
        assert not any(
            "param.relative_default" in reason for reason in specs.NOT_DECLARABLE.values()
        )
        assert "CompanyFilings" in specs.DECLARED_MODELS
        assert "CompanyFilings" not in specs.NOT_DECLARABLE
        assert "InsiderTrading" not in specs.NOT_DECLARABLE, (
            "C97 shipped columns.null_sentinel, so this row must be declared, not recorded"
        )

    def test_each_refusal_names_a_need_its_census_row_records(self) -> None:
        """Reconciliation: all three current refusals quote a need their census row records.

        The comparison is on canonical capability names, not the census' free-text labels. The
        seven news/attributes rows that previously exposed two ledger mismatches are now routed by
        typed fetchers, so the remaining refused rows have a named blocker that intersects their
        recorded need set.
        """
        gap_models = sorted(
            m for m, r in specs.NOT_DECLARABLE.items() if primary_blocker(r) is None
        )
        assert gap_models == [], (
            f"a refusal that names no canonical blocker is a vocabulary gap: {gap_models}"
        )
        silent = [
            model
            for model, reason in specs.NOT_DECLARABLE.items()
            if not (
                canonical_needs(census_rows()[model])
                & {token for token in BLOCKERS if token in reason}
            )
        ]
        assert silent == [], f"the ledger and the census no longer agree on {sorted(silent)}"
        named = [
            model
            for model in specs.NOT_DECLARABLE
            if model not in silent and primary_blocker(specs.NOT_DECLARABLE[model]) is not None
        ]
        assert len(named) == 3, f"{len(named)} of 3 refusals reconcile, not the documented 3"
        assert {"CompanyFilings", "WorldNews"} <= set(specs.DECLARED_MODELS)

    def test_no_refusal_is_about_effort(self) -> None:
        banned = re.compile(r"\b(hard|later|todo|too complex|not written yet|effort|manual)\b")
        for model, reason in specs.NOT_DECLARABLE.items():
            found = banned.search(reason.lower())
            assert found is None, f"{model}: a blocker that says {found!r} is not measured"

    def test_the_documented_histogram_is_the_measured_one(self) -> None:
        """The docstring's counts are a reading, so they are re-derived here."""
        histogram: dict[str, int] = {}
        for reason in specs.NOT_DECLARABLE.values():
            key = primary_blocker(reason) or "unnamed"
            histogram[key] = histogram.get(key, 0) + 1
        assert histogram == {
            "flow.join": 1,
            "flow.preflight_lookup": 2,
        }
        assert sum(histogram.values()) == len(specs.NOT_DECLARABLE)
        assert "param.relative_default" not in histogram, (
            "the engine performs this default now, so it cannot be a primary blocker"
        )
        assert all(primary_blocker(reason) is not None for reason in specs.NOT_DECLARABLE.values())


class TestTheSpecPlaneStillRefusesThem:
    """The refusals are about the declaration format, re-read from the live dataclass."""

    def test_the_spec_field_surface_has_not_grown_a_carrier(self) -> None:
        """A new ``ModelSpec`` field means this ledger has to be re-measured, not left as is.

        Freezing the field names is the cheapest honest tripwire: the day fan-out, join, branch,
        preflight or a body becomes declarable, the wave that adds it fails here first.
        """
        assert {field.name for field in dataclasses.fields(ModelSpec)} == {
            "model",
            "domain",
            "asset_class",
            "period",
            "market",
            "base_url",
            "path",
            "columns",
            "params",
            "rows_pointer",
            "pagination",
            "credential",
            "credential_query_key",
            "async_mode",
            "scenario",
            "error_prefix",
            "row_envelope",
            "static_headers",
            "static_query",
            "notes",
            "decoder",
            "row_filters",
            "composite_keys",
            "row_expansion",
            "column_derives",
            "order_by",
        }

    @pytest.mark.parametrize(
        "keyword",
        (
            "fan_out",
            "join",
            "branch",
            "preflight",
            "request_body",
            "body_pagination",
            "relative_default",
            "null_sentinel",
            "cap_source_order",
            "value_transform",
        ),
    )
    def test_no_keyword_constructs_a_declaration(self, keyword: str) -> None:
        with pytest.raises(TypeError):
            ModelSpec(model="IntrinioProbe", **{keyword: True})

    def test_the_two_pure_kernels_stay_unreachable_from_here(self) -> None:
        """``multi_request``/``request_body`` exist; nothing in the spec plane names them."""
        source_text = (REPO_ROOT / "opendata/data/providers/_engine/spec.py").read_text(
            encoding="utf-8"
        )
        assert "multi_request" not in source_text
        assert "request_body" not in source_text
        assert "FanOutSpec" not in source_text
        assert "RequestBodySpec" not in source_text


CURSOR_PROBE = ModelSpec(
    model="IntrinioCursorProbe",
    domain="intrinio_cursor_probe",
    asset_class="equity",
    period="d",
    market="us",
    base_url="https://api.example.invalid",
    path="/probe",
    rows_pointer="rows",
    params=(ParamSpec("symbol", "str", required=True),),
    columns=(ColumnSpec("id", "str", required=True),),
    pagination=PaginationSpec(
        kind="cursor",
        cursor_key="next_page",
        cursor_field="next_page",
        max_pages=3,
    ),
    scenario="游标分页探针：证明响应发布的 next_page 游标是今天就能声明的",
    error_prefix="PROBE",
    notes=(
        "Not a registered model: this probe witnesses the ledger's cursor finding. Nine intrinio "
        "census rows fold a published-cursor loop onto request.body_pagination; the engine reads "
        "the cursor out of the body via PaginationSpec.cursor_field, so those rows are recorded on "
        "their real blockers instead."
    ),
)

PAGE_ONE = {"rows": [{"id": "A"}], "next_page": "cursor-1"}
PAGE_TWO = {"rows": [{"id": "B"}]}


class TestTheCursorFindingIsWitnessed:
    """One recorded two-page body, replayed offline: cursor paging needs no new capability."""

    def test_a_published_next_page_cursor_is_declarable_today(self) -> None:
        transport = SequencedResponseTransport(
            HttpResponse(200, PAGE_ONE),
            HttpResponse(200, PAGE_TWO),
        )
        generated = make_http_json_fetcher(SOURCE, CURSOR_PROBE)
        cls = type(generated.__name__, (generated,), {"http_transport": transport})
        rows = list(
            cls().fetch(
                ctx=fixture_context(SOURCE, CURSOR_PROBE, sends=2),
                symbol="AAPL",
            )
        )
        assert [row.id for row in rows] == ["A", "B"]
        assert len(transport.calls) == 2
        assert transport.calls[0]["params"].get("next_page") is None
        assert transport.calls[1]["params"]["next_page"] == "cursor-1"


RELATIVE_PROBE = ModelSpec(
    model="RelativeDefaultProbe",
    domain="probe_relative_default",
    asset_class="equity",
    period="d",
    market="us",
    base_url="https://api.example.invalid",
    path="/probe/{symbol}/window",
    rows_pointer="rows",
    params=(
        ParamSpec("symbol", "str", required=True),
        ParamSpec("start_date", "date", relative_default=RelativeDefault(years=-1)),
        ParamSpec("end_date", "date", relative_default=RelativeDefault()),
    ),
    columns=(ColumnSpec("id", "str", required=True),),
    scenario="相对日期默认探针：省略的界在请求侧解析，行侧的名字解析一律拒绝",
    error_prefix="PROBE",
)

CLOCK = date(2026, 3, 14)


def _omitted(**overrides: object) -> SimpleNamespace:
    """A validated-shaped query that named none of the bounds, for the request-side reads."""
    values: dict[str, object] = {"symbol": "AAPL", "start_date": None, "end_date": None}
    values.update(overrides)
    return SimpleNamespace(**values)


class TestTheRelativeDefaultIsFailClosed:
    """The field refuses every combination that would answer one omission two ways.

    A relative bound only earns its keep if the declaration plane cannot quietly write a bound
    that resolves to nothing: each arm below is a combination the reviewed upstreams never needed,
    and each one is a ``ValueError`` at construction rather than a literal substituted at the wire.
    """

    def test_an_offset_that_points_forward_has_no_upstream_to_be_checked_against(self) -> None:
        with pytest.raises(ValueError, match="points forward"):
            RelativeDefault(days=1)
        with pytest.raises(ValueError, match="whole number"):
            RelativeDefault(months="1")
        with pytest.raises(ValueError, match="whole number"):
            RelativeDefault(years=True)

    def test_a_relative_bound_resolves_against_a_bare_calendar_date_only(self) -> None:
        assert RelativeDefault(years=-1).resolve(CLOCK) == date(2025, 3, 14)
        assert RelativeDefault().resolve(CLOCK) == CLOCK
        # A month the target year does not have clamps to its last day, the way relativedelta does.
        assert RelativeDefault(months=-1).resolve(date(2024, 3, 31)) == date(2024, 2, 29)
        with pytest.raises(ValueError, match="datetime.date"):
            RelativeDefault().resolve(datetime(2026, 3, 14, 9))

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        (
            ({"kind": "str"}, "its kind is"),
            ({"default": "2020-01-01"}, "two answers"),
            ({"required": True}, "no omission"),
            ({"relative_default": RelativeDefault}, "must be a RelativeDefault"),
        ),
        ids=["wrong-kind", "literal-plus-relative", "required", "not-a-relative-default"],
    )
    def test_a_contradictory_parameter_declaration_is_refused(
        self, kwargs: dict[str, object], match: str
    ) -> None:
        fields: dict[str, object] = {
            "kind": "date",
            "relative_default": RelativeDefault(years=-1),
        }
        fields.update(kwargs)
        with pytest.raises(ValueError, match=match):
            ParamSpec("start_date", **fields)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("facet", "match"),
        (
            (
                {
                    "row_filters": (
                        RowFilterSpec(op="equals", columns=("id",), value_param="start_date"),
                    )
                },
                "row filter",
            ),
            (
                {
                    "column_derives": (
                        DerivePlan(
                            column="id",
                            source_kind="parameter",
                            source="start_date",
                        ),
                    )
                },
                "stamps parameter",
            ),
            (
                {"order_by": RowOrderSpec(column="id", limit_param="start_date")},
                "limit_param",
            ),
        ),
        ids=["row-filter", "derive-stamp", "order-cap"],
    )
    def test_a_row_side_facet_aimed_at_a_relative_bound_is_refused(
        self, facet: dict[str, object], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            dataclasses.replace(RELATIVE_PROBE, **facet)

    def test_a_relative_bound_alone_makes_a_path_placeholder_addressable(self) -> None:
        """``default is None`` no longer means "this placeholder cannot address a resource"."""
        with pytest.raises(ValueError, match="must be required or defaulted"):
            dataclasses.replace(
                RELATIVE_PROBE,
                params=(
                    ParamSpec("symbol", "str", required=True),
                    ParamSpec("asof", "date"),
                ),
                path="/probe/{symbol}/{asof}",
            )
        declared = dataclasses.replace(
            RELATIVE_PROBE,
            params=(
                ParamSpec("symbol", "str", required=True),
                ParamSpec("asof", "date", relative_default=RelativeDefault()),
            ),
            path="/probe/{symbol}/{asof}",
        )
        assert render_path(declared, _omitted(), now=CLOCK).endswith("/probe/AAPL/2026-03-14")


class TestTheRelativeDefaultReachesTheWire:
    """One omitted window, replayed offline: the resolved bounds leave on the request."""

    def test_encode_query_sends_the_resolved_bounds_for_an_omission(self) -> None:
        encoded = encode_query(_omitted(), RELATIVE_PROBE, now=CLOCK)
        assert encoded == {"start_date": "2025-03-14", "end_date": "2026-03-14"}

    def test_the_callers_own_bound_beats_the_declaration(self) -> None:
        encoded = encode_query(_omitted(start_date=date(2020, 1, 2)), RELATIVE_PROBE, now=CLOCK)
        assert encoded["start_date"] == "2020-01-02"
        assert encoded["end_date"] == "2026-03-14"

    def test_the_path_and_the_query_string_are_rendered_from_one_clock_reading(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, {"rows": [{"id": "A"}]}))
        generated = make_http_json_fetcher(SOURCE, RELATIVE_PROBE)
        cls = type(generated.__name__, (generated,), {"http_transport": transport})
        rows = list(
            cls().fetch(
                ctx=fixture_context(SOURCE, RELATIVE_PROBE, sends=1),
                symbol="AAPL",
            )
        )
        assert [row.id for row in rows] == ["A"]
        call = transport.calls[0]
        today = date.today()
        assert call["url"].endswith("/probe/AAPL/window")
        assert call["params"]["end_date"] == today.isoformat()
        assert call["params"]["start_date"] == RelativeDefault(years=-1).resolve(today).isoformat()

    def test_bound_request_is_the_request_side_view_of_every_declared_parameter(self) -> None:
        view = bound_request(_omitted(), RELATIVE_PROBE, now=CLOCK)
        assert view.start_date == date(2025, 3, 14)
        assert view.symbol == "AAPL"
        with pytest.raises(ValueError, match="datetime.date"):
            bound_request(_omitted(), RELATIVE_PROBE, now=datetime(2026, 3, 14, 9))
