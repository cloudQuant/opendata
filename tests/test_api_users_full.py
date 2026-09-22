"""
Comprehensive tests for users API endpoints.

Covers list, get, update, delete, reset-password endpoints.
"""

import pytest
from httpx import AsyncClient


class TestListUsers:
    """Test list users endpoint."""

    @pytest.mark.asyncio
    async def test_list_users_as_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test listing users as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_users_as_regular_user(self, test_client: AsyncClient, test_user_token: str):
        """Test listing users as regular user (should be forbidden)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/users/", headers=headers)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_list_users_with_role_filter(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test listing users with role filter."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/?role=admin", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_users_with_active_filter(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test listing users with active filter."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/?is_active=true", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_users_with_search(self, test_client: AsyncClient, test_admin_token: str):
        """Test listing users with search query."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/?search=admin", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_users_pagination(self, test_client: AsyncClient, test_admin_token: str):
        """Test listing users with pagination."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/?page=1&page_size=5", headers=headers)
        assert response.status_code == 200


class TestGetUser:
    """Test get user endpoint."""

    @pytest.mark.asyncio
    async def test_get_user_as_admin(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test getting user details as admin."""
        from sqlalchemy import select

        from opendata.models.user import User

        result = await test_db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user:
            headers = {"Authorization": f"Bearer {test_admin_token}"}
            response = await test_client.get(f"/api/users/{user.id}", headers=headers)
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_nonexistent_user(self, test_client: AsyncClient, test_admin_token: str):
        """Test getting non-existent user."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/99999", headers=headers)
        assert response.status_code == 404


class TestUpdateUser:
    """Test update user endpoint."""

    @pytest.mark.asyncio
    async def test_update_user_role(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test updating user role."""
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        user = User(
            username="updatetest",
            email="updatetest@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            f"/api/users/{user.id}",
            headers=headers,
            json={"role": "admin", "is_active": True},
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_update_nonexistent_user(self, test_client: AsyncClient, test_admin_token: str):
        """Test updating non-existent user."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            "/api/users/99999",
            headers=headers,
            json={"full_name": "Test"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_user_duplicate_email(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test updating user with duplicate email."""
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        user1 = User(
            username="emailtest1",
            email="email1@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        user2 = User(
            username="emailtest2",
            email="email2@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add_all([user1, user2])
        await test_db.commit()
        await test_db.refresh(user2)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            f"/api/users/{user2.id}",
            headers=headers,
            json={"email": "email1@example.com"},
        )
        assert response.status_code == 400


class TestDeleteUser:
    """Test delete user endpoint."""

    @pytest.mark.asyncio
    async def test_delete_user(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test deleting a user."""
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        user = User(
            username="deletetest",
            email="deletetest@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(f"/api/users/{user.id}", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_last_admin(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test deleting the last admin user (should fail)."""
        from sqlalchemy import select

        from opendata.models.user import User, UserRole

        # Find the admin user
        result = await test_db.execute(select(User).where(User.role == UserRole.ADMIN))
        admin = result.scalars().first()

        if admin:
            headers = {"Authorization": f"Bearer {test_admin_token}"}
            response = await test_client.delete(f"/api/users/{admin.id}", headers=headers)
            assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_nonexistent_user(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting non-existent user."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/users/99999", headers=headers)
        assert response.status_code == 404


class TestResetPassword:
    """Test reset password endpoint."""

    @pytest.mark.asyncio
    async def test_reset_password(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test resetting user password."""
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        user = User(
            username="resetpw",
            email="resetpw@example.com",
            hashed_password=hash_password("OldPassword!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            f"/api/users/{user.id}/reset-password",
            headers=headers,
            json={"new_password": "NewPassword123!"},
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_reset_password_nonexistent(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test resetting password for non-existent user."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/users/99999/reset-password",
            headers=headers,
            json={"new_password": "NewPass123!"},
        )
        assert response.status_code == 404
