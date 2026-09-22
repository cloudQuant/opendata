"""
Security utilities for authentication and authorization.

Provides password hashing, JWT token creation/verification, and permission checking.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from jwt.exceptions import PyJWTError
from loguru import logger

from opendata.core.config import settings
from opendata.models.user import UserRole


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password

    Returns:
        Hashed password
    """
    # Bcrypt has a 72-byte limit, truncate if necessary
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash.

    Args:
        plain_password: Plain text password to verify
        hashed_password: Hashed password to compare against

    Returns:
        True if password matches, False otherwise
    """
    # Bcrypt has a 72-byte limit, truncate if necessary
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > 72:
        password_bytes = password_bytes[:72]
    hashed_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hashed_bytes)


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Data to encode in the token (typically user_id, email, etc.)
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update(
        {
            "exp": expire,
            "type": "access",
        }
    )

    # PyJWT returns str directly (no need for .decode())
    return jwt.encode(
        to_encode,
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def create_refresh_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a JWT refresh token.

    Args:
        data: Data to encode in the token (typically user_id)
        expires_delta: Optional custom expiration time

    Returns:
        Encoded JWT refresh token
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)

    to_encode.update(
        {
            "exp": expire,
            "type": "refresh",
        }
    )

    return jwt.encode(
        to_encode,
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def decode_token(token: str) -> dict[str, Any] | None:
    """
    Decode and verify a JWT token.

    Args:
        token: JWT token to decode

    Returns:
        Decoded token payload or None if invalid
    """
    try:
        return jwt.decode(  # type: ignore[no-any-return]
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )
    except PyJWTError as e:
        logger.warning(f"Token decode failed: {e}")
        return None


def verify_token(token: str, token_type: str = "access") -> dict[str, Any] | None:
    """
    Verify and decode a JWT token of specific type.

    Args:
        token: JWT token to verify
        token_type: Expected token type ("access" or "refresh")

    Returns:
        Decoded token payload or None if invalid/expired
    """
    payload = decode_token(token)
    if payload is None:
        return None

    if payload.get("type") != token_type:
        logger.warning(f"Token type mismatch: expected {token_type}, got {payload.get('type')}")
        return None

    return payload


class PermissionChecker:
    """
    Permission checker for user authorization.

    Supports role-based and ownership-based access control.
    """

    @staticmethod
    def is_admin(user_role: UserRole | str) -> bool:
        """Check if user has admin role."""
        if isinstance(user_role, str):
            return user_role == UserRole.ADMIN.value
        return user_role == UserRole.ADMIN

    @staticmethod
    def can_access_owned_resource(
        user_id: int,
        resource_owner_id: int,
        is_admin: bool,
    ) -> bool:
        """Quick check: admin or owner."""
        return is_admin or user_id == resource_owner_id

    @staticmethod
    def can_access_resource(
        user_role: UserRole | str, user_id: int, resource_owner_id: int
    ) -> bool:
        """
        Check if user can access a resource.

        Admins can access any resource. Regular users can only
        access their own resources.

        Args:
            user_role: User's role
            user_id: User's ID
            resource_owner_id: ID of the resource owner

        Returns:
            True if user can access resource, False otherwise
        """
        if PermissionChecker.is_admin(user_role):
            return True
        return user_id == resource_owner_id

    @staticmethod
    def can_modify_user(
        current_user_role: UserRole | str, current_user_id: int, target_user_id: int
    ) -> bool:
        """
        Check if current user can modify target user.

        Only admins can modify other users. Users can modify themselves.

        Args:
            current_user_role: Current user's role
            current_user_id: Current user's ID
            target_user_id: ID of user to modify

        Returns:
            True if modification is allowed, False otherwise
        """
        if PermissionChecker.is_admin(current_user_role):
            return True
        return current_user_id == target_user_id
