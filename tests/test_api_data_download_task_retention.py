"""The download task scheduled by POST /api/data/download stays reachable.

``trigger_download`` answers 202 and hands the work to ``asyncio.create_task``.
The event loop keeps only a *weak* reference to a scheduled task, so the task
dies with the caller's frame unless the route module holds it: five accepted
downloads would then be visible to the operator, each with its ``TaskExecution``
row, while none of them ever reaches the source seam. That is exactly the
symptom of an empty ``source_calls`` in the frontend-e2e leg.

The recorder below stands where the download work happens (the service call that
reaches the source adapter one layer below, the same seam the frontend-e2e test
stubs), and it holds the seam open on purpose so the pending task can be
inspected while nothing but the route module can see it. These tests run on
sqlite and need neither a browser nor a database container.
"""

import asyncio
import gc
from typing import Any
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api import data as data_module
from opendata.models.interface import DataInterface, InterfaceCategory

_DOWNLOAD_SEAM = "opendata.api.data.data_service.execute_download"


def _in_flight() -> dict:
    """Whatever the route module uses to hold its scheduled downloads.

    Read through ``getattr`` with a default so that a route which retains
    nothing fails the assertion below with its stated reason, instead of
    crashing this module on import.
    """
    held = getattr(data_module, "_DOWNLOAD_TASKS", None)
    return {} if held is None else held


async def _wait_until(predicate, *, attempts: int = 400, interval: float = 0.005) -> bool:
    """Yield to the loop until ``predicate()`` holds, bounded by ``attempts``.

    Returns:
        Whether the predicate held within the bound (2s at defaults).
    """
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _create_interface(db: AsyncSession, name: str) -> DataInterface:
    """Create one active interface together with its category."""
    category = InterfaceCategory(name=f"cat_{name}", description="Test", sort_order=99)
    db.add(category)
    await db.flush()
    iface = DataInterface(
        name=name,
        display_name=f"Retention {name}",
        category_id=category.id,
        parameters={},
        is_active=True,
    )
    db.add(iface)
    await db.commit()
    await db.refresh(iface)
    return iface


async def _post_download(client, token: str, interface_id: int) -> int:
    """Accept one download and return its execution id."""
    response = await client.post(
        "/api/data/download",
        headers={"Authorization": f"Bearer {token}"},
        json={"interface_id": interface_id, "parameters": {}},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return int(body["execution_id"])


def _collect(*, rounds: int = 3) -> None:
    """Force full collections, the way an interpreter under pressure would."""
    for _ in range(rounds):
        gc.collect()


async def test_pending_download_is_reachable_only_through_the_route_module(
    test_client, test_user_token: str, test_db: AsyncSession
):
    """A download still in flight is held by the module, then released.

    The response has been returned, so no caller frame references the task any
    more, and a full collection runs before the download is allowed to finish.
    The registry has to keep it alive across that collection (the fix), and the
    task still has to arrive at the seam it was scheduled for. Release is
    asserted too: a reference that outlives its task is an unbounded leak.
    """
    iface = await _create_interface(test_db, "retention_pending")
    release = asyncio.Event()
    reached: list[int] = []

    async def _recorder(**kwargs: Any) -> None:
        reached.append(kwargs["interface_id"])
        await release.wait()

    with patch(_DOWNLOAD_SEAM, _recorder):
        execution_id = await _post_download(test_client, test_user_token, iface.id)

        _collect()

        # No local task reference exists at this point: the registry, or nothing.
        assert execution_id in _in_flight(), (
            "the route returned 202 but keeps no reference to the task it scheduled; the "
            "event loop holds only a weak reference, so this download can be collected "
            "before it ever reaches the source seam"
        )
        assert not _in_flight()[execution_id].done()

        assert await _wait_until(lambda: len(reached) == 1), f"reached only {reached}"
        assert execution_id in _in_flight(), "a running task lost its reference"

        release.set()
        assert await _wait_until(lambda: execution_id not in _in_flight())
        assert reached == [iface.id], "the download did not arrive at the seam intact"


async def test_accepted_downloads_all_reach_the_source_after_collection(
    test_client, test_user_token: str, test_db: AsyncSession
):
    """Five acceptances become five source calls, even with a collection between.

    This is the reading the frontend-e2e leg asserts (every accepted interface
    reaching the source seam) reproduced without a browser: the downloads are
    accepted, every reference the request held is dropped, the collector runs,
    and each task must still arrive.
    """
    interface_ids = [
        (await _create_interface(test_db, f"retention_batch_{index}")).id for index in range(5)
    ]
    hold = asyncio.Event()
    reached: list[int] = []

    async def _recorder(**kwargs: Any) -> None:
        # Append, then wait: the task is at the seam and still pending, which is
        # the state a collector can only reach through the route's reference.
        reached.append(kwargs["interface_id"])
        await hold.wait()

    with patch(_DOWNLOAD_SEAM, _recorder):
        execution_ids = [
            await _post_download(test_client, test_user_token, interface_id)
            for interface_id in interface_ids
        ]

        _collect(rounds=5)

        in_flight = _in_flight()
        assert await _wait_until(lambda: len(reached) == 5), f"only reached {reached}"
        assert sorted(reached) == sorted(interface_ids)
        assert set(execution_ids) <= set(in_flight), (
            f"scheduled {len(execution_ids)} downloads, kept "
            f"{len(set(execution_ids) & set(in_flight))} referenced while in flight"
        )

        hold.set()
        assert await _wait_until(lambda: not (set(execution_ids) & set(_in_flight())))
