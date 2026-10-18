"""Declaration-and-guard counterfact for ``tmx::EquityHistorical``, measured by the shipped judge.

Wave 31 step 1 lands one row, and a landing is only a landing if the instrument that priced it
before the landing still refuses it after the landing is undone. Every arm here therefore calls
``incomplete_row_guard_check._row`` -- the same function that printed
``tmx::EquityHistorical ROW_CLASS_NOT_CONTRACT`` in the wave-30 baseline -- rather than a rebuilt
predicate, so the verdict ladder is the repo's own and not a paraphrase of it.

Four arms, each with the reading it must not collapse into:

A. ``A_declared`` -- the post-landing face: verdict, the per-method raise census, and the runtime
   contract identity ``contract_model('tmx_equity_historical') is row_model``.

B. ``B_declaration_removed`` -- ``row_model`` deleted from the class *in memory*, restorably, and
   the shipped row measured again. This is the pre-landing reading reproduced without editing the
   file, and it is what makes A's movement attributable: a landing that moved nothing would print
   the same verdict in both arms.

C. ``C_ruling_parity`` -- the new raise sits in ``transform_data`` of the history fetcher and
   carries ``TMX_BAD_ROWS``, the same code and the same method as the over-limit guard the
   ``CompanyFilings`` fetcher in this package already shipped. The claim being tested is
   "the guard applies a ruling this provider already makes", so the carrier prints both sites
   side by side; if the filings row had no such site, the parity sentence in the witness module
   would be unsupported.

D. ``D_ast_vs_runtime`` -- the two known judge artifacts, each against a landed control:
   ``_row_class_face`` reads construction off the AST with one helper expansion, so tmx's
   ``_row(model, ...)`` indirection yields ``emitted=[]``/``is_contract=False`` for a fetcher whose
   served rows *are* the contract class; and ``CLIENT_MODULE_SEGMENT = "_client"`` never matches
   this package's ``models/client.py``, so its post-transport raise sites read as if no governed
   client call precedes them. The controls are the two government_us rows the instrument already
   gates itself on, where both axes do light up.

Nothing in this file relaxes or replaces a judge: it only reproduces, and then un-does, the
measurement that made this row the cheapest genuinely-landable residue row in the batch.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[4]
GUARD_DIR = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"

for _p in (HERE, GUARD_DIR, REPO_ROOT / "scripts" / "quality"):
    sys.path.insert(0, str(_p))

import incomplete_row_guard_check as icg  # noqa: E402  # the shipped four-axis row judge
import seam_surface_match as seam  # noqa: E402  # the shipped judge-report reader

from opendata.data.domains import contract_model  # noqa: E402

IDENTITY: str = "tmx::EquityHistorical"
PARITY: str = "tmx::CompanyFilings"
CONTROLS: tuple[str, ...] = icg.CONTROLS_POSITIVE
TMX_PACKAGE_DIR: str = "opendata/data/providers/tmx/models"


def _digest(rel: str) -> str:
    """First 16 hex of the sha256 the pins use, for the file named by repo-relative path."""
    return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()[:16]


def _face(identity: str, judged: dict[str, str]) -> dict[str, Any]:
    """One shipped ``_row`` measurement, addressed by identity."""
    return dict(icg._row(identity, judged.get(identity, "?")))


def _contract_is_row(fetcher: object) -> str:
    """The runtime identity the AST arm cannot see, printed rather than asserted."""
    row_model = getattr(fetcher, "row_model", None)
    domain = getattr(getattr(fetcher, "capability", None), "domain", None)
    expected = contract_model(domain) if domain else None
    qual = getattr(row_model, "__qualname__", row_model)
    exp = getattr(expected, "__qualname__", expected)
    return (
        f"domain={domain} row_model={qual} contract_model={exp} runtime_is={row_model is expected}"
    )


def arm_declaration(judged: dict[str, str]) -> bool:
    """Arms A and B: the landed verdict, then the same verdict with the declaration removed."""
    before = _face(IDENTITY, judged)
    print(
        f"A_declared {IDENTITY} judge_status={before['status']} verdict={before['verdict']} "
        f"declared={before['declared']} is_contract_ast={before['is_contract']} "
        f"emitted_ast={before['emitted']} contract={before['contract']} "
        f"counts={before['counts']} trippable={before['trippable']} "
        f"raises={before['raises']} code_raises={before['code_raises']}"
    )
    for site in before["doc_sites"]:
        print(f"A_declared_site {IDENTITY} {site['where']} {site['method']} {site['code']}")

    fetcher, error = icg._fetcher(*IDENTITY.partition("::")[::2])
    if fetcher is None:
        print(f"B_declaration_removed {IDENTITY} UNBOUND resolve_error={error} PASS=False")
        return False
    print(f"B_runtime_identity {IDENTITY} {_contract_is_row(fetcher)}")

    saved = dict(vars(type(fetcher)))
    try:
        del type(fetcher).row_model
        stripped = _face(IDENTITY, judged)
    finally:
        for name, value in saved.items():
            setattr(type(fetcher), name, value)
    restored = _face(IDENTITY, judged)
    print(
        f"B_declaration_removed {IDENTITY} verdict={stripped['verdict']} "
        f"declared={stripped['declared']} is_contract_ast={stripped['is_contract']}"
    )
    print(f"B_restored {IDENTITY} verdict={restored['verdict']} declared={restored['declared']}")
    ok = (
        before["verdict"] == "DOCUMENT_REACHABLE"
        and stripped["verdict"] == "ROW_CLASS_NOT_CONTRACT"
        and restored["verdict"] == "DOCUMENT_REACHABLE"
    )
    print(
        f"B_MOVES {IDENTITY} declared={before['verdict']} stripped={stripped['verdict']} PASS={ok}"
    )
    return bool(ok)


def arm_parity(judged: dict[str, str]) -> bool:
    """Arm C: the new refusal shares code and method with the ruling already shipped in tmx."""
    hist = _face(IDENTITY, judged)
    files = _face(PARITY, judged)
    hist_codes = {(site["code"], site["method"]) for site in hist["doc_sites"]}
    file_codes = {(site["code"], site["method"]) for site in files["doc_sites"]}
    shared = hist_codes & file_codes
    for site in files["doc_sites"]:
        print(f"C_parity_site {PARITY} {site['where']} {site['method']} {site['code']}")
    ok = ("TMX_BAD_ROWS", "transform_data") in shared
    print(
        f"C_ruling_parity history_codes={sorted(hist_codes)} filings_codes={sorted(file_codes)} "
        f"shared={sorted(shared)} PASS={ok}"
    )
    return ok


def arm_artifacts(judged: dict[str, str]) -> bool:
    """Arm D: both artifacts printed beside a landed control that does not suffer them."""
    lines = [f"D_client_segment {icg.CLIENT_MODULE_SEGMENT!r}", f"D_tmx_files {TMX_PACKAGE_DIR}"]
    tmx = _face(IDENTITY, judged)
    lines.append(
        f"D_tmx_row_class {IDENTITY} emitted={tmx['emitted']} is_contract={tmx['is_contract']} "
        f"look_alike={tmx['look_alike']} client_names={tmx['client_names']} "
        f"runtime_is={_contract_is_row(icg._fetcher('tmx', 'EquityHistorical')[0])}"
    )
    lines.append(
        f"D_tmx_after {IDENTITY} doc_sites_after={[site['after'] for site in tmx['doc_sites']]}"
    )
    live = False
    for control in CONTROLS:
        face = _face(control, judged)
        after = [site["after"] for site in face["doc_sites"]]
        lines.append(
            f"D_control {control} verdict={face['verdict']} emitted={face['emitted']} "
            f"is_contract={face['is_contract']} client_names={face['client_names']} "
            f"doc_sites_after={after}"
        )
        if face["client_names"] and True in after:
            live = True
    print("\n".join(lines))
    print(
        f"D_readings tmx_ast_contract_false={tmx['is_contract'] is False} "
        f"tmx_client_names_empty={tmx['client_names'] == []} control_both_live={live} "
        "NOTE=both artifacts published, neither used to green a number"
    )
    return live


def pin_face() -> None:
    """Print the digests the tmx shard must now be re-pinned to, and the wave's own scratch root."""
    for rel in (
        "opendata/data/providers/tmx/models/fetchers.py",
        "scripts/quality/iter2_witnesses/tmx.py",
        "opendata/data/providers/tmx/models/client.py",
        "scripts/quality/iter2_claims_probe_runner.py",
    ):
        print(f"PIN sha256:16={_digest(rel)} file={rel}")
    print(f"SCRATCH_NOT_USED scratch_dir={tempfile.gettempdir()}")


def main() -> int:
    """Run the four arms and gate the carrier on the two that must differ."""
    report = seam._load_report()
    judged = {str(row["model_identity"]): str(row["status"]) for row in report["models"]}
    print(
        f"judge_report offline_acceptance={report['summary']['offline_acceptance_count']} "
        f"identity_set={report['inventory']['expected_identity_set_sha256'][:16]} "
        f"incomplete={sum(1 for s in judged.values() if s == 'INCOMPLETE')}"
    )
    pin_face()
    passed = [arm_declaration(judged), arm_parity(judged), arm_artifacts(judged)]
    print(f"COUNTERFACT_DONE arms=3 arms_passed={sum(1 for p in passed if p)}")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    sys.exit(main())
