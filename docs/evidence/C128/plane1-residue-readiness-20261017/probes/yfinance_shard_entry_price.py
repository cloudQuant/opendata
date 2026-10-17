"""Price the 15 yfinance ``NEED_SHARD_ENTRY`` rows against the seam that already ships.

Section 94's branch (b) set a gate in words: before touching those rows, build a counterfact
instrument that proves they can be served by ``kind=shared_http_client`` *without editing the
runner*. This probe answers that by measurement, in two halves that must not be confused:

* the **shipped seam** is :func:`iter2_claims_probe_runner._client_seam`. It replaces one named
  attribute of named modules with ``lambda _c=client: _c`` -- arity ``(1, 0)`` -- so a landed row
  gets back the ``_FakeSharedClient``, whose only verb is ``get``. nasdaq's landed rows ride on
  exactly that, because the attribute they patch is a 0-positional factory used as ``.get(...)``.
* yfinance never reaches either engine seam. Its fetch legs call ``_sdk.require_*(...)``, and the
  only HTTP-establishing point inside the package is
  ``opendata.data.providers.yfinance.models._sdk.governed_yfinance_session``, which ``_sdk.py:78``
  calls with **one positional argument inside a ``with``**. A seam that swallows the argument and
  returns a ``get``-only object cannot serve that shape, and -- because the lambda accepts the
  positional instead of raising -- it fails *silently*.

Arms, every one of which can fail:

``A_void_gate``
    the judged report still carries the population this ladder was priced against
    (``kinds.LANDED_TOTAL`` rows, ``kinds.LANDED_ACCEPTED`` accepted, identity set equal to
    ``kinds.LANDED_IDENTITY_SET``). Otherwise print ``VOID=yes`` and refuse: the 15-row denominator
    is a reading of this generation, and §94 says a moved identity set invalidates the action table
    before any conclusion drawn from it.
``B_population_is_declared_rows``
    the subject rows are the ones the shipped per-row judge reads as ``declared``
    (:func:`witness_seam_kind_census._declared_rows`), counted rather than hand-carried, and every
    verdict is in ``axes.TRIPPABLE`` -- so the bucket rule arm H applies really covers all of them.
``C_no_witness_module``
    ``yfinance`` is absent from the shipped loader's ``providers_with_witness()`` while many other
    providers are present, so the absence is read off the loader and a shard entry really is the
    next job rather than something already paid for.
``D_seam_reads_are_zero_for_this_package``
    :func:`witness_seam_kind_census._package_seam_reads` reads ``(0, 0)`` for yfinance and non-zero
    for at least one other provider package -- the two-sided control that makes the zero a property
    of the package rather than of the counter.
``E_shipped_refusal``
    the shipped seam, driven with the landed nasdaq entry's *own* attribute name against the
    yfinance module, raises the shipped ``WitnessBlocked`` refusal (message printed), while the same
    seam through nasdaq's landed ``_SEAM`` patches cleanly and its fake answers a declared URL with
    one recorded call. The second half stops arm E from being a dead measurer.
``F_patch_accepted_then_shape_refused``
    with the attribute name the package actually publishes (``governed_yfinance_session``) the seam
    *accepts* the patch, and then four readings are taken against the installed callable, each one
    reporting the shipped ``_seam_guard`` verdict too. Passing one positional returns *that
    positional* instead of the fake -- passthrough, the fake silently discarded, guard fires on zero
    calls. Using the result as ``with ... as session`` raises a shape error, and so does putting the
    fake itself in a ``with``: the seam hands back a client, the product needs a session. The fourth
    reading is the one that keeps arm F honest: called with zero positionals and used as ``.get``
    directly, the same patch answers the declared URL with one recorded call and a clean guard. So
    the blocker is *not* the fake's verb set -- it is the substitution shape.
``G_call_site_shapes_differ``
    the shapes above come from the AST of the product code, not from this docstring: the yfinance
    site passes 1 positional and is ``with``-wrapped at every site; the landed attribute's sites
    across every provider package pass a minimum of 0 positionals and are never ``with``-wrapped;
    and the installed seam's own arity is ``(1, 0)``. The arm requires the readings to *differ*,
    which is the different-expected-values rule §94 put on a contrast arm.
``H_classifier_cannot_be_moved_by_widening``
    the shipped classifier (:func:`residue_axis_census._action`) is re-fed each row's measured
    verdict with ``fake_ok`` as-measured, then widened to the union of every provider's
    census-measured verbs, then forged ``True``. All 15 rows stay ``shard_entry_needs_harness``
    under widening (``rows_flipped_by_widening=0``, because yfinance's ``package_verbs`` is
    empty and the shipped predicate starts with ``bool(package_verbs)``) and all 15 move to
    ``shard_entry_only`` only when the predicate itself is forged. The gap between those two counts
    is the finding: the bucket moves if the *predicate* moves, not if the fake's verb set does. The
    arm also re-derives each provider's shipped ``fake_answers_all`` and requires zero
    disagreements, so the forged half is known to differ from the measurement in one argument only.
``I_tmx_is_a_different_cause``
    tmx's census line reads a non-empty verb set that the shipped get-only fake does not answer, in
    a different bucket with a different mechanism, while yfinance's reads empty. The two classes
    have different prices and must not be pooled into one "needs harness" bucket in the next plan.
``J_runner_edit_price``
    the price of editing the runner is read from the shipped pin walker, not asserted: how many
    receipt slots pin ``iter2_claims_probe_runner.py`` today, plus that probe's own ``A_clean``
    (``stale=0``, since this probe writes no pinned byte) and ``B_forced``
    (``stale == targeted_slots``) liveness face.
``K_gate_is_trippable``
    arm A's gate re-run, in memory, on one forged generation of the judged report: flip a single
    ``OFFLINE_ACCEPTED`` row to ``UNREVIEWED`` and replace the identity set. The gate must read
    ``void=True`` with ``accepted_delta=-1``. Without this arm, arm A's green ``VOID=no`` is
    indistinguishable from a gate that can never trip.

Exits non-zero when an arm fails or when arm A voids. Nothing here lands a row: §94 asked for a
*price*, and this measures it in shapes -- a seam kind that swallows an argument and yields a
``get``-only object, on top of the fixtures and the five phases every landed row still needs.

    PY=/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python
    P=docs/evidence/C128/plane1-residue-readiness-20261017/probes/yfinance_shard_entry_price.py
    PYTHONPATH= $PY $P                     # the green take, 11 arms
    PYTHONPATH= $PY $P --forge-fake-ok     # the red take: arm H must go red and the run must exit 1

Run straight, never through a pipe. Socket refusal is the runner's own function, installed before
any dynamic arm runs, so a leg that missed the seam surfaces as a refusal, not a fetch.
"""

