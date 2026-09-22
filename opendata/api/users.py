"""
User management API routes.

Provides endpoints for managing users (admin functions).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import CurrentAdmin, get_db
from opendata.api.schemas import (
    APIResponse,
    PaginatedParams,
    PaginatedResponse,
    ResetPasswordRequest,
    UserResponse,
    UserUpdateRequest,
)
from opendata.core.security import hash_password
from opendata.models.user import User, UserRole

router = APIRouter()


@router.get("/", response_model=PaginatedResponse)
async def list_users(
    current_admin: CurrentAdmin,
    params: PaginatedParams = Depends(),
    role: UserRole | None = Query(None, description="Filter by role"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    search: str | None = Query(None, description="Search by username, email, or name"),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse:
    """
    List all users.

    Admin only endpoint for listing all registered users.
    """
    # Build query
    query = select(User)

    if role is not None:
        query = query.where(User.role == role)

    if is_active is not None:
        query = query.where(User.is_active == is_active)

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            (User.username.ilike(search_pattern))
            | (User.email.ilike(search_pattern))
            | (User.full_name.ilike(search_pattern))
        )

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = (
        query.order_by(User.created_at.desc())
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
    )

    result = await db.execute(query)
    users = result.scalars().all()

    items = [UserResponse.model_validate(user) for user in users]

    return PaginatedResponse(
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        total_pages=(total + params.page_size - 1) // params.page_size,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    current_admin: CurrentAdmin,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """
    Get user details.

    Admin only endpoint for getting detailed user information.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    current_admin: CurrentAdmin,
    user_update: UserUpdateRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """
    Update user.

    Admin only endpoint for updating user information.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Get update data
    email = user_update.email if user_update else None
    full_name = user_update.full_name if user_update else None
    role = user_update.role if user_update else None
    is_active = user_update.is_active if user_update else None
    is_verified = user_update.is_verified if user_update else None

    # Check if email is being changed and if it's already taken
    if email and email != user.email:
        existing = await db.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        user.email = email

    # Update fields
    if full_name is not None:
        user.full_name = full_name
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    if is_verified is not None:
        user.is_verified = is_verified

    await db.commit()
    await db.refresh(user)

    return UserResponse.model_validate(user)


@router.delete("/{user_id}", response_model=APIResponse)
async def delete_user(
    user_id: int,
    current_admin: CurrentAdmin,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Delete user.

    Admin only endpoint for deleting users. This will also
    delete all associated tasks and data.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Prevent deleting the last admin
    if user.role == UserRole.ADMIN:
        # Check if there are other admins
        admin_count = await db.execute(
            select(func.count()).select_from(
                select(User).where(User.role == UserRole.ADMIN).subquery()
            )
        )
        if (admin_count.scalar() or 0) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin user",
            )

    await db.delete(user)
    await db.commit()

    return APIResponse(
        success=True,
        message=f"User {user.username} deleted successfully",
    )


@router.post("/{user_id}/reset-password", response_model=APIResponse)
async def reset_user_password(
    user_id: int,
    body: ResetPasswordRequest,
    current_admin: CurrentAdmin,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Reset user password.

    Admin only endpoint for resetting user passwords.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    user.hashed_password = hash_password(body.new_password)
    await db.commit()

    return APIResponse(
        success=True,
        message=f"Password reset for user {user.username}",
    )
