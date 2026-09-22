"""
Direct tests for settings API endpoints to maximize coverage.
"""

import pytest

from opendata.core.security import hash_password
from opendata.models.user import User, UserRole


async def _admin(db):
    u = User(
        username="sadm",
        email="sadm@t.com",
        hashed_password=hash_password("P!1"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


class TestGetDatabaseConfigDirect:
    @pytest.mark.asyncio
    async def test_get_config(self, test_db):
        from opendata.api.settings import get_database_config

        admin = await _admin(test_db)
        result = await get_database_config(current_admin=admin)
        assert result.is_warehouse is False
        assert result.host is not None


class TestGetWarehouseConfigDirect:
    @pytest.mark.asyncio
    async def test_get_warehouse(self, test_db):
        from opendata.api.settings import get_warehouse_config

        admin = await _admin(test_db)
        result = await get_warehouse_config(current_admin=admin)
        assert result.is_warehouse is True


class TestTestConnectionDirect:
    @pytest.mark.asyncio
    async def test_connection_failure(self, test_db):
        from opendata.api.settings import TestConnectionRequest, test_database_connection

        admin = await _admin(test_db)
        req = TestConnectionRequest(
            host="invalid_host", port=3306, database="test", user="test", password="test"
        )
        result = await test_database_connection(request=req, current_admin=admin, db=test_db)
        assert result.success is False
        assert "失败" in result.message


class TestUpdateConfigDirect:
    @pytest.mark.asyncio
    async def test_update_writes_env(self, test_db):
        from unittest.mock import patch

        from opendata.api.settings import DatabaseConfigRequest, update_database_config

        admin = await _admin(test_db)
        req = DatabaseConfigRequest(
            host="newhost", port=3307, database="newdb", user="newuser", password="newpass"
        )

        with patch("opendata.api.settings._update_env_file") as mock_update:
            result = await update_database_config(request=req, current_admin=admin)
            mock_update.assert_called_once()
            call_args = mock_update.call_args
            updates = call_args[0][1]
            assert updates["MYSQL_HOST"] == "newhost"
            assert updates["MYSQL_PORT"] == "3307"
        assert result.host == "newhost"
        assert result.port == 3307

    @pytest.mark.asyncio
    async def test_update_warehouse_writes_env(self, test_db):
        from unittest.mock import patch

        from opendata.api.settings import DatabaseConfigRequest, update_database_config

        admin = await _admin(test_db)
        req = DatabaseConfigRequest(
            host="wh", port=3308, database="whdb", user="whu", password="whp", is_warehouse=True
        )

        with patch("opendata.api.settings._update_env_file") as mock_update:
            result = await update_database_config(request=req, current_admin=admin)
            updates = mock_update.call_args[0][1]
            assert "DATA_MYSQL_HOST" in updates
        assert result.is_warehouse is True

    def test_update_env_file_helper(self, tmp_path):
        from opendata.api.settings import _update_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("HOST=old\n# comment\nPORT=3306\n")
        _update_env_file(env_file, {"HOST": "new", "PORT": "3307", "EXTRA": "val"})
        content = env_file.read_text()
        assert "HOST=new" in content
        assert "PORT=3307" in content
        assert "EXTRA=val" in content
        assert "# comment" in content

    def test_update_env_file_creates_new(self, tmp_path):
        from opendata.api.settings import _update_env_file

        env_file = tmp_path / ".env_new"
        _update_env_file(env_file, {"KEY": "value"})
        assert "KEY=value" in env_file.read_text()


class TestWarehouseTestConnectionDirect:
    @pytest.mark.asyncio
    async def test_warehouse_connection(self, test_db):
        from opendata.api.settings import TestConnectionRequest, test_warehouse_connection

        admin = await _admin(test_db)
        req = TestConnectionRequest(
            host="invalid_host", port=3306, database="test", user="test", password="test"
        )
        result = await test_warehouse_connection(request=req, current_admin=admin)
        assert result.success is False
