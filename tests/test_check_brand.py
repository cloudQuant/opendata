from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.quality import check_brand

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


TRANSFER_PATHS = (
    "scripts/ops/migrate_legacy_stock_daily.py",
    "tests/test_legacy_stock_daily_transfer.py",
)
INVENTORY_PATHS = (
    "opendata/runtime_inventory.py",
    "tests/test_runtime_inventory.py",
    "docs/afi204-runtime-inventory.json",
    "docs/afi204-migration-catalog.json",
)


def _write_fixture(root: Path, relative_path: str, contents: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _brand_token(suffix: str) -> str:
    return "_".join(("akshare", suffix))


def test_legacy_schema_token_is_allowed_only_at_exact_transfer_and_inventory_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_token = _brand_token("data")
    unrelated_path = "tests/fixtures/unrelated.py"
    monkeypatch.setattr(check_brand, "REPO_ROOT", tmp_path)

    for relative_path in (*TRANSFER_PATHS, *INVENTORY_PATHS):
        _write_fixture(tmp_path, relative_path, schema_token)
    _write_fixture(tmp_path, unrelated_path, schema_token)

    expected_allowlist = {
        schema_token: frozenset((*TRANSFER_PATHS, *INVENTORY_PATHS)),
        _brand_token("network"): frozenset({"docs/afi204-migration-catalog.json"}),
    }
    assert expected_allowlist == check_brand.TOKEN_PATH_ALLOWLIST
    assert not {*TRANSFER_PATHS, *INVENTORY_PATHS}.intersection(check_brand.WHITELISTED_FILES)
    assert [hit.file for hit in check_brand.scan_token(schema_token)] == [unrelated_path]


def test_transfer_paths_still_reject_other_brands_and_app_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_token = _brand_token("data")
    other_tokens = (_brand_token("mysql"), _brand_token("web"))
    app_references = (
        "from " + "app" + ".models import User",
        "import " + "app" + ".models",
    )
    contents = "\n".join((schema_token, *other_tokens, *app_references))
    monkeypatch.setattr(check_brand, "REPO_ROOT", tmp_path)
    for relative_path in (*TRANSFER_PATHS, *INVENTORY_PATHS):
        _write_fixture(tmp_path, relative_path, contents)

    for token in other_tokens:
        assert {hit.file for hit in check_brand.scan_token(token)} == {
            *TRANSFER_PATHS,
            *INVENTORY_PATHS,
        }
    app_hits = check_brand.scan_app_references()
    assert len(app_hits) == len((*TRANSFER_PATHS, *INVENTORY_PATHS)) * len(app_references)
    assert {hit.file for hit in app_hits} == {*TRANSFER_PATHS, *INVENTORY_PATHS}


def test_historical_network_filename_is_allowed_only_in_exact_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _brand_token("network")
    allowed = "docs/afi204-migration-catalog.json"
    unrelated = "docs/another-catalog.json"
    monkeypatch.setattr(check_brand, "REPO_ROOT", tmp_path)
    _write_fixture(tmp_path, allowed, token)
    _write_fixture(tmp_path, unrelated, token)
    assert [hit.file for hit in check_brand.scan_token(token)] == [unrelated]
