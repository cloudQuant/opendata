"""Behavioral tests for the frozen published-API compatibility gate."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, cast

import pytest

from scripts.quality import check_published_openapi as gate
from scripts.quality.check_published_openapi import (
    BASELINE_PATHS,
    check_live_contracts,
    compare_contracts,
    extract_surface,
)


def _contract() -> dict[str, Any]:
    """Build a compact OpenAPI contract with refs, arrays, and composition."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Fixture API", "version": "1.0"},
        "paths": {
            "/api/v2/items": {
                "get": {
                    "operationId": "getItems",
                    "security": [{"Bearer": []}],
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Node"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/v2/status": {
                "get": {"operationId": "getStatus", "responses": {"200": {"description": "OK"}}}
            },
        },
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "choice": {
                            "anyOf": [
                                {"type": "string", "enum": ["a", "b"]},
                                {"type": "integer", "minimum": 0},
                            ]
                        },
                        "exclusive": {
                            "oneOf": [
                                {"type": "string", "minLength": 1},
                                {"type": "integer", "minimum": 0},
                            ]
                        },
                        "labels": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            },
                        },
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Node"},
                        },
                    },
                },
                "AlternateNode": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                },
            },
            "securitySchemes": {"Bearer": {"type": "http", "scheme": "bearer"}},
        },
    }


def _coverage_contract() -> dict[str, Any]:
    """Build the frozen v2 coverage operation before its optional publication pin."""
    document = _contract()
    document["paths"]["/api/v2/datasets/{dataset_id}/coverage"] = {
        "get": {
            "operationId": "getDatasetCoverage",
            "parameters": [
                {
                    "name": "dataset_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "start_date",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "anyOf": [
                            {"format": "date", "type": "string"},
                            {"type": "null"},
                        ],
                        "title": "Start Date",
                    },
                },
                {
                    "name": "end_date",
                    "in": "query",
                    "required": False,
                    "schema": {
                        "anyOf": [
                            {"format": "date", "type": "string"},
                            {"type": "null"},
                        ],
                        "title": "End Date",
                    },
                },
                {
                    "name": "X-API-Key",
                    "in": "header",
                    "required": False,
                    "schema": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "title": "X-Api-Key",
                    },
                },
            ],
            "responses": {"200": {"description": "Success"}},
        }
    }
    return document


def _publication_id_parameter() -> dict[str, Any]:
    """Return the one optional query parameter permitted by the coverage gate."""
    return {
        "in": "query",
        "name": "publication_id",
        "required": False,
        "schema": {
            "anyOf": [
                {"maxLength": 32, "minLength": 32, "type": "string"},
                {"type": "null"},
            ],
            "title": "Publication Id",
        },
    }


def _coverage_contract_with_publication_pin() -> dict[str, Any]:
    """Build the updated frozen coverage operation after its publication pin was accepted."""
    document = _coverage_contract()
    document["paths"]["/api/v2/datasets/{dataset_id}/coverage"]["get"]["parameters"].insert(
        3, _publication_id_parameter()
    )
    return document


def _compare(candidate: dict[str, Any]) -> list[str]:
    """Compare a fixture against itself as a frozen v2 contract."""
    return compare_contracts(_contract(), candidate, "v2")


def test_real_app_matches_both_frozen_contracts() -> None:
    """The app's real OpenAPI document remains within its pinned v1 and v2 contracts."""
    assert check_live_contracts() == {"v1": [], "v2": []}
    assert os.environ["TESTING"] == "true"


def test_v1_snapshot_contains_both_real_alias_families_and_excludes_v2() -> None:
    """The frozen v1 artifact covers mounted legacy aliases without inventing endpoints."""
    baseline = json.loads(BASELINE_PATHS["v1"].read_text(encoding="utf-8"))
    v1_paths = set(baseline["paths"])
    assert len(v1_paths) == 140
    assert "/api/v1/data/catalog" in v1_paths
    assert "/api/data/catalog" in v1_paths
    assert all(path.startswith("/api/v1/") or path.startswith("/api/") for path in v1_paths)
    assert not any(path.startswith("/api/v2/") for path in v1_paths)
    assert len(extract_surface(baseline, "v1")["paths"]) == len(v1_paths)


