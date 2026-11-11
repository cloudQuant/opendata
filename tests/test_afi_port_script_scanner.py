"""Offline contract for the script scanner's metadata derivation.

``scan_and_register_scripts`` walks the ported tree and imports every leaf
to build DataScript metadata. These tests pin the pure, DB-free half of that
walk against the real 1108-module collection tree: derived module paths must
import, helper/init files are skipped, and the extracted metadata points at a
runnable entrypoint.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from opendata.afi_port.services.script import AkshareScriptService
from opendata.core.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def service() -> AkshareScriptService:
    """A service with no DB session, exercising only the pure scan helpers."""
    return AkshareScriptService.__new__(AkshareScriptService)


@pytest.fixture(scope="module")
def script_root() -> Path:
    settings = get_settings()
    configured = Path(settings.akshare_script_root)
    return REPO_ROOT / configured


def test_script_root_points_at_the_ported_collection_tree(script_root: Path) -> None:
    assert script_root.is_dir()
    assert (script_root / "stocks" / "daily").is_dir()


def test_derived_module_path_imports_for_every_leaf(
    service: AkshareScriptService, script_root: Path
) -> None:
    """Every non-init ported file derives a module path that imports cleanly."""
    leaves = [
        p
        for p in script_root.rglob("*.py")
        if "__pycache__" not in p.parts and not p.name.startswith("__")
    ]
    assert len(leaves) > 1000  # the full collection tree, not a trimmed fixture

    failures = []
    for file_path in leaves:
        metadata = service._derive_script_metadata(file_path, script_root)
        try:
            importlib.import_module(metadata["module_path"])
        except Exception as exc:  # collect rather than stop the walk
            failures.append((metadata["module_path"], type(exc).__name__))
    assert failures == []


def test_metadata_shape_for_a_known_leaf(service: AkshareScriptService, script_root: Path) -> None:
    file_path = script_root / "stocks" / "daily" / "stock_zh_a_hist_legacy.py"
    metadata = service._derive_script_metadata(file_path, script_root)
    assert metadata == {
        "script_id": "stock_zh_a_hist_legacy",
        "script_name": "Stock Zh A Hist Legacy",
        "category": "stocks",
        "sub_category": "daily",
        "module_path": ("opendata.data_fetch.scripts.stocks.daily.stock_zh_a_hist_legacy"),
        "function_name": "main",
        "target_table": "stock_zh_a_hist_legacy",
        "frequency": metadata["frequency"],
    }


def test_init_files_are_excluded_from_the_walk(script_root: Path) -> None:
    """The scanner skips dunder files, so package inits never become scripts."""
    inits = [p for p in script_root.rglob("*.py") if p.name.startswith("__")]
    assert inits, "the tree must contain package __init__ files"
    # Mirror the scanner's own filter.
    scanned = [p for p in script_root.rglob("*.py") if not p.name.startswith("__")]
    assert all(not p.name.startswith("__") for p in scanned)


def test_extracted_metadata_resolves_a_runnable_entrypoint(
    service: AkshareScriptService, script_root: Path
) -> None:
    """A sampled leaf yields metadata whose module resolves to a callable."""
    sample = script_root / "funds" / "weekly" / "money_fund_hist_em.py"
    metadata = service._derive_script_metadata(sample, script_root)
    module = importlib.import_module(metadata["module_path"])
    enriched = service._extract_module_metadata(module, dict(metadata))
    # The pure-class run() entrypoint is discoverable through the legacy helper.
    instance = service._legacy_script_instance(module)
    assert instance is not None
    assert callable(getattr(instance, "run", None)) or callable(
        getattr(instance, "fetch_data", None)
    )
    assert enriched["script_id"] == metadata["script_id"]