from __future__ import annotations

import ast
import copy
import importlib
import re
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import incomplete_row_guard_check as axes  # the shipped TRIPPABLE verdict set
import receipt_pin_consistency as pin_probe  # the shipped receipt pin walker
import residue_axis_census as ra  # the shipped classifier and census reader
import seam_surface_match as seam  # the shipped judged-report reader and fake surface
import witness_seam_kind_census as kinds  # the shipped seam census readers and landed face

REPO_ROOT = seam.REPO_ROOT
QUALITY = REPO_ROOT / "scripts" / "quality"
sys.path.insert(0, str(QUALITY))

import iter2_claims_fixtures as witnesses  # noqa: E402  # the shipped witness loader
import iter2_claims_probe_runner as runner  # noqa: E402  # the shipped seam implementations

SUBJECT_PROVIDER = "yfinance"
SDK_MODULE = "opendata.data.providers.yfinance.models._sdk"
SDK_FILE = REPO_ROOT / "opendata/data/providers/yfinance/models/_sdk.py"
PROVIDERS_DIR = REPO_ROOT / "opendata/data/providers"
CONTROL_ATTR = "governed_yfinance_session"
CONTRAST_PROVIDER = "tmx"
NASDAQ_WITNESS = importlib.import_module("iter2_witnesses.nasdaq")
PIN_PROBE = Path(__file__).resolve().parent / "receipt_pin_consistency.py"
RUNNER_FILE = "iter2_claims_probe_runner.py"
SEAM_NEEDLE = "https://offline.invalid/declared"
# The counterfact switch described on :func:`_fake_ok`; the default take never sets it.
FORGE_FAKE_OK = "--forge-fake-ok" in sys.argv
# Arm K forges one generation of the judged report in memory; this is the identity set it forges in.
FORGED_IDENTITY_SET = "0" * 64
# Exceptions that mean "the installed shape does not fit the call site", as opposed to a fetch.
SHAPE_ERRORS = ("TypeError", "AttributeError")


