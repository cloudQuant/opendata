"""Database connection and session management.

Provides async database session management using SQLAlchemy.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from opendata.core.config import settings

# Create metadata with extend_existing for development convenience
metadata = MetaData()


class Base(DeclarativeBase):
    """Base class for all database models."""

    metadata = metadata

    # Allow table redefinition for development (SQLAlchemy expects this dict)
    __table_args__ = {
        "extend_existing": True,
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }


# Connection reuse relies on pool_recycle rather than pool_pre_ping.
#
# SQLAlchemy's MySQL do_ping() decides how to call the DBAPI by inspecting the
# *sync* pymysql signature, then calls either ping(False) or ping(). The async
# aiomysql adapter, however, declares ping(self, reconnect) with the argument
# required, so whenever the probe decides "ping()" every pooled-connection reuse
# raises TypeError: AsyncAdapt_aiomysql_connection.ping() missing 1 required
# positional argument: 'reconnect'. pymysql >= 1.2 defaults reconnect to False,
# which makes the probe take exactly that branch.
#
# pool_recycle covers the dominant real-world failure (server-side idle
# timeout) without depending on SQLAlchemy internals. Revisit if upstream makes
# the async dialect compute the flag itself.
_POOL_RECYCLE_SECONDS = 1800

# Create async engine
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.app_debug,
    pool_recycle=_POOL_RECYCLE_SECONDS,
)

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Data warehouse async engine (for akshare data tables)
data_engine = create_async_engine(
    settings.data_database_url_async,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.app_debug,
    pool_recycle=_POOL_RECYCLE_SECONDS,
)

# Data warehouse async session factory
data_session_maker = async_sessionmaker(
    data_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for dependency injection.

    Yields:
        Async database session

    Example:
        ```python
        @app.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            return result.scalars().all()
        ```
    """
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_data_db() -> AsyncGenerator[AsyncSession, None]:
    """Get data warehouse database session for dependency injection."""
    async with data_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for use in background tasks.

    Yields:
        Async database session
    """
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database with default data.

    Creates database tables if they don't exist and initializes
    default data like admin user and system settings.
    """
    from sqlalchemy import select

    from opendata.core.security import hash_password
    from opendata.models.interface import InterfaceCategory
    from opendata.models.user import User, UserRole

    async with async_session_maker() as session:
        # Each default resource is provisioned independently. Guarding the whole
        # function on the admin user alone was not idempotent: a database that
        # already had the categories but not the admin (e.g. a first run that
        # died mid-way) failed on every later startup with a duplicate key on
        # the unique interface_categories.name index.

        # Create default admin user if missing (password from env or generated)
        import os
        import secrets

        existing_admin = await session.execute(select(User).where(User.username == "admin"))
        admin_created = False
        if existing_admin.scalar_one_or_none() is None:
            default_pw = os.getenv("ADMIN_DEFAULT_PASSWORD") or secrets.token_urlsafe(16)
            if not os.getenv("ADMIN_DEFAULT_PASSWORD"):
                logger.warning(
                    "No ADMIN_DEFAULT_PASSWORD set. Generated random password: "
                    f"{default_pw[:3]}{'*' * (len(default_pw) - 3)} "
                    "(set ADMIN_DEFAULT_PASSWORD env var to control this)"
                )
            admin_user = User(
                username="admin",
                email="admin@opendata.com",
                hashed_password=hash_password(default_pw),
                full_name="System Administrator",
                role=UserRole.ADMIN,
                is_active=True,
                is_verified=True,
            )
            session.add(admin_user)
            admin_created = True

        # Create whichever default interface categories are still missing
        existing_names = set(
            (await session.execute(select(InterfaceCategory.name))).scalars().all()
        )
        default_categories = [
            InterfaceCategory(name="stock", description="股票数据", sort_order=1),
            InterfaceCategory(name="fund", description="基金数据", sort_order=2),
            InterfaceCategory(name="futures", description="期货数据", sort_order=3),
            InterfaceCategory(name="index", description="指数数据", sort_order=4),
            InterfaceCategory(name="bond", description="债券数据", sort_order=5),
            InterfaceCategory(name="forex", description="外汇数据", sort_order=6),
            InterfaceCategory(name="economic", description="经济数据", sort_order=7),
            InterfaceCategory(name="macro", description="宏观数据", sort_order=8),
        ]
        missing_categories = [
            category for category in default_categories if category.name not in existing_names
        ]
        session.add_all(missing_categories)

        if not admin_created and not missing_categories:
            logger.info("Database already initialized")
            return

        await session.commit()
        logger.info(
            f"Database initialized successfully "
            f"(admin_created={admin_created}, categories_added={len(missing_categories)})"
        )


async def create_tables() -> None:
    """Create all database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created successfully")


async def close_db() -> None:
    """Close database connections."""
    await engine.dispose()
    await data_engine.dispose()
    logger.info("Database connections closed")


async def check_db_connection() -> bool:
    """Check if database connection is healthy.

    Returns:
        True if connection is successful, False otherwise
    """
    try:
        async with async_session_maker() as session:
            await session.execute(select(1))
        return True
    except Exception:
        logger.exception("Database connection failed")
        return False
