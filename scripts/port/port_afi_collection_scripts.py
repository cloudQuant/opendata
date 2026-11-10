"""One-shot codemod: port the AFI collection-script tree into OpenData's B layer.

Copies AFI's ``app/data_fetch`` collection plane (the 1044 AkshareToMysql leaf
scripts, their ``common`` helpers, and the supporting ``core``/``configs``/
``providers``/``utils`` infrastructure) into
``opendata/data_fetch`` and rewrites every internal ``app.`` reference to the
OpenData package name.

The OpenData-native plane is preserved: its ``akshare_provider.py`` (the
``AkshareProvider`` base used by the A2 self-developed scheduler and tests),
``configs/__init__.py`` (DATA_DB_CONFIG), ``utils/retry.py`` and the package
``__init__`` re-exports are never overwritten. The AFI provider of the same
name lands as ``legacy_akshare_provider.py`` exposing
``LegacyAkshareProvider`` so both can coexist.

This is deliberately a mechanical, byte-faithful port: the destination is the
mypy-dark, ANN401-exempt B layer whose gate is ruff ``--select E,F`` (syntax and
undefined names only). The only post-rewrite requirement is therefore that no
``app.`` name remains unresolved.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AFI_ROOT = REPO_ROOT.parent / "ai-for-investor" / "src" / "backend" / "app"
AFI_DATA_FETCH = AFI_ROOT / "data_fetch"
OD_DATA_FETCH = REPO_ROOT / "opendata" / "data_fetch"

# OpenData-native files that must not be overwritten by the AFI tree.
PROTECTED_PATHS = {
    Path("__init__.py"),
    Path("providers/__init__.py"),
    Path("configs/__init__.py"),
    Path("utils/__init__.py"),
    Path("utils/retry.py"),
    Path("scripts/__init__.py"),
}

# AFI files that collide with an OpenData-native path are copied under a new
# name instead of overwriting the native file. The native AkshareProvider base
# and the native stock_zh_a_hist reference script stay in place; the AFI
# production versions land alongside them with distinct names.
RENAMES = {
    Path("providers/akshare_provider.py"): Path("providers/legacy_akshare_provider.py"),
    Path("scripts/stocks/daily/stock_zh_a_hist.py"): Path(
        "scripts/stocks/daily/stock_zh_a_hist_legacy.py"
    ),
}

# Token-level rewrites applied only inside renamed files (keyed by destination).
CLASS_RENAMES = {
    Path("providers/legacy_akshare_provider.py"): (r"\bAkshareProvider\b", "LegacyAkshareProvider"),
}

# Static import/string rewrites. Order matters: the longest, most specific
# roots are rewritten first so a prefix rule never partially rewrites them.
IMPORT_REWRITES: tuple[tuple[str, str], ...] = (
    ("app.data_fetch", "opendata.data_fetch"),
    ("app.utils.akshare_catalog_utils", "opendata.data_fetch.utils.akshare_catalog_utils"),
    ("app.utils.logger", "opendata.data_fetch.utils.logger"),
    ("from app.config import", "from opendata.core.config import"),
    ("app.config", "opendata.core.config"),
    ("from app import", "from opendata import"),
)


def rewrite_text(text: str) -> str:
    """Rewrite every internal AFI reference in one source string."""
    for old, new in IMPORT_REWRITES:
        text = text.replace(old, new)
    return text


def _destination(relative: Path) -> Path:
    """Map an AFI-relative path to its OpenData destination, applying renames."""
    return RENAMES.get(relative, relative)


def port_tree(*, dry_run: bool) -> dict[str, int]:
    """Copy and rewrite the AFI data_fetch tree; return a small count report."""
    if not AFI_DATA_FETCH.is_dir():
        raise SystemExit(f"AFI source tree not found: {AFI_DATA_FETCH}")

    copied = 0
    skipped = 0
    rewritten = 0
    for source in sorted(AFI_DATA_FETCH.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        relative = source.relative_to(AFI_DATA_FETCH)
        if relative in PROTECTED_PATHS:
            skipped += 1
            continue
        destination = OD_DATA_FETCH / _destination(relative)
        text = source.read_text(encoding="utf-8")
        new_text = rewrite_text(text)
        class_rename = CLASS_RENAMES.get(_destination(relative))
        if class_rename is not None:
            pattern, replacement = class_rename
            new_text = re.sub(pattern, replacement, new_text)
        if new_text != text:
            rewritten += 1
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(new_text, encoding="utf-8")
        copied += 1

    return {"copied": copied, "skipped_protected": skipped, "rewritten": rewritten}


def main(argv: list[str] | None = None) -> int:
    """CLI: dry-run by default, ``--apply`` writes the ported tree."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write files instead of reporting")
    args = parser.parse_args(argv)
    report = port_tree(dry_run=not args.apply)
    mode = "apply" if args.apply else "dry_run"
    print(f"{mode}: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
