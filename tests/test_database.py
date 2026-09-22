"""
Database module tests.

Tests for database initialization, connection, and utilities.
"""


class TestDatabase:
    """Test database functions."""

    def test_get_db_session(self, test_db):
        """Test get_db dependency."""
        import asyncio

        from opendata.core.database import get_db

        async def check_session():
            gen = get_db()
            db = await gen.__anext__()

            try:
                assert db is not None
                from sqlalchemy.ext.asyncio import AsyncSession

                assert isinstance(db, AsyncSession)
            finally:
                await gen.aclose()

        asyncio.run(check_session())

    def test_base_metadata(self):
        """Test Base metadata."""
        from opendata.core.database import Base

        assert Base is not None
        assert hasattr(Base, "metadata")

    def test_engine_creation(self):
        """Test engine creation."""
        from opendata.core.database import engine

        assert engine is not None

    def test_session_maker(self):
        """Test session maker."""
        from opendata.core.database import async_session_maker

        assert async_session_maker is not None
