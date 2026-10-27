"""Offline negative vectors for reviewed-runtime dependency validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event

from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
from opendata.pipeline.runtime_roles import (
    RuntimeRole,
    RuntimeRolePlan,
    build_runtime_role_plan,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


def _plan(*roles: RuntimeRole) -> RuntimeRolePlan:
    """Build a reviewed plan with a fixed test-only sink declaration."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles=roles,
        reviewed_config_id="tests.reviewed-runtime-boundaries",
        reviewed_sink_ids=("tests.reviewed-runtime-boundaries",),
    )


def _runtime(engine: Engine, *roles: RuntimeRole) -> ReviewedRuntime:
    """Compose one runtime without opening its owned in-memory Engine."""
    return build_reviewed_runtime(
        metadata_engine=engine,
        catalog=ReviewedTaskCatalog([]),
        runtime_plan=_plan(*roles),
    )


@pytest.fixture
def owned_engine() -> Iterator[tuple[Engine, list[bool]]]:
    """Track connection attempts and always dispose this test-owned Engine."""
    engine = create_engine("sqlite://")
    connection_events: list[bool] = []
    event.listen(engine, "connect", lambda *_args: connection_events.append(True))
    try:
        yield engine, connection_events
    finally:
        engine.dispose()


def test_factory_checks_role_plan_type_before_other_dependencies(
    owned_engine: tuple[Engine, list[bool]],
) -> None:
    """Reject malformed plans before attempting to inspect caller dependencies."""
    _engine, connection_events = owned_engine
    with pytest.raises(TypeError, match="runtime_plan must be a RuntimeRolePlan"):
        build_reviewed_runtime(
            metadata_engine=object(),
            catalog=object(),
            runtime_plan=object(),
        )
    assert connection_events == []


@pytest.mark.parametrize(
    ("dependency", "invalid", "message"),
    [
        ("metadata_engine", object(), "metadata_engine must be a synchronous"),
        ("catalog", object(), "catalog must be a ReviewedTaskCatalog"),
    ],
)
def test_factory_rejects_unsupported_dependency_types_without_connecting(
    owned_engine: tuple[Engine, list[bool]],
    dependency: str,
    invalid: object,
    message: str,
) -> None:
    """Check concrete dependency types without opening the borrowed Engine."""
    engine, connection_events = owned_engine
    arguments: dict[str, object] = {
        "metadata_engine": engine,
        "catalog": ReviewedTaskCatalog([]),
        "runtime_plan": _plan("api"),
    }
    arguments[dependency] = invalid

    with pytest.raises(TypeError, match=message):
        build_reviewed_runtime(**arguments)
    assert connection_events == []


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        ("runtime_plan", object(), TypeError, "runtime_plan must be a RuntimeRolePlan"),
        ("_lifecycle", object(), TypeError, "runtime lifecycle state is invalid"),
        ("catalog", object(), TypeError, "catalog must be a ReviewedTaskCatalog"),
        ("dispatcher", object(), TypeError, "dispatcher must be a RunDispatcher"),
        ("ledger", object(), TypeError, "ledger must be a RunLedger"),
    ],
)
def test_public_runtime_post_init_rejects_invalid_dependency_types(
    owned_engine: tuple[Engine, list[bool]],
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    """Reject manually assembled runtime values that violate public field types."""
    engine, connection_events = owned_engine
    runtime = _runtime(engine, "api")
    with pytest.raises(error_type, match=message):
        replace(runtime, **{field: value})
    assert connection_events == []


def test_public_runtime_post_init_rejects_a_nonreviewed_role_plan(
    owned_engine: tuple[Engine, list[bool]],
) -> None:
    """Require the reviewed profile even when all runtime dependencies are valid."""
    engine, connection_events = owned_engine
    runtime = _runtime(engine, "api")
    legacy_plan = build_runtime_role_plan()

    with pytest.raises(ValueError, match="explicit reviewed role plan"):
        replace(runtime, runtime_plan=legacy_plan)
    assert connection_events == []


def test_public_runtime_post_init_rejects_a_lifecycle_from_another_plan(
    owned_engine: tuple[Engine, list[bool]],
) -> None:
    """Keep lifecycle state tied to the exact reviewed plan exposed by a runtime."""
    engine, connection_events = owned_engine
    api_runtime = _runtime(engine, "api")
    scheduler_runtime = _runtime(engine, "scheduler")

    with pytest.raises(ValueError, match="lifecycle plan must match"):
        replace(api_runtime, _lifecycle=scheduler_runtime._lifecycle)
    assert connection_events == []


def test_public_runtime_post_init_rejects_dispatcher_from_another_lifecycle(
    owned_engine: tuple[Engine, list[bool]],
) -> None:
    """Prevent a valid dispatcher from bypassing this runtime's role and close state."""
    engine, connection_events = owned_engine
    api_runtime = _runtime(engine, "api")
    scheduler_runtime = _runtime(engine, "scheduler")

    with pytest.raises(ValueError, match="dispatcher lifecycle must match"):
        replace(api_runtime, dispatcher=scheduler_runtime.dispatcher)
    assert connection_events == []


def test_worker_role_rejects_a_non_engine_before_binding_a_worker(
    owned_engine: tuple[Engine, list[bool]],
) -> None:
    """Require a real Engine before the worker constructor or DB listeners run."""
    engine, connection_events = owned_engine
    runtime = _runtime(engine, "worker")

    with pytest.raises(TypeError, match="worker_engine must be a verified SQLAlchemy Engine"):
        runtime.to_cli_application(
            worker_engine=object(),
            worker_id="boundary-worker",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.1,
        )
    assert connection_events == []
