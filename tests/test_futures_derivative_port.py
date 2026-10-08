"""Provenance and offline-import checks for the futures derivative port."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_PACKAGE = "opendata.data.providers.akshare._vendor"
VENDOR_ROOT = REPO_ROOT / "opendata" / "data" / "providers" / "akshare" / "_vendor"
PACKAGE_DIR = VENDOR_ROOT / "futures_derivative"
UPSTREAM_COMMIT = "c4f6a631c259783dbc2507b6b27d179b3e88079d"
EXPECTED_MODULES = {
    "__init__.py",
    "cons.py",
    "futures_contract_info_cffex.py",
    "futures_contract_info_czce.py",
    "futures_contract_info_dce.py",
    "futures_contract_info_gfex.py",
    "futures_contract_info_ine.py",
    "futures_contract_info_shfe.py",
    "futures_cot_sina.py",
    "futures_hog.py",
    "futures_index_sina.py",
    "futures_spot_sys.py",
}
EXPECTED_EXPORTS = {
    "futures_contract_info_cffex": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_cffex",
        "futures_contract_info_cffex",
    ),
    "futures_contract_info_czce": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_czce",
        "futures_contract_info_czce",
    ),
    "futures_contract_info_dce": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_dce",
        "futures_contract_info_dce",
    ),
    "futures_contract_info_gfex": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_gfex",
        "futures_contract_info_gfex",
    ),
    "futures_contract_info_ine": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_ine",
        "futures_contract_info_ine",
    ),
    "futures_contract_info_shfe": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_contract_info_shfe",
        "futures_contract_info_shfe",
    ),
    "futures_hog_core": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_hog",
        "futures_hog_core",
    ),
    "futures_hog_cost": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_hog",
        "futures_hog_cost",
    ),
    "futures_hog_supply": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_hog",
        "futures_hog_supply",
    ),
    "futures_main_sina": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_index_sina",
        "futures_main_sina",
    ),
    "futures_display_main_sina": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_index_sina",
        "futures_display_main_sina",
    ),
    "futures_hold_pos_sina": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_cot_sina",
        "futures_hold_pos_sina",
    ),
    "futures_spot_sys": (
        f"{VENDOR_PACKAGE}.futures_derivative.futures_spot_sys",
        "futures_spot_sys",
    ),
}


def test_futures_derivative_files_are_locked_to_one_upstream_revision() -> None:
    lock = json.loads((VENDOR_ROOT / "upstream.lock").read_text())
    assert lock["upstream"]["commit"] == UPSTREAM_COMMIT

    records = {entry["path"]: entry for entry in lock["files"]}
    expected_records = {
        f"futures_derivative/{name}": f"akshare/futures_derivative/{name}"
        for name in EXPECTED_MODULES
    }
    actual_records = {path for path in records if path.startswith("futures_derivative/")}
    assert actual_records == set(expected_records)
    for ported_path, upstream_path in expected_records.items():
        record = records[ported_path]
        assert record["upstream_path"] == upstream_path
        assert record["manual_edits"] is False
        assert len(record["sha256"]) == 64

        lines = (PACKAGE_DIR / Path(ported_path).name).read_text().splitlines()[:6]
        assert any(
            line == "# Ported from akshare (https://github.com/cloudQuant/akshare.git)"
            f" @ {UPSTREAM_COMMIT}"
            for line in lines
        )
        assert any("MIT License (see THIRD_PARTY_NOTICES.md)" in line for line in lines)

    actual_paths = {path.name for path in PACKAGE_DIR.glob("*.py") if path.is_file()}
    assert actual_paths == EXPECTED_MODULES


def test_root_exports_import_offline_without_loading_akshare() -> None:
    code = f"""
import importlib
import inspect
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
vendor = importlib.import_module({VENDOR_PACKAGE!r})
expected = {EXPECTED_EXPORTS!r}
missing = [name for name in expected if not callable(getattr(vendor, name, None))]
assert not missing, f'missing root exports: {{missing}}'
for name, (module_name, symbol_name) in expected.items():
    exported = getattr(vendor, name)
    implementation = getattr(importlib.import_module(module_name), symbol_name)
    assert exported is implementation, f'{{name}} is not the original function object'
    assert inspect.signature(exported) == inspect.signature(implementation)
version_module = importlib.import_module({(VENDOR_PACKAGE + '._version')!r})
assert vendor.__version__ is version_module.__version__
assert vendor.__version__ == '1.18.64'
try:
    getattr(vendor, '__not_a_real_akshare_export__')
except AttributeError:
    pass
else:
    raise AssertionError('unknown facade exports must be rejected')
assert not any(name == 'akshare' or name.startswith('akshare.') for name in sys.modules)
"""
    result = subprocess.run(  # noqa: S603  # literal isolated-import probe payload
        [sys.executable, "-I", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
