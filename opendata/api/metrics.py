"""
Lightweight Prometheus-compatible metrics endpoint.

Exposes request counts, latency histograms, and and task execution stats
in Prometheus text exposition format without requiring a heavy client library.

Supports multi-process aggregation via Redis when available.
"""

import os
import threading
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Response
from loguru import logger

router = APIRouter()


class _Metrics:
    """Thread-safe in-process metrics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_count: dict[str, int] = defaultdict(int)
        self._request_duration_sum: dict[str, float] = defaultdict(float)
        self._task_executions: dict[str, int] = defaultdict(int)
        self._start_time = time.time()

    def record_request(self, method: str, path: str, status: int, duration_s: float) -> None:
        key = f"{method}:{path}:{status}"
        with self._lock:
            self._request_count[key] += 1
            self._request_duration_sum[key] += duration_s

    def record_task_execution(self, status: str) -> None:
        with self._lock:
            self._task_executions[status] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "request_count": dict(self._request_count),
                "request_duration_sum": dict(self._request_duration_sum),
                "task_executions": dict(self._task_executions),
                "uptime_seconds": time.time() - self._start_time,
            }

    def to_prometheus(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        lines: list[str] = []
        snap = self.snapshot()

        lines.append("# HELP akshare_uptime_seconds Time since process start")
        lines.append("# TYPE akshare_uptime_seconds gauge")
        lines.append(f"akshare_uptime_seconds {snap['uptime_seconds']:.1f}")

        lines.append("# HELP akshare_http_requests_total Total HTTP requests")
        lines.append("# TYPE akshare_http_requests_total counter")
        for key, count in snap["request_count"].items():
            method, path, status = key.rsplit(":", 2)
            norm_path = _normalize_path(path)
            lines.append(
                f'akshare_http_requests_total{{method="{method}",path="{norm_path}",status="{status}"}} {count}'
            )

        lines.append(
            "# HELP akshare_http_request_duration_seconds_total Sum of HTTP request durations"
        )
        lines.append("# TYPE akshare_http_request_duration_seconds_total counter")
        for key, total in snap["request_duration_sum"].items():
            method, path, status = key.rsplit(":", 2)
            norm_path = _normalize_path(path)
            lines.append(
                f'akshare_http_request_duration_seconds_total{{method="{method}",path="{norm_path}",status="{status}"}} {total:.4f}'
            )

        lines.append("# HELP akshare_task_executions_total Total task executions by status")
        lines.append("# TYPE akshare_task_executions_total counter")
        for status, count in snap["task_executions"].items():
            lines.append(f'akshare_task_executions_total{{status="{status}"}} {count}')

        lines.append("")
        return "\n".join(lines)


def _normalize_path(path: str) -> str:
    """Reduce path cardinality by replacing dynamic segments."""
    parts = path.strip("/").split("/")
    normalized = []
    for part in parts:
        if part.isdigit():
            normalized.append(":id")
        else:
            normalized.append(part)
    return "/" + "/".join(normalized)


class _RedisMetricsAggregator:
    """Multi-process metrics aggregator using Redis."""

    def __init__(self, redis_client: Any | None):
        self._redis = redis_client
        self._prefix = "akshare:metrics"
        self._ttl = 300

    def _get_redis(self) -> Any:
        if self._redis:
            return self._redis
        try:
            import redis.asyncio as redis

            redis_url = os.getenv("REDIS_URL")
            if not redis_url:
                return None
            return redis.from_url(redis_url)
        except ImportError:
            logger.debug("Redis not available, metrics will not be aggregated")
            return None

    async def increment_counter(self, name: str, labels: dict[str, str], value: float = 1.0) -> None:
        r = await self._get_redis()
        if not r:
            return
        try:
            key = f"{self._prefix}:{name}:{':'.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
            await r.incrbyfloat(key, value)
            await r.expire(key, self._ttl)
        except Exception as e:
            logger.debug(f"Failed to increment Redis metric: {e}")

    async def get_aggregated_metrics(self) -> dict[str, dict[str, float]]:
        r = await self._get_redis()
        if not r:
            return {}
        try:
            result: dict[str, dict[str, float]] = {}
            cursor = 0
            while True:
                cursor, keys = await r.scan(cursor=cursor, match=f"{self._prefix}:*", count=100)
                if not keys:
                    break
                for key in keys:
                    value = await r.get(key)
                    if value:
                        parts = key.decode().split(":")
                        if len(parts) >= 3:
                            metric_name = parts[2]
                            labels_str = ":".join(parts[3:]) if len(parts) > 3 else ""
                            if metric_name not in result:
                                result[metric_name] = {}
                            result[metric_name][labels_str] = float(value)
                if cursor == 0:
                    break
            return result
        except Exception as e:
            logger.debug(f"Failed to get aggregated metrics: {e}")
            return {}


redis_aggregator: _RedisMetricsAggregator | None = None


def init_redis_aggregator() -> None:
    global redis_aggregator
    redis_aggregator = _RedisMetricsAggregator(None)


def record_request_with_aggregation(
    method: str, path: str, status: int, duration_s: float
) -> None:
    metrics.record_request(method, path, status, duration_s)
    if redis_aggregator:
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    redis_aggregator.increment_counter(
                        "http_requests_total",
                        {"method": method, "path": _normalize_path(path), "status": str(status)},
                    )
                )
                asyncio.create_task(
                    redis_aggregator.increment_counter(
                        "http_request_duration_seconds_total",
                        {"method": method, "path": _normalize_path(path), "status": str(status)},
                        duration_s,
                    )
                )
        except RuntimeError:
            pass


metrics = _Metrics()


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus-compatible metrics endpoint."""
    local_metrics = metrics.to_prometheus()

    if redis_aggregator:
        try:
            aggregated = await redis_aggregator.get_aggregated_metrics()
            if aggregated:
                lines = [local_metrics]
                lines.append("\n# Multi-process aggregated metrics from Redis")
                lines.append("# HELP akshare_multiprocess_http_requests_total Aggregated HTTP requests across all workers")
                lines.append("# TYPE akshare_multiprocess_http_requests_total counter")
                for labels_str, count in aggregated.get("http_requests_total", {}).items():
                    lines.append(f"akshare_multiprocess_http_requests_total{{{labels_str}}} {int(count)}")
                lines.append(
                    "# HELP akshare_multiprocess_http_request_duration_seconds_total Aggregated request durations"
                )
                lines.append("# TYPE akshare_multiprocess_http_request_duration_seconds_total counter")
                for labels_str, total in aggregated.get(
                    "http_request_duration_seconds_total", {}
                ).items():
                    lines.append(
                        f"akshare_multiprocess_http_request_duration_seconds_total{{{labels_str}}} {total:.4f}"
                    )
                local_metrics = "\n".join(lines)
        except Exception as e:
            logger.debug(f"Failed to get aggregated metrics: {e}")

    return Response(
        content=local_metrics,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
