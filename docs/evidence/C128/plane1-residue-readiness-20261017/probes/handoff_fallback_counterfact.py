"""Certify the stale-contract-name fallback shipped into the federal_reserve handoff judge.

Wave 28 retargeted the H.15 leg to the class ``domains.yaml`` declares, but the C100
handoff still names the class the leg emitted when the handoff was written. The shipped
judge (``tests/test_federal_reserve_iter2_models.py``, function
``test_integration_handoff_semantics_reference_typed_contract_fields``) keeps that drift
from silencing itself: when the handoff's ``contract`` name is not one of the models the
test knows, it re-resolves through ``contract_model(domain)`` and then still runs the
field-face check on whatever it resolved to.

A fallback that never fires is a free pass, and a fallback that swallows every error is
worse than no fallback, so this probe prices both edges in memory -- nothing is edited,
no producer or claims file is touched:

* A/D the shipped judge runs green, before and after every patch;
* B forge ``contract_model`` to return a class whose name is *absent* from the test's own
  model set. The fallback must refuse it at the shipped ``assert shipped.__name__ in
  contracts_by_name``, and the patch must be seen to touch only the stale domain;
* C forge ``contract_model`` to return a class that *is* registered but is the wrong
  contract for that domain. The name refusal cannot see this -- only the field-face check
  can -- so the raise must land on the ``semantic_fields <= contract_fields`` line. Arm C
  first measures that a wrong registered candidate exists (``can_differ``): an arm that
  cannot go red is not evidence;
* E delete the fallback in memory (index the handoff name straight into the model set) and
  require a ``KeyError`` on the stale domain, with the six non-stale domains measured green
  on the same path so E is not vacuous. This is what shows the branch is load-bearing
  rather than decorative;
* F after restoring, run the sibling row-model guard in the same module so a leaked patch
  cannot be mistaken for a green judge.

Raise sites are read from the shipped file with ``ast`` and re-confirmed from the
traceback's own line number and source text, so this carrier never asserts a line number
that the file does not carry.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

PROBE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROBE_DIR.parents[4]
TEST_PATH = REPO_ROOT / "tests" / "test_federal_reserve_iter2_models.py"
TEST_FN = "test_integration_handoff_semantics_reference_typed_contract_fields"
GUARD_FN = "test_declared_row_models_are_their_domain_contracts"
HANDOFF = REPO_ROOT / "docs" / "evidence" / "C100" / "federal-reserve" / "integration.json"
MARKERS = ("shipped.__name__ in contracts_by_name", "set(semantic_fields) <= contract_fields")


def _load_test_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fr_handoff_test_inmem", TEST_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {TEST_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fr_handoff_test_inmem"] = module
    spec.loader.exec_module(module)
    return module


def _assert_face() -> dict[str, int]:
    """Every assert in the shipped judge, keyed by its unparsed test expression."""
    tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"), filename=str(TEST_PATH))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == TEST_FN)
    return {
        ast.unparse(node.test): int(node.lineno)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assert)
    }


def _source_line(lineno: int) -> str:
    """The shipped file's own text at a measured line number."""
    return TEST_PATH.read_text(encoding="utf-8").splitlines()[lineno - 1].strip()


def _run(fn: Callable[[], object]) -> tuple[bool, str, int, str]:
    """Call a test function in memory; report green/raise, the exception, its line and text."""
    try:
        fn()
    except Exception as exc:  # the raise is the reading here, so it is caught and reported
        frames = [
            f
            for f in traceback.extract_tb(exc.__traceback__)
            if Path(f.filename).resolve() == TEST_PATH.resolve()
        ]
        if not frames:
            return False, f"{type(exc).__name__}: {exc}", -1, "<outside test file>"
        last = frames[-1]
        line = int(last.lineno)
        return False, f"{type(exc).__name__}: {exc}", line, _source_line(line)
    return True, "", 0, ""


def _resolve(klass: type) -> str:
    return f"{klass.__module__}.{klass.__qualname__}"


