"""FastAPI application main entry point.

Initializes and configures the FastAPI application with all routes,
middleware, and startup/shutdown events.
"""

import os
import sys
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.base import RequestResponseEndpoint

from opendata.api import api_router
from opendata.api.rate_limit import get_limiter
from opendata.core.config import settings
from opendata.core.database import close_db, init_db
from opendata.middleware.security import SecurityHeadersMiddleware
from opendata.services.scheduler import task_scheduler
from opendata.utils.constants import DEFAULT_SECRET_KEY


def _get_pool_stats(pool: object) -> dict[str, object]:
    """Extract connection pool stats; SQLAlchemy Pool type stubs are incomplete."""
    return {
        "size": getattr(pool, "size", lambda: 0)(),
        "checked_in": getattr(pool, "checkedin", lambda: 0)(),
        "checked_out": getattr(pool, "checkedout", lambda: 0)(),
        "overflow": getattr(pool, "overflow", lambda: 0)(),
        "invalid": getattr(pool, "status", lambda: "")(),
    }


# Check if we're in testing mode
TESTING = os.getenv("TESTING", "false").lower() == "true"

# Configure loguru logging (file rotation + level from settings)
logger.remove()  # Remove default stderr handler

_LOG_FORMAT_PRETTY = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

if settings.log_json and not TESTING:
    logger.add(sys.stderr, level=settings.log_level, serialize=True)
else:
    logger.add(sys.stderr, level=settings.log_level, format=_LOG_FORMAT_PRETTY)

if not TESTING:
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        settings.log_file,
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        level=settings.log_level,
        encoding="utf-8",
        serialize=settings.log_json,
    )

# Initialize Sentry error tracking (if configured)
if settings.sentry_dsn and not TESTING:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            environment=settings.app_env,
            release=settings.app_version,
        )
        logger.info("Sentry error tracking initialized")
    except ImportError:
        logger.warning("SENTRY_DSN configured but sentry-sdk not installed. pip install sentry-sdk")

# Initialize rate limiter (only in production)

