"""
Authentication API routes.

Provides endpoints for user registration, login, token refresh, and logout.
"""

import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import CurrentUser, get_db
from opendata.api.rate_limit import rate_limit
from opendata.api.schemas import (
    APIResponse,
    ErrorResponse,
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
)
from opendata.core.config import settings
from opendata.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
    verify_token,
)
from opendata.core.token_blacklist import token_blacklist
from opendata.models.user import User, UserRole
from opendata.utils.constants import MAX_USERNAME_GENERATION_ATTEMPTS

router = APIRouter()

DEFAULT_PASSWORD = "admin123"


class ChangePasswordRequest(BaseModel):
    """Request model for password change."""

    old_password: str
    new_password: str


@router.post(
    "/register",
    response_model=APIResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request (email exists, validation)"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
@rate_limit("5/minute")  # Limit registration attempts
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Register a new user account.

    Creates a new user with regular user role.
    """
    # Normalize email to lowercase
    email = request.email.lower()

    # Check if email already exists
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Generate username from email (part before @)
    base_username = email.split("@")[0][:46]  # Max length 50, leave room for counter
    username = base_username
    counter = 1

    while counter <= MAX_USERNAME_GENERATION_ATTEMPTS:
        result = await db.execute(select(User).where(User.username == username))
        if result.scalar_one_or_none() is None:
            break
        username = f"{base_username}{counter}"
        counter += 1

    if counter > MAX_USERNAME_GENERATION_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to generate unique username. Please contact support.",
        )

    # Create new user
    user = User(
        username=username,
        email=email,  # Use normalized email
        hashed_password=hash_password(request.password),
        role=UserRole.USER,
        is_active=True,
    )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Create tokens
    access_token = create_access_token(data={"sub": str(user.id), "email": user.email})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    return APIResponse(
        success=True,
        message="注册成功",
        data={
            "user_id": user.id,
            "email": user.email,
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
    )


@router.post(
    "/login",
    response_model=APIResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
        403: {"model": ErrorResponse, "description": "Account disabled"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    },
)
@rate_limit("10/minute")
async def login(
    request: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Authenticate user and return access token.

    Validates credentials and returns JWT access token for authentication.
    """
    email = request.email.lower()

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    user.last_login = datetime.now(UTC)
    await db.commit()

    access_token = create_access_token(data={"sub": str(user.id), "email": user.email})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    require_password_change = False
    if settings.is_production:
        if user.password_changed_at is None:
            require_password_change = True
        elif verify_password(DEFAULT_PASSWORD, user.hashed_password):
            require_password_change = True

    return APIResponse(
        success=True,
        message="登录成功",
        data={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "require_password_change": require_password_change,
            "user": {
                "user_id": user.id,
                "email": user.email,
                "role": user.role.value,
                "created_at": user.created_at.isoformat(),
                "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            },
        },
    )


@router.post(
    "/refresh",
    response_model=APIResponse,
    responses={401: {"model": ErrorResponse, "description": "Invalid or expired refresh token"}},
)
async def refresh_token(
    request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Refresh access token using refresh token.

    Validates refresh token and returns new access token.
    """
    payload = verify_token(request.refresh_token, token_type="refresh")

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Create new access token
    access_token = create_access_token(data={"sub": str(user.id), "email": user.email})
    new_refresh_token = create_refresh_token(data={"sub": str(user.id)})

    return APIResponse(
        success=True,
        message="Token refreshed",
        data={
            "access_token": access_token,
            "refresh_token": new_refresh_token,
        },
    )


@router.post(
    "/logout",
    response_model=APIResponse,
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)
async def logout(
    current_user: CurrentUser,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
) -> APIResponse:
    """
    Logout current user.

    Revokes the current access token so it cannot be reused.
    """
    token = credentials.credentials
    payload = decode_token(token)
    # Use token expiry for blacklist auto-cleanup
    expires_at = payload.get("exp") if payload else None
    token_blacklist.revoke(token, expires_at=expires_at)

    return APIResponse(
        success=True,
        message="Logout successful",
    )


@router.get(
    "/me",
    response_model=APIResponse,
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)
async def get_current_user_info(
    current_user: CurrentUser,
) -> APIResponse:
    """
    Get current user information.

    Returns the authenticated user's profile information.
    """
    return APIResponse(
        success=True,
        data={
            "user_id": current_user.id,
            "email": current_user.email,
            "role": current_user.role.value,
            "is_active": current_user.is_active,
            "created_at": current_user.created_at.isoformat(),
            "updated_at": current_user.updated_at.isoformat() if current_user.updated_at else None,
        },
    )


@router.post(
    "/change-password",
    response_model=APIResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid old password"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
    },
)
async def change_password(
    request: ChangePasswordRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Change user password.

    Validates old password and updates to new password.
    In production, forces password change if using default password.
    """
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if not verify_password(request.old_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码错误",
        )

    if len(request.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码长度至少为8位",
        )

    if request.new_password == request.old_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码不能与旧密码相同",
        )

    user.hashed_password = hash_password(request.new_password)
    user.password_changed_at = datetime.now(UTC)
    await db.commit()

    return APIResponse(
        success=True,
        message="密码修改成功",
    )
