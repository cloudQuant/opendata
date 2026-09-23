"""Consumer API key management (design §10.3, FR-19).

Owner or admin can create, list, revoke and rotate keys. The plaintext
key is returned exactly once - on creation or rotation - and never
again: only its hash is stored.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # FastAPI resolves it at runtime

from opendata.api.dependencies import CurrentUser, get_db
from opendata.models.user import User, UserRole
from opendata.services.api_key_service import ApiKeyError, ApiKeyService

if TYPE_CHECKING:
    from opendata.models.api_key import ApiKey

router = APIRouter()


class ApiKeyCreateRequest(BaseModel):
    """Body of a key creation request."""

    name: str = Field(min_length=1, max_length=100, description="Human label for the key")
    scopes: list[str] = Field(
        default_factory=list,
        description="Domain whitelist; ['*'] allows every domain, [] denies every domain",
    )
    rate_limit: int | None = Field(default=None, gt=0, description="Requests per minute")
    expires_at: datetime | None = Field(default=None, description="Optional hard expiry")
    owner_user_id: int | None = Field(
        default=None, description="Admin only: issue on behalf of another user"
    )


class ApiKeyResponse(BaseModel):
    """Key metadata; never contains the hash or the plaintext."""

    id: int
    name: str
    owner_user_id: int
    scopes: list[str]
    rate_limit: int
    status: str
    last_used_at: datetime | None
    expires_at: datetime | None
    rotated_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_record(cls, record: ApiKey) -> ApiKeyResponse:
        """Build the response from a stored key.

        Args:
            record: The stored key row.

        Returns:
            The metadata payload.
        """
        return cls(
            id=record.id,
            name=record.name,
            owner_user_id=record.owner_user_id,
            scopes=list(record.scopes or []),
            rate_limit=record.rate_limit,
            status=record.status.value,
            last_used_at=record.last_used_at,
            expires_at=record.expires_at,
            rotated_at=record.rotated_at,
            created_at=record.created_at,
        )


class IssuedApiKeyResponse(BaseModel):
    """Creation/rotation response: the only place the plaintext appears."""

    key: str = Field(description="Plaintext key, shown once; store it now")
    api_key: ApiKeyResponse


async def _authorized_key(
    key_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    """Load a key the caller may manage (owner or admin).

    Args:
        key_id: Key primary key.
        current_user: Authenticated user.
        db: Database session.

    Returns:
        The key row.

    Raises:
        HTTPException: 404 when missing, 403 when not owner/admin.
    """
    record = await ApiKeyService(db).get(key_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    is_admin = current_user.role is UserRole.ADMIN
    if not is_admin and record.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You may only manage your own API keys",
        )
    return record


@router.get("/", response_model=list[ApiKeyResponse])
async def list_api_keys(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    owner_user_id: int | None = None,
) -> list[ApiKeyResponse]:
    """List API keys: every key for an admin, own keys otherwise.

    Args:
        current_user: Authenticated user.
        db: Database session.
        owner_user_id: Admin-only filter by owner.

    Returns:
        Key metadata, newest first.
    """
    service = ApiKeyService(db)
    if current_user.role is UserRole.ADMIN:
        if owner_user_id is None:
            records = await service.list_keys()
        else:
            owner = await db.get(User, owner_user_id)
            records = await service.list_keys(owner) if owner is not None else []
    else:
        records = await service.list_keys(current_user)
    return [ApiKeyResponse.from_record(record) for record in records]


@router.post("/", response_model=IssuedApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> IssuedApiKeyResponse:
    """Create an API key and return its plaintext once.

    Args:
        payload: Key definition.
        current_user: Authenticated user.
        db: Database session.

    Returns:
        The plaintext key plus its metadata.

    Raises:
        HTTPException: 403 when a non-admin targets another owner, 400
            when the definition is invalid.
    """
    owner = current_user
    if payload.owner_user_id is not None and payload.owner_user_id != current_user.id:
        if current_user.role is not UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an admin can issue a key for another user",
            )
        target = await db.get(User, payload.owner_user_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owner not found")
        owner = target
    try:
        issued = await ApiKeyService(db).issue(
            owner=owner,
            name=payload.name,
            scopes=payload.scopes,
            rate_limit=payload.rate_limit,
            expires_at=payload.expires_at,
        )
    except ApiKeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return IssuedApiKeyResponse(
        key=issued.plaintext, api_key=ApiKeyResponse.from_record(issued.record)
    )


@router.post("/{key_id}/revoke", response_model=ApiKeyResponse)
async def revoke_api_key(
    key_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    """Revoke a key permanently.

    Args:
        key_id: Key primary key.
        current_user: Authenticated user.
        db: Database session.

    Returns:
        The revoked key metadata.

    Raises:
        HTTPException: 404/403 from the ownership check, 400 when the
            key is already revoked.
    """
    record = await _authorized_key(key_id, current_user, db)
    try:
        revoked = await ApiKeyService(db).revoke(record)
    except ApiKeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ApiKeyResponse.from_record(revoked)


@router.post("/{key_id}/rotate", response_model=IssuedApiKeyResponse)
async def rotate_api_key(
    key_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> IssuedApiKeyResponse:
    """Replace a key, revoking the old one and returning the new plaintext.

    Args:
        key_id: Key primary key.
        current_user: Authenticated user.
        db: Database session.

    Returns:
        The new plaintext key plus its metadata.

    Raises:
        HTTPException: 404/403 from the ownership check, 400 when the
            key is revoked or its owner disappeared.
    """
    record = await _authorized_key(key_id, current_user, db)
    try:
        issued = await ApiKeyService(db).rotate(record)
    except ApiKeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return IssuedApiKeyResponse(
        key=issued.plaintext, api_key=ApiKeyResponse.from_record(issued.record)
    )
