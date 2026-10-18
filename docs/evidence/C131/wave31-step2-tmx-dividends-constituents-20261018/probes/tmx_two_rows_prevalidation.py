r"""Pre-validate the wave-31 step 2 tmx landings through the shipped runner paths.

Nothing here is evidence: fixtures land in a scratch dir outside the committed evidence root, and
every verdict comes from the repo's own judge -- the 35 phase runs are the real
``iter2_claims_probe_runner.py`` subprocesses the producer will spawn, and the control arms drive
the shipped ``_client_seam`` + ``_grant_context`` + ``fetch`` sequence, the same three calls
``_run_direct_with_seam`` makes. These two rows are the first tmx landings that wave 30 priced as
*"needs a guard invented"* and wave 31 measures as *"already refuses"*: each mapper builds its row
through the ``_row`` helper, so the refusal site is one ``raise TmxProviderError("TMX_BAD_ROW")``
away from a document tamper, and the only thing the rows lacked was the ``row_model`` declaration.
The arms are therefore about (a) whether the declaration alone is what moves them and (b) whether
the refusal a tamper produces is the cell the entry claims.

Arms, in the order they print:

* ``PHASE_TALLY`` -- 7 models x 5 phases, each in its own subprocess with sockets blocked, tallied
  by exit code (0 pass, 3 blocked, anything else fail). All seven run, not only the two new ones,
  because editing ``fetchers.py`` and this provider's witness module moves the source-pin digest
  every tmx row was signed under, so the five rows already landed have to re-serve from this
  generation or the merge drops them;
* ``CONTROL_SEVEN_SERVED`` -- every witnessed row served through the seam, printing the row class
  the fetcher actually emitted against the class the domain resolves to, compared with ``is``;
* ``CONTROL_DECLARE_REMOVED`` -- the shipped direct leg run twice per new row with the ``row_model``
  ClassVar deleted in memory and restored in the same ``finally``: before the restore the runner's
  own guard must refuse, after it the row must serve, and the restore is verified by ``is`` against
  the saved class object rather than by "no error was raised";
* ``CONTROL_DIVIDEND_PAGE_TERMINAL`` -- the untampered page stops the pagination loop after exactly
  one seam call, and the send budget is printed as three numbers (what the carrier can answer, what
  the runner grants, the shipped floor) because the first is 1 and the second is 3: the carrier does
  *not* set this entry's budget, ``max(_DEFAULT_SENDS, answerable)`` does;
* ``CONTROL_DIVIDEND_PAGINATION_LIVE`` -- a four-page sequence carrier (``hasNextPage`` true on
  pages 1-3) serves eight rows over four seam calls. Without it "the loop stopped after one call"
  is equally consistent with "the loop never runs", so the pagination guard gets a positive control
  and the carrier's own answer count (4) finally exceeds the floor;
* ``CONTROL_DIVIDEND_SEQUENCE_EXHAUSTED`` -- one non-terminal page under a one-page carrier makes
  the seam withhold the second answer, and what the caller receives is ``TMX_TRANSPORT_ERROR``
  from ``client.py:153``: the provider's bare ``except Exception`` swallows the harness's own
  ``WitnessFailure`` and re-raises it as a transport refusal ``from None``. This is the reading that
  refutes this provider's witness-module claim that no seam can withhold an answer, so the module
  now says so;
* ``CONTROL_DIVIDEND_SEEN_DISJUNCT`` -- a page numbered 1 served to the second ask is the only way
  to reach the ``pageNumber in seen_page_numbers`` disjunct, and the face shows it is not
  attributable: the ``page_number != page`` disjunct in the same ``if`` fires first, and the loop's
  monotonic ``page`` makes the seen disjunct unreachable on any sequence the seam can answer;
* ``CONTROL_DIVIDEND_PAGE_SITES`` -- a ``pageNumber`` that disagrees with the page asked for and a
  non-boolean ``hasNextPage`` both refuse with ``TMX_BAD_PAGE`` from *different* frames, printed as
  file:line:function, so "these are separate guards" is measured rather than asserted; a
  non-terminal page is run too, and what it actually hits is printed whatever it is;
* ``CONTROL_DIVIDEND_WINDOW_BOUND`` -- the same page under four windows (unbounded, ending at the
  first record's ex-date, starting at the last, ending before both) prints the served dates, so
  ``ex_dividend_date`` is shown to be a column the fetcher compares on both sides rather than one it
  merely copies. The window is written into ``entry["query"]``: the shipped direct leg reads its
  kwargs from the entry, and a first draft of this arm passed the query some other way and printed
  the unbounded reading while claiming to measure the bound;
* ``CONTROL_CONSTITUENT_KEY`` -- the control's two members carry distinct ``symbol`` values (the
  domain's whole natural key), and nulling either refuses while nothing else changes;
* ``CONTROL_CONSTITUENT_KEY_NOT_POLICED`` -- the second member is overwritten with the *first*
  member's symbol and both rows still serve, which is the honest scope of the key claim:
  ``natural_key`` names a storage identity the mapper does not enforce, so the entry tampers the
  cell that *is* enforced. The first draft collided a member with ``sorted(keys)[0]``, which for
  this carrier is that member's own value -- a no-op that would have "shown" tolerance without
  asking anything;
* ``CONTROL_CONSTITUENT_NULLABLE_COLUMNS`` -- dropping the source's ``longName`` key serves
  ``name=None``, printed next to the contract's required/optional field face, so the choice of
  ``symbol`` (required) over ``weight`` (optional) as the tampered cell is measured;
* ``CONTROL_TAMPER_ONE_CELL`` -- every declared tamper re-applied to its own carrier and the two
  documents diffed leaf by leaf, so the ``tamper_reason`` claims "no other column is touched" and
  "the refusal is about one cell" are counted rather than asserted. All seven report exactly one
  differing path, and the *shape* of that path is what separates the two refusal kinds: five name
  a column (``.../price``, ``.../employees``, ``.../consensusAnalysts``,
  ``.../dividends/0/exDate``, ``.../constituents/0/symbol``) and two name a list length
  (``filings[length 1->2]``, ``getCompanyPriceHistory[length 1->2]``), which is the face that
  tells a single-cell refusal from an over-long reply;
* ``CONTROL_TAMPER_BITES`` -- all seven entries re-read: each declared tamper refuses with its
  declared code and its untampered control still serves;
* ``STATIC_VERSUS_RUNTIME`` -- the shipped C128 residue instrument's own ``_row`` face for the two
  rows, printed next to the runtime readings, because the instrument counts only ``raise``
  statements lexically inside the mapper methods and tmx refuses inside a helper: the two readings
  are allowed to disagree and this probe prints both instead of choosing the flattering one.
"""

