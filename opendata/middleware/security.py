"""
Security headers middleware.

Adds common security headers to each response. The Strict-Transport-Security
header (HSTS) is enabled only in production to avoid interfering with local/dev
environments.
"""

from fastapi import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from loguru import logger

from opendata.core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that injects security-related HTTP headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)

        # Always set these headers
        try:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["X-XSS-Protection"] = "1; mode=block"
        except Exception as e:
            # Should not affect normal response; log but don't break flow
            logger.debug("Failed to set security headers: %s", e)

        # Enable HSTS only in production
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            logger.debug("HSTS header applied (production)")

        return response