class _StandIn:
    """Stands for whatever the product would pass as ``sdk`` at the patched call site."""


_STANDIN = _StandIn()


def _declared_faces() -> list[dict[str, Any]]:
    """The subject rows as the shipped per-row judge reports them, counted not hand-carried."""
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    return kinds._declared_rows(judged)


def _entry_for(seam_spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A minimal runner-shaped entry plus an envelope that answers only the declared needle."""
    entry = {"client_seam": seam_spec, "hosts": ["offline.invalid"], "query": {}}
    document = {"responses": [{"url_contains": SEAM_NEEDLE, "body": "{}"}]}
    return entry, document


def _drive_landed(installed: Any, client: Any) -> str:  # noqa: ANN401
    """Use the patched attribute the way a landed row does: 0 positional, then ``.get``."""
    response = installed().get(f"{SEAM_NEEDLE}/quote")
    return "answered" if response.status_code == 200 else f"status_{response.status_code}"


def _drive_positional_passthrough(installed: Any, client: Any) -> str:  # noqa: ANN401
    """Call it the way ``_sdk.py:78`` does and report whether the fake comes back."""
    return "passthrough" if installed(_STANDIN) is _STANDIN else f"returned_{type(client).__name__}"


def _drive_product_context(installed: Any, client: Any) -> str:  # noqa: ANN401
    """Reproduce the product statement ``with governed_yfinance_session(sdk) as session:``."""
    with installed(_STANDIN):
        return "entered"


def _drive_fake_as_context(installed: Any, client: Any) -> str:  # noqa: ANN401
    """Put the fake client itself in a ``with``, which is what a 0-positional call returns."""
    with installed():
        return "entered"


def _seam_probe(seam_spec: dict[str, Any], drive: Any) -> dict[str, Any]:  # noqa: ANN401
    """Enter the shipped client seam, run ``drive`` inside it, and report what the shape allowed."""
    entry, document = _entry_for(seam_spec)
    try:
        with runner._client_seam(entry, document) as client:
            module = importlib.import_module(seam_spec["modules"][0])
            installed = getattr(module, seam_spec["attr"])
            exception = "None"
            try:
                outcome = drive(installed, client)
            except Exception as exc:  # the exception name IS the measurement
                outcome, exception = f"raised:{type(exc).__name__}", type(exc).__name__
            try:
                runner._seam_guard(client)
                guard = "clean"
            except Exception as exc:
                guard = type(exc).__name__
            return {
                "outcome": outcome,
                "exception": exception,
                "calls": len(client.calls),
                "guard": guard,
            }
    except runner.WitnessBlocked as blocked:
        return {
            "outcome": "refused",
            "exception": "WitnessBlocked",
            "calls": -1,
            "guard": "not_installed",
            "message": str(blocked)[:140],
        }


def _call_site_shape(root: Path, name: str) -> dict[str, Any]:
    """Read one callable's use sites under ``root``: count, smallest positional, ``with`` wraps."""
    files = 0
    sites: list[int] = []
    in_with = 0
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        files += 1
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        wrapped = {
            id(item.context_expr)
            for node in ast.walk(tree)
            if isinstance(node, (ast.With, ast.AsyncWith))
            for item in node.items
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, (ast.Attribute, ast.Name)
            ):
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", "")) != name:
                continue
            sites.append(len(node.args))
            in_with += id(node) in wrapped
    return {
        "files": files,
        "sites": len(sites),
        "min_positional": min(sites) if sites else None,
        "with_wrapped": in_with,
    }


_CENSUS: list[dict[str, str]] | None = None


def _census_lines() -> list[dict[str, str]]:
    """The shipped reachability census, run once per process and parsed by the shipped reader."""
    global _CENSUS  # a census is a subprocess, so cache it deliberately
    if _CENSUS is None:
        _CENSUS = ra._run(ra.CENSUS_PROBE, ra.CENSUS_FIELDS)
    return _CENSUS


def _verbs_of(lines: list[dict[str, str]], provider: str) -> set[str]:
    """Decode one provider's census ``package_verbs`` field with the shipped decoder."""
    for line in lines:
        if line["seam_census_provider"] == provider:
            return ra._verbs(line.get("package_verbs", "[]"))
    return set()


def _fake_ok(provider: str, surface: set[str]) -> bool:
    """Re-derive the shipped census predicate for one provider against a given fake surface.

    ``--forge-fake-ok`` forces it to ``True``, which is the counterfact "someone widened the fake":
    arm H must then read every row as ``shard_entry_only`` and disagree with the shipped census on
    all 15, so the forged take has to exit non-zero. Without that take a green H says only that the
    predicate was False, not that this probe would notice when it stops being so.
    """
    if FORGE_FAKE_OK:
        return True
    verbs = _verbs_of(_census_lines(), provider)
    return bool(verbs) and verbs <= surface


def _field(line: str, key: str) -> str:
    """Pull one ``key=value`` token from a shipped face line, not a re-implemented parser."""
    for token in line.split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1]
    return "NA"


