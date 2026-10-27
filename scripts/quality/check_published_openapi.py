"""Fail closed when the published HTTP API drifts from its reviewed contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATHS = {
    "v1": ROOT / "docs" / "afi204-v1-openapi.json",
    "v2": ROOT / "docs" / "afi204-v2-openapi.json",
}
# Updating either artifact requires an explicit, reviewed digest change here. The checker
# intentionally has no mode that generates or replaces a baseline from the candidate.
BASELINE_SHA256 = {
    "v1": "2b738ff5ab2d2aa8a2603c908a05256a1b8b7522f72aa5a699ba963d4271bf74",
    "v2": "86b7dd1982e6d50e477b67742e13258c32922436d4867e5d55333a1ac115a9e5",
}
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head"})
_DOC_SCHEMA_KEYS = frozenset({"title", "description", "examples", "example"})
_SCHEMA_CHILD_KEYS = frozenset(
    {
        "items",
        "prefixItems",
        "properties",
        "additionalProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
    }
)


def _canonical(value: object) -> str:
    """Return deterministic JSON for comparing OpenAPI values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _pointer_tokens(reference: str) -> list[str]:
    """Decode a local JSON pointer and reject external or malformed references."""
    if not reference.startswith("#/components/"):
        raise ValueError(f"unsupported OpenAPI reference: {reference}")
    decoded: list[str] = []
    for token in reference[2:].split("/"):
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    raise ValueError(f"malformed JSON pointer escape: {reference}")
                index += 2
            else:
                index += 1
        decoded.append(token.replace("~1", "/").replace("~0", "~"))
    if len(decoded) < 3:
        raise ValueError(f"incomplete component reference: {reference}")
    return decoded


def _resolve_reference(document: Mapping[str, Any], reference: str) -> object:
    """Resolve a local component reference without network or filesystem access."""
    target: object = document
    for token in _pointer_tokens(reference):
        if not isinstance(target, Mapping) or token not in target:
            raise ValueError(f"unresolved OpenAPI reference: {reference}")
        target = target[token]
    return target


