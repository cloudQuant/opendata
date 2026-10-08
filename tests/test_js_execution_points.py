"""JS execution point registry guard (A2.6).

The ported tree executes JavaScript through ``py_mini_racer`` and
evaluates remote page text with builtin ``eval`` in a handful of
sina interfaces. That surface is registered per file in
``docs/quality/js-execution-points.json``; this test fails when a
re-sync adds or removes a site without a reviewed registry update
(same fail-closed philosophy as the zero-dependency baseline).
"""

import json
from pathlib import Path

import pytest

from scripts.quality import scan_js_points
from scripts.quality.source_layout import SourceLayoutError

REGISTRY_PATH = scan_js_points.REGISTRY_PATH


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_registry_exists_and_is_versioned():
    registry = _registry()

    assert registry["version"] == scan_js_points.REGISTRY_VERSION
    assert registry["engine"] == "py_mini_racer"
    assert registry["js_payload_source"] == (
        "opendata/data/providers/akshare/_vendor/stock/cons.py:hk_js_decode (hardcoded literal)"
    )


def test_engine_sites_match_registry():
    registry = _registry()

    assert scan_js_points.scan()["engine_sites"] == registry["engine_sites"], (
        "JS engine sites drifted; review the new/changed MiniRacer() sites, "
        "then run: python scripts/quality/scan_js_points.py --update"
    )


def test_builtin_eval_sites_match_registry():
    registry = _registry()

    assert scan_js_points.scan()["builtin_eval_sites"] == registry["builtin_eval_sites"], (
        "builtin eval sites drifted; review the remote-text parsing, "
        "then run: python scripts/quality/scan_js_points.py --update"
    )


def test_no_builtin_exec_sites():
    assert scan_js_points.scan()["builtin_exec_sites"] == {}


def test_check_command_passes_on_synced_registry():
    assert scan_js_points.check() == 0


def test_totals_match_site_maps():
    current = scan_js_points.scan()
    totals = current["totals"]

    assert totals["engine_sites"] == sum(current["engine_sites"].values())
    assert totals["builtin_eval_sites"] == sum(current["builtin_eval_sites"].values())


def test_vendor_scan_has_expected_complete_inventory():
    current = scan_js_points.scan()

    assert current["totals"] == {
        "engine_sites": 76,
        "builtin_eval_sites": 35,
        "builtin_exec_sites": 0,
        "files": 42,
    }


def test_scan_uses_exact_vendor_scope_and_deduplicates_overlapping_roots(
    tmp_path: Path,
):
    vendor = tmp_path / "opendata" / "data" / "providers" / "akshare" / "_vendor"
    vendor.mkdir(parents=True)
    (vendor / "sample.py").write_text("MiniRacer()\neval(text)\n", encoding="utf-8")

    adapter = vendor.parent / "adapter.py"
    adapter.write_text("MiniRacer()\n", encoding="utf-8")
    lookalike = vendor.parent / "_vendorish" / "sample.py"
    lookalike.parent.mkdir()
    lookalike.write_text("eval(text)\n", encoding="utf-8")

    current = scan_js_points.scan(
        root=tmp_path,
        roots=("opendata", "opendata/data/providers/akshare/_vendor"),
    )

    assert current["engine_sites"] == {"opendata/data/providers/akshare/_vendor/sample.py": 1}
    assert current["builtin_eval_sites"] == {"opendata/data/providers/akshare/_vendor/sample.py": 1}
    assert current["totals"] == {
        "engine_sites": 1,
        "builtin_eval_sites": 1,
        "builtin_exec_sites": 0,
        "files": 1,
    }


@pytest.mark.parametrize(
    "roots",
    [
        ("opendata/data/providers/akshare/_vendor",),
        ("missing/vendor",),
    ],
)
def test_scan_fails_closed_for_empty_or_missing_vendor_scope(
    tmp_path: Path, roots: tuple[str, ...]
):
    if roots[0] == "opendata/data/providers/akshare/_vendor":
        (tmp_path / roots[0]).mkdir(parents=True)

    with pytest.raises(SourceLayoutError):
        scan_js_points.scan(root=tmp_path, roots=roots)
