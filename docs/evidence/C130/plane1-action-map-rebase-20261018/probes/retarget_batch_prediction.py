"""Price the whole ``retarget_fetcher_output`` bucket per row, on the axes a batch is picked from.

Wave 30 step 0 re-took ``residue_axis_census.py`` at the 281/69 generation: it printed
``retarget_fetcher_output: {federal_reserve: 1, tmx: 7, yfinance: 1}`` with eight arms green and
``witnessable_today=0``. That map answers "what is the *first* job of each residue row" and stops at
the first failing axis, so a row it labels this way may still be missing a code-carrying guard, may
sit in a provider with no witness module, and -- the axis that mis-priced wave 26's federal_reserve
batch -- may need the *published field face* to change, which is a product ruling rather than wave
work. The shipped ``_verdict`` collapses all of that into one label, so this probe re-feeds each
candidate's own measured face to ``_verdict`` with the row-class axis forced green and prints the
other axes beside it.

A third axis had to be added, because the shipped one is blind in a way that mis-bucketed 8 of these
9 rows. ``_row_class_face`` attributes a construction it can see as ``Model(...)`` -- in the class
body, or inside a module-level helper it re-scans. tmx and yfinance build through
``_row(TmxEquityQuote, {...})``: the class is an *argument*, and the helper's own body only ever
mentions the parameter ``model``, so the shipped scan prints ``emitted=[]`` and the
whole-residue map calls the row a fetcher-output retarget. Reading the same bytes a second way --
any call inside the fetcher's class whose first positional argument names a
``ContractModel`` subclass of this module --
shows that 7 of the 8 already pass ``type(row) is contract_model(domain)``, the exact identity
``provider_model_query._serialize_contract_row`` demands. Their first job is then a ``row_model``
declaration (the wave-29 deribit pattern), not an identity edit, and that re-pricing is what this
arm measures and what arms G/H falsify.

Nothing is re-typed and no population is hardcoded: row lines come from a live subprocess run of
``residue_axis_census.py`` (the child gets the repo root on ``PYTHONPATH``, since those probes
resolve ``opendata`` from cwd and a bare ``python <path>`` invocation does not), each row's columns
from ``incomplete_row_guard_check._row``, the per-provider seam columns from
``seam_reachability_census.py``, and the simulated verdict is the shipped ``_verdict``.

Two questions are answered apart, because a batch needs both:

``can the harness serve it`` -- seam bucket, verbs called on the governed client, ``fake_ok`` read
against the *live* fake surface and again against a forged ``{"get"}`, and whether the provider
already has a witness module (a first module cost 168-482 lines in waves 24/25/29; a later row costs
one declaration plus one CONFIG entry).
``what does the identity edit publish`` -- the field *names* of the classes the serving path
constructs versus ``contract_model(domain)``. A non-empty ``contract_only`` means the contract asks
for fields the row class does not carry, so re-targeting is a mapping decision
(``needs_ruling``); an empty one means the published rows already carry everything the
contract asks (``mechanical``). The
test is name-level, not a value-mappability proof, so ``emitted`` and ``emitted_fields`` print for a
reader to check.

Arms, all eight of which can fail:

``A_population_from_carrier``
    row lines parsed == the census's printed ``residue=`` == the live report's ``report_rows -
    OFFLINE_ACCEPTED``, and the candidate set is non-empty. Two of the three come from different
    documents.
``B_action_reproduced``
    ``residue_axis_census._action`` re-run on this probe's measurements disagrees with the carrier's
    own label on zero rows. A divergence is a bug report on one of the two paths, not an expectation
    to re-pin.
``C_simulation_reads_the_flag``
    re-feeding each face with ``declared=False, is_contract=False`` returns its live verdict (the
    flag flip must be the only thing driving the simulation), and every candidate's simulated
    verdict is not ``ROW_CLASS_NOT_CONTRACT`` -- the simulation must be capable of differing.
``D_field_face_two_sided``
    the field-face predicate is forged, not asserted: a superset input must read
    ``needs_ruling`` and the shared-fields-only subset must read ``mechanical``; and
    ``federal_reserve::FomcDocuments`` --
    recorded since wave 26 as macro 7 fields versus contract 5 sharing one field -- must land on the
    ``needs_ruling`` side from live bytes.
``E_guard_axis_live``
    every candidate carries ``_class_sites`` output (files non-empty) and the job classes sum to the
    candidate count. Rows whose ``raises`` is zero are listed in ``sim_via_no_raises`` because the
    shipped ``_verdict`` fallthrough labels such a row ``UNBOUND`` after a retarget -- a quirk of
    the label, not a second binding job.
``F_widening_is_the_mover``
    the live fake surface must measure ``{"get", "request"}`, and forging it back to ``{"get"}``
    must flip ``fake_ok`` for exactly the candidates whose provider calls ``request``. That
    prices what
    wave 29's widening bought this bucket: reachability, and zero rows.
``G_builder_axis_two_sided``
    the new axis is forged too, in both directions: dropping the contract class from one candidate's
    measured ``built`` set must change its job label away from *whatever it really is*, and dropping
    one field its payload actually fills must make ``unfilled_required`` name that field; while the
    two rows the shipped scan *can* read (federal_reserve's look-alike, yfinance's invisible shape)
    must still refuse the declare label from live bytes. The probe row is selected by the
    measurement (first sorted candidate whose ``built`` set carries its own contract class, and
    whose payload is literal), not by its label: the step-0c carrier took ``declare_only``, and step
    2 landed those four tmx rows, so the selection went stale the moment the prediction it priced
    came true. That is a landed-face constant baked into an arm, and re-basing it to the axis is the
    fix -- the arm is about what the builder reading carries, not about which label exists this
    generation.
``H_declare_calibration``
    the declaration being proposed is the shape the judge already accepts: every row of the shipped
    ``_accepted_sample`` of ``OFFLINE_ACCEPTED`` identities must satisfy ``row_model is
    contract_model(domain)``, and the job classes must partition the candidates with no row
    defaulted into ``declare_only`` by a missing reading (``shape_unknown`` rows are listed, never
    counted).

Exits non-zero when an arm fails.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

PROBE_ROOT = Path(__file__).resolve().parents[3] / "C128"
C128_PROBES = PROBE_ROOT / "plane1-residue-readiness-20261017" / "probes"
REPO_ROOT = Path(__file__).resolve().parents[5]
# The shipped probes import ``opendata`` at module scope and a bare ``python <path>`` puts neither
# the repo root nor cwd on sys.path, so this process and its children get it explicitly.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(C128_PROBES))

import incomplete_row_guard_check as axes  # noqa: E402
import residue_axis_census as rac  # noqa: E402
import seam_surface_match as seam  # noqa: E402

PROBE_DIR = C128_PROBES
CENSUS_PROBE = PROBE_DIR / "residue_axis_census.py"
REACH_PROBE = PROBE_DIR / "seam_reachability_census.py"
ACTION = "retarget_fetcher_output"
FED_ROW = "federal_reserve::FomcDocuments"
YF_ROW = "yfinance::KeyMetrics"
JOBS = (
    "declare_only",
    "declare_plus_guard",
    "declare_unreachable",
    "needs_ruling",
    "shape_unknown",
)
KV = re.compile(r"(\w+)=(\S+)")


def _stdout(probe: Path) -> tuple[list[str], int]:
    """Run one shipped probe for its raw lines, reporting its own exit code."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    run = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(probe)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return run.stdout.splitlines(), run.returncode


