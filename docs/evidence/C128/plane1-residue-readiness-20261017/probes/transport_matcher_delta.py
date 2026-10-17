"""Re-measure draft 2's transport predicate against the shipped one, over arm E's own population.

Draft 2 of ``incomplete_row_guard_check.py`` was overwritten in place by draft 3, so its reading
cannot be re-run from its own source. This instrument re-implements ONLY the predicate that differed
-- transport line = the first call whose attribute is a transport verb, receiver unchecked -- and
applies it to the same row population the shipped arm E iterates. Everything else, including the
site enumeration, is imported from the shipped probe, so the two readings differ by exactly one rule
and the loose reading gets a carrier instead of being retyped from memory.

Each flip is printed with the receiver expression the two rules matched, recovered from the AST of
the call node at the transport line. The receiver is what the two rules disagree about, so
it is stated in the parser's own terms. String constants inside a receiver are replaced by an
ellipsis
before printing: for these modules the loose rule's match hangs off an environment lookup, and
pasting that text would make this archive the next credential-scan finding, not the provider module.

Exits non-zero when any arm fails.

Arms A-D cover the ``extract_data`` sites, which is the population the shipped arm E judges. Arm E
here covers the same rows' ``transform_data`` sites, because that is where draft 2's four false
positives actually sat; the shipped probe's transport line is printed as ``no_transport_call@0``
there, which is what "this function never touches the client" looks like in this instrument.
"""

from __future__ import annotations

import ast
import copy
import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

PROBES = Path("docs/evidence/C128/plane1-residue-readiness-20261017/probes").resolve()
sys.path.insert(0, str(PROBES))

import incomplete_row_guard_check as probe  # noqa: E402  # the shipped wave-26 instrument

LANDED_ARM_E = {True: 3, False: 7}  # the face line shipped in runs/wave26-step0-second-axis.txt
SITE = dict[str, Any]
# (arm, row identity, transport line) -> "receiver.verb", filled while a wrapped rule runs
MATCHED: dict[tuple[str, str, int], str] = {}


def _loose_transport_line(func: ast.AST, _names: set[str]) -> int:
    """Draft 2's rule: verb match only, receiver unchecked."""
    lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in probe.TRANSPORT_ATTRS
    ]
    return min(lines) if lines else 0


def _redacted_text(node: ast.expr) -> str:
    """Unparse an expression with every string constant replaced.

    No literal from a provider module rides into this archive's carrier. Argument separators are
    compacted so a flip line stays one whitespace-separated token.
    """
    pristine = copy.deepcopy(node)
    for sub in ast.walk(pristine):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            sub.value = "..."
    return ast.unparse(pristine).replace(", ", ",")


