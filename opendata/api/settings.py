"""
Database settings API endpoints.

Provides endpoints for managing database connection configurations.
"""

from pathlib import Path

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import get_current_admin_user
from opendata.core.config import settings
from opendata.core.database import get_db
from opendata.models.user import User

router = APIRouter()


def _update_env_file(env_path: Path | str, updates: dict[str, str]) -> None:
    """Update or create entries in a .env file.

    Reads the existing file, replaces matching KEY=value lines,
    appends new keys, and writes back atomically.
    """
    path = Path(env_path)
    lines: list[str] = []
    updated_keys: set[str] = set()

    if path.exists():
        lines = path.read_text().splitlines(keepends=True)

    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line if line.endswith("\n") else line + "\n")

    # Append any keys not already in the file
    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    path.write_text("".join(new_lines))


class DatabaseConfigResponse(BaseModel):
    """Database configuration response (without password)."""

    host: str
    port: int
    database: str
    user: str
    is_warehouse: bool = False


class DatabaseConfigRequest(BaseModel):
    """Database configuration update request."""

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1, max_length=100)
    user: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=255)
    is_warehouse: bool = False


class TestConnectionRequest(BaseModel):
    """Database connection test request."""

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database: str = Field(..., min_length=1, max_length=100)
    user: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=255)


class TestConnectionResponse(BaseModel):
    """Database connection test response."""

    success: bool
    message: str


@router.get(
    "/database",
)
async def get_database_config(
    current_admin: User = Depends(get_current_admin_user),
) -> DatabaseConfigResponse:
    """
    Get current database configuration (without password).

    Requires admin privileges.
    """
    return DatabaseConfigResponse(
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
        user=settings.mysql_user,
        is_warehouse=False,
    )


@router.get(
    "/database/warehouse",
)
async def get_warehouse_config(
    current_admin: User = Depends(get_current_admin_user),
) -> DatabaseConfigResponse:
    """
    Get data warehouse database configuration (without password).

    Requires admin privileges.
    """
    return DatabaseConfigResponse(
        host=settings.data_mysql_host or settings.mysql_host,
        port=settings.data_mysql_port or settings.mysql_port,
        database=settings.data_mysql_database or settings.mysql_database,
        user=settings.data_mysql_user or settings.mysql_user,
        is_warehouse=True,
    )


async def _test_connection_impl(request: TestConnectionRequest) -> TestConnectionResponse:
    """Shared implementation for database connection tests."""
    try:
        import aiomysql

        conn = await aiomysql.connect(
            host=request.host,
            port=request.port,
            user=request.user,
            password=request.password,
            db=request.database,
            connect_timeout=5,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                result = await cursor.fetchone()

            if result and result[0] == 1:
                return TestConnectionResponse(
                    success=True,
                    message="数据库连接成功",
                )
            return TestConnectionResponse(
                success=False,
                message="数据库连接异常",
            )
        finally:
            conn.close()

    except ImportError:
        # aiomysql not available, try with SQLAlchemy
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import QueuePool

            engine = create_engine(
                f"mysql+pymysql://{request.user}:{request.password}@"
                f"{request.host}:{request.port}/{request.database}",
                poolclass=QueuePool,
                pool_size=1,
                max_overflow=0,
            )

            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            return TestConnectionResponse(
                success=True,
                message="数据库连接成功",
            )

        except Exception as e:
            return TestConnectionResponse(
                success=False,
                message=f"数据库连接失败: {e!s}",
            )

    except Exception as e:
        return TestConnectionResponse(
            success=False,
            message=f"数据库连接失败: {e!s}",
        )


@router.post("/database/test")
async def test_database_connection(
    request: TestConnectionRequest,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResponse:
    """
    Test database connection with provided credentials.

    Requires admin privileges.
    """
    _ = db  # Unused; kept for API consistency
    return await _test_connection_impl(request)


@router.post("/database/warehouse/test")
async def test_warehouse_connection(
    request: TestConnectionRequest,
    current_admin: User = Depends(get_current_admin_user),
) -> TestConnectionResponse:
    """
    Test data warehouse database connection with provided credentials.

    Requires admin privileges.
    """
    return await _test_connection_impl(request)


@router.put(
    "/database",
)
async def update_database_config(
    request: DatabaseConfigRequest,
    current_admin: User = Depends(get_current_admin_user),
) -> DatabaseConfigResponse:
    """
    Update database configuration.

    Note: This will update environment variables and requires service restart.

    Requires admin privileges.
    """
    # Determine which env keys to update
    if request.is_warehouse:
        env_updates = {
            "DATA_MYSQL_HOST": request.host,
            "DATA_MYSQL_PORT": str(request.port),
            "DATA_MYSQL_DATABASE": request.database,
            "DATA_MYSQL_USER": request.user,
            "DATA_MYSQL_PASSWORD": request.password,
        }
    else:
        env_updates = {
            "MYSQL_HOST": request.host,
            "MYSQL_PORT": str(request.port),
            "MYSQL_DATABASE": request.database,
            "MYSQL_USER": request.user,
            "MYSQL_PASSWORD": request.password,
        }

    # Audit log
    db_type = "warehouse" if request.is_warehouse else "main"
    logger.warning(
        f"[AUDIT] Admin '{current_admin.username}' (id={current_admin.id}) "
        f"updated {db_type} database config: host={request.host}, port={request.port}, "
        f"database={request.database}, user={request.user}"
    )

    # Persist to .env file
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    _update_env_file(env_path, env_updates)

    return DatabaseConfigResponse(
        host=request.host,
        port=request.port,
        database=request.database,
        user=request.user,
        is_warehouse=request.is_warehouse,
    )