def test_v1_v2_prefix_boundaries_do_not_leak_between_surfaces() -> None:
    """Exact v2 and v2 descendants stay out of the legacy v1 alias surface."""
    document = _contract()
    document["paths"].update(
        {
            "/api/v1/item": {},
            "/api/item": {},
            "/api/v2": {},
            "/api/v2/item": {},
            "/api/v2draft": {},
        }
    )
    v1_paths = set(extract_surface(document, "v1")["paths"])
    v2_paths = set(extract_surface(document, "v2")["paths"])
    assert {"/api/v1/item", "/api/item", "/api/v2draft"} <= v1_paths
    assert "/api/v2" not in v1_paths
    assert "/api/v2/item" not in v1_paths
    assert {"/api/v2", "/api/v2/item"} <= v2_paths
    assert "/api/v2draft" not in v2_paths


def test_additive_route_and_optional_response_field_are_compatible() -> None:
    """New routes and optional response properties do not invalidate old clients."""
    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"]["extra"] = {"type": "string"}
    candidate["paths"]["/api/v2/new-capability"] = {
        "get": {"operationId": "newCapability", "responses": {"200": {"description": "OK"}}}
    }
    assert _compare(candidate) == []


def test_removed_path_or_method_is_blocked() -> None:
    """Removing an existing path or HTTP method is reported as breaking."""
    candidate = _contract()
    candidate["paths"].pop("/api/v2/items")
    assert any("path was removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"].pop("get")
    assert any("operation was removed" in issue for issue in _compare(candidate))