def _attribute_call_at(func: ast.AST, lineno: int) -> str:
    """``receiver.verb`` for the attribute call the rule landed on, recovered from its AST node."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.lineno == lineno
        ):
            return f"{_redacted_text(node.func.value)}.{node.func.attr}"
    return "<unparsed>"


def _recording(
    rule: Callable[[ast.AST, set[str]], int], arm: str, row: str, func: ast.AST, names: set[str]
) -> int:
    """Run one rule with its own arguments intact, then remember which receiver it chose.

    ``names`` is the module's client-binding set that the shipped rule checks receivers against;
    dropping it would not be a differential, it would be a third rule.
    """
    lineno = rule(func, names)
    MATCHED[(arm, row, lineno)] = (
        _attribute_call_at(func, lineno) if lineno else "no_transport_call"
    )
    return lineno


def _collect(rule: Callable[[ast.AST, set[str]], int], arm: str, method: str) -> list[SITE]:
    """Swap the transport rule in for the duration of one walk over the shipped population."""
    original = probe._transport_line
    try:
        report = probe.seam._load_report()
        judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
        identities = sorted(i for i, s in judged.items() if s == "INCOMPLETE")
        sites: list[SITE] = []
        for identity in [*identities, *probe.CONTROLS_POSITIVE, probe.CONTROL_NEGATIVE]:
            probe._transport_line = partial(_recording, rule, arm, identity)
            measured = probe._row(identity, judged.get(identity, "?"))
            sites.extend(
                {
                    "identity": identity,
                    "where": str(site["where"]),
                    "transport_at": int(site["transport_at"]),
                    "after": bool(site["after"]),
                    "arm": arm,
                    "method": method,
                }
                for site in measured.get("doc_sites", [])
                if site["method"] == method
            )
        return sites
    finally:
        probe._transport_line = original


def _by_site(sites: list[SITE]) -> dict[tuple[str, str], SITE]:
    """Index by (identity, site) so the two arms join on the site itself, not on a line offset."""
    return {(str(row["identity"]), str(row["where"])): row for row in sites}


def _counts(rows: list[SITE]) -> dict[bool, int]:
    """True/False split of the ``after`` flag, the same shape arm E prints."""
    return {value: sum(1 for row in rows if row["after"] is value) for value in (True, False)}


def _matched(arm: str, row: str, lineno: int) -> str:
    return MATCHED.get((arm, row, lineno), "<absent>")


def _flip_lines(
    strict_by: dict[tuple[str, str], SITE],
    loose_by: dict[tuple[str, str], SITE],
    flips: list[tuple[str, str]],
    strict_arm: str,
    loose_arm: str,
) -> list[str]:
    """One line per flip, carrying both rules' matched receivers and the line each landed on."""
    out = []
    for identity, where in flips:
        row, other = strict_by[(identity, where)], loose_by[(identity, where)]
        strict_at, loose_at = int(row["transport_at"]), int(other["transport_at"])
        out.append(
            f"  flip {identity} {where} strict_after={row['after']} "
            f"strict_matched={_matched(strict_arm, identity, strict_at)}@{strict_at} "
            f"loose_after={other['after']} "
            f"loose_matched={_matched(loose_arm, identity, loose_at)}@{loose_at}"
        )
    return out


def main() -> int:
    """Print both readings over one population, and gate the contrast on being able to differ."""
    strict = _collect(probe._transport_line, "strict", "extract_data")
    loose = _collect(_loose_transport_line, "loose", "extract_data")
    strict_by, loose_by = _by_site(strict), _by_site(loose)

    sc, lc = _counts(strict), _counts(loose)
    reproduced = sc == LANDED_ARM_E and len(strict) == 10
    same_sites = set(strict_by) == set(loose_by)
    flips = sorted(key for key in strict_by if strict_by[key]["after"] != loose_by[key]["after"])
    superset_ok = all(loose_by[key]["after"] for key in strict_by if strict_by[key]["after"])

    print(
        f"A_arm_e_reproduced sites={len(strict)} after_counts={sc} "
        f"matches_landed_face={reproduced} PASS={reproduced and same_sites}"
    )
    print(
        f"B_loose_draft2_rule sites={len(loose)} after_counts={lc} "
        f"same_site_population={same_sites} PASS={same_sites}"
    )
    for line in _flip_lines(strict_by, loose_by, flips, "strict", "loose"):
        print(line)
    print(f"C_differential flipped={len(flips)} PASS={len(flips) > 0}")
    print(f"D_loose_is_superset_of_strict PASS={superset_ok}")

    td_strict = _collect(probe._transport_line, "strict_td", "transform_data")
    td_loose = _collect(_loose_transport_line, "loose_td", "transform_data")
    ts_by, tl_by = _by_site(td_strict), _by_site(td_loose)
    tsc, tlc = _counts(td_strict), _counts(td_loose)
    td_same = set(ts_by) == set(tl_by)
    tflips = sorted(key for key in ts_by if ts_by[key]["after"] != tl_by[key]["after"])
    print(
        f"E_transform_data_sites sites={len(td_strict)} strict_counts={tsc} loose_counts={tlc} "
        f"flipped={len(tflips)} same_site_population={td_same} PASS={td_same}"
    )
    for line in _flip_lines(ts_by, tl_by, tflips, "strict_td", "loose_td"):
        print(line)

    print(
        f"transport_matcher_delta strict={sc} loose={lc} net_flips={len(flips)} "
        f"extract_data_sites={len(strict)} transform_data_flips={len(tflips)}"
    )
    return 0 if (reproduced and same_sites and flips and superset_ok and td_same) else 1


if __name__ == "__main__":
    raise SystemExit(main())
