"""
WebSocket endpoint for real-time task execution status updates.

Clients connect to /ws/executions and receive JSON messages whenever
a task execution changes status (PENDING → RUNNING → COMPLETED/FAILED).
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter()


# ---------------------------------------------------------------------------
# Connection manager (pub/sub hub)
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)
        logger.debug(f"WebSocket connected, total={len(self._connections)}")

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections = [c for c in self._connections if c is not ws]
        logger.debug(f"WebSocket disconnected, total={len(self._connections)}")

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all connected clients."""
        if not self._connections:
            return

        payload = json.dumps(message, default=str)
        stale: list[WebSocket] = []

        async with self._lock:
            for ws in self._connections:
                try:
                    await ws.send_text(payload)
                except Exception as e:
                    logger.debug(f"WebSocket send failed (stale connection): {e}")
                    stale.append(ws)

            # Clean up broken connections
            for ws in stale:
                self._connections = [c for c in self._connections if c is not ws]

    @property
    def active_count(self) -> int:
        return len(self._connections)


# Global singleton
ws_manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Helper to broadcast from anywhere in the codebase
# ---------------------------------------------------------------------------


async def broadcast_execution_update(
    execution_id: str,
    task_id: int | None,
    status: str,
    *,
    rows_before: int | None = None,
    rows_after: int | None = None,
    error_message: str | None = None,
    duration: float | None = None,
) -> None:
    """Broadcast an execution status change to all WebSocket clients.

    Call this from scheduler / execution_service whenever status changes.
    """
    await ws_manager.broadcast(
        {
            "type": "execution_update",
            "data": {
                "execution_id": execution_id,
                "task_id": task_id,
                "status": status,
                "rows_before": rows_before,
                "rows_after": rows_after,
                "error_message": error_message,
                "duration": duration,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        }
    )


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


async def _authenticate_ws(ws: WebSocket) -> bool:
    """Validate token query param for WebSocket connections.

    Accepts ``/ws/executions?token=<jwt>``.  Returns True if valid,
    False otherwise (caller should close the socket).
    """
    token = ws.query_params.get("token")
    if not token:
        return False

    from opendata.core.security import verify_token
    from opendata.core.token_blacklist import token_blacklist

    if token_blacklist.is_revoked(token):
        return False

    payload = verify_token(token, token_type="access")
    return payload is not None


_WS_IDLE_TIMEOUT = 90  # seconds: disconnect if no client message received
_WS_PING_INTERVAL = 30  # seconds: server-side ping to keep connection alive


@router.websocket("/ws/executions")
async def execution_updates(ws: WebSocket) -> None:
    """
    WebSocket endpoint for real-time execution updates.

    Connect with a valid JWT token as query parameter:
    ``ws://host/ws/executions?token=<access_token>``

    Clients receive JSON messages:
    ```json
    {
      "type": "execution_update",
      "data": {
        "execution_id": "exec_20250225_...",
        "task_id": 42,
        "status": "running",
        "timestamp": "2025-02-25T12:00:00+00:00"
      }
    }
    ```

    The server sends ``{"type": "ping"}`` every 30 s. If the client sends
    nothing for 90 s the connection is closed to prevent zombie sockets.
    """
    # Authenticate before accepting
    if not await _authenticate_ws(ws):
        await ws.close(code=4001, reason="Authentication required")
        return

    await ws_manager.connect(ws)

    async def _server_ping() -> None:
        """Periodically send server-side ping to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(_WS_PING_INTERVAL)
                await ws.send_text(json.dumps({"type": "ping"}))
        except Exception as e:
            logger.debug("WebSocket ping loop ended: %s", e)

    ping_task = asyncio.create_task(_server_ping())
    try:
        while True:
            # Apply idle timeout: if client sends nothing within the window, disconnect
            data = await asyncio.wait_for(ws.receive_text(), timeout=_WS_IDLE_TIMEOUT)
            # Clients can send "ping" to keep alive
            if data.strip().lower() == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except TimeoutError:
        logger.debug("WebSocket idle timeout, closing connection")
        await ws.close(code=4002, reason="Idle timeout")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        ping_task.cancel()
        await ws_manager.disconnect(ws)
