"""Merge per-provider Iteration-2 claims shards into the single judged document.

The evidence judge reads one claims file against the pinned 350-identity inventory, while the
producer and the recorder work per provider so parallel waves never write the same file. This
tool is only that join: claims arrays and ``primary_reviews`` arrays concatenate, and the
document-level fields come from the judge itself.

It refuses rather than smooths over disagreement: two shards carrying a different pinned
identity set, or two claims for the same ``provider/model/phase`` that are not byte-identical,
or a row reviewed twice with different decisions are all merge-time conflicts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality import iter2_model_evidence as judge  # noqa: E402

DEFAULT_SHARDS: Final = "docs/evidence/C101/*/claims.reviewed.json"


def _read(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"refusing: {path} is not a JSON object")
    return document


def merge(documents: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """Join shard documents into one judged document, refusing silent conflicts."""
    if not documents:
        raise SystemExit("refusing: no shard documents to merge")
    identity_sets = {doc.get("identity_set_sha256") for _, doc in documents}
    if len(identity_sets) != 1:
        raise SystemExit(f"refusing: shards disagree on the pinned identity set: {identity_sets}")
    identity_set = identity_sets.pop()

    merged: dict[str, Any] = {
        "schema_version": judge.SCHEMA_VERSION,
        "identity_set_sha256": identity_set,
        "generated_by": "scripts/quality/iter2_claims_merge.py",
        "notes": (
            "per-provider shards joined here; every claim was produced by running its witness "
            "subprocess and every review by re-verifying it against the files on disk"
        ),
        "claims": [],
        "primary_reviews": [],
    }

    seen_claims: dict[tuple[str, str, str], tuple[str, Path]] = {}
    for path, document in documents:
        for claim in document.get("claims") or []:
            key = (claim["provider"], claim["upstream_model"], claim["phase"])
            digest = judge.sha256_bytes(
                json.dumps(claim, sort_keys=True, ensure_ascii=False).encode("utf-8")
            )
            if key in seen_claims:
                earlier_digest, earlier_path = seen_claims[key]
                if earlier_digest != digest:
                    raise SystemExit(
                        f"refusing: {key} differs between {earlier_path} and {path}; "
                        "one identity one phase must have one claim"
                    )
                continue
            seen_claims[key] = (digest, path)
            merged["claims"].append(claim)

    reviews: dict[tuple[str, str], tuple[str, Path]] = {}
    for path, document in documents:
        for review in document.get("primary_reviews") or []:
            key = (review["provider"], review["upstream_model"])
            digest = judge.sha256_bytes(
                json.dumps(review, sort_keys=True, ensure_ascii=False).encode("utf-8")
            )
            if key in reviews:
                earlier_digest, earlier_path = reviews[key]
                if earlier_digest != digest:
                    raise SystemExit(
                        f"refusing: {key} carries two different reviews ({earlier_path} and {path})"
                    )
                continue
            reviews[key] = (digest, path)
            merged["primary_reviews"].append(review)

    return merged


def main(argv: list[str] | None = None) -> int:
    """Resolve the shard glob, merge it, and print the joined denominators."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", default=DEFAULT_SHARDS)
    parser.add_argument("--output", default="docs/evidence/C101/claims.merged.json")
    args = parser.parse_args(argv)

    shard_paths = sorted(p for p in REPO_ROOT.glob(args.shards) if p.is_file())
    if not shard_paths:
        print(f"refusing: no shard matched {args.shards}", file=sys.stderr)
        return 2
    documents = [(p, _read(p)) for p in shard_paths]
    merged = merge(documents)

    output_path = (REPO_ROOT / args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    identities = {(c["provider"], c["upstream_model"]) for c in merged["claims"]}
    accepted = {
        (r["provider"], r["upstream_model"])
        for r in merged["primary_reviews"]
        if r.get("decision") == "ACCEPT"
    }
    print(f"shards merged={len(shard_paths)}")
    for path in shard_paths:
        print(f"  shard {path.relative_to(REPO_ROOT)}")
    print(
        f"claims={len(merged['claims'])} identities={len(identities)} "
        f"reviews={len(merged['primary_reviews'])} accepted_identities={len(accepted)}"
    )
    print(f"wrote {output_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