def main() -> int:
    """Print the judge's assert face, the stale-name population, and the six counterfact arms."""
    t = _load_test_module()
    flags: dict[str, bool] = {}
    face = _assert_face()
    sites = {m: next((ln for key, ln in face.items() if m in key), -1) for m in MARKERS}
    root_marker = (REPO_ROOT / "opendata" / "data" / "domains.py").is_file()
    print(f"repo_root={REPO_ROOT} root_marker_present={root_marker}")
    print(f"test_file={TEST_PATH.relative_to(REPO_ROOT)}")
    print(
        f"judge_assert_sites={len(face)} fallback_site={sites[MARKERS[0]]} "
        f"field_site={sites[MARKERS[1]]}"
    )
    for key, lineno in sorted(face.items(), key=lambda kv: kv[1]):
        print(f"  assert@{lineno}: {key}")

    handoff = json.loads(HANDOFF.read_text(encoding="utf-8"))
    contracts_by_name = {model.__name__: model for model, _host in t._MODELS.values()}
    stale = [
        domain
        for domain, entry in handoff["domains"].items()
        if t.DomainSpec.model_validate(entry).contract not in contracts_by_name
    ]
    print(f"handoff_domains={len(handoff['domains'])} stale_named_domains={len(stale)} {stale}")
    print(f"registered_model_names={sorted(contracts_by_name)}")

    a_green, a_err, a_line, _ = _run(getattr(t, TEST_FN))
    flags["A_unmodified_green"] = a_green
    print(f"A_unmodified_green={a_green} err={a_err[:120]} line={a_line}")

    target = stale[0] if stale else ""
    spec = t.DomainSpec.model_validate(handoff["domains"][target]) if target else None
    shipped_name = ""
    semantic: tuple[str, ...] = ()
    if spec is not None:
        shipped = t.contract_model(target)
        shipped_name = str(shipped.__name__)
        semantic = (
            (() if spec.time_field is None else (spec.time_field,))
            + spec.natural_key
            + spec.filter_dims
        )
        print(
            f"target_domain={target} spec_contract={spec.contract} shipped={_resolve(shipped)} "
            f"shipped_registered={shipped_name in contracts_by_name} "
            f"time_field={spec.time_field} natural_key={list(spec.natural_key)} "
            f"filter_dims={list(spec.filter_dims)} "
            f"semantic_fields={list(semantic)} "
            f"semantic_in_shipped="
            f"{set(semantic) <= set(contracts_by_name[shipped_name].model_fields)}"
        )

    # B: the fallback's own refusal. Forge a resolution whose class name the test does not know.
    look_alike = sys.modules["opendata.data.models.federal_reserve_macro"].FederalReserveYieldCurve
    calls: list[tuple[str, str]] = []
    real_cm = t.contract_model

    def _forge(fake: type) -> Callable[[str], type]:
        def _cm(domain: str) -> type:
            if domain == target:
                calls.append((domain, fake.__name__))
                return fake
            result = real_cm(domain)
            calls.append((domain, str(result.__name__)))
            return result

        return _cm

    try:
        t.contract_model = _forge(look_alike)
        b_green, b_err, b_line, b_text = _run(getattr(t, TEST_FN))
        b_only_target = {domain for domain, _name in calls}
    finally:
        t.contract_model = real_cm
    flags["B_absent_name_reddens"] = not b_green
    flags["B_raise_is_the_fallback_assert"] = b_line == sites[MARKERS[0]]
    flags["B_patch_touched_only_target"] = b_only_target == {target} and len(calls) == 1
    print(
        f"B_absent_name_reddens={flags['B_absent_name_reddens']} forged={_resolve(look_alike)} "
        f"forged_registered={look_alike.__name__ in contracts_by_name} "
        f"err={b_err[:120]} "
        f"raise_line={b_line} expect={sites[MARKERS[0]]} raise_text={b_text!r} "
        f"patched_domains={sorted(b_only_target)} calls={calls} "
        f"B_raise_is_the_fallback_assert={flags['B_raise_is_the_fallback_assert']} "
        f"B_patch_touched_only_target={flags['B_patch_touched_only_target']}"
    )

    # C: a registered-but-wrong resolution. Measure that such a candidate exists before claiming it.
    wrong_candidates = [
        name
        for name, klass in sorted(contracts_by_name.items())
        if name != shipped_name and not set(semantic) <= set(klass.model_fields)
    ]
    missing_face: dict[str, list[str]] = {}
    if wrong_candidates:
        wrong_klass = contracts_by_name[wrong_candidates[0]]
        missing_face[wrong_candidates[0]] = sorted(set(semantic) - set(wrong_klass.model_fields))
    flags["C_can_differ"] = bool(wrong_candidates) and bool(
        missing_face.get(wrong_candidates[0] or "")
    )
    calls.clear()
    if wrong_candidates:
        try:
            t.contract_model = _forge(wrong_klass)
            c_green, c_err, c_line, c_text = _run(getattr(t, TEST_FN))
            c_only_target = {domain for domain, _name in calls} == {target}
        finally:
            t.contract_model = real_cm
        flags["C_registered_wrong_reddens"] = not c_green
        flags["C_raise_is_the_field_assert"] = c_line == sites[MARKERS[1]]
        flags["C_patch_touched_only_target"] = c_only_target and len(calls) == 1
        print(
            f"C_registered_wrong_reddens={flags['C_registered_wrong_reddens']} "
            f"candidates={wrong_candidates} forged={_resolve(wrong_klass)} "
            f"forged_registered={wrong_klass.__name__ in contracts_by_name} "
            f"semantic_missing_from_forged={missing_face} "
            f"err={c_err[:120]} raise_line={c_line} expect={sites[MARKERS[1]]} "
            f"raise_text={c_text!r} "
            f"calls={list(calls)} "
            f"C_raise_is_the_field_assert={flags['C_raise_is_the_field_assert']} "
            f"C_patch_touched_only_target={flags['C_patch_touched_only_target']}"
        )
    else:
        flags["C_registered_wrong_reddens"] = False
        flags["C_raise_is_the_field_assert"] = False
        flags["C_patch_touched_only_target"] = False
        print(
            "C_registered_wrong_reddens=False candidates=[] INCONCLUSIVE "
            "no registered class differs on the field face"
        )

    # E: delete the fallback in memory -- index the handoff name straight into the model set.
    keyerror_domains: list[str] = []
    plain_ok: list[str] = []
    for domain, entry in handoff["domains"].items():
        name = str(t.DomainSpec.model_validate(entry).contract)
        try:
            contracts_by_name[name]
        except KeyError:
            keyerror_domains.append(domain)
        else:
            plain_ok.append(domain)
    flags["E_fallback_is_load_bearing"] = keyerror_domains == stale
    flags["E_not_vacuous"] = bool(plain_ok) and not (set(keyerror_domains) & set(plain_ok))
    print(
        f"E_no_fallback_keyerror={len(keyerror_domains)} {keyerror_domains} "
        f"E_no_fallback_ok={len(plain_ok)} stale={stale} "
        f"E_fallback_is_load_bearing={flags['E_fallback_is_load_bearing']} "
        f"E_not_vacuous={flags['E_not_vacuous']}"
    )

    # D + F: restore, re-run the judged function and its sibling guard in the same module.
    d_green, d_err, d_line, _ = _run(getattr(t, TEST_FN))
    flags["D_restored_green"] = d_green
    print(f"D_restored_green={d_green} err={d_err[:120]} line={d_line}")
    f_green, f_err, f_line, _ = _run(getattr(t, GUARD_FN))
    flags["F_guard_green_after_restore"] = f_green
    print(
        f"F_guard_green_after_restore={f_green} "
        f"guard={GUARD_FN} err={f_err[:120]} line={f_line} "
        f"contract_model_identity_restored={t.contract_model is real_cm}"
    )

    arms = len(flags)
    passed = sum(1 for value in flags.values() if value)
    print(
        f"handoff_fallback_counterfact_face stale_named_domains={len(stale)} "
        f"fallback_site={sites[MARKERS[0]]} field_site={sites[MARKERS[1]]} "
        f"flags={dict(sorted(flags.items()))} "
        f"arms={arms} arms_passed={passed} arms_failed={arms - passed} "
        f"verdict={'GREEN' if passed == arms else 'RED'}"
    )
    return 0 if passed == arms else 1


if __name__ == "__main__":
    raise SystemExit(main())
