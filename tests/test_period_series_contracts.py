"""Frozen tests for the shared period-preserving economic series contracts."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models import period_series
from opendata.data.models.period_series import (
    BlsCatalogItem,
    BlsCatalogPage,
    BlsFootnote,
    BlsObservation,
)
from opendata.data.providers.bls.models import _contracts

_PRE_MOVE_SCHEMA_SHA256 = {
    "BlsCatalogItem": "4edbb78dbc797f5277ec78eb195aac90d489c0a2c3df70e2a840b157cb835e67",
    "BlsFootnote": "468f229f14f57fbb3598c8ea497069c9cd0c5658867264b44bf0ef9c8261807f",
    "BlsObservation": "b190cb273c486941a2230708985b2030ee502fbeda948452d541dece4988a5d2",
}


def _schema_fingerprint(model: type[Any]) -> str:
    """Hash a stable JSON representation of the Pydantic schema."""
    schema = json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(schema.encode()).hexdigest()


def test_pydantic_schemas_match_the_pre_move_contracts() -> None:
    """The moved models retain their exact pre-move Pydantic schemas."""
    models = (BlsCatalogItem, BlsFootnote, BlsObservation)
    assert {model.__name__: _schema_fingerprint(model) for model in models} == (
        _PRE_MOVE_SCHEMA_SHA256
    )


def test_provider_contracts_are_identity_reexports() -> None:
    """The source package exposes the central class objects without wrappers."""
    assert _contracts.BlsCatalogItem is period_series.BlsCatalogItem
    assert _contracts.BlsCatalogPage is period_series.BlsCatalogPage
    assert _contracts.BlsFootnote is period_series.BlsFootnote
    assert _contracts.BlsObservation is period_series.BlsObservation


def test_nested_contracts_and_page_round_trip_without_losing_period_semantics() -> None:
    """Dimensions, notes, M13, status fields, and page metadata round-trip."""
    first = BlsCatalogItem(
        series_id="CUUR0000SA0",
        survey="CU",
        title="Consumer price index",
        dimensions={"periodicity_code": "R", "seasonal": "U"},
        source_metadata={"series_title": "Consumer price index"},
        catalog_as_of=date(2026, 10, 1),
    )
    second = BlsCatalogItem(series_id="CES0000000001", survey="CE", title="Employment")
    assert BlsCatalogItem.model_validate_json(first.model_dump_json()) == first

    observation = BlsObservation(
        series_id=first.series_id,
        year=2024,
        period="M13",
        period_name="Annual",
        value=12.5,
        footnotes=(BlsFootnote(), BlsFootnote(code="P", text="Preliminary.")),
        latest=True,
        preliminary=True,
        api_version="v2",
    )
    restored_observation = BlsObservation.model_validate_json(observation.model_dump_json())
    assert restored_observation == observation
    assert restored_observation.period == "M13"
    assert restored_observation.period_name == "Annual"
    assert restored_observation.footnotes[1].code == "P"
    assert restored_observation.footnotes[1].text == "Preliminary."
    assert restored_observation.latest is True
    assert restored_observation.preliminary is True
    assert restored_observation.api_version == "v2"

    page = BlsCatalogPage(items=(first, second), total=12, offset=4, limit=2)
    dumped_page = asdict(page)
    restored_page = BlsCatalogPage(
        items=tuple(BlsCatalogItem.model_validate(row) for row in dumped_page["items"]),
        total=dumped_page["total"],
        offset=dumped_page["offset"],
        limit=dumped_page["limit"],
    )
    assert restored_page == page
    assert restored_page.total == 12
    assert restored_page.offset == 4
    assert restored_page.limit == 2
    assert len(restored_page) == 2
    assert restored_page[0] is restored_page.items[0]
    assert restored_page[-1] == second
    assert restored_page[:] == (first, second)
    assert restored_page[1:] == (second,)


def test_nested_dimensions_keep_strict_string_values() -> None:
    """Survey-specific dimensions stay a strict string-to-string map."""
    with pytest.raises(ValidationError):
        BlsCatalogItem(
            series_id="CUUR0000SA0",
            survey="CU",
            dimensions={"periodicity_code": 12},  # type: ignore[dict-item]
        )


def test_shared_contract_module_imports_only_shared_base_stdlib_and_pydantic() -> None:
    """The central model family has no source-provider or infrastructure dependency."""
    module_path = Path(period_series.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    assert imported_modules == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "pydantic",
        "opendata.data.models.base",
    }
