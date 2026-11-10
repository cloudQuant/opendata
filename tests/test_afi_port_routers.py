"""Assembly, auth and contract tests for the ported /afi-data routers.

These tests pin the things a port must not silently change: the additive
namespace, the auth boundary (read = any user, write = admin), and the AFI
``{items,total,page,page_size}`` pagination envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_afi_data_routes_are_namespaced() -> None:
    """Every ported route lives under the additive /afi-data namespace."""
    from opendata.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    # Normalise trailing slashes so the assertion does not depend on each
    # router's redirect_slashes style.
    normalised = {p.rstrip("/") for p in paths}
    assert "/api/v1/afi-data/scripts" in normalised
    assert "/api/v1/afi-data/tasks" in normalised
    assert "/api/v1/afi-data/data-governance/providers" in normalised
    # The ported plane is fully namespaced: every afi-data route carries the
    # prefix, and OpenData's own pre-existing routes still exist alongside it.
    assert all("/afi-data/" in p for p in paths if "afi-data" in p)
    assert "/api/v1/scripts" in normalised
    assert "/api/v1/tasks" in normalised


async def test_read_routes_require_authentication(test_client: AsyncClient) -> None:
    """Unauthenticated requests to the ported plane are rejected with 401."""
    for path in (
        "/api/v1/afi-data/scripts",
        "/api/v1/afi-data/tables",
        "/api/v1/afi-data/executions",
        "/api/v1/afi-data/tasks/schedule/templates",
    ):
        response = await test_client.get(path)
        assert response.status_code == 401, path


async def test_scripts_list_keeps_afi_envelope(
    test_client: AsyncClient, test_user_token: str
) -> None:
    """The list endpoint returns the AFI pagination envelope, not the OD one."""
    response = await test_client.get(
        "/api/v1/afi-data/scripts",
        headers={"Authorization": f"Bearer {test_user_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert isinstance(body["items"], list)


async def test_execution_stats_and_templates_readable(
    test_client: AsyncClient, test_user_token: str
) -> None:
    """Read-only stats/templates are available to any authenticated user."""
    headers = {"Authorization": f"Bearer {test_user_token}"}

    stats = await test_client.get("/api/v1/afi-data/executions/stats", headers=headers)
    assert stats.status_code == 200
    assert "total_count" in stats.json()

    templates = await test_client.get("/api/v1/afi-data/tasks/schedule/templates", headers=headers)
    assert templates.status_code == 200
    assert len(templates.json()["templates"]) == 6


async def test_admin_write_routes_reject_non_admin(
    test_client: AsyncClient, test_user_token: str
) -> None:
    """A non-admin caller cannot reach the write/admin-only routes."""
    headers = {"Authorization": f"Bearer {test_user_token}"}
    write_calls = [
        ("POST", "/api/v1/afi-data/scripts/scan", None),
        ("POST", "/api/v1/afi-data/data-governance/bootstrap", None),
        (
            "POST",
            "/api/v1/afi-data/tasks",
            {"script_id": "missing", "schedule_expression": "0 8 * * *"},
        ),
    ]
    for method, path, payload in write_calls:
        response = await test_client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 403, (method, path, response.status_code)


async def test_admin_can_reach_governance_read(
    test_client: AsyncClient, test_admin_token: str
) -> None:
    """An admin caller passes the governance router-level dependency."""
    response = await test_client.get(
        "/api/v1/afi-data/data-governance/providers",
        headers={"Authorization": f"Bearer {test_admin_token}"},
    )
    assert response.status_code == 200


async def test_create_task_unknown_script_is_a_400(
    test_client: AsyncClient, test_admin_token: str
) -> None:
    """The ported task service maps a missing script to a 400, not a 500."""
    response = await test_client.post(
        "/api/v1/afi-data/tasks",
        headers={"Authorization": f"Bearer {test_admin_token}"},
        json={
            "script_id": "no_such_script_xyz",
            "name": "x",
            "schedule_type": "daily",
            "schedule_expression": "0 8 * * *",
        },
    )
    assert response.status_code == 400
