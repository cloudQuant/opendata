"""Rate limiting utilities that work with both tests and production.

The slowapi library doesn't work well with httpx's ASGI test client.
This module provides conditional rate limiting that can be disabled in tests.
"""

import os
from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any

from slowapi import Limiter
from slowapi.util import get_remote_address


def is_testing() -> bool:
    """Check if we're in test mode."""
    return os.getenv("TESTING", "false").lower() == "true"


# Singleton limiter instance (created lazily for production)
_limiter: Limiter | None = None


def get_limiter() -> Limiter | None:
    """Get or create the shared rate limiter instance.

    Returns None in test mode.
    """
    global _limiter
    if is_testing():
        return None
    return _get_or_create_limiter()


def _get_or_create_limiter() -> Limiter:
    """Return the shared limiter used to register decorated endpoints."""
    global _limiter
    if _limiter is None:
        _limiter = Limiter(key_func=get_remote_address)
    return _limiter


def rate_limit(limit_string: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Conditional rate limiting decorator.

    In test mode (TESTING=true), the wrapper bypasses the registered limit.
    Otherwise, SlowAPI's supported ``Limiter.limit`` decorator enforces it.

    Args:
        limit_string: Rate limit string (e.g., "5/minute")

    Returns:
        Decorator function
    """

    def decorator(func: Callable) -> Callable:
        parameters = signature(func).parameters
        if "request" in parameters or "websocket" in parameters:
            limited_func = _get_or_create_limiter().limit(limit_string)(func)
        else:
            # SlowAPI can only enforce endpoint limits when the handler exposes
            # a Starlette Request or WebSocket parameter. Preserve the prior
            # pass-through behavior for plain callables used outside routes.
            limited_func = func

        if iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                if is_testing():
                    return await func(*args, **kwargs)
                return await limited_func(*args, **kwargs)

            wrapper: Callable = async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                if is_testing():
                    return func(*args, **kwargs)
                return limited_func(*args, **kwargs)

            wrapper = sync_wrapper

        wrapper.__rate_limit_string__ = limit_string  # type: ignore[attr-defined]
        wrapper.__original_func__ = func  # type: ignore[attr-defined]
        return wrapper

    return decorator