limiter = get_limiter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager.

    Handles startup and shutdown events.
    """
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Environment: {settings.app_env}")

    # Security warning
    if settings.secret_key == DEFAULT_SECRET_KEY:
        logger.warning("⚠️  USING DEFAULT SECRET KEY! Change this in production!")

    # Initialize database
    if settings.is_production:
        # In production, rely on alembic migrations (run `alembic upgrade head` before deploy)
        logger.info("Production mode: skipping create_tables (use alembic migrations)")
        # Check for pending migrations
        try:
            from alembic.config import Config as AlembicConfig
            from alembic.runtime.migration import MigrationContext
            from alembic.script import ScriptDirectory
            from sqlalchemy import create_engine

            sync_engine = create_engine(settings.database_url_sync)
            with sync_engine.connect() as conn:
                context = MigrationContext.configure(conn)
                current_rev = context.get_current_revision()
            sync_engine.dispose()

            alembic_cfg = AlembicConfig("alembic.ini")
            script_dir = ScriptDirectory.from_config(alembic_cfg)
            head_rev = script_dir.get_current_head()

            if current_rev != head_rev:
                logger.warning(
                    f"⚠️  Database migration is NOT up to date! "
                    f"Current: {current_rev}, Head: {head_rev}. "
                    f"Run 'alembic upgrade head' before starting."
                )
            else:
                logger.info(f"Database migration is up to date (rev: {current_rev})")
        except Exception as e:
            logger.debug(f"Could not check alembic migration status: {e}")
    else:
        from opendata.core.database import create_tables

        await create_tables()

    await init_db()

    # Register provider capabilities (A2.4): P0 domain fetchers enter
    # the registry unverified (FR-3: excluded from source="auto"
    # routing until cross-validation) so the capabilities API and the
    # registry-mode interface scan can see them.
    from opendata.data.providers import register_providers

    registered = register_providers()
    if registered:
        logger.info(f"Registered {len(registered)} provider capabilities")

    # Start task scheduler (only safe in single-worker mode or with external job store)
    if settings.workers > 1 and not settings.redis_url:
        logger.warning(
            f"⚠️  Scheduler DISABLED: running {settings.workers} workers without Redis. "
            "APScheduler in-memory store would cause duplicate task execution. "
            "Set WORKERS=1 or configure REDIS_URL."
        )
    else:
        await task_scheduler.start()

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down application...")
    await task_scheduler.shutdown()

    # Shutdown the akshare thread pool executor
    from opendata.services.data_acquisition import DataAcquisitionService

    DataAcquisitionService._akshare_executor.shutdown(wait=False)

    await close_db()
    logger.info("Application shutdown complete")


# OpenAPI tag metadata
openapi_tags = [
    {"name": "Authentication", "description": "用户注册、登录、令牌管理"},
    {"name": "Data Interfaces", "description": "akshare 数据接口浏览与管理"},
    {"name": "Scheduled Tasks", "description": "定时数据采集任务的增删改查与触发"},
    {"name": "Data Acquisition", "description": "手动触发数据下载与进度追踪"},
    {"name": "Data Tables", "description": "数据表浏览、查询、导出与删除"},
    {"name": "User Management", "description": "用户列表、编辑、角色管理（管理员）"},
    {"name": "Data Scripts", "description": "数据脚本注册、扫描与状态管理"},
    {"name": "Task Executions", "description": "任务执行记录查询与统计"},
    {"name": "Settings", "description": "数据库连接等系统设置（管理员）"},
    {"name": "WebSocket", "description": "实时任务执行状态推送"},
]

# Create FastAPI application
app = FastAPI(
    title=f"{settings.app_name} API v1",
    description=(
        "Web-based financial data management platform for akshare.\n\n"
        "**Base URL**: `/api/v1`  \n"
        "The legacy `/api` prefix is deprecated and will be removed after 2026-06-01."
    ),
    version=settings.app_version,
    docs_url="/docs" if settings.app_debug else None,
    redoc_url="/redoc" if settings.app_debug else None,
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

# Attach rate limiter to app state (required by slowapi)
if limiter is not None:
    app.state.limiter = limiter

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# Request logging middleware (skip in testing to reduce noise)
if not TESTING:
    from opendata.middleware.request_logging import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)

# Security headers middleware (applied on all requests)
app.add_middleware(SecurityHeadersMiddleware)

# Rate limit exception handler (only in production)
if not TESTING:
    from slowapi.errors import RateLimitExceeded

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        """Handle rate limit exceeded exceptions."""
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please try again later."},
        )


# Global exception handlers for structured error responses
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Handle ValueError with structured response."""
    logger.warning(f"ValueError: {exc}")
    return JSONResponse(
        status_code=400,
        content={"success": False, "message": str(exc), "error_code": "VALUE_ERROR"},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle uncaught exceptions with structured response."""
    logger.error(
        "Unhandled exception: {}: {}\n{}",
        type(exc).__name__,
        exc,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error",
            "error_code": "INTERNAL_ERROR",
        },
    )


# Include API routes (v1 is the canonical prefix)
app.include_router(api_router, prefix="/api/v1")
app.include_router(api_router, prefix="/api")


@app.middleware("http")
async def deprecation_header_middleware(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    """Add deprecation header for requests using /api/ without version prefix."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/v"):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = "2026-06-01"
        response.headers["Link"] = '</api/v1>; rel="successor-version"'
    return response


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint.

    Returns application health status.
    """
    from opendata.core.database import check_db_connection, data_engine, engine

    db_healthy = await check_db_connection()
    scheduler_running = task_scheduler.is_running

    # Determine overall status
    if db_healthy and scheduler_running:
        overall_status = "healthy"
    elif db_healthy:
        overall_status = "degraded"  # Scheduler not running
    else:
        overall_status = "unhealthy"

    # Connection pool stats (helps detect pool exhaustion)
    pool_status: dict[str, dict[str, object]] = {}
    for label, eng in [("main", engine), ("data", data_engine)]:
        pool_status[label] = _get_pool_stats(eng.pool)

    return {
        "status": overall_status,
        "app_name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "checks": {
            "database": "connected" if db_healthy else "disconnected",
            "scheduler": "running" if scheduler_running else "stopped",
        },
        "pool": pool_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Serve Vue frontend static files
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_DIR.is_dir():
    # Mount static assets directory
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static-assets")

    @app.get("/")
    async def serve_index() -> FileResponse:
        """Serve Vue SPA index page."""
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.exception_handler(404)
    async def spa_not_found(request: Request, exc: Exception) -> Response:
        """Serve index.html for SPA client-side routing on 404."""
        # Only serve SPA for non-API paths
        path = request.url.path
        if path.startswith(("/api/", "/docs", "/redoc", "/openapi", "/health")):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        # Try to serve static file first
        file_path = FRONTEND_DIR / path.lstrip("/")
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
else:

    @app.get("/")
    async def root() -> dict:
        """Root endpoint with basic information."""
        return {
            "app_name": settings.app_name,
            "version": settings.app_version,
            "description": "Web-based financial data management platform for akshare",
            "docs": "/docs" if settings.app_debug else None,
        }
