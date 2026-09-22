"""JS execution point registry guard (A2.6).

The ported tree executes JavaScript through ``py_mini_racer`` and
evaluates remote page text with builtin ``eval`` in a handful of
sina interfaces. That surface is registered per file in
``docs/quality/js-execution-points.json``; this test fails when a
re-sync adds or removes a site without a reviewed registry update
(same fail-closed philosophy as the zero-dependency baseline).
"""

import json

from scripts.quality import scan_js_points

REGISTRY_PATH = scan_js_points.REGISTRY_PATH


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_registry_exists_and_is_versioned():
    registry = _registry()

    assert registry["version"] == scan_js_points.REGISTRY_VERSION
    assert registry["engine"] == "py_mini_racer"


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