def _row_lines(lines: list[str]) -> list[dict[str, str]]:
    """Parse the census's per-row lines; the identity is the bare token before the first field."""
    rows: list[dict[str, str]] = []
    for line in lines:
        if not line.startswith("row "):
            continue
        identity, _sep, rest = line[4:].partition(" ")
        rows.append({"identity": identity, **dict(KV.findall(rest))})
    return rows


def _reach_lines(lines: list[str]) -> dict[str, dict[str, str]]:
    """Provider to its reachability reading, including the verbs its models call."""
    out: dict[str, dict[str, str]] = {}
    for line in lines:
        if not line.startswith("seam_census_provider="):
            continue
        fields = dict(
            token.split("=", 1) for token in re.split(r"\s+(?=\w+=)", line) if "=" in token
        )
        out[fields["seam_census_provider"]] = fields
    return out


def _line(identity: str, cells: list[tuple[str, Any]]) -> str:
    """One printed row: an id, then every named cell in the order given."""
    return " ".join([f"row_out {identity}", *(f"{key}={value}" for key, value in cells)])


def _class_fields(klass: object) -> set[str]:
    """Field names of a pydantic model; empty for anything that is not one."""
    model_fields = getattr(klass, "model_fields", None)
    return set(model_fields) if isinstance(model_fields, dict) else set()


