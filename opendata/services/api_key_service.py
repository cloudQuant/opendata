"""Consumer API key service (design §10.3, FR-19).

Storage rules of design §10.3: the plaintext key is
``secrets.token_urlsafe(32)`` (>= 128 bit of entropy) prefixed with
``od-``, and only ``sha256(pepper + key)`` is persisted. Lookup is by
that hash (equality index) and the match is confirmed with
``hmac.compare_digest``, so a timing side channel cannot confirm a
guess. Expiry, revocation and rotation are all fail-closed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from opendata.core.config import settings
from opendata.models.api_key import ApiKey, ApiKeyStatus
from opendata.models.user import User

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

#: Prefix that makes an API key recognisable in a header.
KEY_PREFIX = "od-"

#: Scope entry that allows every domain.
WILDCARD_SCOPE = "*"


class ApiKeyError(RuntimeError):
    """Raised when an API key cannot be issued or used as requested."""


@dataclass(frozen=True)
class IssuedKey:
    """A freshly issued key: the only time the plaintext exists.

    Attributes:
        record: The stored key row (hash only).
        plaintext: The key to hand to the consumer, shown once.
    """

    record: ApiKey
    plaintext: str


def api_key_pepper() -> str:
    """Return the pepper mixed into every key hash.

    Returns:
        ``settings.api_key_pepper`` when set, otherwise the application
        secret (both come from the environment; production already
        rejects the default secret).
    """
    return settings.api_key_pepper or settings.secret_key


def generate_key() -> str:
    """Generate a new plaintext API key.

    Returns:
        ``od-`` plus 32 url-safe random bytes (>= 128 bit).
    """
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_key(plaintext: str) -> str:
    """Hash a plaintext key for storage and lookup.

    Args:
        plaintext: The key as presented by the consumer.

    Returns:
        The hex digest of ``sha256(pepper + plaintext)``.
    """
    return hashlib.sha256(f"{api_key_pepper()}{plaintext}".encode()).hexdigest()


def normalize_scope_input(scopes: Sequence[str] | None) -> list[str]:
    """Normalize a scope list into the stored representation.

    Args:
        scopes: Domain identifiers, or ``["*"]`` for every domain.

    Returns:
        Sorted, de-duplicated scopes; an empty result denies every
        domain (fail closed).
    """
    if not scopes:
        return []
    return sorted({scope.strip() for scope in scopes if scope and scope.strip()})


def key_allows_domain(scopes: Sequence[str], domain: str) -> bool:
    """Return whether a key's scopes allow a domain.

    Args:
        scopes: The key's stored scopes.
        domain: Domain identifier being queried.

    Returns:
        ``True`` only for an explicit match or the wildcard scope.
    """
    return WILDCARD_SCOPE in scopes or domain in scopes


def is_expired(record: ApiKey, *, now: datetime | None = None) -> bool:
    """Return whether a key is past its expiry.

    Args:
        record: The key row.
        now: Override for "now" (tests).

    Returns:
        ``True`` when ``expires_at`` is set and reached.
    """
    if record.expires_at is None:
        return False
    moment = now or datetime.now(timezone.utc)
    expires_at = record.expires_at
    if expires_at.tzinfo is None:  # MySQL datetime columns come back naive
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= moment


class ApiKeyService:
    """Issue, authenticate and revoke consumer API keys."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the service.

        Args:
            db: Main-database session.
        """
        self._db = db

    async def issue(
        self,
        *,
        owner: User,
        name: str,
        scopes: Sequence[str] | None = None,
        rate_limit: int | None = None,
        expires_at: datetime | None = None,
        rotated_from: ApiKey | None = None,
    ) -> IssuedKey:
        """Create a key and return it with its plaintext.

        Args:
            owner: User the key acts as.
            name: Human label.
            scopes: Domain whitelist; empty denies every domain.
            rate_limit: Requests per minute; defaults to the global
                setting.
            expires_at: Optional hard expiry.
            rotated_from: Key this one replaces; recorded on both rows.

        Returns:
            The issued key, whose plaintext is shown only here.

        Raises:
            ApiKeyError: If the name is empty or the rate limit is not
                positive.
        """
        if not name.strip():
            raise ApiKeyError("an API key needs a name")
        limit = rate_limit if rate_limit is not None else settings.rate_limit_per_minute
        if limit <= 0:
            raise ApiKeyError(f"rate_limit must be positive, got {limit}")
        plaintext = generate_key()
        now = datetime.now(timezone.utc)
        record = ApiKey(
            key_hash=hash_key(plaintext),
            name=name.strip(),
            owner_user_id=owner.id,
            scopes=normalize_scope_input(scopes),
            rate_limit=limit,
            status=ApiKeyStatus.ACTIVE,
            expires_at=expires_at,
            rotated_at=now if rotated_from is not None else None,
        )
        self._db.add(record)
        if rotated_from is not None:
            rotated_from.status = ApiKeyStatus.REVOKED
            rotated_from.rotated_at = now
        await self._db.commit()
        await self._db.refresh(record)
        return IssuedKey(record=record, plaintext=plaintext)

    async def authenticate(self, presented: str) -> ApiKey | None:
        """Resolve a presented key to its active record.

        Args:
            presented: Header value, with or without the ``od-`` prefix.

        Returns:
            The key row when it is active and unexpired; ``None``
            otherwise (unknown, revoked or expired are indistinguishable
            to the caller on purpose).

        Raises:
            ApiKeyError: If the presented value is empty.
        """
        candidate = presented.strip()
        if not candidate:
            raise ApiKeyError("empty API key")
        digest = hash_key(candidate)
        result = await self._db.execute(select(ApiKey).where(ApiKey.key_hash == digest))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        # The index lookup already scoped the row; compare again in
        # constant time so a stored hash cannot be probed by timing.
        if not hmac.compare_digest(record.key_hash, digest):
            return None
        if record.status is not ApiKeyStatus.ACTIVE or is_expired(record):
            return None
        record.last_used_at = datetime.now(timezone.utc)
        await self._db.commit()
        return record

    async def list_keys(self, owner: User | None = None) -> list[ApiKey]:
        """List keys, newest first.

        Args:
            owner: Restrict to one owner; ``None`` lists every key.

        Returns:
            The matching key rows.
        """
        statement = select(ApiKey).order_by(ApiKey.id.desc())
        if owner is not None:
            statement = statement.where(ApiKey.owner_user_id == owner.id)
        result = await self._db.execute(statement)
        return list(result.scalars())

    async def get(self, key_id: int) -> ApiKey | None:
        """Fetch one key by id.

        Args:
            key_id: Primary key.

        Returns:
            The row, or ``None``.
        """
        return await self._db.get(ApiKey, key_id)

    async def revoke(self, record: ApiKey) -> ApiKey:
        """Revoke a key permanently.

        Args:
            record: The key to revoke.

        Returns:
            The revoked row.

        Raises:
            ApiKeyError: If the key is already revoked.
        """
        if record.status is ApiKeyStatus.REVOKED:
            raise ApiKeyError(f"API key {record.id} is already revoked")
        record.status = ApiKeyStatus.REVOKED
        await self._db.commit()
        await self._db.refresh(record)
        return record

    async def rotate(self, record: ApiKey) -> IssuedKey:
        """Replace a key, revoking the old one in the same commit.

        Args:
            record: The key to replace.

        Returns:
            The new key with its plaintext.

        Raises:
            ApiKeyError: If the key is already revoked.
        """
        if record.status is ApiKeyStatus.REVOKED:
            raise ApiKeyError(f"API key {record.id} is revoked and cannot be rotated")
        owner = await self._db.get(User, record.owner_user_id)
        if owner is None:
            raise ApiKeyError(
                f"owner {record.owner_user_id} of API key {record.id} no longer exists"
            )
        return await self.issue(
            owner=owner,
            name=record.name,
            scopes=list(record.scopes),
            rate_limit=record.rate_limit,
            expires_at=record.expires_at,
            rotated_from=record,
        )
