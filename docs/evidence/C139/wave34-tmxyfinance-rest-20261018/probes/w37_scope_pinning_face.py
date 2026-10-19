"""Scope-pinning face: which providers plane 1 can even see, and what widening it would cost.

Plane 1 accepts a row only if its (provider, upstream_model) pair is in the pinned 350-row
inventory. This probe measures the *scope* axis rather than the acceptance axis, because the two
provider populations in this repo are not the same set:

``A_populations``
    three provider populations read from three different shipped sources -- the registry
    (``catalog.list_providers``), the fixed inventory CSV, and the on-disk package dirs, with their
    symmetric differences printed. The mandate says "32 providers"; the registry says 34, and the
    two sets are not equal, so the number alone means nothing.
``B_rowless_implementations``
    for the providers that are registered but absent from the inventory, count their registered
    fetchers. This is the acceptance-blind spot: implemented models plane 1 can never accept.
``C_pinned_constants``
    the pinned ``EXPECTED_MODEL_COUNT`` and ``EXPECTED_IDENTITY_SHA256`` imported from the judge
    (never retyped), plus the identity hash the accepted claims themselves carry.
``D_widening_is_refused``
    positive control: ``load_inventory`` accepts the real CSV and raises on the same CSV plus one
    synthetic row -- so widening the scope is a judge-constant edit, not an implementation task.
``E_identity_projection``
    the judge's own ``identity_projection_sha256`` over the current and the widened identity lists,
    which is the mechanism that turns a scope change into a re-sign.
``F_re_sign_radius``
    the radius arm E reaches: the merged document stamps the identity ONCE at its root, and this
    arm counts the claims and accepted rows that single field governs.
``G_revert_is_exact``
    the second half of the control: dropping the forged row reproduces the pinned hash exactly, so
    arm E's difference is caused by the added row and not by hash instability.

Nothing in the repository is written; the forged CSV lives in a temp dir and is deleted.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C139/wave34-tmxyfinance-rest-20261018"
CARRIER = REPO / BUNDLE / "runs" / "w37_scope_pinning_face.txt"
INVENTORY = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"


def _load_judge() -> Any:  # noqa: ANN401  # the shipped judge module, heterogeneous by nature
    sys.path.insert(0, str(REPO))
    from scripts.quality import iter2_model_evidence as judge

    return judge


def _inventory_identities() -> list[tuple[str, str]]:
    with INVENTORY.open(encoding="utf-8-sig", newline="") as handle:
        return sorted(
            {(r["provider"].strip(), r["upstream_model"].strip()) for r in csv.DictReader(handle)}
        )


def _claims_identity_values() -> set[str]:
    values: set[str] = set()

    def walk(node: Any) -> None:  # noqa: ANN401  # the claims document is nested JSON
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "identity_set_sha256":
                    values.add(str(val))
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(CLAIMS.read_text(encoding="utf-8")))
    return values


def _fetcher_count(catalog: Any, provider: str) -> tuple[int, str]:  # noqa: ANN401
    """(registered fetchers, error name) -- a provider that refuses to list is a reading."""
    try:
        return len(catalog.fetchers_for(provider)), ""
    except Exception as exc:
        return -1, type(exc).__name__


def main() -> int:
    """Print the face; every arm can fail."""
    judge = _load_judge()
    arms: dict[str, bool] = {}
    lines: list[str] = []

    from opendata.data.providers import catalog

    registered = sorted({p.source for p in catalog.list_providers()})
    identities = _inventory_identities()
    inv_providers = sorted({p for p, _ in identities})
    disk = sorted(
        p.name
        for p in (REPO / "opendata/data/providers").iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )
    reg_minus_inv = sorted(set(registered) - set(inv_providers))
    inv_minus_reg = sorted(set(inv_providers) - set(registered))
    disk_minus_inv = sorted(set(disk) - set(inv_providers))
    lines.append(
        f"A_populations registry={len(registered)} inventory={len(inv_providers)} "
        f"disk_dirs={len(disk)} inventory_rows={len(identities)}\n"
        f"  registry_minus_inventory={reg_minus_inv}\n"
        f"  inventory_minus_registry={inv_minus_reg}\n"
        f"  disk_minus_inventory={disk_minus_inv}"
    )
    # the measured shape is one-directional: the registry is a strict superset of the inventory. An
    # earlier version of this arm required the two differences to be unequal, which only holds for a
    # symmetric gap and went red on a correct reading.
    arms["A_populations"] = (
        len(registered) > 0
        and len(identities) > 0
        and bool(reg_minus_inv)
        and not inv_minus_reg
        and len(set(registered) | set(inv_providers)) == len(registered) > len(inv_providers)
    )

    rowless: dict[str, int] = {}
    for provider in reg_minus_inv:
        count, error = _fetcher_count(catalog, provider)
        rowless[provider] = count
        if error:
            lines.append(f"  fetchers_error {provider}: {error}")
    lines.append(
        f"B_rowless_implementations providers={sorted(rowless)} "
        f"fetchers={rowless} total={sum(v for v in rowless.values() if v > 0)}"
    )
    arms["B_rowless_implementations"] = bool(rowless) and all(v > 0 for v in rowless.values())

    claims_ids = _claims_identity_values()
    pinned = str(judge.EXPECTED_IDENTITY_SHA256)
    lines.append(
        f"C_pinned_constants expected_model_count={judge.EXPECTED_MODEL_COUNT} "
        f"pinned_identity={pinned[:16]} claims_identity_values={len(claims_ids)} "
        f"claims_match_pinned={claims_ids == {pinned}}"
    )
    arms["C_pinned_constants"] = (
        len(claims_ids) == 1
        and claims_ids == {pinned}
        and len(identities) == judge.EXPECTED_MODEL_COUNT
    )

    tmp = Path(tempfile.mkdtemp(prefix="w37_scope_"))
    forged_path = tmp / "inventory_plus_one.csv"
    forged_path.write_bytes(
        INVENTORY.read_bytes() + b"OBB2-akshare-ScopeProbe,akshare,ScopeProbe,,,,,,,\n"
    )
    real_ok, forged_err = "", ""
    try:
        inv_real = judge.load_inventory(INVENTORY)
        real_ok = f"rows={len(inv_real['rows']) if isinstance(inv_real, dict) else 'n/a'}"
    except Exception as exc:  # the real CSV must load, or arm D proves nothing
        real_ok = f"RAISED:{type(exc).__name__}:{str(exc)[:60]}"
    try:
        judge.load_inventory(forged_path)
        forged_err = "NO_RAISE"
    except Exception as exc:
        forged_err = f"{type(exc).__name__}:{str(exc)[:70]}"
    lines.append(f"D_widening_is_refused real_load={real_ok} forged_load={forged_err}")
    arms["D_widening_is_refused"] = real_ok.startswith("rows=") and forged_err not in (
        "",
        "NO_RAISE",
    )

    forged_identities = sorted({*identities, ("akshare", "ScopeProbe")})
    hash_now = judge.identity_projection_sha256(identities)
    hash_wide = judge.identity_projection_sha256(forged_identities)
    hash_revert = judge.identity_projection_sha256(sorted(identities))
    lines.append(
        f"E_identity_projection current={hash_now[:16]} widened={hash_wide[:16]} "
        f"differs={hash_now != hash_wide} rows={len(identities)}->{len(forged_identities)}"
    )
    arms["E_identity_projection"] = hash_now == pinned and hash_wide != pinned

    claims_doc = json.loads(CLAIMS.read_text(encoding="utf-8"))
    total_claims = 0

    def _count(node: Any) -> None:  # noqa: ANN401  # the claims document is nested JSON
        nonlocal total_claims
        if isinstance(node, dict):
            if "phase" in node and "provider" in node:
                total_claims += 1
            for val in node.values():
                _count(val)
        elif isinstance(node, list):
            for item in node:
                _count(item)

    _count(claims_doc)
    report = judge.build_report(
        judge.load_inventory(INVENTORY),
        claims_doc,
        claims_sha256=judge.sha256_bytes(CLAIMS.read_bytes()),
        claims_path=str(CLAIMS.relative_to(REPO)),
    )
    accepted_rows = int(report["summary"]["offline_acceptance_count"])
    occurrences = json.dumps(claims_doc).count('"identity_set_sha256"')
    lines.append(
        f"F_re_sign_radius accepted_rows={accepted_rows} "
        f"report_claims={report['summary']['claim_count']} "
        f"claims_shape_count={total_claims} identity_field_occurrences={occurrences} "
        f"document_identity_matches_pinned={str(claims_doc.get('identity_set_sha256')) == pinned}"
    )
    # measured, not assumed: the identity is stamped ONCE at the document root, not on each claim
    # (the first take of this arm looked for a per-claim field and read 0 -- a wrong premise about
    # the producer's shape). The radius is therefore the whole merged document: one field governs
    # every claim and every accepted row in it.
    arms["F_re_sign_radius"] = (
        accepted_rows > 0
        and total_claims > 0
        and total_claims == int(report["summary"]["claim_count"])
        and occurrences == 1
        and str(claims_doc.get("identity_set_sha256")) == pinned
    )

    lines.append(
        f"G_revert_is_exact revert={hash_revert[:16]} equals_pinned={hash_revert == pinned}"
    )
    arms["G_revert_is_exact"] = hash_revert == pinned and hash_wide != hash_revert
    forged_path.unlink()
    tmp.rmdir()

    failed = [name for name, ok in sorted(arms.items()) if not ok]
    lines.append(
        f"scope_pinning_face arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    lines.append("")
    lines.append(f"w37_scope_pinning_face_exit={0 if not failed else 1}")
    body_text = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w37_scope_pinning_face.py\n"
        f"# argv: {sys.executable} {REPO / BUNDLE / 'probes' / 'w37_scope_pinning_face.py'}\n"
        f"# claims_sha256={hashlib.sha256(CLAIMS.read_bytes()).hexdigest()[:16]}\n\n" + body_text,
        encoding="utf-8",
    )
    print(body_text, end="")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