def _resolve(dotted: str) -> object | None:
    """Resolve a ``module.QualName`` printed by ``_row_class_face`` back to its class."""
    module_name, _sep, qual = dotted.partition(".")
    if module_name not in sys.modules:
        try:
            __import__(module_name)
        except Exception:  # an unimportable name is a reading, not a crash
            return None
    obj: object = sys.modules.get(module_name)
    for part in qual.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj


def _contract(domain: object) -> object | None:
    """``contract_model(domain)`` through the shipped resolver; None if it refuses."""
    try:
        return axes.contract_model(domain)
    except Exception:  # NO_CONTRACT_FOR is the shipped reading
        return None


def _field_face(emitted: set[str], contract: set[str]) -> dict[str, Any]:
    """What swapping this row's output class for the contract class would publish."""
    return {
        "shared": emitted & contract,
        "contract_only": contract - emitted,
        "emitted_only": emitted - contract,
        "needs_ruling": bool(contract - emitted),
    }


def _path_fields(face: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Contract field names, and the union of fields this serving path actually constructs."""
    contract = _class_fields(_contract(face.get("domain")))
    emitted: set[str] = set()
    for dotted in [str(name) for name in face.get("emitted", [])]:
        klass = _resolve(dotted)
        if klass is not None:
            emitted |= _class_fields(klass)
    return contract, emitted


def _required_fields(klass: object) -> set[str]:
    """Field names the contract refuses to build without."""
    model_fields = getattr(klass, "model_fields", None)
    if not isinstance(model_fields, dict):
        return set()
    return {name for name, field in model_fields.items() if field.is_required()}


def _dotted(klass: object) -> str:
    """``module.QualName`` for a class, matching the form ``_row_class_face`` prints."""
    return f"{getattr(klass, '__module__', '')}.{getattr(klass, '__qualname__', '')}"


def _contract_class(module: object, node: ast.expr) -> type | None:
    """The ``ContractModel`` subclass an argument or callee name resolves to, else None."""
    if not isinstance(node, ast.Name):
        return None
    obj = getattr(module, node.id, None)
    return obj if isinstance(obj, type) and issubclass(obj, axes.ContractModel) else None


def _dict_keys(node: ast.expr | None) -> set[str]:
    """Keys of a dict literal argument; empty for a variable, which is a reading, not a bug."""
    if not isinstance(node, ast.Dict):
        return set()
    return {str(key.value) for key in node.keys if isinstance(key, ast.Constant)}


def _builder_face(fetcher: object) -> dict[str, Any]:
    """Contract classes passed *as an argument* to a row builder, plus the keys the code fills.

    The shipped ``_row_class_face`` re-scans module-level helpers for ``Model(...)`` literals, so it
    cannot attribute ``_row(TmxEquityQuote, {...})`` -- inside the helper the class is only ever the
    parameter ``model``. This reads the same bytes from the call-site side: any call inside the
    fetcher's own class whose first positional argument (or callee) names a ``ContractModel``
    subclass resolvable in that module, with the keys of its dict-literal payload and its keyword
    names as the fields the code fills. A payload passed as a variable yields built-but-no-keys,
    which is reported
    as a dynamic payload rather than silently as an empty one.
    """
    built: set[str] = set()
    filled: set[str] = set()
    sites: list[str] = []
    for klass in type(fetcher).__mro__:
        module_name = str(klass.__module__)
        if not module_name.startswith("opendata."):
            continue
        module = sys.modules.get(module_name)
        path = inspect.getsourcefile(klass)
        if module is None or path is None:
            continue
        source = Path(path).resolve()
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        owner = klass.__qualname__.split(".")[0]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef) and node.name == owner):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                callee = _contract_class(module, call.func)
                positional = [
                    (index, _contract_class(module, arg)) for index, arg in enumerate(call.args)
                ]
                if callee is not None:
                    built.add(_dotted(callee))
                    filled |= _dict_keys(call.args[0] if call.args else None)
                    filled |= {str(kw.arg) for kw in call.keywords if kw.arg}
                    sites.append(f"{source.name}:{call.lineno} construct")
                for index, klass_arg in positional:
                    if klass_arg is None:
                        continue
                    built.add(_dotted(klass_arg))
                    payload = call.args[index + 1] if len(call.args) > index + 1 else None
                    filled |= _dict_keys(payload)
                    filled |= {str(kw.arg) for kw in call.keywords if kw.arg}
                    sites.append(f"{source.name}:{call.lineno} builder")
    return {"built": built, "filled": filled, "sites": sites}


def _job(
    contract: object | None,
    built: set[str],
    sim: str,
    served: bool,
    payload_keys: set[str],
    required: set[str],
) -> tuple[str, dict[str, Any]]:
    """Name the first job of one retarget candidate, and every axis the name was made from."""
    contract_dotted = "" if contract is None else _dotted(contract)
    constructed = bool(contract_dotted) and contract_dotted in built
    # A payload built from a variable has no statically visible keys, so its required-field face is
    # unmeasured rather than empty; printing [] here would read as "every required field is filled".
    unfilled: list[str] | None = sorted(required - payload_keys) if payload_keys else None
    detail = {
        "contract_dotted": contract_dotted,
        "contract_constructed": constructed,
        "payload_dynamic": bool(constructed and not payload_keys),
        "unfilled_required": unfilled,
    }
    if not constructed:
        detail["job"] = "needs_ruling" if built else "shape_unknown"
        return detail["job"], detail
    if sim not in axes.TRIPPABLE:
        detail["job"] = "declare_plus_guard"
    elif not served:
        detail["job"] = "declare_unreachable"
    else:
        detail["job"] = "declare_only"
    return detail["job"], detail


def _name_in_package(identity: str) -> bool:
    """Whole-token test that this model's own name appears in its provider package."""
    provider, _sep, model = identity.partition("::")
    return bool(re.search(rf"\b{re.escape(model)}\b", rac._package_blob(provider)))


def main() -> int:
    """Print the bucket's per-row axes and the subset this generation can serve."""
    census_lines, census_rc = _stdout(CENSUS_PROBE)
    reach_lines, reach_rc = _stdout(REACH_PROBE)
    print(
        _line(
            "child_exits",
            [
                ("census_rc", census_rc),
                ("reach_rc", reach_rc),
                ("census_stdout_lines", len(census_lines)),
                ("reach_stdout_lines", len(reach_lines)),
            ],
        )
    )
    if census_rc or reach_rc:
        raise SystemExit("a shipped child probe exited non-zero, see child_exits above")
    rows = _row_lines(census_lines)
    face_line = next(
        (
            dict(KV.findall(line))
            for line in census_lines
            if line.startswith("residue_axis_census_face")
        ),
        {},
    )
    reach = _reach_lines(reach_lines)
    report = seam._load_report()
    judged = {str(item["model_identity"]): str(item["status"]) for item in report["models"]}
    accepted = sum(1 for item in report["models"] if item["status"] == "OFFLINE_ACCEPTED")
    report_rows = len(report["models"])
    census_residue = int(face_line.get("residue", "-1"))
    surface = seam._fake_surface()
    get_only = {"get"}

    def verbs_of(provider: str) -> set[str]:
        return rac._verbs(reach.get(provider, {}).get("package_verbs", "[]"))

    def fake_ok(provider: str, active: set[str]) -> bool:
        called = verbs_of(provider)
        return bool(called) and called <= active

    measured = {
        row["identity"]: axes._row(row["identity"], judged.get(row["identity"], "?"))
        for row in rows
    }
    candidates = [row["identity"] for row in rows if row.get("action") == ACTION]
    jobs: dict[str, str] = {}
    detail: dict[str, dict[str, Any]] = {}
    by_job: dict[str, list[str]] = {name: [] for name in JOBS}
    no_raises: list[str] = []
    widen_moved: list[str] = []
    convertible: list[str] = []
    for identity in candidates:
        face = measured[identity]
        provider, _sep, model = identity.partition("::")
        contract_fields, emitted_fields = _path_fields(face)
        sim = axes._verdict({**face, "declared": True, "is_contract": True})
        unflipped = axes._verdict({**face, "declared": False, "is_contract": False})
        field_face = (
            _field_face(emitted_fields, contract_fields)
            if contract_fields and emitted_fields
            else None
        )
        fetcher, _err = axes._fetcher(provider, model)
        contract = _contract(face.get("domain"))
        bf = _builder_face(fetcher) if fetcher is not None else {"built": set(), "filled": set()}
        built = set(face.get("emitted", [])) | set(bf["built"])
        payload = set(bf["filled"])
        required = _required_fields(contract)
        served, served_get = fake_ok(provider, surface), fake_ok(provider, get_only)
        job, job_detail = _job(contract, built, sim, served, payload, required)
        jobs[identity] = job
        detail[identity] = {**job_detail, "built": built, "payload": payload, "sim": sim}
        by_job[job].append(identity)
        if served != served_get:
            widen_moved.append(identity)
        raises = int(face.get("raises", 0) or 0)
        if not raises:
            no_raises.append(identity)
        if job == "declare_only" and not face["ruled_out"]:
            convertible.append(identity)
        print(
            _line(
                identity,
                [
                    ("status", face["status"]),
                    ("verdict", face["verdict"]),
                    ("sim_after_retarget", sim),
                    ("sim_unflipped", unflipped),
                    ("job", job),
                    ("fetcher", face.get("fetcher")),
                    ("contract", face.get("contract")),
                    ("contract_class", job_detail["contract_dotted"] or "None"),
                    ("contract_fields", len(contract_fields)),
                    ("contract_required", len(required)),
                    ("shipped_emitted", face.get("emitted")),
                    ("builder_built", sorted(bf["built"])),
                    ("builder_sites", bf["sites"]),
                    ("contract_constructed", job_detail["contract_constructed"]),
                    ("payload_keys", len(payload)),
                    ("payload_dynamic", job_detail["payload_dynamic"]),
                    ("unfilled_required", job_detail["unfilled_required"]),
                    ("emitted_fields", len(emitted_fields)),
                    ("shared", None if field_face is None else len(field_face["shared"])),
                    (
                        "contract_only",
                        None if field_face is None else sorted(field_face["contract_only"]),
                    ),
                    (
                        "emitted_only",
                        None if field_face is None else len(field_face["emitted_only"]),
                    ),
                    ("raises", raises),
                    ("code_raises", face.get("code_raises")),
                    ("counts", face.get("counts")),
                    ("trippable_sites", len(face.get("trippable", []))),
                    ("files", len(face.get("files", []))),
                    ("seam", reach.get(provider, {}).get("bucket", "ABSENT")),
                    ("verbs", sorted(verbs_of(provider))),
                    ("fake_ok", served),
                    ("fake_ok_if_get_only", served_get),
                    ("witness_module", reach.get(provider, {}).get("witness_module")),
                    ("ruled_out", bool(face["ruled_out"])),
                ],
            )
        )

    off_action = sorted(
        row["identity"]
        for row in rows
        if rac._action(
            str(measured[row["identity"]]["verdict"]),
            bool(measured[row["identity"]]["ruled_out"]),
            str(
                reach.get(row["identity"].partition("::")[0], {}).get(
                    "bucket", "ABSENT_FROM_CENSUS"
                )
            ),
            fake_ok(row["identity"].partition("::")[0], surface),
            _name_in_package(row["identity"]),
        )
        != row["action"]
    )

    fed = measured.get(FED_ROW, {})
    fed_contract, fed_emitted = _path_fields(fed) if fed else (set(), set())
    fed_face = _field_face(fed_emitted, fed_contract) if fed_contract and fed_emitted else None
    forged_super = _field_face(fed_emitted, fed_contract | {"zz_probe_only_field"}) if fed else None
    forged_min = _field_face(fed_emitted, fed_emitted & fed_contract) if fed else None

    arm_a = len(rows) == census_residue == report_rows - accepted and bool(candidates)
    print(
        _line(
            "A_population_from_carrier",
            [
                ("parsed", len(rows)),
                ("census_residue", census_residue),
                ("report_minus_accepted", report_rows - accepted),
                ("report_rows", report_rows),
                ("accepted", accepted),
                ("candidates", len(candidates)),
                ("PASS", arm_a),
            ],
        )
    )

    arm_b = not off_action
    print(
        _line(
            "B_action_reproduced",
            [
                ("rows", len(rows)),
                ("off_action", off_action[:3]),
                ("identity_set", str(report["inventory"]["expected_identity_set_sha256"])[:16]),
                ("PASS", arm_b),
            ],
        )
    )

    unflipped_match = all(
        axes._verdict({**measured[i], "declared": False, "is_contract": False})
        == measured[i]["verdict"]
        for i in candidates
    )
    sim_off = [
        i
        for i in candidates
        if axes._verdict({**measured[i], "declared": True, "is_contract": True})
        != "ROW_CLASS_NOT_CONTRACT"
    ]
    arm_c = unflipped_match and len(sim_off) == len(candidates)
    print(
        _line(
            "C_simulation_reads_the_flag",
            [
                ("unflipped_match", unflipped_match),
                ("sim_not_contract", f"{len(sim_off)}/{len(candidates)}"),
                ("PASS", arm_c),
            ],
        )
    )

    arm_d = bool(
        forged_super
        and forged_super["needs_ruling"]
        and forged_min
        and not forged_min["needs_ruling"]
        and fed_face
        and fed_face["needs_ruling"]
    )
    print(
        _line(
            "D_field_face_two_sided",
            [
                (
                    "forged_super_ruling",
                    None if not forged_super else forged_super["needs_ruling"],
                ),
                ("forged_min_ruling", None if not forged_min else forged_min["needs_ruling"]),
                ("fomc_shared", None if not fed_face else len(fed_face["shared"])),
                (
                    "fomc_contract_only",
                    None if not fed_face else sorted(fed_face["contract_only"]),
                ),
                ("fomc_needs_ruling", None if not fed_face else fed_face["needs_ruling"]),
                ("PASS", arm_d),
            ],
        )
    )

    guard_missing = [i for i in candidates if not measured[i].get("files")]
    job_sum = sum(len(v) for v in by_job.values())
    arm_e = not guard_missing and job_sum == len(candidates) and len(no_raises) <= len(candidates)
    print(
        _line(
            "E_guard_axis_live",
            [
                ("guard_missing", guard_missing),
                ("job_sum", job_sum),
                ("candidates", len(candidates)),
                ("by_job", {key: len(value) for key, value in sorted(by_job.items())}),
                ("sim_via_no_raises", no_raises),
                ("PASS", arm_e),
            ],
        )
    )

    tmx_candidates = [i for i in candidates if i.partition("::")[0] == "tmx"]
    arm_f = surface == {"get", "request"} and set(widen_moved) == set(tmx_candidates)
    print(
        _line(
            "F_widening_is_the_mover",
            [
                ("live_surface", sorted(surface)),
                ("forged_surface", sorted(get_only)),
                ("fake_ok_flipped", len(widen_moved)),
                ("tmx_candidates", len(tmx_candidates)),
                ("flipped", sorted(widen_moved)[:2]),
                ("PASS", arm_f),
            ],
        )
    )

    # G: the new axis must be capable of reading *against* a row, in both of its directions. The
    # probe row is chosen by the measurement, not by this generation's label (see arm docstring).
    probe_row = next(
        (
            i
            for i in sorted(candidates)
            if detail[i]["contract_dotted"] in detail[i]["built"] and detail[i]["payload"]
        ),
        "",
    )
    if probe_row:
        probe_contract = _contract(measured[probe_row]["domain"])
        probe_detail = detail[probe_row]
        probe_required = _required_fields(probe_contract)
        without_class = _job(
            probe_contract,
            set(probe_detail["built"]) - {probe_detail["contract_dotted"]},
            probe_detail["sim"],
            True,
            probe_detail["payload"],
            probe_required,
        )[0]
        filled_required = sorted(set(probe_required) & set(probe_detail["payload"]))
        one_required = filled_required[0] if filled_required else ""
        forged_payload = set(probe_detail["payload"]) - {one_required}
        missing_field = (
            None
            if not forged_payload
            else _job(
                probe_contract,
                probe_detail["built"],
                probe_detail["sim"],
                True,
                forged_payload,
                probe_required,
            )[1]["unfilled_required"]
        )
    else:
        without_class, missing_field, one_required = "NO_PROBE_ROW", [], ""
    fed_refused = jobs.get(FED_ROW) == "needs_ruling"
    yf_refused = jobs.get(YF_ROW) == "shape_unknown"
    arm_g = (
        bool(probe_row)
        and without_class != jobs[probe_row]
        and bool(one_required)
        and one_required in list(missing_field or [])
        and fed_refused
        and yf_refused
    )
    print(
        _line(
            "G_builder_axis_two_sided",
            [
                ("probe_row", probe_row or "NONE"),
                ("real_job", jobs.get(probe_row)),
                ("forged_without_contract_class", without_class),
                ("forged_dropped_field", one_required),
                ("forged_missing_required", missing_field),
                ("fomc_refuses_declare_label", fed_refused),
                ("yfinance_refuses_declare_label", yf_refused),
                ("PASS", arm_g),
            ],
        )
    )

    sample = axes._accepted_sample(judged, stride=40)
    sample_rows = [axes._row(i, judged[i]) for i in sample]
    sample_declared = sum(1 for row in sample_rows if row.get("declared_is_contract"))
    sample_failed = [row["identity"] for row in sample_rows if not row.get("declared_is_contract")]
    defaulted = [i for i in candidates if jobs.get(i) not in JOBS]
    arm_h = (
        bool(sample)
        and not sample_failed
        and not defaulted
        and not (set(by_job["shape_unknown"]) & set(convertible))
    )
    print(
        _line(
            "H_declare_calibration",
            [
                ("accepted_sample", len(sample)),
                ("sample_row_model_is_contract", f"{sample_declared}/{len(sample)}"),
                ("sample_failed", sample_failed),
                ("job_unclassified", defaulted),
                ("shape_unknown_not_defaulted_in", sorted(set(by_job["shape_unknown"]))),
                ("PASS", arm_h),
            ],
        )
    )

    no_module = sorted(
        {
            i.partition("::")[0]
            for i in candidates
            if reach.get(i.partition("::")[0], {}).get("witness_module") != "True"
        }
    )
    print(
        _line(
            "retarget_batch_face",
            [
                ("candidates", len(candidates)),
                ("declare_only", sorted(by_job["declare_only"])),
                ("declare_plus_guard", sorted(by_job["declare_plus_guard"])),
                ("declare_unreachable", sorted(by_job["declare_unreachable"])),
                ("needs_ruling", sorted(by_job["needs_ruling"])),
                ("shape_unknown", sorted(by_job["shape_unknown"])),
                ("payload_dynamic", sorted(i for i in candidates if detail[i]["payload_dynamic"])),
                ("convertible_after_retarget", len(convertible)),
                ("convertible_rows", sorted(convertible)),
                ("providers_without_module", no_module),
                ("fake_surface", sorted(surface)),
            ],
        )
    )
    arms = (arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g, arm_h)
    print(
        _line(
            "face_arms",
            [
                ("arms", len(arms)),
                ("arms_passed", sum(arms)),
                ("arms_failed", len(arms) - sum(arms)),
            ],
        )
    )
    return 0 if all(arms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
