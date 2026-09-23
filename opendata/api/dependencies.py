"""API dependency functions.

Provides dependency injection functions for authentication,
database access, and permission checking.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.core.config import settings
from opendata.core.database import get_db
from opendata.core.security import verify_token
from opendata.models.user import User, UserRole
from opendata.services.api_key_service import (
    KEY_PREFIX,
    ApiKeyError,
    ApiKeyService,
    key_allows_domain,
)
from opendata.services.data_service import DataService
from opendata.services.execution_service import ExecutionService
from opendata.services.script_service import ScriptService

# HTTP Bearer token scheme
security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """The caller behind a request (design §10.3).

    Attributes:
        user: The acting user (the key owner for API-key requests).
        api_key_id: The key id when the caller used an API key.
        scopes: Domain whitelist of that key; ``None`` for JWT callers,
            who are not domain-restricted.
    """

    user: User
    api_key_id: int | None = None
    scopes: tuple[str, ...] | None = None

    @property
    def uses_api_key(self) -> bool:
        """Return whether the caller authenticated with an API key."""
        return self.api_key_id is not None

    def allows_domain(self, domain: str) -> bool:
        """Return whether this caller may read a domain.

        Args:
            domain: Domain identifier.

        Returns:
            ``True`` for JWT callers and for keys whose scopes cover the
            domain; ``False`` otherwise.
        """
        if self.scopes is None:
            return True
        return key_allows_domain(self.scopes, domain)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Get current authenticated user from JWT token.

    Args:
        credentials: HTTP Authorization header with Bearer token
        db: Database session

    Returns:
        Authenticated user object

    Raises:
        HTTPException: If token is invalid or user not found
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Check if token has been revoked (logout)
    from opendata.core.token_blacklist import token_blacklist

    if token_blacklist.is_revoked(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(
        token,
        token_type="access",  # noqa: S106  # nosec B106  # token type, not a credential
    )

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    return user


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
    api_key_header: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Authenticate a request as a user or an API key (design §10.3).

    The API key is recognised first: ``Authorization: Bearer od-<key>``
    or the ``X-API-Key`` header. JWT callers keep working unchanged.

    Args:
        credentials: HTTP Authorization header.
        db: Database session.
        api_key_header: ``X-API-Key`` header value.

    Returns:
        The principal behind the request.

    Raises:
        HTTPException: 401 when neither credential is valid.
    """
    presented = api_key_header
    bearer = credentials.credentials if credentials is not None else None
    if presented is None and bearer is not None and bearer.startswith(KEY_PREFIX):
        presented = bearer
    if presented is not None:
        service = ApiKeyService(db)
        try:
            record = await service.authenticate(presented)
        except ApiKeyError:
            record = None
        if record is None:
            # Uniform delay: a rejected key must not answer faster than
            # an unknown one, or the timing itself enumerates keys.
            await asyncio.sleep(settings.api_key_failure_delay_seconds)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        owner = await db.get(User, record.owner_user_id)
        if owner is None or not owner.is_active:
            await asyncio.sleep(settings.api_key_failure_delay_seconds)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key owner is disabled",
            )
        return Principal(user=owner, api_key_id=record.id, scopes=tuple(record.scopes or ()))

    user = await get_current_user(credentials, db)
    return Principal(user=user)


def require_domain_access(principal: Principal, domain: str) -> None:
    """Enforce an API key's domain whitelist.

    Args:
        principal: The authenticated caller.
        domain: Domain being read.

    Raises:
        HTTPException: 403 when the key's scopes exclude the domain.
    """
    if not principal.allows_domain(domain):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is not scoped for domain {domain!r}",
        )


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Get current active user.

    Ensures user account is active.

    Args:
        current_user: Current authenticated user

    Returns:
        Active user object

    Raises:
        HTTPException: If user account is inactive
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )
    return current_user


async def get_current_admin_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Get current admin user.

    Ensures user has admin role.

    Args:
        current_user: Current authenticated user

    Returns:
        Admin user object

    Raises:
        HTTPException: If user is not an admin
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Admin role required.",
        )
    return current_user


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Get current user if authenticated, otherwise None.

    Args:
        credentials: HTTP Authorization header with Bearer token
        db: Database session

    Returns:
        User object if authenticated, None otherwise
    """
    if credentials is None:
        return None

    try:
        return await get_current_user(credentials, db)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# Service-layer dependency injection
# ---------------------------------------------------------------------------


def get_execution_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExecutionService:
    """Get ExecutionService instance via dependency injection."""
    return ExecutionService(db)


def get_script_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScriptService:
    """Get ScriptService instance via dependency injection."""
    return ScriptService(db)


def get_data_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DataService:
    """Get DataService instance via dependency injection."""
    return DataService(db)


# Type aliases for commonly used dependencies
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
CurrentAdmin = Annotated[User, Depends(get_current_admin_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
DBSession = Annotated[AsyncSession, Depends(get_db)]
