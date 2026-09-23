"""Consumer API key model (design §10.3, FR-19).

Machine-to-machine consumers (``backtrader_web``) authenticate with an
API key instead of a JWT. Only the hash is stored: the plaintext key is
returned once at creation/rotation and is never recoverable afterwards.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base


class ApiKeyStatus(str, enum.Enum):
    """Lifecycle state of an API key."""

    ACTIVE = "active"
    REVOKED = "revoked"


class ApiKey(Base):
    """A consumer API key.

    Attributes:
        id: Primary key.
        key_hash: ``sha256(pepper + plaintext)`` hex digest; the lookup
            key (equality index) and never returned by any endpoint.
        name: Human label chosen by the owner.
        owner_user_id: User the key acts as.
        scopes: Domain whitelist; an empty list denies every domain and
            ``["*"]`` allows all of them (fail closed).
        rate_limit: Requests per minute for this key.
        status: ``active`` until revoked; revocation is permanent.
        last_used_at: Last successful authentication.
        expires_at: Hard expiry; ``None`` means no expiry.
        rotated_at: Set on the replacement key and on the key it replaced.
        created_at: Creation timestamp.
        updated_at: Last modification timestamp.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rate_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    status: Mapped[ApiKeyStatus] = mapped_column(
        Enum(ApiKeyStatus), default=ApiKeyStatus.ACTIVE, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Return a debug representation without the secret material."""
        return f"<ApiKey id={self.id} name={self.name!r} status={self.status.value}>"
