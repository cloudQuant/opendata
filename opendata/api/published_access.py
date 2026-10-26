"""Fail-closed authorization for versioned published-data endpoints.

The registry in this module is intended to be constructed from an
operator-reviewed server-side file and injected into API routes. Grant
records describe the operator's review decision; they do not independently
verify third-party legal rights or licenses.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, field_validator

from opendata.api.dependencies import Principal
from opendata.models.user import User, UserRole

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_GRANT_CONFIG_BYTES = 1_048_576
MAX_DATASET_GRANTS = 2_048
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_OPERATION_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$"
_PURPOSE_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_TICKET_PATTERN = r"^[A-Z][A-Z0-9-]{2,63}$"
_Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
_Operation = Annotated[
    str,
    StringConstraints(strict=True, min_length=3, max_length=64, pattern=_OPERATION_PATTERN),
]
_Purpose = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=_PURPOSE_PATTERN),
]


class PublishedAccessError(PermissionError):
    """Represent a denied published-data access without revealing catalog state."""

    status_code: ClassVar[int] = 403
    detail: ClassVar[str] = "Dataset is unavailable"

    def __init__(self) -> None:
        """Initialize the uniform public denial response."""
        super().__init__(self.detail)


class GrantConfigurationError(ValueError):
    """Report invalid operator grant configuration without echoing its contents."""

    def __init__(self) -> None:
        """Initialize a generic configuration error safe for logs and API startup."""
        super().__init__("Invalid published dataset grant configuration")


class DatasetGrant(BaseModel):
    """An exact, operator-reviewed authorization grant for one dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: _Identifier
    domain: _Identifier
    source_id: _Identifier
    permitted_operations: tuple[_Operation, ...] = Field(min_length=1, max_length=64)
    purposes: tuple[_Purpose, ...] = Field(min_length=1, max_length=32)
    valid_from: datetime
    valid_until: datetime
    review_evidence: Annotated[str, StringConstraints(strict=True, min_length=3, max_length=64)]
    subject_ids: tuple[StrictInt, ...] | None = Field(default=None, min_length=1, max_length=10_000)

    @field_validator("permitted_operations", "purposes", mode="before")
    @classmethod
    def _normalize_identifiers(cls, value: object) -> object:
        """Sort and de-duplicate exact authorization identifiers."""
        if not isinstance(value, (list, tuple)):
            return value
        if any(not isinstance(item, str) for item in value):
            return value
        return tuple(sorted(set(value)))

    @field_validator("subject_ids", mode="before")
    @classmethod
    def _normalize_subject_ids(cls, value: object) -> object:
        """Sort subject ids and reject duplicate or non-integer subjects."""
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            return value
        if any(type(item) is not int for item in value):
            return value
        if len(set(value)) != len(value):
            raise ValueError("subject ids must be unique")
        return tuple(sorted(value))

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _require_aware_time(cls, value: datetime) -> datetime:
        """Require explicit timezone information for grant boundaries."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("grant timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("review_evidence")
    @classmethod
    def _require_safe_review_reference(cls, value: str) -> str:
        """Accept only a ticket identifier or repository-relative evidence path."""
        if re.fullmatch(_TICKET_PATTERN, value):
            return value
        if value.startswith("docs/"):
            parts = value.split("/")[1:]
            if (
                parts
                and all(part not in {"", ".", ".."} for part in parts)
                and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
            ):
                return value
        raise ValueError("review_evidence must be a ticket id or docs-relative reference")

    @field_validator("subject_ids")
    @classmethod
    def _require_positive_subjects(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        """Reject non-positive subject ids."""
        if value is not None and any(subject_id <= 0 for subject_id in value):
            raise ValueError("subject ids must be positive")
        return value

    @field_validator("valid_until")
    @classmethod
    def _require_positive_validity_window(cls, value: datetime, info: object) -> datetime:
        """Require the exclusive end of a grant to follow its inclusive start."""
        valid_from = getattr(info, "data", {}).get("valid_from")
        if isinstance(valid_from, datetime) and value <= valid_from:
            raise ValueError("valid_until must follow valid_from")
        return value


class _GrantDocument(BaseModel):
    """Strict top-level JSON schema for the operator-controlled grant file."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    datasets: tuple[DatasetGrant, ...] = Field(max_length=MAX_DATASET_GRANTS)


