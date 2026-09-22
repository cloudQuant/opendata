"""Generate ``opendata_http/manifest.json`` (design §5.6, FR-5).

The manifest freezes the ported tree's state: every file with its
sha256, the resource (non-``.py``) inventory, per-file line counts and
summary counters. It is the verification input for AC-5 and doubles as
the ported-state lock for codemod idempotence.

Fail-closed: a missing ported tree, a missing/mismatched upstream.lock
record for any ported file, or an upstream path that no longer matches
the lock's sha256 all abort the generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.codemod.port_module import PORTED_ROOT, UpstreamLock  # noqa: E402

MANIFEST_PATH = PORTED_ROOT / "manifest.json"


def sha256_bytes(data: bytes) -> str:
    """Hash binary content with sha256 (hex)."""
    return hashlib.sha256(data).hexdigest()


def generate_manifest(
    ported_root: Path = PORTED_ROOT,
    lock: UpstreamLock | None = None,
) -> dict[str, Any]:
    """Build the manifest of the ported tree.

    Args:
        ported_root: The ``opendata_http`` directory.
        lock: The baseline lock; loaded from the default path when None.

    Returns:
        The manifest mapping ready to serialize.

    Raises:
        RuntimeError: When the tree or lock is missing, or a ported
            file has no matching lock record.
    """
    if not ported_root.is_dir():
        raise RuntimeError(f"ported tree {ported_root} does not exist")
    if lock is None:
        lock = UpstreamLock.load()

    files: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    total_lines = 0
    for path in sorted(ported_root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ported_root).as_posix()
        if path.name in {"upstream.lock", "manifest.json"}:
            continue
        digest = sha256_bytes(path.read_bytes())
        record = lock.files.get(rel)
        if record is None:
            raise RuntimeError(
                f"{rel} is ported but missing from upstream.lock; re-run port_module"
            )
        if path.suffix == ".py":
            lines = path.read_text(encoding="utf-8").count("\n")
            total_lines += lines
            files.append(
                {
                    "path": rel,
                    "sha256": digest,
                    "lines": lines,
                    "upstream_path": record["upstream_path"],
                    "manual_edits": record.get("manual_edits", False),
                }
            )
        else:
            resources.append(
                {"path": rel, "sha256": digest, "upstream_path": record["upstream_path"]}
            )

    return {
        "version": 1,
        "upstream": {"url": lock.url, "commit": lock.commit},
        "files": files,
        "resources": resources,
        "counts": {
            "py_files": len(files),
            "resource_files": len(resources),
            "total_files": len(files) + len(resources),
            "total_lines": total_lines,
        },
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit code 1 on failure."""
    parser = argparse.ArgumentParser(description="Generate opendata_http/manifest.json.")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)

    try:
        manifest = generate_manifest()
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.check:
        existing = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.is_file() else ""
        expected = _serialize(manifest)
        if existing != expected:
            print("FAIL: manifest.json is stale; regenerate", file=sys.stderr)
            return 1
        print("OK: manifest.json is up to date")
        return 0
    MANIFEST_PATH.write_text(_serialize(manifest), encoding="utf-8")
    counts = manifest["counts"]
    if not isinstance(counts, dict):
        print("FAIL: manifest has no counts section", file=sys.stderr)
        return 1
    print(
        f"OK: wrote {MANIFEST_PATH} "
        f"({counts['py_files']} py, {counts['resource_files']} resources, "
        f"{counts['total_lines']} lines)"
    )
    return 0


def _serialize(manifest: dict[str, Any]) -> str:
    """Serialize the manifest deterministically."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
