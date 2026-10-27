"""Explicit reviewed FastAPI composition root.

The factory accepts a prebuilt reviewed runtime and caller-owned authentication
session factory. It does not load a dotenv file itself, resolve a database URL,
create tables, or start workers or schedulers. Existing authentication routes
still depend on legacy settings for JWT and rate-limit behavior; importing them
may load the repository dotenv configuration outside testing mode. Importing
``opendata.api`` also runs its current package initializer, which imports legacy
route modules and may construct legacy default Engine objects. This factory
mounts only the selected auth and reviewed routers, never imports
``opendata.main``, and does not connect those default Engines or initialize
their schemas itself.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opendata.pipeline.reviewed_runtime import ReviewedRuntime


def create_reviewed_app(
    runtime: ReviewedRuntime,
    *,
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    """Create the reviewed API app around explicit, caller-owned dependencies.

    The app serves only the existing authentication routes and the reviewed
    run-control router. Authentication sessions come exclusively from the
    supplied session factory. The caller retains ownership of that factory,
    its engine, the reviewed runtime, and every worker lifecycle.

    The existing authentication dependency chain still uses legacy global
    settings for JWT and rate-limit configuration. This composition root does
    not explicitly load settings or dotenv files, but the imported auth stack
    may load them as part of its existing module initialization. Importing the
    API package also runs its current initializer for legacy route modules;
    only the selected routers are mounted here, and this factory does not
    explicitly connect a default Engine or bootstrap a schema.

    Args:
        runtime: Exact reviewed runtime built by the reviewed composition root.
        auth_session_factory: Caller-owned SQLAlchemy async session factory
            for authentication tables.

    Returns:
        A FastAPI instance with only the reviewed API route surface.

    Raises:
        TypeError: If the runtime or an explicit dependency has the wrong type.
        ValueError: If the runtime is closed, is not in reviewed mode, or does
            not enable the API role.
    """
    from opendata.pipeline.reviewed_runtime import ReviewedRuntime
    from opendata.pipeline.runtime_roles import RuntimeRolePlan

    if type(runtime) is not ReviewedRuntime:
        raise TypeError("runtime must be an exact ReviewedRuntime")
    if runtime.closed:
        raise ValueError("reviewed runtime is closed")
    if not isinstance(runtime.runtime_plan, RuntimeRolePlan):
        raise TypeError("runtime plan must be a RuntimeRolePlan")
    if runtime.runtime_plan.profile != "reviewed":
        raise ValueError("reviewed API requires the reviewed runtime profile")
    if not runtime.runtime_plan.enables("api"):
        raise ValueError("reviewed API role is not enabled")
    if not isinstance(auth_session_factory, async_sessionmaker):
        raise TypeError("auth_session_factory must be an async_sessionmaker")

    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
    from opendata.pipeline.run_ledger import RunLedger

    if not isinstance(runtime.catalog, ReviewedTaskCatalog):
        raise TypeError("reviewed runtime catalog is invalid")
    if not isinstance(runtime.dispatcher, RunDispatcher):
        raise TypeError("reviewed runtime dispatcher is invalid")
    if not isinstance(runtime.ledger, RunLedger):
        raise TypeError("reviewed runtime ledger is invalid")

    from opendata.api import dependencies
    from opendata.api.auth import router as auth_router
    from opendata.api.reviewed_runs import router as reviewed_runs_router

    async def reviewed_auth_db() -> AsyncIterator[AsyncSession]:
        """Yield one session from the caller-owned authentication factory."""
        session = auth_session_factory()
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Bind the borrowed runtime for service and clear app state on exit."""
        if runtime.closed:
            raise RuntimeError("reviewed runtime is closed")
        application.state.reviewed_runtime = runtime
        try:
            yield
        finally:
            application.state.reviewed_runtime = None

    app = FastAPI(
        title="OpenData Reviewed API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.reviewed_runtime = runtime
    app.dependency_overrides[dependencies.get_db] = reviewed_auth_db
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(reviewed_runs_router, prefix="/api/v1", tags=["reviewed-runs"])
    return app


__all__ = ["create_reviewed_app"]