def _gate(report: dict[str, Any]) -> dict[str, Any]:
    """Apply arm A's landed-face gate to one judged report, live or forged."""
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    accepted = sum(1 for status in judged.values() if status == "OFFLINE_ACCEPTED")
    identity_set = str(report["inventory"]["expected_identity_set_sha256"])[:16]
    void = (
        len(judged) != kinds.LANDED_TOTAL
        or accepted != kinds.LANDED_ACCEPTED
        or identity_set != kinds.LANDED_IDENTITY_SET
        or int(report["summary"]["identity_count"]) != len(judged)
    )
    return {
        "total": len(judged),
        "accepted": accepted,
        "identity_set": identity_set,
        "statuses": dict(sorted(Counter(judged.values()).items())),
        "void": void,
    }


def main() -> int:
    """Run every arm, print one face line per arm, and exit non-zero if any of them fails."""
    runner._block_real_sockets()
    arms: list[tuple[str, bool]] = []
    report = seam._load_report()
    gate = _gate(report)
    print(
        f"A_void_gate total={gate['total']} expected={kinds.LANDED_TOTAL} "
        f"accepted={gate['accepted']} expected_accepted={kinds.LANDED_ACCEPTED} "
        f"identity_set={gate['identity_set']} "
        f"expected_identity_set={kinds.LANDED_IDENTITY_SET} statuses={gate['statuses']} "
        f"VOID={'yes' if gate['void'] else 'no'}"
    )
    if gate["void"]:
        print("yfinance_shard_entry_price=VOID reason=identity_set_or_face_moved")
        print("read the §94 void condition: re-take census and action table before any price")
        return 1
    arms.append(("A_void_gate", True))

    faces = _declared_faces()
    verdicts = Counter(str(face["verdict"]) for face in faces)
    statuses = Counter(str(face["status"]) for face in faces)
    all_trippable = all(str(face["verdict"]) in axes.TRIPPABLE for face in faces)
    print(
        f"B_population count={len(faces)} expected={kinds.DECLARED_EXPECTED} "
        f"verdicts={dict(sorted(verdicts.items()))} statuses={dict(sorted(statuses.items()))} "
        f"all_trippable={all_trippable}"
    )
    arms.append(
        (
            "B_population_is_declared_rows",
            len(faces) == kinds.DECLARED_EXPECTED and all_trippable,
        )
    )

    providers = sorted(witnesses.providers_with_witness())
    subject_present = SUBJECT_PROVIDER in providers
    print(
        f"C_no_witness_module subject_has_witness_module={subject_present} "
        f"providers_with_module={len(providers)}"
    )
    arms.append(("C_no_witness_module", not subject_present and len(providers) > 1))

    reads = {
        package.name: kinds._package_seam_reads(package.name)
        for package in sorted(kinds.PROVIDERS_DIR.iterdir())
        if (package / "__init__.py").is_file()
    }
    nonzero = {name: counts for name, counts in reads.items() if counts and any(counts)}
    subject_reads = reads[SUBJECT_PROVIDER]
    print(
        f"D_seam_reads packages_read={len(reads)} "
        f"subject_http_transport={kinds._shown(subject_reads, 0)} "
        f"subject_get_shared_http_client={kinds._shown(subject_reads, 1)} "
        f"providers_reading_a_seam={len(nonzero)} sample={sorted(nonzero)[:3]}"
    )
    arms.append(
        ("D_seam_reads_are_zero_for_this_package", subject_reads == (0, 0) and len(nonzero) >= 1)
    )

    nasdaq_seam = dict(NASDAQ_WITNESS._SEAM)
    e_subject = _seam_probe({**nasdaq_seam, "modules": [SDK_MODULE]}, _drive_landed)
    e_control = _seam_probe(nasdaq_seam, _drive_landed)
    print(
        f"E_shipped_refusal target outcome={e_subject['outcome']} "
        f"exception={e_subject['exception']} message={e_subject.get('message', 'NA')!r} "
        f"control outcome={e_control['outcome']} control_calls={e_control.get('calls', 'NA')} "
        f"control_guard={e_control.get('guard', 'NA')}"
    )
    arms.append(
        (
            "E_shipped_refusal",
            e_subject["outcome"] == "refused"
            and e_subject["exception"] == "WitnessBlocked"
            and e_control["outcome"] == "answered"
            and e_control.get("calls") == 1
            and e_control.get("guard") == "clean",
        )
    )

    subject_cm = {"kind": "shared_http_client", "modules": [SDK_MODULE], "attr": CONTROL_ATTR}
    f_pass = _seam_probe(subject_cm, _drive_positional_passthrough)
    f_ctx = _seam_probe(subject_cm, _drive_product_context)
    f_fake = _seam_probe(subject_cm, _drive_fake_as_context)
    f_direct = _seam_probe(subject_cm, _drive_landed)
    readings = (f_pass, f_ctx, f_fake, f_direct)
    print(
        f"F_shape_gap patch_accepted={all(r['outcome'] != 'refused' for r in readings)} "
        f"positional outcome={f_pass['outcome']} calls={f_pass['calls']} guard={f_pass['guard']} | "
        f"with_product outcome={f_ctx['outcome']} exception={f_ctx['exception']} "
        f"calls={f_ctx['calls']} guard={f_ctx['guard']} | with_fake outcome={f_fake['outcome']} "
        f"exception={f_fake['exception']} calls={f_fake['calls']} guard={f_fake['guard']} | "
        f"zero_arg_get outcome={f_direct['outcome']} calls={f_direct['calls']} "
        f"guard={f_direct['guard']}"
    )
    arms.append(
        (
            "F_patch_accepted_then_shape_refused",
            all(r["outcome"] != "refused" for r in readings)
            and f_pass["outcome"] == "passthrough"
            and f_pass["calls"] == 0
            and f_pass["guard"] == "WitnessFailure"
            and f_ctx["exception"] in SHAPE_ERRORS
            and f_ctx["calls"] == 0
            and f_ctx["guard"] == "WitnessFailure"
            and f_fake["exception"] in SHAPE_ERRORS
            and f_fake["calls"] == 0
            and f_direct["outcome"] == "answered"
            and f_direct["calls"] == 1
            and f_direct["guard"] == "clean",
        )
    )

    yf_shape = _call_site_shape(SDK_FILE.parent, CONTROL_ATTR)
    landed_attr = str(nasdaq_seam["attr"])
    landed_shape = _call_site_shape(PROVIDERS_DIR, landed_attr)
    arity = kinds._installed_seam_arity()
    print(
        f"G_call_site_shapes subject_attr={CONTROL_ATTR} subject={yf_shape} "
        f"landed_attr={landed_attr} landed={landed_shape} "
        f"installed_arity=({kinds._shown(arity, 0)},{kinds._shown(arity, 1)}) "
        f"differ={yf_shape['min_positional'] != landed_shape['min_positional']}"
    )
    arms.append(
        (
            "G_call_site_shapes_differ",
            yf_shape["sites"] > 0
            and yf_shape["min_positional"] == 1
            and yf_shape["with_wrapped"] == yf_shape["sites"]
            and landed_shape["sites"] > 1
            and landed_shape["min_positional"] == 0
            and landed_shape["with_wrapped"] == 0
            and arity == (1, 0),
        )
    )

    lines = _census_lines()
    seam_map = ra._seam_map(lines)
    shipped_surface = set(seam._fake_surface())
    widened_surface = shipped_surface | {
        verb for line in lines for verb in ra._verbs(line.get("package_verbs", "[]"))
    }
    as_measured: Counter[str] = Counter()
    widened: Counter[str] = Counter()
    forged: Counter[str] = Counter()
    disagreements = 0
    flipped_by_widening = 0
    absent: set[str] = set()
    for face in faces:
        provider, _, model = str(face["identity"]).partition("::")
        seam_row = seam_map.get(provider)
        if seam_row is None:
            absent.add(provider)
            continue
        predicate = _fake_ok(provider, shipped_surface)
        disagreements += int(predicate != bool(seam_row["fake_answers_all"]))
        blob = ra._package_blob(provider)
        name_in_package = bool(re.search(rf"\b{re.escape(model)}\b", blob))
        args = (str(face["verdict"]), bool(face["ruled_out"]), str(seam_row["bucket"]))
        every_surface = _fake_ok(provider, widened_surface)
        row_action = ra._action(*args, predicate, name_in_package)
        as_measured[row_action] += 1
        widened_action = ra._action(*args, every_surface, name_in_package)
        widened[widened_action] += 1
        flipped_by_widening += int(row_action != widened_action)
        forged[ra._action(*args, True, name_in_package)] += 1
    ok_shipped = _fake_ok(SUBJECT_PROVIDER, shipped_surface)
    ok_widened = _fake_ok(SUBJECT_PROVIDER, widened_surface)
    print(
        f"H_classifier forge_fake_ok={FORGE_FAKE_OK} "
        f"as_measured={dict(sorted(as_measured.items()))} "
        f"widened={dict(sorted(widened.items()))} "
        f"forged={dict(sorted(forged.items()))} census_lines={len(lines)} "
        f"shipped_surface={sorted(shipped_surface)} widened_surface={sorted(widened_surface)} "
        f"fake_ok_shipped={ok_shipped} fake_ok_widened={ok_widened} "
        f"rows={len(faces)} rows_flipped_by_widening={flipped_by_widening} "
        f"predicate_disagreements={disagreements} absent_providers={sorted(absent)}"
    )
    arms.append(
        (
            "H_classifier_cannot_be_moved_by_widening",
            as_measured.get("shard_entry_needs_harness", 0) == len(faces)
            and forged.get("shard_entry_only", 0) == len(faces)
            and flipped_by_widening == 0
            and not ok_widened
            and not ok_shipped
            and disagreements == 0
            and not absent,
        )
    )

    subject_row = seam_map.get(SUBJECT_PROVIDER, {})
    contrast_row = seam_map.get(CONTRAST_PROVIDER, {})
    subject_verbs = sorted(_verbs_of(lines, SUBJECT_PROVIDER))
    contrast_verbs = sorted(_verbs_of(lines, CONTRAST_PROVIDER))
    contrast_answered = bool(contrast_verbs) and set(contrast_verbs) <= shipped_surface
    print(
        f"I_tmx_is_a_different_cause subject_bucket={subject_row.get('bucket', 'ABSENT')} "
        f"subject_verbs={subject_verbs} subject_mechanism={subject_row.get('mechanism', 'ABSENT')} "
        f"contrast_bucket={contrast_row.get('bucket', 'ABSENT')} "
        f"contrast_verbs={contrast_verbs} "
        f"contrast_mechanism={contrast_row.get('mechanism', 'ABSENT')} "
        f"contrast_answered_by_shipped_fake={contrast_answered} "
        f"pooled={subject_row.get('bucket') == contrast_row.get('bucket')}"
    )
    arms.append(
        (
            "I_tmx_is_a_different_cause",
            subject_verbs == []
            and bool(contrast_verbs)
            and not contrast_answered
            and subject_row.get("bucket") != contrast_row.get("bucket")
            and subject_row.get("bucket") == "SEAM_NEVER_USED"
            and contrast_row.get("bucket") == "BLOCKED_ON_RUNNER_SURFACE",
        )
    )

    receipts = pin_probe._receipts(None)
    slots = list(pin_probe._labelled(receipts))
    runner_slots = sum(1 for _, pin in slots if str(pin["path"]).endswith(RUNNER_FILE))
    pin = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(PIN_PROBE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    pin_lines = pin.stdout.splitlines()
    clean = next((line for line in pin_lines if line.startswith("A_clean")), "")
    forced = next((line for line in pin_lines if line.startswith("B_forced")), "")
    clean_stale = _field(clean, "stale")
    forced_stale = _field(forced, "stale")
    forced_targeted = _field(forced, "targeted_slots")
    print(
        f"J_runner_edit_price receipt_files={len(receipts)} pin_slots={len(slots)} "
        f"runner_pinned_slots={runner_slots} pin_exit={pin.returncode} clean_stale={clean_stale} "
        f"forced_stale={forced_stale} forced_targeted_slots={forced_targeted} "
        f"forced_matches_targeted={forced_stale == forced_targeted}"
    )
    if pin.returncode != 0:
        print(f"J_pin_probe_stderr={pin.stderr.strip()[:200]}")
    arms.append(
        (
            "J_runner_edit_price",
            pin.returncode == 0
            and clean_stale == "0"
            and runner_slots > 0
            and forced_stale not in ("", "NA")
            and forced_stale == forced_targeted,
        )
    )

    forged_report = copy.deepcopy(report)
    flipped = 0
    for row in forged_report["models"]:
        if flipped == 0 and str(row["status"]) == "OFFLINE_ACCEPTED":
            row["status"] = "UNREVIEWED"
            flipped = 1
    forged_report["inventory"]["expected_identity_set_sha256"] = FORGED_IDENTITY_SET
    k_gate = _gate(forged_report)
    print(
        f"K_gate_is_trippable live_void={gate['void']} forged_void={k_gate['void']} "
        f"live_accepted={gate['accepted']} forged_accepted={k_gate['accepted']} "
        f"accepted_delta={k_gate['accepted'] - gate['accepted']} "
        f"forged_identity_set={k_gate['identity_set']} status_rows_flipped={flipped}"
    )
    arms.append(
        (
            "K_gate_is_trippable",
            not gate["void"]
            and k_gate["void"]
            and flipped == 1
            and k_gate["accepted"] == gate["accepted"] - 1
            and k_gate["identity_set"] != kinds.LANDED_IDENTITY_SET,
        )
    )

    passed = sum(1 for _, ok in arms if ok)
    failed = [name for name, ok in arms if not ok]
    print(
        f"yfinance_shard_entry_price_face arms={len(arms)} arms_passed={passed} "
        f"arms_failed={len(failed)} failed={failed} identity_set={gate['identity_set']} "
        f"rows={len(faces)}"
    )
    print(f"chain_exit={0 if not failed else 1}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