@dataclass(frozen=True)
class AccessContext:
    """Authorization facts to bind to a published-data query or cursor."""

    dataset_id: str
    domain: str
    source_id: str
    operation: str
    purpose: str
    access_scope: tuple[str, ...] | None
    grant: DatasetGrant
    grant_sha256: str
    binding_sha256: str
    subject_id: int


@dataclass(frozen=True, init=False)
class DatasetGrantRegistry:
    """Immutable server-side map of reviewed dataset grants."""

    _grants: Mapping[str, DatasetGrant]

    def __init__(self, grants: Sequence[DatasetGrant]) -> None:
        """Build a registry, rejecting duplicate dataset identities."""
        if len(grants) > MAX_DATASET_GRANTS:
            raise GrantConfigurationError
        by_id: dict[str, DatasetGrant] = {}
        for grant in grants:
            if not isinstance(grant, DatasetGrant) or grant.dataset_id in by_id:
                raise GrantConfigurationError
            by_id[grant.dataset_id] = grant
        object.__setattr__(self, "_grants", MappingProxyType(by_id))

    @classmethod
    def from_grants(cls, grants: Sequence[DatasetGrant]) -> DatasetGrantRegistry:
        """Create a registry from already validated, server-reviewed grants."""
        return cls(grants)

    def get(self, dataset_id: str) -> DatasetGrant | None:
        """Return one exact dataset grant, or ``None`` when it is absent."""
        return self._grants.get(dataset_id)

    def list_grants(self) -> tuple[DatasetGrant, ...]:
        """Return grants in stable dataset-id order for authorized catalog filtering."""
        return tuple(self._grants[key] for key in sorted(self._grants))


def load_dataset_grant_registry(path: str | Path) -> DatasetGrantRegistry:
    """Load a bounded, duplicate-free JSON grant file from an explicit server path.

    The caller must choose the trusted path and keep it outside client-writable
    locations. This function performs no environment lookup, logging, or printing.

    Args:
        path: Explicit path to the reviewed JSON configuration.

    Returns:
        An immutable registry of validated grants.

    Raises:
        GrantConfigurationError: If the file is too large or invalid.
    """
    try:
        with Path(path).open("rb") as config_file:
            content = config_file.read(MAX_GRANT_CONFIG_BYTES + 1)
        if len(content) > MAX_GRANT_CONFIG_BYTES:
            raise GrantConfigurationError
        decoded = content.decode("utf-8")
        json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        document = _GrantDocument.model_validate_json(content)
        return DatasetGrantRegistry(document.datasets)
    except GrantConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise GrantConfigurationError from None


def require_published_access(
    principal: Principal,
    dataset_id: str,
    operation: str,
    registry: DatasetGrantRegistry,
    now: datetime | None = None,
    purpose: str = "research",
) -> AccessContext:
    """Authorize one exact dataset operation and return stable binding facts.

    API keys must carry both the exact operation scope (for example
    ``data.read``) and the dataset domain scope. The legacy ``*`` scope only
    satisfies the domain check; it never grants an operation by itself.
    Ordinary JWT users receive published read and snapshot access, while
    ``job.manage`` is restricted to administrators and operator grants.

    Args:
        principal: Authenticated user or API-key principal.
        dataset_id: Exact dataset identifier requested.
        operation: Exact operation such as ``data.read`` or ``snapshot.create``.
        registry: Server-injected operator-reviewed registry.
        now: Optional aware clock value for deterministic boundary tests.
        purpose: Caller-declared purpose that must match the reviewed grant.

    Returns:
        Access metadata suitable for query and cursor binding.

    Raises:
        PublishedAccessError: For every denial, with a uniform public message.
    """
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise PublishedAccessError
    if not isinstance(dataset_id, str) or not isinstance(operation, str):
        raise PublishedAccessError
    if not _IDENTIFIER_RE.fullmatch(dataset_id) or not _OPERATION_RE.fullmatch(operation):
        raise PublishedAccessError
    if (
        not isinstance(purpose, str)
        or not _PURPOSE_RE.fullmatch(purpose)
        or not isinstance(registry, DatasetGrantRegistry)
    ):
        raise PublishedAccessError
    if not isinstance(principal, Principal) or not isinstance(principal.user, User):
        raise PublishedAccessError

    user = principal.user
    if type(user.id) is not int or user.id <= 0 or not user.is_active:
        raise PublishedAccessError
    if operation == "job.manage" and user.role is not UserRole.ADMIN:
        raise PublishedAccessError

    if principal.uses_api_key:
        if type(principal.api_key_id) is not int or principal.api_key_id <= 0:
            raise PublishedAccessError
        if (
            not isinstance(principal.scopes, tuple)
            or len(principal.scopes) > 256
            or not all(
                isinstance(scope, str) and 0 < len(scope) <= 128 for scope in principal.scopes
            )
        ):
            raise PublishedAccessError
    elif principal.scopes is not None:
        raise PublishedAccessError

    grant = registry.get(dataset_id)
    if grant is None:
        raise PublishedAccessError
    current = moment.astimezone(timezone.utc)
    start = grant.valid_from
    end = grant.valid_until
    if current < start or current >= end:
        raise PublishedAccessError
    if operation not in grant.permitted_operations or purpose not in grant.purposes:
        raise PublishedAccessError
    if grant.subject_ids is not None and user.id not in grant.subject_ids:
        raise PublishedAccessError

    if principal.uses_api_key:
        scopes = principal.scopes or ()
        if operation not in scopes or not principal.allows_domain(grant.domain):
            raise PublishedAccessError
        access_scope: tuple[str, ...] | None = tuple(sorted(scopes))
    else:
        if user.role is not UserRole.ADMIN and operation not in _JWT_USER_OPERATIONS:
            raise PublishedAccessError
        access_scope = None

    grant_sha256 = _grant_digest(grant)
    binding_sha256 = _binding_digest(
        principal=principal,
        grant_sha256=grant_sha256,
        dataset_id=dataset_id,
        purpose=purpose,
    )
    return AccessContext(
        dataset_id=grant.dataset_id,
        domain=grant.domain,
        source_id=grant.source_id,
        operation=operation,
        purpose=purpose,
        access_scope=access_scope,
        grant=grant,
        grant_sha256=grant_sha256,
        binding_sha256=binding_sha256,
        subject_id=user.id,
    )


