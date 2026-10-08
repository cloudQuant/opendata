"""Request-scoped source-policy loading for provider-model routes."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import Depends, HTTPException, status

from opendata.api.dependencies import get_current_principal
from opendata.core.config import get_settings
from opendata.data.protocol import FetchContext
from opendata.data.source_policy import (
    SourcePolicyConfigurationError,
    build_source_policy_context,
    parse_source_policy_document,
)
from opendata.services.provider_models import ProviderModelIdentityError, validate_model_identity

if TYPE_CHECKING:
    from opendata.data.request_budget import RequestOperation
    from opendata.data.source_policy import SourcePolicyDocument

_MAX_SOURCE_POLICY_BYTES = 1024 * 1024
_SOURCE_POLICY_UNAVAILABLE = "Provider source policy is unavailable"
_SOURCE_POLICY_FORBIDDEN = "Not authorized for this provider model"


def get_provider_model_context(
    *,
    source: str,
    model: str,
    operation: RequestOperation,
    principal: Any,  # noqa: ANN401 - FastAPI principal dependency
) -> FetchContext | None:
    """Build a fresh context from the configured policy for one route request."""
    policy_path = get_settings().provider_source_policy_file
    if policy_path is None:
        return None

    try:
        validate_model_identity(source, model)
    except (ProviderModelIdentityError, TypeError, ValueError):
        # Keep the service's established 400 identity and 404 registration behavior.
        return None

    principal_user_id = _principal_user_id(principal)
    document = _load_policy_document(policy_path)
    try:
        context = build_source_policy_context(
            document,
            source=source,
            canonical_model=model,
            operation=operation,
            principal_user_id=principal_user_id,
            timeout=30.0,
        )
    except SourcePolicyConfigurationError:
        _raise_unavailable()
    except Exception:
        _raise_unavailable()
    if not isinstance(context, FetchContext):
        _raise_unavailable()
    return context


def _principal_user_id(principal: object) -> int:
    """Return only an exact positive integer owner id from the authenticated principal."""
    try:
        user_id = principal.user.id  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        _raise_forbidden()
    if type(user_id) is not int or user_id <= 0:
        _raise_forbidden()
    return user_id


def _load_policy_document(path: Path) -> SourcePolicyDocument:
    """Open a regular policy file without blocking on a FIFO or reading it unboundedly."""
    descriptor: int | None = None
    try:
        if not isinstance(path, Path) or not path.is_absolute():
            _raise_unavailable()
        flags = os.O_RDONLY | os.O_NONBLOCK
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _raise_unavailable()

        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_SOURCE_POLICY_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_SOURCE_POLICY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_SOURCE_POLICY_BYTES:
            _raise_unavailable()

        body = b"".join(chunks)
        body.decode("utf-8", errors="strict")
        return parse_source_policy_document(body)
    except HTTPException:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        _raise_unavailable()
    except SourcePolicyConfigurationError:
        _raise_unavailable()
    except Exception:
        _raise_unavailable()
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _raise_unavailable() -> NoReturn:
    """Raise a stable configuration response without reflecting policy contents."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_SOURCE_POLICY_UNAVAILABLE,
    ) from None


def _raise_forbidden() -> NoReturn:
    """Reject a request whose authenticated owner has no usable identity."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_SOURCE_POLICY_FORBIDDEN,
    ) from None


def current_provider_model_context(
    source: str,
    model: str,
    operation: RequestOperation,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
) -> FetchContext | None:
    """FastAPI dependency adapter for an exact route identity and principal."""
    return get_provider_model_context(
        source=source,
        model=model,
        operation=operation,
        principal=current_principal,
    )


__all__ = ["current_provider_model_context", "get_provider_model_context"]