def test_new_required_parameter_and_tightened_parameter_schema_are_blocked() -> None:
    """Required input additions and changed input constraints fail closed."""
    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["parameters"].append(
        {"name": "mode", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    assert any("parameters" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["parameters"][0]["schema"]["minimum"] = 10
    assert any("contract value changed" in issue for issue in _compare(candidate))


def test_coverage_allows_only_the_optional_exact_publication_pin() -> None:
    """The publication selector is additive only on its one reviewed operation."""
    baseline = _coverage_contract()
    candidate = deepcopy(baseline)
    candidate["paths"]["/api/v2/datasets/{dataset_id}/coverage"]["get"]["parameters"].insert(
        3, _publication_id_parameter()
    )
    assert compare_contracts(baseline, candidate, "v2") == []


def test_coverage_publication_pin_removal_is_blocked_after_freezing() -> None:
    """Once frozen into the baseline, the publication selector cannot disappear."""
    baseline = _coverage_contract_with_publication_pin()
    candidate = deepcopy(baseline)
    candidate["paths"]["/api/v2/datasets/{dataset_id}/coverage"]["get"]["parameters"].pop(3)
    assert any("parameters" in issue for issue in compare_contracts(baseline, candidate, "v2"))


@pytest.mark.parametrize("mutation", ("required", "type"))
def test_coverage_publication_pin_drift_is_blocked(mutation: str) -> None:
    """Removing existing coverage inputs or tightening the pin cannot pass."""
    baseline = _coverage_contract()
    candidate = deepcopy(baseline)
    parameters = candidate["paths"]["/api/v2/datasets/{dataset_id}/coverage"]["get"]["parameters"]
    parameters.insert(3, _publication_id_parameter())
    if mutation == "required":
        parameters[3]["required"] = True
    else:
        parameters[3]["schema"]["anyOf"][0]["type"] = "integer"
    assert any("parameters" in issue for issue in compare_contracts(baseline, candidate, "v2"))


def test_coverage_publication_pin_is_blocked_on_any_other_route() -> None:
    """The route-specific allowance cannot become a general parameter exception."""
    baseline = _coverage_contract()
    candidate = deepcopy(baseline)
    candidate["paths"]["/api/v2/status"]["get"]["parameters"] = [_publication_id_parameter()]
    assert any(
        "/api/v2/status.get.parameters" in issue
        for issue in compare_contracts(baseline, candidate, "v2")
    )


def test_response_status_removal_and_required_field_addition_are_blocked() -> None:
    """Response status and required-property changes are breaking."""
    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"].pop("200")
    assert any("response status was removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"]["new_id"] = {"type": "string"}
    node["required"].append("new_id")
    issues = _compare(candidate)
    assert any("new response field is required" in issue for issue in issues)
    assert any("required fields changed" in issue for issue in issues)


def test_nested_ref_array_anyof_type_enum_and_nullable_drift_are_blocked() -> None:
    """Schema checks follow nested refs, arrays, and anyOf branches without skipping changes."""
    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"]["choice"]["anyOf"][0]["enum"] = ["a"]
    node["properties"]["labels"]["items"]["properties"]["name"]["type"] = "integer"
    issues = _compare(candidate)
    assert any("enum" in issue for issue in issues)
    assert any("type" in issue for issue in issues)

    candidate = _contract()
    candidate["components"]["schemas"]["Node"]["properties"]["id"]["nullable"] = True
    assert any("nullable" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["components"]["schemas"]["Node"]["properties"]["exclusive"]["oneOf"][0][
        "minLength"
    ] = 2
    assert any("minLength" in issue for issue in _compare(candidate))


def test_changed_reference_auth_scheme_and_unknown_schema_keyword_are_blocked() -> None:
    """Ref drift, security drift, and unknown keyword changes cannot pass silently."""
    candidate = _contract()
    response_schema = candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    response_schema["$ref"] = "#/components/schemas/AlternateNode"
    assert any("reference" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["components"]["securitySchemes"]["Bearer"]["scheme"] = "basic"
    assert any("contract value changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["components"]["schemas"]["Node"]["x-vendor-contract"] = {"value": "known"}
    candidate["components"]["schemas"]["Node"]["x-vendor-contract"]["value"] = "unknown"
    baseline = _contract()
    baseline["components"]["schemas"]["Node"]["x-vendor-contract"] = {"value": "known"}
    issues = compare_contracts(baseline, candidate, "v2")
    assert any("x-vendor-contract" in issue for issue in issues)


def test_invalid_or_external_schema_references_fail_closed() -> None:
    """External and unresolved component references are rejected instead of ignored."""
    candidate = _contract()
    response_schema = candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    response_schema["$ref"] = "https://example.invalid/schema.json"
    assert any("unsupported OpenAPI reference" in issue for issue in _compare(candidate))

    candidate = _contract()
    response_schema = candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    response_schema["$ref"] = "#/components/schemas/Missing"
    assert any("component is missing" in issue for issue in _compare(candidate))


def test_documentation_only_changes_and_optional_response_field_pass() -> None:
    """Documentation edits do not change compatibility, but requiredness remains pinned."""
    candidate = _contract()
    operation = candidate["paths"]["/api/v2/items"]["get"]
    operation["summary"] = "A clearer summary"
    operation["description"] = "Updated docs"
    candidate["components"]["schemas"]["Node"]["properties"]["optional"] = {"type": "boolean"}
    assert _compare(candidate) == []

    candidate = _contract()
    schema = candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    schema["description"] = "Old description"
    baseline = deepcopy(candidate)
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["description"] = "Updated description"
    assert compare_contracts(baseline, candidate, "v2") == []


def test_top_level_openapi_contract_fields_are_preserved_and_presence_checked() -> None:
    """OpenAPI 3.1 dialect, webhooks, security and missing-vs-null cannot be ignored."""
    candidate = _contract()
    candidate["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
    issues = _compare(candidate)
    assert any("top-level contract field was added or removed" in issue for issue in issues)

    baseline = _contract()
    baseline["jsonSchemaDialect"] = "https://old.example.invalid/schema"
    candidate = deepcopy(baseline)
    candidate["jsonSchemaDialect"] = "https://new.example.invalid/schema"
    assert any(
        "jsonSchemaDialect" in issue for issue in compare_contracts(baseline, candidate, "v2")
    )

    candidate = _contract()
    candidate["webhooks"] = {}
    assert any("webhooks" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["security"] = None
    assert any("security must be an array" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["servers"] = None
    assert any("servers must be an array" in issue for issue in _compare(candidate))


def test_candidate_helpers_do_not_mutate_the_baseline_fixture() -> None:
    """Comparison does not mutate either input document while resolving refs."""
    baseline = _contract()
    candidate = deepcopy(baseline)
    original = deepcopy(baseline)
    assert compare_contracts(baseline, candidate, "v2") == []
    assert baseline == original


def test_contract_extraction_rejects_malformed_documents_and_references() -> None:
    """Missing surfaces, malformed refs, and invalid security shapes are not skipped."""
    with pytest.raises(ValueError, match="paths"):
        extract_surface({"openapi": "3.1.0"}, "v2")
    with pytest.raises(ValueError, match="version"):
        extract_surface({"paths": {"/api/v2/x": {}}}, "v2")
    with pytest.raises(ValueError, match="unknown OpenAPI surface"):
        extract_surface(_contract(), "v3")
    no_surface = {"openapi": "3.1.0", "paths": {"/health": {}}, "components": {}}
    with pytest.raises(ValueError, match="no routes"):
        extract_surface(no_surface, "v2")
    invalid_components = {"openapi": "3.1.0", "paths": {"/api/v2/x": {}}, "components": []}
    with pytest.raises(ValueError, match="components"):
        extract_surface(invalid_components, "v2")
    invalid_key = cast("dict[str, Any]", {1: "not-a-json-object-key"})
    with pytest.raises(ValueError, match="keys must be strings"):
        extract_surface(invalid_key, "v2")
    invalid_path_key = _contract()
    invalid_path_key["paths"][1] = {}
    with pytest.raises(ValueError, match="path keys must be strings"):
        extract_surface(invalid_path_key, "v2")

    malformed_ref = _contract()
    malformed_ref["paths"]["/api/v2/items"]["get"]["x-ref"] = {"$ref": 1}
    assert any("$ref must be a string" in issue for issue in _compare(malformed_ref))

    invalid_security = _contract()
    invalid_security["paths"]["/api/v2/items"]["get"]["security"] = ["Bearer"]
    assert any(
        "security requirement must be an object" in issue for issue in _compare(invalid_security)
    )

    valid_root_security = _contract()
    valid_root_security["security"] = [{"Bearer": []}]
    assert extract_surface(valid_root_security, "v2")["components"]["securitySchemes"]
    invalid_root_security = _contract()
    invalid_root_security["security"] = cast("list[dict[str, Any]]", [{1: []}])
    assert any(
        "security-scheme names must be strings" in issue
        for issue in compare_contracts(_contract(), invalid_root_security, "v2")
    )

    for invalid_field, bad_value, message in (
        ("servers", ["bad server"], "servers must be an array of objects"),
        ("jsonSchemaDialect", 1, "jsonSchemaDialect must be a string"),
        ("webhooks", [], "webhooks must be an object"),
    ):
        invalid = _contract()
        invalid[invalid_field] = bad_value
        assert any(message in issue for issue in compare_contracts(_contract(), invalid, "v2"))


def test_json_pointer_decoder_and_resolver_handle_escaping_and_errors() -> None:
    """Local component pointers decode escaped names and reject malformed targets."""
    assert gate._pointer_tokens("#/components/schemas/A~1B~0C") == [
        "components",
        "schemas",
        "A/B~C",
    ]
    with pytest.raises(ValueError, match="malformed JSON pointer"):
        gate._pointer_tokens("#/components/schemas/A~2B")
    with pytest.raises(ValueError, match="incomplete component"):
        gate._pointer_tokens("#/components/")
    with pytest.raises(ValueError, match="unresolved OpenAPI reference"):
        gate._resolve_reference(_contract(), "#/components/schemas/Node/missing")


def test_exact_ref_comparator_rejects_target_and_sibling_drift_and_bad_targets() -> None:
    """Exact input contracts resolve refs, check siblings, and fail closed on bad targets."""
    baseline = _contract()
    candidate = deepcopy(baseline)
    issues: list[str] = []
    gate._compare_exact(
        baseline,
        candidate,
        {"$ref": "#/components/schemas/Node"},
        {"$ref": "#/components/schemas/AlternateNode"},
        "fixture",
        issues,
        set(),
    )
    assert any("reference was added, removed, or changed" in issue for issue in issues)

    issues = []
    gate._compare_exact(
        baseline,
        candidate,
        {"$ref": "#/components/schemas/Node", "description": "old"},
        {"$ref": "#/components/schemas/Node", "description": "new"},
        "fixture",
        issues,
        set(),
    )
    assert any("reference siblings changed" in issue for issue in issues)

    issues = []
    gate._compare_exact(
        baseline,
        baseline,
        {"$ref": "#/components/schemas/Missing"},
        {"$ref": "#/components/schemas/Missing"},
        "fixture",
        issues,
        set(),
    )
    assert any("unresolved OpenAPI reference" in issue for issue in issues)

    issues = []
    gate._compare_schema(
        baseline,
        baseline,
        {"$ref": "#/components/schemas/Missing"},
        {"$ref": "#/components/schemas/Missing"},
        "fixture",
        issues,
        set(),
    )
    assert any("unresolved OpenAPI reference" in issue for issue in issues)

    issues = []
    gate._compare_schema(baseline, candidate, True, {"type": "string"}, "fixture", issues, set())
    assert any("schema shape changed" in issue for issue in issues)


def test_parameter_component_reference_changes_are_resolved_and_detected() -> None:
    """References nested in parameter arrays are followed, including component drift."""
    baseline = _contract()
    baseline["components"]["parameters"] = {
        "PageSize": {
            "name": "page_size",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": 50},
        }
    }
    baseline["paths"]["/api/v2/items"]["get"]["parameters"] = [
        {"$ref": "#/components/parameters/PageSize"}
    ]
    candidate = deepcopy(baseline)
    candidate["components"]["parameters"]["PageSize"]["schema"]["maximum"] = 10
    issues = compare_contracts(baseline, candidate, "v2")
    assert any("$ref.schema.maximum" in issue for issue in issues)

    candidate = deepcopy(baseline)
    candidate["paths"]["/api/v2/items"]["get"]["parameters"][0]["$ref"] = (
        "#/components/parameters/Missing"
    )
    assert any(
        "component is missing" in issue for issue in compare_contracts(baseline, candidate, "v2")
    )


def test_path_item_reference_is_detected_but_fails_closed_as_unsupported() -> None:
    """Path Item refs are collected but cannot bypass operation-aware comparison."""
    baseline = _contract()
    baseline["components"]["pathItems"] = {
        "Shared": {"get": {"responses": {"200": {"description": "OK"}}}}
    }
    baseline["paths"]["/api/v2/items"]["$ref"] = "#/components/pathItems/Shared"
    issues = compare_contracts(baseline, deepcopy(baseline), "v2")
    assert any("Path Item $ref is unsupported" in issue for issue in issues)


def test_response_field_removal_and_nested_composition_structure_are_blocked() -> None:
    """Existing response fields and composition branches cannot disappear."""
    candidate = _contract()
    candidate["components"]["schemas"]["Node"]["properties"].pop("id")
    assert any("existing response field was removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["components"]["schemas"]["Node"]["properties"]["choice"]["anyOf"].pop()
    assert any("nested schema structure changed" in issue for issue in _compare(candidate))


def test_response_media_status_and_auth_contract_changes_are_blocked() -> None:
    """Media types, new statuses, auth requirements, and schemes remain pinned."""
    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"]["text/plain"] = {
        "schema": {"type": "string"}
    }
    assert any("response media types changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["201"] = {"description": "Created"}
    assert any("new response status requires review" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["security"] = []
    assert any("contract array length changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["components"]["securitySchemes"].pop("Bearer")
    assert any("securitySchemes/Bearer" in issue for issue in _compare(candidate))


def test_path_metadata_operation_id_and_root_server_drift_are_blocked() -> None:
    """Non-documentation operation IDs and server/path-item behavior remain frozen."""
    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["operationId"] = "renamedOperation"
    assert any("operationId" in issue for issue in _compare(candidate))

    baseline = _contract()
    baseline["servers"] = [{"url": "https://old.example.invalid"}]
    candidate = _contract()
    candidate["servers"] = [{"url": "https://new.example.invalid"}]
    server_issues = compare_contracts(baseline, candidate, "v2")
    assert any("servers" in issue and "changed" in issue for issue in server_issues)

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["parameters"] = [
        {"name": "version", "in": "query", "required": False, "schema": {"type": "string"}}
    ]
    assert any("path-item field was added" in issue for issue in _compare(candidate))


def test_response_shape_and_schema_constraint_changes_fail_closed() -> None:
    """Malformed response envelopes and changed nullability/types/enums are blocked."""
    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"] = None
    assert any("response object changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"]["id"]["type"] = "integer"
    assert any("response schema constraint changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"]["id"]["enum"] = ["only-one"]
    assert any("response schema structure changed" in issue for issue in _compare(candidate))


def test_malformed_schema_response_and_operation_shapes_fail_closed() -> None:
    """Unexpected JSON shapes on response contracts cannot bypass comparison."""
    candidate = _contract()
    node = candidate["components"]["schemas"]["Node"]
    node["properties"] = []
    node["required"] = "id"
    issues = _compare(candidate)
    assert any("properties must remain an object" in issue for issue in issues)
    assert any("required fields must remain an array" in issue for issue in issues)

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"] = []
    assert any("response content must remain an object" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ] = None
    assert any("media type contract changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["headers"] = {
        "X-Count": {"schema": {"type": "integer"}}
    }
    assert any("response field was added or removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"]["application/json"][
        "encoding"
    ] = {"value": {"contentType": "application/json"}}
    assert any("media type field was added or removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"].pop(
        "application/json"
    )
    assert any("response media types changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
        "application/json"
    ].pop("schema")
    assert any("response schema was added or removed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["deprecated"] = True
    assert any(
        "operation contract field was added or removed" in issue for issue in _compare(candidate)
    )

    candidate = _contract()
    candidate["openapi"] = "3.0.3"
    assert any("OpenAPI version changed" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"] = None
    assert any("path item must remain an object" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"] = None
    assert any("operation must remain an object" in issue for issue in _compare(candidate))

    candidate = _contract()
    candidate["paths"]["/api/v2/items"]["get"]["responses"] = []
    assert any(
        "operation responses must remain an object" in issue for issue in _compare(candidate)
    )


def test_frozen_pin_and_cli_success_blocked_and_error_results(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Baseline pins and all CLI verdicts map to explicit process exit statuses."""
    monkeypatch.setitem(gate.BASELINE_SHA256, "v1", "0" * 64)
    with pytest.raises(ValueError, match="reviewed pin update"):
        gate._load_baseline("v1")

    monkeypatch.setattr(gate, "check_live_contracts", lambda: {"v1": [], "v2": []})
    monkeypatch.setattr(gate, "load_live_openapi", lambda: _contract())
    monkeypatch.setattr(gate, "extract_surface", lambda _document, _surface: {"paths": {"x": {}}})
    assert gate.main([]) == 0
    assert "v1 compatible" in capsys.readouterr().out

    monkeypatch.setattr(gate, "check_live_contracts", lambda: {"v1": ["removed"], "v2": []})
    assert gate.main([]) == 1
    assert "blocked" in capsys.readouterr().err

    monkeypatch.setattr(
        gate, "check_live_contracts", lambda: (_ for _ in ()).throw(OSError("missing"))
    )
    assert gate.main([]) == 2
    assert "ERROR: missing" in capsys.readouterr().err