def list_permitted_datasets(
    principal: Principal,
    registry: DatasetGrantRegistry,
    operation: str = "data.read",
    now: datetime | None = None,
    purpose: str = "research",
) -> tuple[AccessContext, ...]:
    """List only grants usable by this principal, operation, and purpose."""
    contexts: list[AccessContext] = []
    for grant in registry.list_grants():
        context = _try_published_access(
            principal,
            grant.dataset_id,
            operation,
            registry,
            now=now,
            purpose=purpose,
        )
        if context is not None:
            contexts.append(context)
    return tuple(contexts)


def _try_published_access(
    principal: Principal,
    dataset_id: str,
    operation: str,
    registry: DatasetGrantRegistry,
    *,
    now: datetime | None,
    purpose: str,
) -> AccessContext | None:
    """Return an authorized context, or ``None`` for catalog filtering."""
    try:
        return require_published_access(
            principal,
            dataset_id,
            operation,
            registry,
            now=now,
            purpose=purpose,
        )
    except PublishedAccessError:
        return None


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting duplicate keys at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON NaN and infinity literals."""
    raise ValueError(f"invalid JSON constant: {value}")


def _grant_digest(grant: DatasetGrant) -> str:
    """Hash the canonical, secret-free representation of one grant."""
    canonical = json.dumps(
        grant.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _binding_digest(
    *,
    principal: Principal,
    grant_sha256: str,
    dataset_id: str,
    purpose: str,
) -> str:
    """Bind opaque continuation tokens to identity, key scopes, and grant state."""
    scopes = None if principal.scopes is None else sorted(principal.scopes)
    canonical = json.dumps(
        {
            "dataset_id": dataset_id,
            "grant_sha256": grant_sha256,
            "principal": {
                "api_key_id": principal.api_key_id,
                "scopes": scopes,
                "user_id": principal.user.id,
            },
            "purpose": purpose,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_IDENTIFIER_RE = re.compile(_IDENTIFIER_PATTERN)
_OPERATION_RE = re.compile(_OPERATION_PATTERN)
_PURPOSE_RE = re.compile(_PURPOSE_PATTERN)
_JWT_USER_OPERATIONS = frozenset(
    {"data.read", "dataset.read", "coverage.read", "snapshot.read", "snapshot.create"}
)


__all__ = [
    "AccessContext",
    "DatasetGrant",
    "DatasetGrantRegistry",
    "GrantConfigurationError",
    "MAX_DATASET_GRANTS",
    "MAX_GRANT_CONFIG_BYTES",
    "PublishedAccessError",
    "list_permitted_datasets",
    "load_dataset_grant_registry",
    "require_published_access",
]