def _references(value: object) -> list[str]:
    """Collect component references recursively from a JSON-like OpenAPI fragment."""
    found: list[str] = []
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise ValueError("OpenAPI $ref must be a string")
            found.append(reference)
        for child in value.values():
            found.extend(_references(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_references(child))
    return found


def _escape_pointer_token(value: str) -> str:
    """Encode one token for an RFC 6901 JSON pointer."""
    return value.replace("~", "~0").replace("/", "~1")


def _security_names(security: object) -> list[str]:
    """Return security-scheme names from an OpenAPI security-requirement list."""
    if not isinstance(security, list):
        raise ValueError("OpenAPI security must be an array")
    names: list[str] = []
    for requirement in security:
        if not isinstance(requirement, Mapping):
            raise ValueError("OpenAPI security requirement must be an object")
        if not all(isinstance(name, str) for name in requirement):
            raise ValueError("OpenAPI security-scheme names must be strings")
        names.extend(requirement)
    return names


def extract_surface(document: Mapping[str, Any], surface: str) -> dict[str, Any]:
    """Select the mounted v1 aliases or v2 routes and their referenced components."""
    if not all(isinstance(key, str) for key in document):
        raise ValueError("OpenAPI document keys must be strings")
    if not isinstance(document.get("openapi"), str):
        raise ValueError("OpenAPI document is missing its version")
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI document is missing its paths object")
    if not all(isinstance(path, str) for path in paths):
        raise ValueError("OpenAPI path keys must be strings")
    if surface == "v1":
        selected = {
            path: item
            for path, item in paths.items()
            if isinstance(path, str)
            and (
                path == "/api/v1"
                or path.startswith("/api/v1/")
                or path == "/api"
                or (
                    path.startswith("/api/")
                    and path != "/api/v2"
                    and not path.startswith("/api/v2/")
                )
            )
        }
    elif surface == "v2":
        selected = {
            path: item
            for path, item in paths.items()
            if isinstance(path, str) and (path == "/api/v2" or path.startswith("/api/v2/"))
        }
    else:
        raise ValueError(f"unknown OpenAPI surface: {surface}")
    if not selected:
        raise ValueError(f"OpenAPI document contains no routes for {surface}")

    components = document.get("components", {})
    if not isinstance(components, Mapping):
        raise ValueError("OpenAPI components must be an object")
    pending = _references(selected)
    # Preserve and walk every non-documentation top-level field. OpenAPI 3.1
    # fields such as jsonSchemaDialect and webhooks affect the published
    # contract; unknown extensions must also fail closed instead of vanishing.
    omitted_top_level = {"openapi", "info", "paths", "components", "tags"}
    top_level_fields = set(document) - omitted_top_level
    for key in sorted(top_level_fields):
        pending.extend(_references(document[key]))

    if "security" in document:
        if not isinstance(document["security"], list):
            raise ValueError("OpenAPI security must be an array")
        pending.extend(
            f"#/components/securitySchemes/{_escape_pointer_token(name)}"
            for name in _security_names(document["security"])
        )
    if "servers" in document:
        servers = document["servers"]
        if not isinstance(servers, list) or not all(isinstance(item, Mapping) for item in servers):
            raise ValueError("OpenAPI servers must be an array of objects")
    if "jsonSchemaDialect" in document and not isinstance(document["jsonSchemaDialect"], str):
        raise ValueError("OpenAPI jsonSchemaDialect must be a string")
    if "webhooks" in document and not isinstance(document["webhooks"], Mapping):
        raise ValueError("OpenAPI webhooks must be an object")
    for item in selected.values():
        if isinstance(item, Mapping):
            for method, operation in item.items():
                if (
                    isinstance(method, str)
                    and method.lower() in _HTTP_METHODS
                    and isinstance(operation, Mapping)
                    and "security" in operation
                ):
                    pending.extend(
                        f"#/components/securitySchemes/{_escape_pointer_token(name)}"
                        for name in _security_names(operation["security"])
                    )

    picked: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    while pending:
        reference = pending.pop()
        tokens = _pointer_tokens(reference)
        section, name = tokens[1], tokens[2]
        identity = (section, name)
        if identity in seen:
            continue
        section_values = components.get(section)
        if not isinstance(section_values, Mapping) or name not in section_values:
            raise ValueError(f"OpenAPI component is missing: {section}/{name}")
        seen.add(identity)
        component = section_values[name]
        picked.setdefault(section, {})[name] = component
        pending.extend(_references(component))

    result: dict[str, Any] = {
        "openapi": document.get("openapi"),
        "info": document.get("info", {}),
        "paths": dict(sorted(selected.items())),
        "components": picked,
    }
    for key in sorted(top_level_fields):
        result[key] = document[key]
    return result


def _add_issue(issues: list[str], path: str, message: str) -> None:
    """Append a stable diagnostic for a contract change."""
    issues.append(f"{path}: {message}")


def _compare_exact(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    old: object,
    new: object,
    path: str,
    issues: list[str],
    visited: set[tuple[str, str]],
) -> None:
    """Compare a frozen value exactly, resolving matching refs and recursive cycles."""
    old_ref = old.get("$ref") if isinstance(old, Mapping) else None
    new_ref = new.get("$ref") if isinstance(new, Mapping) else None
    if old_ref is not None or new_ref is not None:
        if not isinstance(old_ref, str) or not isinstance(new_ref, str) or old_ref != new_ref:
            _add_issue(issues, path, "reference was added, removed, or changed")
            return
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            _add_issue(issues, path, "reference must be an object")
            return
        identity = (old_ref, new_ref)
        if identity not in visited:
            visited.add(identity)
            try:
                _compare_exact(
                    baseline,
                    candidate,
                    _resolve_reference(baseline, old_ref),
                    _resolve_reference(candidate, new_ref),
                    path + ".$ref",
                    issues,
                    visited,
                )
            except ValueError as exc:
                _add_issue(issues, path, str(exc))
        old_siblings = {key: value for key, value in old.items() if key != "$ref"}
        new_siblings = {key: value for key, value in new.items() if key != "$ref"}
        if _canonical(old_siblings) != _canonical(new_siblings):
            _add_issue(issues, path, "reference siblings changed")
        return
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        if set(old) != set(new):
            _add_issue(issues, path, "contract object fields changed")
            return
        for key in sorted(old):
            _compare_exact(
                baseline, candidate, old[key], new[key], f"{path}.{key}", issues, visited
            )
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            _add_issue(issues, path, "contract array length changed")
            return
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            _compare_exact(
                baseline, candidate, old_item, new_item, f"{path}[{index}]", issues, visited
            )
        return
    if old != new:
        _add_issue(issues, path, "contract value changed")


def _compare_schema(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    old: object,
    new: object,
    path: str,
    issues: list[str],
    visited: set[tuple[str, str]],
) -> None:
    """Compare a response schema, allowing only additional optional properties."""
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        if _canonical(old) != _canonical(new):
            _add_issue(issues, path, "schema shape changed")
        return
    old_ref = old.get("$ref")
    new_ref = new.get("$ref")
    if old_ref is not None or new_ref is not None:
        if not isinstance(old_ref, str) or not isinstance(new_ref, str) or old_ref != new_ref:
            _add_issue(issues, path, "schema reference was added, removed, or changed")
            return
        identity = (old_ref, new_ref)
        if identity not in visited:
            visited.add(identity)
            try:
                _compare_schema(
                    baseline,
                    candidate,
                    _resolve_reference(baseline, old_ref),
                    _resolve_reference(candidate, new_ref),
                    path + ".$ref",
                    issues,
                    visited,
                )
            except ValueError as exc:
                _add_issue(issues, path, str(exc))
        old = {key: value for key, value in old.items() if key != "$ref"}
        new = {key: value for key, value in new.items() if key != "$ref"}

    for key in sorted((set(old) | set(new)) - _DOC_SCHEMA_KEYS):
        old_has = key in old
        new_has = key in new
        if key == "properties":
            old_properties = old.get(key, {})
            new_properties = new.get(key, {})
            if not isinstance(old_properties, Mapping) or not isinstance(new_properties, Mapping):
                _add_issue(issues, path + ".properties", "schema properties must remain an object")
                continue
            required = new.get("required", [])
            if not isinstance(required, list):
                _add_issue(issues, path + ".required", "required fields must remain an array")
                required = []
            for name, old_property in old_properties.items():
                if name not in new_properties:
                    _add_issue(
                        issues, f"{path}.properties.{name}", "existing response field was removed"
                    )
                else:
                    _compare_schema(
                        baseline,
                        candidate,
                        old_property,
                        new_properties[name],
                        f"{path}.properties.{name}",
                        issues,
                        visited,
                    )
            for name in new_properties.keys() - old_properties.keys():
                if name in required:
                    _add_issue(
                        issues, f"{path}.properties.{name}", "new response field is required"
                    )
            continue
        if key == "required":
            old_required = old.get(key, [])
            new_required = new.get(key, [])
            if not isinstance(old_required, list) or not isinstance(new_required, list):
                _add_issue(issues, path + ".required", "required fields must remain an array")
            elif set(old_required) != set(new_required):
                _add_issue(issues, path + ".required", "response required fields changed")
            continue
        if not old_has or not new_has:
            _add_issue(issues, f"{path}.{key}", "response schema structure changed")
            continue
        if key in _SCHEMA_CHILD_KEYS:
            _compare_schema_value(
                baseline,
                candidate,
                old[key],
                new[key],
                f"{path}.{key}",
                issues,
                visited,
            )
        elif _canonical(old[key]) != _canonical(new[key]):
            _add_issue(issues, f"{path}.{key}", "response schema constraint changed")


def _compare_schema_value(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    old: object,
    new: object,
    path: str,
    issues: list[str],
    visited: set[tuple[str, str]],
) -> None:
    """Walk nested schema constructs and fail closed when their structure changes."""
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        _compare_schema(baseline, candidate, old, new, path, issues, visited)
        return
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            _compare_schema_value(
                baseline,
                candidate,
                old_item,
                new_item,
                f"{path}[{index}]",
                issues,
                visited,
            )
        return
    if _canonical(old) != _canonical(new):
        _add_issue(issues, path, "nested schema structure changed")


def _compare_response(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    old: object,
    new: object,
    path: str,
    issues: list[str],
    visited: set[tuple[str, str]],
) -> None:
    """Compare a response envelope, checking media schemas and preserving headers."""
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        if _canonical(old) != _canonical(new):
            _add_issue(issues, path, "response object changed")
        return
    ignored = {"description", "content"}
    for key in sorted((set(old) | set(new)) - ignored):
        if key not in old or key not in new:
            _add_issue(issues, f"{path}.{key}", "response field was added or removed")
        else:
            _compare_exact(
                baseline, candidate, old[key], new[key], f"{path}.{key}", issues, visited
            )
    old_content = old.get("content", {})
    new_content = new.get("content", {})
    if not isinstance(old_content, Mapping) or not isinstance(new_content, Mapping):
        _add_issue(issues, path + ".content", "response content must remain an object")
        return
    if set(old_content) != set(new_content):
        _add_issue(issues, path + ".content", "response media types changed")
    for media_type, old_media in old_content.items():
        new_media = new_content.get(media_type)
        media_path = f"{path}.content.{media_type}"
        if not isinstance(old_media, Mapping) or not isinstance(new_media, Mapping):
            if _canonical(old_media) != _canonical(new_media):
                _add_issue(issues, media_path, "media type contract changed")
            continue
        for key in sorted((set(old_media) | set(new_media)) - {"schema", "description"}):
            if key not in old_media or key not in new_media:
                _add_issue(issues, f"{media_path}.{key}", "media type field was added or removed")
            else:
                _compare_exact(
                    baseline,
                    candidate,
                    old_media[key],
                    new_media[key],
                    f"{media_path}.{key}",
                    issues,
                    visited,
                )
        if "schema" not in old_media or "schema" not in new_media:
            if "schema" in old_media or "schema" in new_media:
                _add_issue(issues, media_path + ".schema", "response schema was added or removed")
        else:
            _compare_schema(
                baseline,
                candidate,
                old_media["schema"],
                new_media["schema"],
                media_path + ".schema",
                issues,
                visited,
            )


def _compare_operation(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    path: str,
    issues: list[str],
    visited: set[tuple[str, str]],
) -> None:
    """Compare an operation while treating docs as annotations and operation IDs as contract."""
    special = {"responses", "summary", "description", "tags"}
    for key in sorted((set(old) | set(new)) - special):
        if key not in old or key not in new:
            _add_issue(issues, f"{path}.{key}", "operation contract field was added or removed")
        elif key == "parameters" and _allowed_coverage_publication_parameter(
            path, old[key], new[key]
        ):
            continue
        else:
            _compare_exact(
                baseline, candidate, old[key], new[key], f"{path}.{key}", issues, visited
            )
    old_responses = old.get("responses")
    new_responses = new.get("responses")
    if not isinstance(old_responses, Mapping) or not isinstance(new_responses, Mapping):
        _add_issue(issues, path + ".responses", "operation responses must remain an object")
        return
    for code, old_response in old_responses.items():
        response_path = f"{path}.responses.{code}"
        if code not in new_responses:
            _add_issue(issues, response_path, "response status was removed")
        else:
            _compare_response(
                baseline,
                candidate,
                old_response,
                new_responses[code],
                response_path,
                issues,
                visited,
            )
    for code in new_responses.keys() - old_responses.keys():
        _add_issue(issues, f"{path}.responses.{code}", "new response status requires review")


def _allowed_coverage_publication_parameter(path: str, old: object, new: object) -> bool:
    """Allow only the optional publication pin on the exact v2 coverage route."""
    if path != "/api/v2/datasets/{dataset_id}/coverage.get":
        return False
    if not isinstance(old, list) or not isinstance(new, list) or len(new) != len(old) + 1:
        return False
    expected = {
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
    additions = [
        item
        for item in new
        if isinstance(item, Mapping)
        and item.get("name") == "publication_id"
        and item.get("in") == "query"
    ]
    if len(additions) != 1 or _canonical(additions[0]) != _canonical(expected):
        return False
    retained = list(new)
    retained.remove(additions[0])
    return _canonical(retained) == _canonical(old)


def compare_contracts(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], surface: str
) -> list[str]:
    """Return breaking or unsupported changes between a frozen and live API surface."""
    try:
        old = extract_surface(baseline, surface)
        new = extract_surface(candidate, surface)
    except ValueError as exc:
        return [f"{surface}: {exc}"]
    issues: list[str] = []
    if old.get("openapi") != new.get("openapi"):
        _add_issue(issues, surface + ".openapi", "OpenAPI version changed")
    visited: set[tuple[str, str]] = set()
    ignored_top_level = {"openapi", "info", "paths", "components", "tags"}
    for key in sorted((set(old) | set(new)) - ignored_top_level):
        if key not in old or key not in new:
            _add_issue(issues, f"{surface}.{key}", "top-level contract field was added or removed")
        else:
            _compare_exact(
                baseline,
                candidate,
                old[key],
                new[key],
                f"{surface}.{key}",
                issues,
                visited,
            )

    old_paths, new_paths = old["paths"], new["paths"]
    for path, old_item in old_paths.items():
        if path not in new_paths:
            _add_issue(issues, path, "published path was removed")
            continue
        new_item = new_paths[path]
        if not isinstance(old_item, Mapping) or not isinstance(new_item, Mapping):
            _add_issue(issues, path, "path item must remain an object")
            continue
        if "$ref" in old_item or "$ref" in new_item:
            _add_issue(issues, path, "Path Item $ref is unsupported and requires explicit review")
        old_methods = {key.lower() for key in old_item if key.lower() in _HTTP_METHODS}
        new_methods = {key.lower() for key in new_item if key.lower() in _HTTP_METHODS}
        for key in sorted((set(old_item) | set(new_item)) - old_methods - new_methods):
            if key not in old_item or key not in new_item:
                _add_issue(issues, f"{path}.{key}", "path-item field was added or removed")
            else:
                _compare_exact(
                    baseline,
                    candidate,
                    old_item[key],
                    new_item[key],
                    f"{path}.{key}",
                    issues,
                    visited,
                )
        for method in sorted(old_methods):
            operation_path = f"{path}.{method}"
            if method not in new_methods:
                _add_issue(issues, operation_path, "published operation was removed")
                continue
            old_operation, new_operation = old_item[method], new_item[method]
            if not isinstance(old_operation, Mapping) or not isinstance(new_operation, Mapping):
                _add_issue(issues, operation_path, "operation must remain an object")
            else:
                _compare_operation(
                    baseline,
                    candidate,
                    old_operation,
                    new_operation,
                    operation_path,
                    issues,
                    visited,
                )

    old_security = old.get("components", {}).get("securitySchemes", {})
    new_security = new.get("components", {}).get("securitySchemes", {})
    if isinstance(old_security, Mapping):
        for name, old_scheme in old_security.items():
            if not isinstance(new_security, Mapping) or name not in new_security:
                _add_issue(
                    issues, f"components.securitySchemes.{name}", "security scheme was removed"
                )
            else:
                _compare_exact(
                    baseline,
                    candidate,
                    old_scheme,
                    new_security[name],
                    f"components.securitySchemes.{name}",
                    issues,
                    visited,
                )
    return sorted(set(issues))


def _load_baseline(surface: str) -> dict[str, Any]:
    """Load a baseline only when its reviewed SHA-256 pin matches."""
    path = BASELINE_PATHS[surface]
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    expected = BASELINE_SHA256[surface]
    if actual != expected:
        raise ValueError(
            f"{path.relative_to(ROOT)} sha256 is {actual}, expected {expected}; "
            "baseline edits require a reviewed pin update"
        )
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return document


def load_live_openapi() -> dict[str, Any]:
    """Import the actual app in offline mode without running lifespan or database calls."""
    os.environ["APP_ENV"] = "testing"
    # main.py checks this flag before configuring log files and other startup
    # integrations; APP_ENV alone does not suppress those import-time effects.
    os.environ["TESTING"] = "true"
    os.environ["REDIS_URL"] = ""
    os.environ["MYSQL_HOST"] = "openapi-check-no-database.invalid"
    os.environ["DATA_MYSQL_HOST"] = "openapi-check-no-database.invalid"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from opendata.main import app

    document = app.openapi()
    if not isinstance(document, dict):
        raise ValueError("FastAPI app.openapi() did not return an object")
    return document


def check_live_contracts() -> dict[str, list[str]]:
    """Compare real app routes with the immutable v1 and v2 contract artifacts."""
    current = load_live_openapi()
    return {
        surface: compare_contracts(_load_baseline(surface), current, surface)
        for surface in ("v1", "v2")
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline compatibility gate and print actionable findings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        findings = check_live_contracts()
        current = load_live_openapi()
        counts = {
            surface: len(extract_surface(current, surface)["paths"]) for surface in ("v1", "v2")
        }
    except (OSError, ValueError) as exc:
        print(f"published OpenAPI gate: ERROR: {exc}", file=sys.stderr)
        return 2
    errors = [(surface, issue) for surface, messages in findings.items() for issue in messages]
    if errors:
        for surface, issue in errors:
            print(f"published OpenAPI gate: {surface}: {issue}", file=sys.stderr)
        print(f"published OpenAPI gate: blocked ({len(errors)} finding(s))", file=sys.stderr)
        return 1
    for surface in ("v1", "v2"):
        print(f"published OpenAPI gate: {surface} compatible ({counts[surface]} live paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
