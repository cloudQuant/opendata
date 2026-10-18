r"""Pre-validate the wave-31 step 3 ``government_us::WeatherBulletinDownload`` landing.

Nothing here is evidence: fixtures land in a scratch dir outside the committed evidence root, and
every verdict comes from the repo's own runner -- the 25 phase runs are the real
``iter2_claims_probe_runner.py`` subprocesses the producer will spawn, and the control arms
drive the shipped ``_client_seam`` + ``_grant_context`` + ``fetch`` sequence, the same three
calls ``_run_direct_with_seam`` makes.

This row is the first in the residue population that step 0's four-axis instrument priced as
``add_code_carrying_guard``: before the landing its only ``raise`` statements were bare
``ValueError`` in query validation, all of them upstream of transport, so no fixture tamper could
reach any of them. The landing put two code-carrying guards *after* the governed ``get_bytes`` call,
which is the shape the shipped counterfact mechanism can bite. The arms below therefore do the two
things the tmx and eia prevalidations did not have to: show that the guard the claims shard
*cannot* cover (empty body) still bites under the same seam, and show that the guards the query
owns are genuinely pre-transport rather than merely untested.

Arms, in the order they print:

* ``PHASE_TALLY`` -- 5 models x 5 phases, each in its own subprocess with sockets blocked,
  tallied by exit code (0 pass, 3 blocked, anything else fail). All five run, not only the new
  one, because editing this provider's witness module moves the pin digest every government_us
  row was signed under, so the four rows already landed have to re-serve from this generation or
  the merge drops them;
* ``CONTROL_FIVE_SERVED`` -- every witnessed row served through the seam, printing the row class the
  fetcher actually emitted against the class the domain resolves to, compared with ``is``;
* ``CONTROL_DECLARE_REMOVED`` -- the new ``row_model`` ClassVar deleted in memory and restored
  in the same ``finally``, with *both* shipped readers of the declaration driven: the
  implementation phase refuses with its own ``WitnessFailure``, the direct leg dies on a bare
  ``AttributeError`` raised inside ``iter2_claims_probe_runner.py`` at the row-class comparison
  (the fetcher never consults ``row_model``, so an undocked row still reaches
  ``transform_data``). The restore is verified by ``is`` against the saved class object rather
  than by "no error was raised";
* ``CONTROL_MEDIA_TYPES`` -- the four members of ``DOCUMENT_MEDIA_TYPES`` each serve, and two types
  outside it each refuse from the guard's own frame, so the frozenset is priced by its members and
  not by the one tamper the shard carries; includes the case-and-parameter arm (``Text/Plain;
  charset=utf-8``) because the normalization that makes it a member lives in ``_client.get_bytes``,
  not in the fetcher;
* ``CONTROL_EMPTY_BODY`` -- the guard the single-tamper-per-row mechanism cannot publish: one
  zero-length answer refuses with ``NWS_DOCUMENT_EMPTY_BODY`` from the fetcher's frame, and the
  side-by-side control is the same carrier with the retained 33 bytes, which serves. Printed next to
  the ``max(…)`` the base64 of that payload would have become, because "empty" and "absent" are the
  two cells this row could silently publish;
* ``CONTROL_MULTI_URL_LOOP`` -- two distinct official URLs under a two-response carrier serve two
  rows in ask order over two seam calls. Without it, "the guard ran" is equally consistent with "the
  loop ran once and no loop exists";
* ``CONTROL_QUERY_PRE_TRANSPORT`` -- four query shapes the row refuses (foreign host, eleven URLs,
  blank, repeated) each print their refusal class *and* the seam call count, which is where "fires
  before transport" stops being prose and becomes ``seam_calls=0``;
* ``CONTROL_TAMPER_ONE_CELL`` -- every declared tamper re-applied to its own carrier and the two
  documents diffed leaf by leaf, so ``tamper_reason``'s implicit "nothing else changed" is counted;
* ``CONTROL_TAMPER_BITES`` -- all five entries re-read: each declared tamper refuses with its
  declared code and its untampered control still serves;
* ``CONTROL_DOC_CITES`` -- the witness module's docstring names line-numbered cites for the
  fetcher it just landed (raise sites, the ``row_model`` line, the ``get_bytes`` line); this arm
  re-derives them from the module on disk, refuses a cite that no longer resolves, and prints the
  documented sites this side of the landing that the docstring does *not* cite, because a docstring
  cite that has drifted is the same defect as a pin that has drifted;
* ``STATIC_VERSUS_RUNTIME`` -- the shipped C128 residue instrument's ``_row`` face for the landed
  row, printed next to the runtime readings, because the instrument reads raise statements lexically
  and cannot see whether the seam answers the call that precedes them.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RUNNER_REL = "scripts/quality/iter2_claims_probe_runner.py"
AXES_REL = "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
WITNESS_REL = "scripts/quality/iter2_witnesses/government_us.py"
FETCHER_REL = "opendata/data/providers/government_us/models/weather_bulletin_download.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
sys.path.insert(0, str(REPO_ROOT / AXES_REL))
sys.path.insert(0, str(REPO_ROOT))

runner = importlib.import_module("iter2_claims_probe_runner")
axes = importlib.import_module("incomplete_row_guard_check")
witness_src = (REPO_ROOT / WITNESS_REL).read_text(encoding="utf-8")
fetcher_src = (REPO_ROOT / FETCHER_REL).read_text(encoding="utf-8")

PROVIDER = "government_us"
MODELS = (
    "TreasuryAuctions",
    "WeatherBulletin",
    "CommodityPsdData",
    "CommodityPsdReport",
    "WeatherBulletinDownload",
)
NEW = "WeatherBulletinDownload"
PHASES = ("implementation", "direct", "service", "contract", "counterfact")

#: The three content types outside the declared allow-list that this arm asks about, plus the two
#: inside it that the claims shard never exercises.
FOREIGN_TYPES = ("text/html", "application/json")
NORMALIZED_TYPE = "Text/Plain; charset=utf-8"

#: Set at import so every face prints against the same measured denominator.
CHECKS: list[tuple[str, bool]] = []


def check(name: str, condition: bool) -> bool:
    """Record one arm's verdict so the exit code is a count of readings, not of prints."""
    CHECKS.append((name, condition))
    return condition