from __future__ import annotations

import importlib
import json
import subprocess  # nosec B404
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
AXES_REL = "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
sys.path.insert(0, str(REPO_ROOT / AXES_REL))
sys.path.insert(0, str(REPO_ROOT))

runner = importlib.import_module("iter2_claims_probe_runner")
axes = importlib.import_module("incomplete_row_guard_check")

MODELS = (
    "EquityQuote",
    "EquityInfo",
    "CompanyFilings",
    "PriceTargetConsensus",
    "EquityHistorical",
    "HistoricalDividends",
    "IndexConstituents",
)
NEW = ("HistoricalDividends", "IndexConstituents")
PHASES = ("implementation", "direct", "service", "contract", "counterfact")

#: Scratch evidence root, created at import so the printed face carries the real path.
SCRATCH = Path(tempfile.mkdtemp(prefix="w31s2-tmx-preval-"))


def materialize() -> None:
    """Write each fixture the way the producer does: shipped builder, shipped serialization."""
    fixtures = SCRATCH / "fixtures" / "tmx"
    fixtures.mkdir(parents=True, exist_ok=True)
    models = runner.witnesses.PROVIDER_WITNESSES["tmx"]["models"]
    for model in MODELS:
        builder = models[model]["fixture_document"]
        payload = json.dumps(builder(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        (fixtures / f"{model}.json").write_text(payload, encoding="utf-8")
    print(f"materialized: fixtures={len(MODELS)} root={SCRATCH}")


def run_phases() -> int:
    """Run every phase of every witnessed tmx model as the producer's own subprocess."""
    passed = failed = blocked = 0
    for model in MODELS:
        for phase in PHASES:
            completed = subprocess.run(  # noqa: S603  # nosec B603  # argv is the shipped runner
                [
                    sys.executable,
                    str(REPO_ROOT / RUNNER_REL),
                    "--provider",
                    "tmx",
                    "--model",
                    model,
                    "--phase",
                    phase,
                    "--evidence-root",
                    str(SCRATCH),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            )
            verdict = next(
                (line for line in completed.stdout.splitlines() if line.startswith("VERDICT=")), "?"
            )
            if completed.returncode == 0:
                passed += 1
            elif completed.returncode == 3:
                blocked += 1
            else:
                failed += 1
            print(f"phase: tmx::{model}::{phase} exit={completed.returncode} {verdict}")
            if completed.returncode != 0:
                print(f"   stderr_tail={completed.stderr.strip().splitlines()[-3:]}")
    total = len(MODELS) * len(PHASES)
    print(f"PHASE_TALLY passed={passed} failed={failed} blocked={blocked} total={total}")
    return failed + blocked


def _entry(model: str) -> dict[str, Any]:
    entry = dict(runner.witnesses.model_entry("tmx", model))
    entry["hosts"] = runner.witnesses.provider_config("tmx")["allowed_hosts"]
    return entry


def _serve(
    model: str, document: dict[str, Any], *, query: dict[str, Any] | None = None
) -> tuple[list[Any], Any, type[Any]]:
    """Run the shipped direct leg on one document, whatever it produces.

    ``query`` replaces the entry's own kwargs *inside the entry object* -- the shipped direct leg
    reads ``entry["query"]``, so a query handed to the fetcher some other way never reaches it.
    """
    entry = _entry(model)
    if query is not None:
        entry["query"] = query
    served: tuple[list[Any], Any, type[Any]] = runner._run_direct_with_seam(
        "tmx", model, entry, document
    )
    return served


def _fetcher_class(model: str) -> type[Any]:
    """Resolve the fetcher class by the ``canonical_model`` it publishes, not by a typed map."""
    from opendata.data.providers.tmx.models import fetchers as tmx_fetchers

    for name in sorted(dir(tmx_fetchers)):
        obj = getattr(tmx_fetchers, name)
        if isinstance(obj, type) and getattr(obj, "canonical_model", None) == model:
            return obj
    raise LookupError(f"no tmx fetcher publishes canonical_model={model}")


def _refusal_site(exc: BaseException) -> str:
    """The innermost frame a refusal came from, preferring the provider's own file.

    A guard that lives in the runner (the ``row_model`` lookup, the seam's sequence cursor) must be
    named too, or the face reads as though no code site exists.
    """
    frames = list(traceback.extract_tb(exc.__traceback__))
    provider = [frame for frame in frames if "providers/tmx" in str(frame.filename)]
    if not provider and frames:
        provider = [frames[-1]]
    if not provider:
        return "<no frame>"
    last = provider[-1]
    return f"{Path(last.filename).name}:{last.lineno}:{last.name}"


def _refusal(exc: BaseException) -> str:
    """Name the class, the provider code if any, the message and the frame -- in that order."""
    return (
        f"{type(exc).__name__} code={getattr(exc, 'code', None)} "
        f"msg={str(exc)[:90]} site={_refusal_site(exc)}"
    )


def _refuse(model: str, document: dict[str, Any]) -> str:
    """Serve a document that must not pass and return its refusal face, or NOT_REFUSED."""
    try:
        _serve(model, document)
    except Exception as exc:  # the refusal under test is whatever the provider or runner ships
        return _refusal(exc)
    return "NOT_REFUSED"


def _with_cell(
    model: str,
    path: list[Any],
    value: Any = None,  # noqa: ANN401  # tamper values are fixture JSON of any shape
    *,
    op: str = "set",
) -> dict[str, Any]:
    """Tamper the shipped carrier through the runner's own tamper op, so no second shape exists."""
    entry = _entry(model)
    tamper = {"op": op, "path": path}
    if op != "drop":
        tamper["value"] = value
    document: dict[str, Any] = runner._apply_tamper(entry["fixture_document"](), tamper)
    return document


def served_arm() -> None:
    """Print the row class each fetcher really emitted against the domain's contract class."""
    from opendata.data.domains import contract_model

    face: list[str] = []
    for model in MODELS:
        document = _entry(model)["fixture_document"]()
        rows, seam, fetcher_type = _serve(model, document)
        contract = contract_model(fetcher_type.capability.domain)
        identity = all(type(row) is contract for row in rows)
        face.append(
            f"{model}:rows={len(rows)} emitted={sorted({type(r).__name__ for r in rows})} "
            f"contract={contract.__name__} runtime_identity={identity} "
            f"calls={len(seam.calls)} methods={sorted(set(seam.methods))}"
        )
    print("CONTROL_SEVEN_SERVED " + " | ".join(face))


def declare_arm() -> None:
    """Delete and restore each new row_model in memory, and verify the restore by identity."""
    face: list[str] = []
    for model in NEW:
        cls = _fetcher_class(model)
        saved = cls.row_model
        document = _entry(model)["fixture_document"]()
        try:
            del cls.row_model
            refused = _refuse(model, document)
        except Exception as exc:  # a delete that fails is itself the finding
            refused = f"ARM_ERROR {type(exc).__name__}:{exc}"
        finally:
            cls.row_model = saved
        restored = cls.__dict__.get("row_model") is saved
        served_rows, _, _ = _serve(model, document)
        face.append(
            f"{model}:without={refused} restored_is_saved={restored} "
            f"with_rows={len(served_rows)} row_class={type(served_rows[0]).__name__}"
        )
    print("CONTROL_DECLARE_REMOVED " + " | ".join(face))


def _dividend_page(cell: str) -> list[Any]:
    """The tamper path to one page-level key of this module's own dividend carrier."""
    return ["responses", 0, "body", "data", "dividendHistory", cell]


def _dividend_cell(index: int, cell: str) -> list[Any]:
    """The tamper path to one column of one dividend record of this module's own carrier."""
    return [*_dividend_page("dividends"), index, cell]


def _dividend_page_body(page_number: int, has_next: bool) -> dict[str, Any]:
    """One page of the dividend carrier, wrapped the way ``_document`` wraps a reply."""
    records: list[Any] = _entry("HistoricalDividends")["fixture_document"]()["responses"][0][
        "body"
    ]["data"]["dividendHistory"]["dividends"]
    return {
        "data": {
            "dividendHistory": {
                "pageNumber": page_number,
                "hasNextPage": has_next,
                "dividends": records,
            }
        }
    }


def _dividend_sequence_document(pages: list[tuple[int, bool]]) -> dict[str, Any]:
    """A carrier whose answer differs per send, from explicit ``(pageNumber, hasNext)`` pages."""
    base = _entry("HistoricalDividends")["fixture_document"]()
    document: dict[str, Any] = json.loads(json.dumps(base))
    spec = document["responses"][0]
    spec["sequence"] = [_dividend_page_body(number, has_next) for number, has_next in pages]
    spec.pop("body", None)
    return document


def _carrier_answerable(document: dict[str, Any]) -> int:
    """Recount what the carrier can answer, the way the shipped :func:`_carrier_sends` does."""
    return sum(
        len(spec["sequence"]) if spec.get("sequence") else 1
        for spec in document["responses"]
        if isinstance(spec, dict)
    )


def dividend_arms() -> None:
    """Bite the pagination guards apart and show the window column is compared, not copied."""
    entry = _entry("HistoricalDividends")
    control = entry["fixture_document"]()
    rows, seam, _ = _serve("HistoricalDividends", control)
    answerable = _carrier_answerable(control)
    granted = runner._carrier_sends(entry, control)
    print(
        f"CONTROL_DIVIDEND_PAGE_TERMINAL control_rows={len(rows)} seam_calls={len(seam.calls)} "
        f"carrier_answerable={answerable} sends_granted={granted} "
        f"default_floor={runner._DEFAULT_SENDS} budget_is_carrier={granted == answerable}"
    )

    paged = _dividend_sequence_document([(1, True), (2, True), (3, True), (4, False)])
    paged_rows, paged_seam, _ = _serve("HistoricalDividends", paged)
    print(
        f"CONTROL_DIVIDEND_PAGINATION_LIVE pages=4 served={len(paged_rows)} "
        f"seam_calls={len(paged_seam.calls)} carrier_answerable={_carrier_answerable(paged)} "
        f"sends_granted={runner._carrier_sends(entry, paged)} "
        f"exdates={[str(row.ex_dividend_date) for row in paged_rows]}"
    )

    exhausted = _dividend_sequence_document([(1, True)])
    print(
        f"CONTROL_DIVIDEND_SEQUENCE_EXHAUSTED carrier_pages=1 "
        f"refusal={_refuse('HistoricalDividends', exhausted)}"
    )

    repeated = _dividend_sequence_document([(1, True), (1, False)])
    print(
        f"CONTROL_DIVIDEND_SEEN_DISJUNCT refusal={_refuse('HistoricalDividends', repeated)} "
        f"second_page_number=1 asked_page=2 note=page_number_ne_page_fires_first"
    )

    page_flag = _dividend_page("hasNextPage")
    arms = (
        ("page_number_two", _with_cell("HistoricalDividends", _dividend_page("pageNumber"), 2)),
        ("has_next_page_string", _with_cell("HistoricalDividends", page_flag, "false")),
        ("non_terminal_page", _with_cell("HistoricalDividends", page_flag, True)),
        (
            "ex_date_not_a_date",
            _with_cell("HistoricalDividends", _dividend_cell(0, "exDate"), 20260614),
        ),
    )
    sites = [f"{label}: {_refuse('HistoricalDividends', document)}" for label, document in arms]
    frames = sorted({site.rsplit("site=", 1)[-1] for site in sites if "site=" in site})
    print(
        "CONTROL_DIVIDEND_PAGE_SITES "
        + " | ".join(sites)
        + f" distinct_frames={len(frames)} frames={frames}"
    )

    records = control["responses"][0]["body"]["data"]["dividendHistory"]["dividends"]
    if not isinstance(records, list):
        raise RuntimeError(f"carrier shape: dividends={type(records).__name__}")
    first_ex_date, last_ex_date = str(records[0]["exDate"]), str(records[-1]["exDate"])
    symbol = entry["query"]["symbol"]
    window_arms = (
        ("unbounded", {"symbol": symbol}),
        ("end_at_first", {"symbol": symbol, "end_date": first_ex_date}),
        ("start_at_last", {"symbol": symbol, "start_date": last_ex_date}),
        ("end_before_first", {"symbol": symbol, "end_date": "2026-01-01"}),
    )
    faces = [_window_face(label, query, control) for label, query in window_arms]
    print(
        f"CONTROL_DIVIDEND_WINDOW_BOUND page_records={len(records)} "
        f"exdates={first_ex_date} {last_ex_date} " + " | ".join(faces)
    )


def _window_face(label: str, query: dict[str, Any], control: dict[str, Any]) -> str:
    """Serve one window and report the dates it kept, or the refusal a bound produced."""
    bounds = f"{query.get('start_date', '-')}..{query.get('end_date', '-')}"
    try:
        kept, seam, _ = _serve("HistoricalDividends", control, query=query)
    except Exception as exc:  # a bound that keeps nothing surfaces as the runner's zero-row refusal
        return f"{label}[{bounds}] refused={_refusal(exc)}"
    return (
        f"{label}[{bounds}] served={len(kept)} "
        f"dates={[str(row.ex_dividend_date) for row in kept]} "
        f"amounts={[row.amount for row in kept]} seam_calls={len(seam.calls)}"
    )


def constituent_arm() -> None:
    """Show what the index domain's one-column natural key is and is not enforced by."""
    document = _entry("IndexConstituents")["fixture_document"]()
    members = document["responses"][0]["body"]["data"]["constituents"]
    if not isinstance(members, list):
        raise RuntimeError(f"carrier shape: constituents={type(members).__name__}")
    keys = sorted(str(member["symbol"]) for member in members)
    rows, _, _ = _serve("IndexConstituents", document)
    served_keys = sorted(str(row.symbol) for row in rows)

    def member_cell(index: int, cell: str) -> list[Any]:
        return ["responses", 0, "body", "data", "constituents", index, cell]

    null_first = _with_cell("IndexConstituents", member_cell(0, "symbol"), None)
    null_second = _with_cell("IndexConstituents", member_cell(1, "symbol"), None)
    quoted_weight = _with_cell("IndexConstituents", member_cell(1, "weight"), "3.14")
    absent_name = _with_cell("IndexConstituents", member_cell(0, "longName"), op="drop")
    # collide the *other* way round than the carrier already is: members[1] already carries its own
    # key, so overwriting it with keys[0] (sorted, therefore already LUN.TO) would be a no-op that
    # "proves" the mapper tolerates duplicates by testing nothing at all.
    collision = _with_cell("IndexConstituents", member_cell(1, "symbol"), str(members[0]["symbol"]))
    collide_to = str(members[0]["symbol"])
    optional, required = _contract_field_face("IndexConstituents")
    print(
        f"CONTROL_CONSTITUENT_KEY control_members={keys} served={served_keys} "
        f"null_first={_refuse('IndexConstituents', null_first)} "
        f"null_second={_refuse('IndexConstituents', null_second)} "
        f"quoted_weight={_refuse('IndexConstituents', quoted_weight)} "
        f"declared_code={_entry('IndexConstituents')['refusal_code']}"
    )
    print(
        f"CONTROL_CONSTITUENT_KEY_NOT_POLICED members={[str(m['symbol']) for m in members]} "
        f"second_set_to={collide_to} "
        f"{_serve_face('IndexConstituents', collision)} "
        f"note=natural_key_is_a_storage_identity_not_a_mapper_guard"
    )
    print(
        f"CONTROL_CONSTITUENT_NULLABLE_COLUMNS source_column_absent="
        f"{_serve_face('IndexConstituents', absent_name)} contract_optional={optional} "
        f"contract_required={required}"
    )


def _contract_field_face(model: str) -> tuple[list[str], list[str]]:
    """Split the row contract's own fields into optional and required, by ``is_required()``."""
    fields = _fetcher_class(model).row_model.model_fields
    optional = [name for name, field in fields.items() if not field.is_required()]
    required = [name for name, field in fields.items() if field.is_required()]
    return optional, required


def _serve_face(model: str, document: dict[str, Any]) -> str:
    """Report what a document that is allowed to serve *does* serve, or name its refusal."""
    try:
        rows, _, _ = _serve(model, document)
    except Exception as exc:  # the arms that expect a refusal are the ones that print one
        return f"refused=True {_refusal(exc)}"
    dumps = [row.model_dump(mode="python", by_alias=False) for row in rows]
    return (
        f"served={len(rows)} symbols={sorted(str(row.symbol) for row in rows)} "
        f"cells={[sorted(d.items()) for d in dumps]} refused=False"
    )


def _differing_cells(left: Any, right: Any, path: tuple[Any, ...] = ()) -> list[str]:  # noqa: ANN401
    """Every leaf path where two JSON documents disagree, so 'one cell' can be counted."""
    if isinstance(left, dict) and isinstance(right, dict):
        found: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            found += _differing_cells(left.get(key), right.get(key), (*path, key))
        return found
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{'/'.join(str(p) for p in path)}[length {len(left)}->{len(right)}]"]
        return [
            cell
            for i in range(len(left))
            for cell in _differing_cells(left[i], right[i], (*path, i))
        ]
    if left == right:
        return []
    return ["/".join(str(p) for p in path)]


def one_cell_arm() -> None:
    """Count the cells each declared tamper actually changes, for all seven entries."""
    faces: list[str] = []
    for model in MODELS:
        entry = _entry(model)
        control = entry["fixture_document"]()
        tampered = runner._apply_tamper(control, entry["tamper"])
        changed = _differing_cells(control, tampered)
        faces.append(f"{model}:changed_cells={len(changed)} paths={changed}")
    print("CONTROL_TAMPER_ONE_CELL " + " | ".join(faces))


def tamper_arm() -> None:
    """Re-read every declared tamper in the module against its declared code."""
    codes: list[str] = []
    for model in MODELS:
        entry = _entry(model)
        rows, _, _ = _serve(model, entry["fixture_document"]())
        tampered = runner._apply_tamper(entry["fixture_document"](), entry["tamper"])
        observed = _refuse(model, tampered)
        declared = str(entry["refusal_code"])
        codes.append(
            f"{model}:control={len(rows)} declared={declared} observed={observed} "
            f"match={'code=' + declared in observed}"
        )
    print("CONTROL_TAMPER_BITES " + " | ".join(codes))


def static_arm() -> None:
    """Print the shipped residue instrument's face next to the runtime readings."""
    face: list[str] = []
    for model in NEW:
        row = axes._row(f"tmx::{model}", "INCOMPLETE")
        simulated = axes._verdict({**row, "declared": False, "is_contract": False})
        face.append(
            f"{model}:verdict={row['verdict']} declared={row['declared']} "
            f"is_contract={row['is_contract']} counts={row['counts']} "
            f"raises={row['raises']} code_raises={row['code_raises']} "
            f"trippable={len(row['trippable'])} undocked={simulated}"
        )
    print("STATIC_VERSUS_RUNTIME " + " | ".join(face))


def main() -> int:
    """Block sockets, run the 35 shipped phase verdicts, then print the control faces."""
    runner._block_real_sockets()  # the shipped judge does this first, so does the arm
    materialize()
    tally = run_phases()
    served_arm()
    declare_arm()
    dividend_arms()
    constituent_arm()
    one_cell_arm()
    tamper_arm()
    static_arm()
    print(f"PREVALIDATION_DONE phase_failures_or_blocked={tally}")
    return 0 if tally == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
