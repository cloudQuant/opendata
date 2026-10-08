"""Synthetic tests for static provider metadata inventory validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from scripts.quality.provider_model_inventory import (
    UPSTREAM_COMMIT,
    ExpectedCounts,
    ModelAnchor,
    ParsedProviderSource,
    extract_provider_models,
    main,
    validate_credentials,
    validate_inventory,
)


def _parse(source_file: str, text: str) -> ParsedProviderSource:
    """Parse a synthetic provider module without importing its dependencies."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    return extract_provider_models(text, source_file, digest)


def _synthetic_sources() -> tuple[list[str], list[ModelAnchor], list[dict[str, str]]]:
    """Return two small literal Provider declarations for counterfactual tests."""
    alpha_file = "providers/alpha/openbb_alpha/__init__.py"
    alpha_text = '''"""Synthetic provider."""
from arbitrary_module import Provider

alpha = Provider(
    name="alpha",
    credentials=["api_key"],
    fetcher_dict={
        "SharedModel": object,
        "AlphaOnly": object,
    },
)
'''
    eia_file = "providers/eia/openbb_eia/__init__.py"
    eia_text = """from arbitrary_module import Provider

eia = Provider(
    name="eia",
    credentials=["api_key"],
    fetcher_dict={
        "PetroleumStatusReport": object,
        "ShortTermEnergyOutlook": object,
    },
)
"""
    parsed = [
        _parse(alpha_file, alpha_text),
        _parse(eia_file, eia_text),
    ]
    records = [record for source in parsed for record in source.records]
    ledger_rows = [record.ledger_values() for record in records]
    for row in ledger_rows:
        row["credential_fields"] = "api_key"
    return [alpha_file, eia_file], records, ledger_rows


def test_extracts_literal_provider_keys_and_key_lines_without_importing_source() -> None:
    source_file = "providers/alpha/openbb_alpha/__init__.py"
    source_text = """from arbitrary_module import Provider
alpha = Provider(
    name="alpha",
    credentials=["api_key", "token"],
    fetcher_dict={
        "One": object,
        "Two": object,
    },
)
"""

    parsed = _parse(source_file, source_text)

    assert parsed.issues == ()
    assert parsed.declared_provider == "alpha"
    assert parsed.credentials == ("api_key", "token")
    assert [(record.model, record.upstream_shape_line) for record in parsed.records] == [
        ("One", 6),
        ("Two", 7),
    ]
    assert all(record.upstream_commit == UPSTREAM_COMMIT for record in parsed.records)


def test_dynamic_credential_metadata_is_rejected_without_execution() -> None:
    parsed = _parse(
        "providers/alpha/openbb_alpha/__init__.py",
        """from arbitrary_module import Provider
alpha = Provider(
    name="alpha",
    credentials=load_credentials(),
    fetcher_dict={"One": object},
)
""",
    )

    assert any(issue["code"] == "CREDENTIALS_NOT_LITERAL" for issue in parsed.issues)


def test_synthetic_inventory_matches_all_ledger_anchors() -> None:
    source_files, records, ledger_rows = _synthetic_sources()

    issues, counts = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert issues == []
    assert counts["source_count"] == 2
    assert counts["model_count"] == 4
    assert counts["unique_model_count"] == 4


def test_static_credentials_match_ledger_fields() -> None:
    _, _, ledger_rows = _synthetic_sources()

    issues = validate_credentials(
        {"alpha": ["api_key"], "eia": ["api_key"]},
        ledger_rows,
        expected_source_count=None,
    )

    assert issues == []


def test_credential_name_mismatch_fails() -> None:
    _, _, ledger_rows = _synthetic_sources()
    ledger_rows[0]["credential_fields"] = "fred_api_key"

    issues = validate_credentials(
        {"alpha": ["api_key"], "eia": ["api_key"]},
        ledger_rows,
        expected_source_count=None,
    )

    assert any(issue["code"] == "CREDENTIAL_FIELDS_MISMATCH" for issue in issues)


@pytest.mark.parametrize(
    "missing_model",
    ["PetroleumStatusReport", "ShortTermEnergyOutlook"],
)
def test_missing_eia_model_fails_even_when_ledger_contains_the_old_row(
    missing_model: str,
) -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    records = [record for record in records if record.model != missing_model]

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert any(issue["code"] == "REQUIRED_EIA_MODEL_MISSING" for issue in issues)
    assert any(issue["code"] == "COUNT_MISMATCH" for issue in issues)


def test_duplicate_upstream_identity_fails() -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    records.append(records[0])

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert any(issue["code"] == "DUPLICATE_UPSTREAM_IDENTITY" for issue in issues)


def test_changed_count_denominator_fails() -> None:
    source_files, records, ledger_rows = _synthetic_sources()

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=3, unique_model_count=4),
    )

    assert any(
        issue["code"] == "COUNT_MISMATCH" and issue["field"] == "model_count" for issue in issues
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upstream_shape_sha256", "0" * 64),
        ("upstream_shape_file", "providers/alpha/other.py"),
        ("upstream_shape_line", "999"),
        ("upstream_commit", "f" * 40),
    ],
)
def test_wrong_file_hash_line_or_commit_fails(field: str, value: str) -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    ledger_rows[0][field] = value

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert any(
        issue["code"] == "LEDGER_FIELD_MISMATCH" and issue["field"] == field for issue in issues
    )


def test_duplicate_task_id_in_ledger_fails() -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    ledger_rows.append(dict(ledger_rows[0]))

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert any(issue["code"] == "DUPLICATE_LEDGER_TASK_ID" for issue in issues)
    assert any(issue["code"] == "DUPLICATE_LEDGER_IDENTITY" for issue in issues)


def test_wrong_task_id_fails() -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    ledger_rows[0]["task_id"] = "OBB2-alpha-wrong"

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    assert any(
        issue["code"] == "LEDGER_FIELD_MISMATCH" and issue["field"] == "task_id" for issue in issues
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "other"), ("upstream_model", "OtherModel")],
)
def test_wrong_provider_or_model_identity_fails(field: str, value: str) -> None:
    source_files, records, ledger_rows = _synthetic_sources()
    ledger_rows[0][field] = value

    issues, _ = validate_inventory(
        records,
        ledger_rows,
        source_files,
        ExpectedCounts(source_count=2, model_count=4, unique_model_count=4),
    )

    codes = {issue["code"] for issue in issues}
    assert "MISSING_LEDGER_IDENTITY" in codes
    assert "EXTRA_LEDGER_IDENTITY" in codes


def test_missing_upstream_emits_not_available_and_nonzero_exit(tmp_path: Any, capsys: Any) -> None:
    exit_code = main(
        [
            "--upstream-path",
            str(tmp_path),
            "--task-ledger",
            str(tmp_path / "missing.csv"),
        ]
    )

    report = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert report["status"] == "NOT_AVAILABLE"
    assert report["issues"][0]["code"] == "UPSTREAM_NOT_AVAILABLE"


def test_model_anchor_representation_uses_only_literal_source_metadata() -> None:
    record = ModelAnchor(
        task_id="OBB2-alpha-Example",
        provider="alpha",
        provider_name="alpha",
        model="Example",
        upstream_commit=UPSTREAM_COMMIT,
        upstream_shape_file="providers/alpha/openbb_alpha/__init__.py",
        upstream_shape_sha256="a" * 64,
        upstream_shape_line=7,
    )

    assert record.ledger_values()["upstream_shape_line"] == "7"
    assert replace(record, upstream_shape_line=8).upstream_shape_line == 8
