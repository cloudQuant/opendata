"""
Database connection and session management.

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
    __table_args__ = {  # noqa: RUF012
        "extend_existing": True,
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }


# Create async engine
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.app_debug,
    pool_pre_ping=True,
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
    pool_pre_ping=True,
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
    """
    Get database session for dependency injection.

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
    """
    Get database session for use in background tasks.

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
    """
    Initialize database with default data.

    Creates database tables if they don't exist and initializes
    default data like admin user and system settings.
    """
    from sqlalchemy import select

    from opendata.core.security import hash_password
    from opendata.models.interface import InterfaceCategory
    from opendata.models.user import User, UserRole

    async with async_session_maker() as session:
        # Check if database is already initialized
        result = await session.execute(select(User).where(User.username == "admin"))
        if result.scalar_one_or_none() is not None:
            logger.info("Database already initialized")
            return

        # Create default admin user (password from env or generated)
        import os
        import secrets

        default_pw = os.getenv("ADMIN_DEFAULT_PASSWORD") or secrets.token_urlsafe(16)
        if not os.getenv("ADMIN_DEFAULT_PASSWORD"):
            logger.warning(
                "No ADMIN_DEFAULT_PASSWORD set. Generated random password: "
                f"{default_pw[:3]}{'*' * (len(default_pw) - 3)} "
                "(set ADMIN_DEFAULT_PASSWORD env var to control this)"
            )
        admin_user = User(
            username="admin",
            email="admin@akshare.com",
            hashed_password=hash_password(default_pw),
            full_name="System Administrator",
            role=UserRole.ADMIN,
            is_active=True,
            is_verified=True,
        )
        session.add(admin_user)

        # Create default interface categories
        categories = [
            InterfaceCategory(name="stock", description="股票数据", sort_order=1),
            InterfaceCategory(name="fund", description="基金数据", sort_order=2),
            InterfaceCategory(name="futures", description="期货数据", sort_order=3),
            InterfaceCategory(name="index", description="指数数据", sort_order=4),
            InterfaceCategory(name="bond", description="债券数据", sort_order=5),
            InterfaceCategory(name="forex", description="外汇数据", sort_order=6),
            InterfaceCategory(name="economic", description="经济数据", sort_order=7),
            InterfaceCategory(name="macro", description="宏观数据", sort_order=8),
        ]
        session.add_all(categories)

        await session.commit()
        logger.info("Database initialized successfully")


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
    """
    Check if database connection is healthy.

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
