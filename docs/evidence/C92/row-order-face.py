#!/usr/bin/env python3
"""C92 rows.order_limit acceptance face: a declaration read twice, and a stage that can be removed.

The goal wording asks for 可复算证据面 -- a judgement probe plus a counterfact arm, not a spoken
verdict -- so this archive answers four separate questions, each with an arm that can read the
other way:

* ``ARM-DECLARE`` -- the engine's own read of the one production declaration, next to the census
  judge's AST count of it. A text grep and a live object are two different witnesses.
* ``ARM-PIPELINE`` -- the shipped stage run through the real fetcher over a recorded transport,
  offline. The network read path is replaced by a function that raises, so a green run is a run
  that never reached a socket.
* ``ARM-COUNTERFACT`` -- the same two declarations with ``apply_row_order`` patched to pass through.
  Both directions then collapse onto the published sequence: if this arm ever matches the expected
  order, the stage is not load-bearing and the pipeline arm proves nothing.
* ``ARM-PRESENCE`` -- ``verify_registry`` re-run against an anchor that is not in the proof file.
  Presence must go to ``absent``; an entry that stays ``present`` under a forged anchor is a claim,
  not a measurement.

Run: ``python docs/evidence/C92/row-order-face.py`` (exit 0 only when every arm reads as stated).
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import model_capability_census as mcc  # noqa: E402

from opendata.data.providers._engine import http_json  # noqa: E402
from opendata.data.providers._engine.http_json import (  # noqa: E402
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import (  # noqa: E402
    ColumnSpec,
    ModelSpec,
    PaginationSpec,
    ParamSpec,
    RowOrderSpec,
)
from opendata.data.providers._engine.testing import (  # noqa: E402
    FixedResponseTransport,
    fixture_context,
)
from opendata.data.providers.federal_reserve.specs import MONEY_MEASURES  # noqa: E402

CAPABILITY = "rows.order_limit"

HOLDINGS: list[dict[str, Any]] = [
    {"ticker": "MSFT", "security_name": "Microsoft", "weight": 6.2},
    {"ticker": "AAPL", "security_name": "Apple", "weight": 7.1},
    {"ticker": "GE", "security_name": "General Electric", "weight": 3.4},
    {"ticker": "XOM", "security_name": "Exxon", "weight": 3.4},
    {"ticker": "BRK", "security_name": "Berkshire"},
]
PUBLISHED = ["MSFT", "AAPL", "GE", "XOM", "BRK"]
ASCENDING = ["GE", "XOM", "MSFT", "AAPL", "BRK"]
DESCENDING = ["AAPL", "MSFT", "GE", "XOM", "BRK"]

COLUMNS = (
    ColumnSpec("symbol", "str", source_key="ticker", required=True),
    ColumnSpec("name", "str", source_key="security_name", required=True),
    ColumnSpec("weight", "float", source_key="weight"),
)


def _probe_spec(order: RowOrderSpec | None, *, paging: bool = False) -> ModelSpec:
    # A paging spec has to be a *legal* pager before the ordering facet can be refused on top of
    # it; a half-declared one would refuse for its own reason and the arm below would read green
    # while measuring nothing.
    pagination = (
        PaginationSpec(kind="offset", limit_key="limit", offset_key="offset")
        if paging
        else PaginationSpec()
    )
    params: tuple[ParamSpec, ...] = (ParamSpec("top", "int"),)
    if paging:
        params += (ParamSpec("limit", "int"), ParamSpec("offset", "int"))
    declaration: dict[str, Any] = {
        "model": "FaceProbe",
        "domain": "probe_faceprobe",
        "asset_class": "equity",
        "period": "snapshot",
        "market": "all",
        "base_url": "https://probe.test",
        "path": "/holdings",
        "columns": COLUMNS,
        "params": params,
        "rows_pointer": "records",
        "scenario": "离线判定面：行排序与 top-n 的引擎能力",
        "error_prefix": "PROBE_FACE",
        "pagination": pagination,
        "order_by": order,
    }
    return ModelSpec(**declaration)


def _fetch(spec: ModelSpec, **query: object) -> list[Any]:
    """Serve the published document to ``spec`` offline and return the rows the engine emitted."""
    transport = FixedResponseTransport(HttpResponse(200, {"records": HOLDINGS}))
    generated = make_http_json_fetcher("probe", spec)
    fetcher = type("FaceFetcher", (generated,), {"http_transport": transport})()
    return list(fetcher.fetch(ctx=fixture_context("probe", spec, sends=2), **query))


def _tickers(rows: list[Any]) -> list[str]:
    return [str(getattr(row, "symbol", "?")) for row in rows]


def main() -> int:
    """Run every arm, print what each measured, and refuse a green that no arm earned."""
    failures: list[str] = []

    # ---- ARM-DECLARE: the judge's count and the engine's own read of the same declaration ----
    counts, per_file, modules = mcc.count_declarations(mcc.REGISTRY, REPO_ROOT)
    readings = mcc.verify_registry(mcc.REGISTRY, REPO_ROOT, counts)
    entry = next(e for e in mcc.REGISTRY if e.capability == CAPABILITY)
    reading = readings[CAPABILITY]
    carrier = per_file[CAPABILITY]
    print(
        f"ARM-DECLARE judge count={counts[CAPABILITY]} modules={modules} carrier={carrier}"
        f"\nARM-DECLARE registry status={reading.status} anchor_found={reading.anchor_found} "
        f"cross_anchor_found={reading.cross_anchor_found} anchor_line={reading.anchor_line_number} "
        f"entry_claim={entry.claim} read_from_disk={entry.read_from_disk}"
        f"\nARM-DECLARE live-object model={MONEY_MEASURES.model} "
        f"order_by={MONEY_MEASURES.order_by}"
    )
    # ``>= 1`` and not ``== 1`` on purpose: the count rises the moment the next model declares an
    # ordering, and a per-round constant here would only be a pin the following round has to repair.
    if counts[CAPABILITY] < 1:
        failures.append(
            f"judge count for {CAPABILITY} is {counts[CAPABILITY]}, expected at least 1"
        )
    if reading.status != "present":
        failures.append(f"registry reads {reading.status}, expected present")
    if MONEY_MEASURES.order_by is None:
        failures.append("MONEY_MEASURES carries no order_by -- the declaration arm has no carrier")
    fed_key = "opendata/data/providers/federal_reserve/specs.py"
    if fed_key not in carrier:
        failures.append(f"the judge did not attribute a declaration to federal_reserve: {carrier}")
    declared = MONEY_MEASURES.order_by
    if declared is not None:
        print(
            f"ARM-DECLARE fed carrier_count={carrier.get(fed_key)} "
            f"declared={declared.column!r} {declared.direction!r} limit={declared.limit!r} "
            f"limit_param={declared.limit_param!r}"
        )

    # ---- ARM-PIPELINE: the shipped stage through the real fetcher, transport recorded ----
    def forbidden(*args: object, **kwargs: object) -> HttpResponse:
        raise AssertionError("a row-order face reached the network")

    original_stage = http_json.apply_row_order
    original_transport = http_json._http_get_json
    http_json._http_get_json = forbidden
    refused = False
    collapses = False
    try:
        asc_spec = _probe_spec(RowOrderSpec(column="weight", direction="asc"))
        desc_spec = _probe_spec(RowOrderSpec(column="weight", direction="desc"))
        cap_spec = _probe_spec(RowOrderSpec(column="weight", direction="desc", limit=2))
        param_spec = _probe_spec(RowOrderSpec(column="weight", direction="asc", limit_param="top"))
        asc_rows = _tickers(_fetch(asc_spec))
        desc_rows = _tickers(_fetch(desc_spec))
        cap_rows = _tickers(_fetch(cap_spec))
        param_rows = _tickers(_fetch(param_spec, top=2))
        print(
            f"ARM-PIPELINE published={PUBLISHED}\n"
            f"ARM-PIPELINE asc={asc_rows} desc={desc_rows}\n"
            f"ARM-PIPELINE cap-literal-2={cap_rows} cap-param-2={param_rows}"
        )
        if asc_rows != ASCENDING:
            failures.append(f"ascending read {asc_rows} != {ASCENDING}")
        if desc_rows != DESCENDING:
            failures.append(f"descending read {desc_rows} != {DESCENDING}")
        if cap_rows != ["AAPL", "MSFT"]:
            failures.append(f"top-2 read {cap_rows} != ['AAPL', 'MSFT']")
        if param_rows != ASCENDING[:2]:
            failures.append(f"parameter cap read {param_rows} != {ASCENDING[:2]}")

        # ---- ARM-COUNTERFACT: remove the stage; both directions must collapse to published ----
        http_json.apply_row_order = lambda records, query, spec: list(records)
        cf_asc = _tickers(_fetch(asc_spec))
        cf_desc = _tickers(_fetch(desc_spec))
        collapses = cf_asc == cf_desc == PUBLISHED
        print(
            f"ARM-COUNTERFACT stage-removed asc={cf_asc} desc={cf_desc} "
            f"directions_collapse={cf_asc == cf_desc} matches_published={cf_asc == PUBLISHED}"
        )
        if cf_asc != PUBLISHED or cf_desc != PUBLISHED:
            failures.append("the stage-removed arm still ordered rows -- the arm cannot differ")
        if cf_asc != cf_desc:
            failures.append("the stage-removed arm still distinguishes asc from desc")
        if [asc_rows, desc_rows] == [cf_asc, cf_desc]:
            failures.append("the pipeline arm equals the counterfact arm: nothing was measured")

        # ---- ARM-REFUSAL: a declaration that cannot mean anything is refused at build time ----
        # Two sides: the same legal pager without an ordering must build, or the refusal below
        # would only prove that pagination is hard to declare.
        _probe_spec(None, paging=True)
        try:
            paging_spec = _probe_spec(
                RowOrderSpec(column="weight", direction="asc", limit=3), paging=True
            )
            failures.append(f"a paging+order declaration was accepted: {paging_spec.model}")
        except (ValueError, ProviderEngineError) as exc:
            message = str(exc)
            refused = "order_by" in message
            print(
                f"ARM-REFUSAL paging-without-order builds; paging+order refused "
                f"blamed_order_by={refused}: {message.splitlines()[0][:120]}"
            )
            if not refused:
                failures.append(f"the refusal did not name order_by: {message[:160]}")
    finally:
        http_json.apply_row_order = original_stage
        http_json._http_get_json = original_transport
    if not refused:
        failures.append("the paging+cap arm was never refused by the constructor")

    # ---- ARM-PRESENCE: presence is read off the disk, so a forged anchor must drop it ----
    forged = replace(entry, anchor="def apply_row_order_never_written_here(")
    forged_reading = mcc.verify_registry((forged,), REPO_ROOT, counts)[CAPABILITY]
    print(
        f"ARM-PRESENCE real={reading.status} forged={forged_reading.status} "
        f"flipped={reading.status != forged_reading.status}"
    )
    if forged_reading.status != "absent":
        failures.append("presence survived a forged anchor: the entry asserts rather than measures")

    # ---- the published roadmap faces this round moved ----
    roadmap_path = next(REPO_ROOT.glob("docs/**/capability-roadmap.json"))
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    art = roadmap["capability_registry"][CAPABILITY]
    print(
        f"ARTIFACT round={roadmap['round_id']} status={art['status']} "
        f"declarations={art['production_declarations']} "
        f"shipped_cover={len(roadmap['roadmap']['shipped_cover_rows'])} "
        f"built_but_unused={roadmap['built_but_unused']} "
        f"canonical={roadmap['taxonomy']['canonical_capability_count']}"
    )
    if art["status"] != "present" or art["production_declarations"] < 1:
        failures.append("the published artifact does not carry this round's order face")

    for reason in failures:
        print(f"FAIL {reason}")
    print(
        f"VERDICT declared={counts[CAPABILITY]} status={reading.status} "
        f"pipeline_ordered={asc_rows == ASCENDING and desc_rows == DESCENDING} "
        f"stage_removal_collapses={collapses} paging_cap_refused={refused} "
        f"presence_flippable={forged_reading.status == 'absent'} failures={len(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