def materialize() -> None:
    """Write each fixture the way the producer does: shipped builder, shipped serialization."""
    fixtures = SCRATCH / "fixtures" / PROVIDER
    fixtures.mkdir(parents=True, exist_ok=True)
    models = runner.witnesses.PROVIDER_WITNESSES[PROVIDER]["models"]
    for model in MODELS:
        builder = models[model]["fixture_document"]
        payload = json.dumps(builder(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        (fixtures / f"{model}.json").write_text(payload, encoding="utf-8")
    print(f"materialized: fixtures={len(MODELS)} root={SCRATCH}")


SCRATCH = Path(tempfile.mkdtemp(prefix="w31s3-gov-preval-"))


def run_phases() -> int:
    """Run every phase of every witnessed government_us model as the producer's own subprocess."""
    passed = failed = blocked = 0
    for model in MODELS:
        for phase in PHASES:
            completed = subprocess.run(  # noqa: S603  # nosec B603  # argv is the shipped runner
                [
                    sys.executable,
                    str(REPO_ROOT / RUNNER_REL),
                    "--provider",
                    PROVIDER,
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
            print(f"phase: {PROVIDER}::{model}::{phase} exit={completed.returncode} {verdict}")
            if completed.returncode != 0:
                print(f"   stderr_tail={completed.stderr.strip().splitlines()[-3:]}")
    total = len(MODELS) * len(PHASES)
    check("phase_tally_clean", failed + blocked == 0)
    print(f"PHASE_TALLY passed={passed} failed={failed} blocked={blocked} total={total}")
    return failed + blocked


def _entry(model: str) -> dict[str, Any]:
    entry = dict(runner.witnesses.model_entry(PROVIDER, model))
    entry["hosts"] = runner.witnesses.provider_config(PROVIDER)["allowed_hosts"]
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
        PROVIDER, model, entry, document
    )
    return served


def _fetcher_class(model: str) -> type[Any]:
    """Resolve the fetcher class by the ``canonical_model`` it publishes, not by a typed map."""
    from opendata.data.protocol import Fetcher

    models_dir = REPO_ROOT / "opendata/data/providers/government_us/models"
    for module_name in sorted(path.stem for path in models_dir.glob("*.py")):
        module = importlib.import_module(
            f"opendata.data.providers.government_us.models.{module_name}"
        )
        for name in sorted(dir(module)):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Fetcher)
                and getattr(obj, "canonical_model", None) == model
            ):
                return obj
    raise LookupError(f"no {PROVIDER} fetcher publishes canonical_model={model}")


def _refusal_site(exc: BaseException, provider_fragment: str = "providers/government_us") -> str:
    """The innermost frame a refusal came from, preferring the provider's own file.

    A guard that lives in the runner (the ``row_model`` lookup, the seam's sequence cursor) must be
    named too, or the face reads as though no code site exists.
    """
    frames = list(traceback.extract_tb(exc.__traceback__))
    inside = [frame for frame in frames if provider_fragment in str(frame.filename)]
    if not inside and frames:
        inside = [frames[-1]]
    if not inside:
        return "<no frame>"
    last = inside[-1]
    return f"{Path(last.filename).name}:{last.lineno}:{last.name}"


def _refusal(exc: BaseException) -> str:
    """Name the class, the provider code if any, the message and the frame -- in that order."""
    return (
        f"{type(exc).__name__} code={getattr(exc, 'code', None)} "
        f"msg={str(exc)[:70]} site={_refusal_site(exc)}"
    )


def _attempt(
    model: str, document: dict[str, Any], query: dict[str, Any] | None = None
) -> tuple[list[Any] | None, int, str]:
    """Run the shipped pipeline steps over one carrier and return (rows, seam calls, face).

    The direct leg's own ``_run_direct_with_seam`` hides the seam object when the fetcher raises
    inside the ``with``, which loses the call count -- and the call count is the whole face of the
    pre-transport arms. This drives the same three shipped calls (``_client_seam``,
    ``_grant_context``, ``fetch``) so a refusal still reports how many requests it cost.
    """
    entry = _entry(model)
    if query is not None:
        entry["query"] = query
    fetcher = _fetcher_class(model)()
    with runner._client_seam(entry, document) as seam:
        ctx = runner._grant_context(
            PROVIDER, model, entry["hosts"], sends=runner._carrier_sends(entry, document)
        )
        try:
            rows = list(fetcher.fetch(ctx=ctx, **entry["query"]))
        except Exception as exc:  # the refusal under test is the subject of the arm
            return None, len(seam.calls), _refusal(exc)
        return rows, len(seam.calls), f"SERVED rows={len(rows)}"


def _refuse(model: str, document: dict[str, Any], query: dict[str, Any] | None = None) -> str:
    """Serve a document that must not pass; return its refusal face or NOT_REFUSED."""
    rows, calls, face = _attempt(model, document, query)
    if rows is None:
        return f"{face} seam_calls={calls}"
    return f"NOT_REFUSED rows={len(rows)} seam_calls={calls}"


def _tampered(
    model: str,
    path: list[Any],
    value: Any,  # noqa: ANN401  # tamper values are fixture JSON
) -> dict[str, Any]:
    """Tamper the shipped carrier through the runner's own op, so no second shape exists."""
    entry = _entry(model)
    document: dict[str, Any] = runner._apply_tamper(
        entry["fixture_document"](), {"op": "set", "path": path, "value": value}
    )
    return document


def served_arm() -> None:
    """Print the row class each fetcher really emitted against the domain's contract class."""
    from opendata.data.domains import contract_model

    face: list[str] = []
    all_identity = True
    for model in MODELS:
        document = _entry(model)["fixture_document"]()
        rows, seam, fetcher_type = _serve(model, document)
        contract = contract_model(fetcher_type.capability.domain)
        identity = all(type(row) is contract for row in rows)
        all_identity = all_identity and identity
        face.append(
            f"{model}:rows={len(rows)} emitted={sorted({type(r).__name__ for r in rows})} "
            f"contract={contract.__name__} runtime_identity={identity} "
            f"calls={len(seam.calls)} methods={sorted(set(seam.methods))}"
        )
    check("five_served_identity", all_identity)
    print("CONTROL_FIVE_SERVED " + " | ".join(face))


def declare_arm() -> None:
    """Delete and restore the new row_model in memory, and verify the restore by identity.

    Two shipped guards read the declaration and they do not read it the same way, so the arm runs
    both: the implementation phase looks the attribute up with a default and refuses with its own
    ``WitnessFailure``, while the direct leg touches ``fetcher_type.row_model`` bare, which is an
    ``AttributeError`` out of the runner rather than a provider refusal. Printing both is what tells
    a reader *which* shipped phase is the one that actually prices the declaration -- and that the
    fetcher itself never consults ``row_model``, it constructs the contract class by name, so an
    undocked row would still serve through ``fetch``.
    """
    cls = _fetcher_class(NEW)
    saved = cls.row_model
    document = _entry(NEW)["fixture_document"]()
    implementation_face = direct_face = "ARM_NOT_REACHED"
    try:
        del cls.row_model
        try:
            runner.witness_implementation(PROVIDER, NEW, _entry(NEW))
            implementation_face = "NOT_REFUSED"
        except Exception as exc:  # the guard under test is whatever refuses
            implementation_face = f"{type(exc).__name__}: {str(exc)[:80]}"
        try:
            served_rows, _, _ = _serve(NEW, document)
            direct_face = f"NOT_REFUSED rows={len(served_rows)}"
        except Exception as exc:  # same reading for the shipped direct leg
            direct_face = f"{type(exc).__name__}: {str(exc)[:80]} site={_refusal_site(exc)}"
    except Exception as exc:  # a delete that fails is itself the finding
        implementation_face = direct_face = f"ARM_ERROR {type(exc).__name__}:{exc}"
    finally:
        cls.row_model = saved
    restored = cls.__dict__.get("row_model") is saved
    served_rows, _, _ = _serve(NEW, document)
    check("implementation_phase_refuses_undocked", "WitnessFailure" in implementation_face)
    check("direct_leg_refuses_undocked", not direct_face.startswith("NOT_REFUSED"))
    check("declaration_restored_by_identity", restored)
    print(
        f"CONTROL_DECLARE_REMOVED {NEW}:implementation={implementation_face} "
        f"direct={direct_face} restored_is_saved={restored} "
        f"with_rows={len(served_rows)} row_class={type(served_rows[0]).__name__}"
    )


def _media_types() -> list[str]:
    """Read the fetcher module's own frozenset, which is a module constant, not a class attr."""
    module = importlib.import_module(
        "opendata.data.providers.government_us.models.weather_bulletin_download"
    )
    return sorted(module.DOCUMENT_MEDIA_TYPES)


def media_arm() -> None:
    """Price every member of DOCUMENT_MEDIA_TYPES, not only the one the shard tampers."""
    entry = _entry(NEW)
    allowed = _media_types()
    document = entry["fixture_document"]()
    declared_type = document["responses"][0]["content_type"]
    faces: list[str] = []
    served_types: list[str] = []
    refused: dict[str, str] = {}
    for media in [*allowed, NORMALIZED_TYPE, *FOREIGN_TYPES]:
        tampered = _tampered(NEW, ["responses", 0, "content_type"], media)
        rows, calls, face = _attempt(NEW, tampered)
        if rows is None:
            refused[media] = face
            faces.append(f"{media!r}: refused=[{face}] calls={calls}")
            continue
        published = rows[0].data_format["media_type"]
        served_types.append(media)
        faces.append(
            f"{media!r}: served={len(rows)} published={published!r} "
            f"bytes={len(base64.b64decode(rows[0].content))} calls={calls}"
        )
    check("allowed_types_all_serve", set(allowed) <= set(served_types))
    check("foreign_types_all_refuse", set(FOREIGN_TYPES) <= set(refused))
    check("normalized_type_serves", NORMALIZED_TYPE in served_types)
    print(
        f"CONTROL_MEDIA_TYPES declared_members={allowed} declared_content_type={declared_type!r} "
        f"empty_announcement_note=coerced_by_fetcher served={served_types} "
        f"refused={sorted(refused)} " + " | ".join(faces)
    )


def empty_body_arm() -> None:
    """Bite the guard the single-tamper mechanism has no slot for, beside its own control."""
    control = _entry(NEW)["fixture_document"]()
    control_rows, control_calls, _ = _attempt(NEW, control)
    if control_rows is None:
        raise AssertionError("empty-body arm lost its own control")
    control_bytes = base64.b64decode(control_rows[0].content)
    empty = _tampered(NEW, ["responses", 0, "body_b64"], "")
    rows, calls, face = _attempt(NEW, empty)
    check("empty_body_refuses", rows is None and "NWS_DOCUMENT_EMPTY_BODY" in face)
    check("empty_body_control_serves", len(control_rows) == 1 and len(control_bytes) > 0)
    answer = f"NOT_REFUSED rows={len(rows)}" if rows is not None else f"refused={face}"
    print(
        f"CONTROL_EMPTY_BODY control_rows={len(control_rows)} control_bytes={len(control_bytes)} "
        f"control_calls={control_calls} guard_line={_guard_line('NWS_DOCUMENT_EMPTY_BODY')} "
        f"empty_answer={answer} calls={calls} mechanism_note=one_tamper_slot_per_row"
    )


def multi_url_arm() -> None:
    """Serve two URLs through one carrier so the loop's existence is measured, not assumed."""
    entry = _entry(NEW)
    control = entry["fixture_document"]()
    first_url = str(entry["query"]["urls"])
    second_url = "https://api.weather.gov/products/SECOND-20261012T1200Z/text"
    second_spec = {
        "url_contains": "/products/SECOND-",
        "content_type": control["responses"][0]["content_type"],
        "body_b64": base64.b64encode(b"SECOND TEST BULLETIN\n").decode("ascii"),
    }
    document = json.loads(json.dumps(control))
    document["responses"].append(second_spec)
    rows, seam, _ = _serve(NEW, document, query={"urls": [first_url, second_url]})
    order = [str(row.data_format["source_url"]) for row in rows]
    answerable = runner._carrier_sends(entry, document)
    check("two_urls_serve_two_rows", len(rows) == 2 and len(seam.calls) == 2)
    check("ask_order_preserved", order == [first_url, second_url])
    print(
        f"CONTROL_MULTI_URL_LOOP urls=2 served={len(rows)} seam_calls={len(seam.calls)} "
        f"carrier_answerable={len(document['responses'])} sends_granted={answerable} "
        f"order={order} second_bytes={len(base64.b64decode(rows[1].content))}"
    )


def query_arm() -> None:
    """Show that the query refusals cost no transport, which is the landing's whole claim."""
    entry = _entry(NEW)
    control = entry["fixture_document"]()
    good = str(entry["query"]["urls"])
    arms = (
        ("foreign_host", "https://example.com/products/TEST/text"),
        ("blank_url", " "),
        ("repeated", [good, good]),
        ("eleven_urls", [f"https://api.weather.gov/products/P{i}/text" for i in range(11)]),
        ("date_instead", {"urls": good, "start_date": "2026-10-12"}),
    )
    faces: list[str] = []
    zero_calls = True
    for label, urls in arms:
        kwargs: dict[str, Any] = {"urls": urls}
        if label == "date_instead":
            kwargs = {"urls": good, "start_date": "2026-10-12"}
        refused = _refuse(NEW, control, query=kwargs)
        calls = int(refused.rsplit("seam_calls=", 1)[1]) if "seam_calls=" in refused else -1
        zero_calls = zero_calls and calls == 0
        faces.append(f"{label}: {refused}")
    check("query_refusals_cost_no_transport", zero_calls)
    print(f"CONTROL_QUERY_PRE_TRANSPORT query_arms={len(arms)} " + " | ".join(faces))


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
    """Count the cells each declared tamper actually changes, for all five entries."""
    faces: list[str] = []
    singles = True
    for model in MODELS:
        entry = _entry(model)
        control = entry["fixture_document"]()
        tampered = runner._apply_tamper(control, entry["tamper"])
        changed = _differing_cells(control, tampered)
        singles = singles and len(changed) == 1
        faces.append(f"{model}:changed_cells={len(changed)} paths={changed}")
    check("every_tamper_one_cell", singles)
    print("CONTROL_TAMPER_ONE_CELL " + " | ".join(faces))


def tamper_arm() -> None:
    """Re-read every declared tamper in the module against its declared code."""
    codes: list[str] = []
    bites = True
    for model in MODELS:
        entry = _entry(model)
        rows, _, _ = _serve(model, entry["fixture_document"]())
        tampered = runner._apply_tamper(entry["fixture_document"](), entry["tamper"])
        observed = _refuse(model, tampered)
        declared = str(entry["refusal_code"])
        match = f"code={declared}" in observed
        bites = bites and match
        codes.append(
            f"{model}:control={len(rows)} declared={declared} observed={observed} match={match}"
        )
    check("every_declared_tamper_bites", bites)
    print("CONTROL_TAMPER_BITES " + " | ".join(codes))


def _guard_line(code: str) -> int:
    """The line number the raise of ``code`` really sits on in the landed fetcher."""
    hits = [i + 1 for i, line in enumerate(fetcher_src.splitlines()) if f'"{code}"' in line]
    if len(hits) != 1:
        raise AssertionError(f"{code} raise sites={hits}, expected exactly one")
    return hits[0]


def _collect_cites() -> list[int]:
    """Every line number the witness docstring cites for the landed fetcher, ranges expanded.

    Two shapes carry a cite: ``weather_bulletin_download.py:96`` and a bare ``:135`` inside a
    sentence that already named the file. The qualified form of *other* files' cites
    (``commodity_psd_data.py:181-185``) is excluded by requiring the colon not to follow a name,
    because a line 181 of a 157-line module would otherwise read as a dangling cite.
    """
    numbers: set[int] = set()
    patterns = (
        re.compile(r"weather_bulletin_download\.py:(\d+)(?:-(\d+))?"),
        re.compile(r"(?<![\w.]):(\d{2,3})(?:-(\d{2,3}))?"),
    )
    for pattern in patterns:
        for match in pattern.finditer(witness_src):
            low = int(match.group(1))
            high = match.group(2)
            numbers.update(range(low, int(high) + 1) if high else [low])
    return sorted(numbers)


def cites_arm() -> None:
    """Re-derive every file:line cite the witness module's docstring makes about the new row."""
    lines = fetcher_src.splitlines()
    row_model_line = next(
        i + 1 for i, line in enumerate(lines) if line.strip().startswith("row_model: ClassVar")
    )
    get_bytes_line = next(i + 1 for i, line in enumerate(lines) if "_client.get_bytes(" in line)
    coded = [
        (i + 1, re.search(r'GovernmentUSProviderError\("([A-Z_]+)"\)', line).group(1))
        for i, line in enumerate(lines)
        if "raise _client.GovernmentUSProviderError(" in line
    ]
    bare = [i + 1 for i, line in enumerate(lines) if re.search(r"\braise ValueError\b", line)]
    cited = _collect_cites()
    dangling = [n for n in cited if n > len(lines)]
    documented = {row_model_line, get_bytes_line, *(n for n, _ in coded), *bare}
    missing = sorted(documented - set(cited))
    check("cites_in_range", not dangling)
    check("guard_lines_cited", all(number in cited for number, _ in coded))
    check("declaration_lines_cited", row_model_line in cited and get_bytes_line in cited)
    check("bare_refusals_cited", set(bare) <= set(cited))
    print(
        f"CONTROL_DOC_CITES fetcher_lines={len(lines)} raise_sites_total={len(bare) + len(coded)} "
        f"bare_valueerror={bare} code_carrying={coded} row_model_line={row_model_line} "
        f"get_bytes_line={get_bytes_line} cited={cited} dangling={dangling} "
        f"documented_but_uncited={missing}"
    )


def static_arm() -> None:
    """Print the shipped residue instrument's face next to the runtime readings."""
    row = axes._row(f"{PROVIDER}::{NEW}", "INCOMPLETE")
    simulated = axes._verdict({**row, "declared": False, "is_contract": False})
    runtime_site = _guard_line("NWS_DOCUMENT_BAD_CONTENT_TYPE")
    check("axes_verdict_trippable", str(row["verdict"]) in axes.TRIPPABLE)
    print(
        f"STATIC_VERSUS_RUNTIME {NEW}:verdict={row['verdict']} declared={row['declared']} "
        f"is_contract={row['is_contract']} counts={row['counts']} raises={row['raises']} "
        f"code_raises={row['code_raises']} trippable={len(row['trippable'])} "
        f"doc_site_line={runtime_site} undocked={simulated}"
    )


def inject_credential_env() -> None:
    """Do what the shipped CLI does before any phase: plant the fixture token, print only names.

    The runner's own entry point fills ``credential_env`` from the witness config when the
    variable is unset (``iter2_claims_probe_runner.py:748-750``), and the subprocess arms above go
    through it, which is why all 25 legs pass. The in-process control arms do not, so without this
    the two PSD rows refuse at ``USDA_FAS_API_KEY_MISSING_OR_INVALID`` -- a harness artifact, not a
    provider finding. Only the variable *names* are printed; the fixture values are written to this
    process's environment and, with sockets blocked, never reach a socket.
    """
    config = runner.witnesses.provider_config(PROVIDER)
    declared = sorted(config.get("credential_env", {}))
    injected = {
        name: value
        for name, value in config.get("credential_env", {}).items()
        if not os.environ.get(name)
    }
    os.environ.update(injected)
    print(
        f"CREDENTIAL_ENV declared_names={declared} was_unset={sorted(injected)} "
        f"already_set={sorted(set(declared) - set(injected))} values_printed=no"
    )


def main() -> int:
    """Block sockets, run the 25 shipped phase verdicts, then print the control faces."""
    runner._block_real_sockets()  # the shipped judge does this first, so does the arm
    inject_credential_env()
    materialize()
    tally = run_phases()
    served_arm()
    declare_arm()
    media_arm()
    empty_body_arm()
    multi_url_arm()
    query_arm()
    one_cell_arm()
    tamper_arm()
    cites_arm()
    static_arm()
    failed = [name for name, ok in CHECKS if not ok]
    print(f"PREVALIDATION_CHECKS arms={len(CHECKS)} failed={len(failed)} failed_names={failed}")
    print(f"PREVALIDATION_DONE phase_failures_or_blocked={tally} arm_failures={len(failed)}")
    return 0 if tally == 0 and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
