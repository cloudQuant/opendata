"""
Request logging middleware.

Logs request ID, method, path, status code, and response time for every request.
Uses loguru contextualize() so all downstream log messages automatically include
the request_id (trace_id) without manual threading.
"""

import time
import uuid

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that logs request details and response time."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        start_time = time.perf_counter()

        # Skip logging for health checks and static files
        path = request.url.path
        skip_log = path in ("/health",) or path.startswith("/assets/")

        # Bind request_id to all log messages within this request scope
        with logger.contextualize(request_id=request_id):
            if not skip_log:
                logger.info(f"{request.method} {path}")

            try:
                response = await call_next(request)
            except Exception as e:
                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.error(
                    "%s %s 500 (%.1fms) exception=%s",
                    request.method,
                    path,
                    duration_ms,
                    e,
                )
                raise

            duration_ms = (time.perf_counter() - start_time) * 1000

            if not skip_log:
                log_fn = logger.info if response.status_code < 400 else logger.warning
                log_fn(f"{request.method} {path} {response.status_code} ({duration_ms:.1f}ms)")

            # Record metrics for Prometheus endpoint
            try:
                from opendata.api.metrics import metrics

                metrics.record_request(
                    request.method, path, response.status_code, duration_ms / 1000
                )
            except Exception as e:
                logger.debug("Metrics record skipped: %s", e)

        response.headers["X-Request-ID"] = request_id
        return response
