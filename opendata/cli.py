"""Command-line interface for opendata.

Provides CLI commands for managing the application.
"""

import asyncio
import sys
from pathlib import Path

import click
from loguru import logger

from opendata.core.config import settings
from opendata.core.database import create_tables, init_db
from opendata.services.interface_loader import InterfaceLoader


@click.group()
def cli() -> None:
    """Opendata CLI - financial data platform."""


@cli.command()
@click.option("--host", default=settings.host, help="Host to bind to")
@click.option("--port", default=settings.port, type=int, help="Port to bind to")
@click.option("--reload", is_flag=True, help="Enable auto-reload")
def run(host: str, port: int, reload: bool) -> None:
    """Start the application server."""
    import uvicorn

    uvicorn.run(
        "opendata.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@cli.command()
@click.confirmation_option(prompt="Are you sure you want to drop all tables?")
def reset_db() -> None:
    """Reset the database (drops all tables and recreates them)."""

    async def _reset() -> None:
        from opendata.core.database import Base, engine

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        await init_db()
        logger.info("Database reset successfully")

    asyncio.run(_reset())


@cli.command()
def init_db_cmd() -> None:
    """Initialize database with default data."""

    async def _init() -> None:
        await create_tables()
        await init_db()
        logger.info("Database initialized successfully")

    asyncio.run(_init())


@cli.command()
@click.option("--categories", default=".", help="Path to interface definitions")
def load_interfaces(categories: str) -> None:
    """Load data interfaces from akshare."""

    async def _load() -> None:
        loader = InterfaceLoader()
        count = await loader.load_interfaces()
        logger.info(f"Loaded {count} interfaces from akshare")

    asyncio.run(_load())


@cli.command()
@click.option("--user", default="admin", help="Username")
def create_admin(user: str) -> None:
    """Create an admin user."""

    async def _create() -> None:
        from sqlalchemy import select

        from opendata.core.database import async_session_maker
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        async with async_session_maker() as db:
            # Check if user exists
            result = await db.execute(select(User).where(User.username == user))
            if result.scalar_one_or_none():
                logger.error(f"User {user} already exists")
                sys.exit(1)

            # Get password
            password = click.prompt("Password", hide_input=True, confirmation_prompt=True)

            # Create user
            admin_user = User(
                username=user,
                email=f"{user}@opendata.com",
                hashed_password=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
                is_verified=True,
            )
            db.add(admin_user)
            await db.commit()

            logger.info(f"Admin user {user} created successfully")

    asyncio.run(_create())


@cli.command()
def generate_secret() -> None:
    """Generate a random secret key for configuration."""
    import secrets

    secret = secrets.token_urlsafe(32)
    click.echo(f"SECRET_KEY={secret}")


@cli.command()
@click.option("--output", default="-", help="Output file (default: stdout)")
def export_config(output: str) -> None:
    """Export current configuration."""
    import json

    config = {
        "app_name": settings.app_name,
        "app_env": settings.app_env,
        "app_debug": settings.app_debug,
        "host": settings.host,
        "port": settings.port,
        "database_url": settings.database_url[:20] + "...",  # Partially hide
    }

    if output == "-":
        click.echo(json.dumps(config, indent=2))
    else:
        with Path(output).open("w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Configuration exported to {output}")


@cli.command()
def check_health() -> None:
    """Check application health."""

    async def _check() -> None:
        from opendata.core.database import check_db_connection

        # Check database
        db_healthy = await check_db_connection()

        status = "healthy" if db_healthy else "unhealthy"
        click.echo(f"Status: {status}")
        click.echo(f"Database: {'connected' if db_healthy else 'disconnected'}")

        sys.exit(0 if db_healthy else 1)

    asyncio.run(_check())


@cli.command()
@click.option("--message", "-m", required=True, help="Migration message")
def db_migrate(message: str) -> None:
    """Generate a new Alembic migration from model changes."""
    from alembic.config import Config

    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.revision(alembic_cfg, message=message, autogenerate=True)
    click.echo(f"Migration generated: {message}")


@cli.command()
@click.option("--revision", default="head", help="Target revision (default: head)")
def db_upgrade(revision: str) -> None:
    """Apply database migrations up to a revision."""
    from alembic.config import Config

    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, revision)
    click.echo(f"Database upgraded to: {revision}")


@cli.command()
@click.option("--revision", default="-1", help="Target revision (default: -1 = one step back)")
def db_downgrade(revision: str) -> None:
    """Revert database migrations."""
    from alembic.config import Config

    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.downgrade(alembic_cfg, revision)
    click.echo(f"Database downgraded to: {revision}")


@cli.command()
def db_history() -> None:
    """Show migration history."""
    from alembic.config import Config

    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.history(alembic_cfg, verbose=True)


def main() -> None:
    """Main CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
