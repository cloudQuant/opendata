"""Offline guard tests for the isolated-stack verifier."""

from __future__ import annotations

import json
import stat
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from scripts.ops import verify_isolated_stack as verifier


def _inspect_document() -> list[dict[str, Any]]:
    binding = {"HostIp": "127.0.0.1", "HostPort": "33566"}
    return [
        {
            "Name": "/opendata-iteration01-c65-app",
            "Image": f"sha256:{'a' * 64}",
            "Config": {
                "Image": "opendata-iteration01:c65-current",
                "Labels": {
                    "codex.task": "opendata-c65",
                    "codex.source": "b" * 64,
                },
                "Env": [
                    "MYSQL_HOST=host.docker.internal",
                    "MYSQL_PORT=33565",
                    "MYSQL_DATABASE=opendata",
                    "DATA_MYSQL_HOST=host.docker.internal",
                    "DATA_MYSQL_PORT=33565",
                    "DATA_MYSQL_DATABASE=opendata_data",
                ],
            },
            "HostConfig": {"PortBindings": {"8000/tcp": [binding]}},
            "NetworkSettings": {
                "Ports": {"8000/tcp": [binding]},
                "Networks": {"opendata_c65": {"Aliases": ["opendata-iteration01-c65-app"]}},
            },
        }
    ]


def _set_env(document: list[dict[str, Any]], key: str, value: str) -> None:
    environment = document[0]["Config"]["Env"]
    document[0]["Config"]["Env"] = [
        f"{key}={value}" if entry.startswith(f"{key}=") else entry for entry in environment
    ]


def _set_nested(document: list[dict[str, Any]], path: tuple[str, ...], value: Any) -> None:
    target: Any = document[0]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


def test_owned_inspect_proves_image_source_ports_and_database_targets() -> None:
    identity = verifier.validate_docker_inspect(_inspect_document())

    assert identity.image_id == f"sha256:{'a' * 64}"
    assert identity.source_identity == "b" * 64


def test_owned_container_network_alias_is_allowed_for_both_database_hosts() -> None:
    document = _inspect_document()
    _set_env(document, "MYSQL_HOST", "opendata-iteration01-c65")
    _set_env(document, "DATA_MYSQL_HOST", "opendata-iteration01-c65")

    assert verifier.validate_docker_inspect(document).source_identity == "b" * 64


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (
            lambda doc: _set_env(doc, "MYSQL_HOST", "database.example.net"),
            "database_host_not_owned",
        ),
        (lambda doc: _set_env(doc, "DATA_MYSQL_PORT", "3306"), "database_port_not_owned"),
        (lambda doc: _set_env(doc, "MYSQL_DATABASE", "production"), "database_schema_not_isolated"),
        (
            lambda doc: _set_env(doc, "DATA_MYSQL_DATABASE", "opendata"),
            "database_schema_not_isolated",
        ),
        (
            lambda doc: _set_nested(doc, ("Config", "Labels", "codex.task"), "another-task"),
            "task_label_not_owned",
        ),
        (lambda doc: _set_nested(doc, ("Image",), ""), "image_id_invalid"),
        (
            lambda doc: _set_nested(
                doc,
                ("NetworkSettings", "Ports"),
                {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "33566"}],
                    "3306/tcp": None,
                },
            ),
            "published_ports_not_isolated",
        ),
        (
            lambda doc: _set_nested(
                doc,
                ("HostConfig", "PortBindings"),
                {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33566"}]},
            ),
            "published_port_not_loopback_owned",
        ),
        (
            lambda doc: _set_nested(
                doc,
                ("NetworkSettings", "Ports"),
                {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "33567"}]},
            ),
            "published_port_not_loopback_owned",
        ),
    ],
)
def test_rejects_non_owned_container_identity_and_targets(mutate: Any, error_type: str) -> None:
    document = deepcopy(_inspect_document())
    mutate(document)

    with pytest.raises(verifier.VerificationError) as exc_info:
        verifier.validate_docker_inspect(document)

    assert exc_info.value.error_type == error_type


def test_rejects_missing_image_source_identity_and_extra_environment() -> None:
    missing_image = deepcopy(_inspect_document())
    missing_image[0]["Config"].pop("Image")
    with pytest.raises(verifier.VerificationError, match="image_reference_not_owned"):
        verifier.validate_docker_inspect(missing_image)

    missing_source = deepcopy(_inspect_document())
    missing_source[0]["Config"]["Labels"].pop("codex.source")
    with pytest.raises(verifier.VerificationError, match="source_identity_missing"):
        verifier.validate_docker_inspect(missing_source)

    duplicate_database_host = deepcopy(_inspect_document())
    duplicate_database_host[0]["Config"]["Env"].append("MYSQL_HOST=host.docker.internal")
    with pytest.raises(verifier.VerificationError, match="database_environment_invalid"):
        verifier.validate_docker_inspect(duplicate_database_host)


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/app.js",
        "//example.invalid/app.js",
        "/assets/../secret.js",
        "/assets/app.js?redirect=https://example.invalid",
    ],
)
def test_static_asset_guard_rejects_nonlocal_references(reference: str) -> None:
    with pytest.raises(verifier.VerificationError, match="static_asset_reference_not_local"):
        verifier._asset_path(reference)


def test_default_cli_is_dry_run_and_does_not_inspect_or_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_apply() -> None:
        raise AssertionError("dry-run must not inspect Docker or connect to HTTP")

    monkeypatch.setattr(verifier, "_apply_verification", unexpected_apply)

    assert verifier.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["endpoint"] == "http://127.0.0.1:33566"
    assert report["userdb_write_isolated"] is False
    assert report["error"] is None


def test_report_writer_creates_a_new_private_file(tmp_path: Any) -> None:
    report_path = tmp_path / "isolated-stack.json"
    report = verifier._base_report("dry-run")

    verifier._write_report(report_path, report)

    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_generated_registration_user_matches_auth_contract_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.routing import APIRoute

    from opendata.api.auth import router as auth_router
    from opendata.api.schemas import APIResponse, RegisterRequest

    user = verifier._new_test_user()
    payload = {
        "email": user["email"],
        "password": user["password"],
        "password_confirm": user["password"],
    }
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**payload, "email": f"{user['username']}@example.invalid"})

    request_model = RegisterRequest.model_validate(payload)
    assert str(request_model.email).endswith("@example.org")

    registration_route = next(
        route
        for route in auth_router.routes
        if isinstance(route, APIRoute) and route.path == "/register"
    )
    assert registration_route.status_code == 201
    assert registration_route.response_model is APIResponse

    response = APIResponse(
        success=True,
        message="registered",
        data={
            "user_id": 1,
            "email": user["email"],
            "access_token": "fixture-access-token",
            "refresh_token": "fixture-refresh-token",
        },
    )
    calls: list[tuple[str, str]] = []

    def fake_http_request(
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes]:
        del headers
        assert body is not None
        received = json.loads(body)
        validated = RegisterRequest.model_validate(received)
        assert str(validated.email).endswith("@example.org")
        calls.append((path, method))
        return 201, "application/json", response.model_dump_json().encode("utf-8")

    monkeypatch.setattr(verifier, "_http_request", fake_http_request)

    verifier._register_user(user)

    assert calls == [("/api/v1/auth/register", "POST")]
