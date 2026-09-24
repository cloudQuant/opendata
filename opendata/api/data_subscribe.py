"""WebSocket data subscription (design §10.2, AC-11/FR-16).

``WS /ws/data/subscribe`` replaces the ``?token=`` pattern of the older
executions socket with **first-frame authentication**: the socket is
accepted, then the client has ``FIRST_FRAME_TIMEOUT`` seconds to send
``{"action": "auth", "token": "..."}``. Credentials never travel in the
URL, so they cannot leak into nginx access logs or proxy caches.

The protocol is "meta first": a subscription defaults to notifications
(domain, source, batch, window, row count) and the client pulls the
rows over REST when it wants them. ``payload: "full"`` is experimental
and opt-in, and it is framed by the hub with hard row/byte ceilings.

Reconnection is a first-class case, not an afterthought: the pipeline
records every batch in ``batch_watermark`` *before* pushing it, so a
client that comes back with ``since_batch_id`` has the missed events
replayed from the durable record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select

from opendata.api.dependencies import Principal
from opendata.core.security import verify_token
from opendata.pipeline.subscription import (
    MAX_BACKLOG,
    MAX_SUBSCRIPTIONS_PER_CONNECTION,
    PAYLOAD_MODES,
    SUBSCRIBABLE_LAYERS,
    Subscriber,
    Subscription,
    hub,
)
from opendata.pipeline.watermark import (
    DEFAULT_REPLAY_LIMIT,
    BatchWatermark,
    latest_batches,
    replay_since,
)

router = APIRouter()

#: Seconds a client has to complete first-frame authentication.
FIRST_FRAME_TIMEOUT = 5.0
#: Seconds without a client frame before the socket is closed.
IDLE_TIMEOUT = 90.0
#: Seconds between server-side heartbeats.
PING_INTERVAL = 30.0

#: Close codes (4001+ are application-defined, per RFC 6455).
CLOSE_AUTH_FAILED = 4001
CLOSE_IDLE_TIMEOUT = 4002

#: Error codes with the Chinese business wording and the remedy the
#: design asks for (``{"type": "error", "code", "message", "suggestion"}``).
ERROR_TEXT: dict[str, tuple[str, str]] = {
    "AUTH_REQUIRED": (
        "未在时限内完成首帧认证",
        f'连接后 {FIRST_FRAME_TIMEOUT:g} 秒内发送 {{"action": "auth", "token": "..."}}',
    ),
    "AUTH_FAILED": (
        "令牌或 API Key 无效、已过期或已吊销",
        "重新登录获取 JWT，或确认 API Key 未过期；不要使用 query 参数传令牌",
    ),
    "INVALID_MESSAGE": (
        "消息不是合法 JSON 或缺少 action 字段",
        '发送形如 {"action": "subscribe", "domain": "stock_daily"} 的 JSON 文本帧',
    ),
    "UNKNOWN_ACTION": (
        "未知的 action",
        "支持 auth / subscribe / unsubscribe / ping 四种动作",
    ),
    "INVALID_SUBSCRIPTION": (
        "订阅参数不合法",
        f"layer 取值为 {SUBSCRIBABLE_LAYERS}，payload 取值为 {PAYLOAD_MODES}，domain 必须已注册",
    ),
    "SUBSCRIPTION_LIMIT": (
        "单连接订阅数超过上限",
        f"每个连接最多订阅 {MAX_SUBSCRIPTIONS_PER_CONNECTION} 个数据域，请先 unsubscribe 再订阅",
    ),
    "DOMAIN_FORBIDDEN": (
        "当前凭证无权读取该数据域",
        "使用具备该域 scope 的 API Key，或改用有权限的账号",
    ),
    "REPLAY_UNAVAILABLE": (
        "补发水位读取失败",
        "稍后重试，或改用 REST 按窗口重新拉取",
    ),
}


def _error(code: str, **extra: object) -> dict[str, Any]:
    """Render an error frame with its business wording.

    Args:
        code: One of :data:`ERROR_TEXT`.
        **extra: Additional fields merged into the frame.

    Returns:
        The ``error`` message.
    """
    message, suggestion = ERROR_TEXT.get(code, (code, "请联系管理员"))
    return {"type": "error", "code": code, "message": message, "suggestion": suggestion, **extra}


@dataclass
class _Session:
    """Per-connection state.

    The subscription map is the hub subscriber's own dict rather than a
    copy: the hub routes by what it holds, so a parallel map in the
    route would silently deliver nothing.

    Attributes:
        principal: The authenticated caller.
        subscriber: The hub handle this connection registered.
        queue: Bounded outbound queue (the hub sheds payload past it).
        replays: Batches replayed per domain, for the subscribe ack.
    """

    principal: Principal
    subscriber: Subscriber
    queue: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=MAX_BACKLOG)
    )
    replays: dict[str, int] = field(default_factory=dict)

    @property
    def subscriptions(self) -> dict[str, Subscription]:
        """The interests the hub routes by.

        Returns:
            The live subscription map of this connection.
        """
        return self.subscriber.subscriptions


@router.websocket("/ws/data/subscribe")
async def data_subscribe(ws: WebSocket) -> None:
    """Subscribe to live data batches of one or more domains.

    The client authenticates with the first frame, then subscribes::

        {"action": "auth", "token": "<jwt or od-key>"}
        {"action": "subscribe", "domain": "stock_daily", "layer": "dwd",
         "payload": "meta", "symbols": ["600519"], "since_batch_id": "..."}

    and receives ``data.update`` notifications, ``data.diff_alert``
    broadcasts and a ``ping`` every 30 seconds. Sending nothing for 90
    seconds closes the socket.
    """
    await ws.accept()
    principal = await _await_auth(ws)
    if principal is None:
        return

    subscriber = await hub.register(_make_send(ws))
    session = _Session(principal=principal, subscriber=subscriber)
    hub.attach_queue(subscriber, session.queue)
    writer = asyncio.create_task(_writer(ws, session.queue))
    try:
        await _receive_loop(ws, session)
    finally:
        writer.cancel()
        session.queue.put_nowait(None)
        await hub.unregister(subscriber)
        logger.debug(f"data subscription closed, subscribers={hub.subscriber_count}")


async def _await_auth(ws: WebSocket) -> Principal | None:
    """Read the first frame and authenticate it.

    Args:
        ws: The accepted socket.

    Returns:
        The principal, or None when authentication failed (the socket
        is already closed by then).
    """
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=FIRST_FRAME_TIMEOUT)
    except (TimeoutError, WebSocketDisconnect):
        await _fail(ws, "AUTH_REQUIRED", CLOSE_AUTH_FAILED)
        return None
    message = _parse(raw)
    if message is None or message.get("action") != "auth":
        await _fail(ws, "AUTH_REQUIRED", CLOSE_AUTH_FAILED)
        return None
    token = message.get("token")
    if not isinstance(token, str) or not token:
        await _fail(ws, "AUTH_FAILED", CLOSE_AUTH_FAILED)
        return None
    principal = await principal_resolver(token)
    if principal is None:
        await _fail(ws, "AUTH_FAILED", CLOSE_AUTH_FAILED)
        return None
    await ws.send_text(
        json.dumps(
            {
                "type": "auth.ok",
                "user": principal.user.username,
                "scopes": None if principal.scopes is None else list(principal.scopes),
            }
        )
    )
    return principal


async def _resolve_principal(
    token: str,
    *,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> Principal | None:
    """Resolve a JWT or API key into a principal.

    The credential arrives in the first *frame*, not in the handshake,
    so it cannot be a FastAPI dependency - and that is exactly why the
    socket is testable through the :data:`principal_resolver` seam
    instead of a live user database.

    Args:
        token: The credential from the auth frame.
        session_maker: Session factory to read the user/key from; the
            application's own factory when omitted (tests inject theirs
            so the resolver itself is covered, not just the seam).

    Returns:
        The principal, or None when the credential is not valid.
    """
    from opendata.models.user import User
    from opendata.services.api_key_service import KEY_PREFIX, ApiKeyError, ApiKeyService

    if session_maker is None:
        from opendata.core.database import async_session_maker

        session_maker = async_session_maker

    async with session_maker() as db:
        if token.startswith(KEY_PREFIX):
            service = ApiKeyService(db)
            try:
                record = await service.authenticate(token)
            except ApiKeyError:
                return None
            if record is None:
                return None
            owner = await db.get(User, record.owner_user_id)
            if owner is None or not owner.is_active:
                return None
            return Principal(user=owner, api_key_id=record.id, scopes=tuple(record.scopes or ()))
        from opendata.core.token_blacklist import token_blacklist

        if token_blacklist.is_revoked(token):
            return None
        # "access" is the token type, not a credential (bandit B106 is a
        # literal-argument heuristic); the same nosec as the REST path.
        payload = verify_token(
            token,
            token_type="access",  # noqa: S106  # nosec B106  # token type, not a secret
        )
        if payload is None:
            return None
        user_id = payload.get("sub")
        if user_id is None:
            return None
        user = (await db.execute(select(User).where(User.id == int(user_id)))).scalar_one_or_none()
        if user is None or not user.is_active:
            return None
        return Principal(user=user)


#: Credential resolver used by the auth frame. Production keeps the real
#: one; tests swap it so the socket protocol can be driven without a
#: live user database (the credential travels in a frame, so no FastAPI
#: dependency can see it).
principal_resolver: Callable[[str], Awaitable[Principal | None]] = _resolve_principal


def _default_warehouse_engine() -> Engine:
    """Return the process-wide warehouse engine (design §8.1).

    Returns:
        The warehouse engine shared with the REST query layer.
    """
    from opendata.api.data_query import get_warehouse_engine

    return get_warehouse_engine()


#: Warehouse engine used for watermark replay. A seam for the same
#: reason as the resolver: the replay runs inside a socket, where no
#: dependency override can reach it.
warehouse_engine_factory: Callable[[], Engine] = _default_warehouse_engine


def _make_send(ws: WebSocket) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Build the hub's async sink for one socket.

    Args:
        ws: The socket to write to.

    Returns:
        An async callable that serializes and sends one message.
    """

    async def send(message: dict[str, Any]) -> None:
        await ws.send_text(json.dumps(message, default=str, ensure_ascii=False))

    return send


async def _writer(ws: WebSocket, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
    """Drain the outbound queue, heartbeating while it is idle.

    Args:
        ws: The socket to write to.
        queue: The connection's bounded outbound queue.
    """
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
            except TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            if message is None:  # shutdown sentinel
                return
            await ws.send_text(json.dumps(message, default=str, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.debug(f"data subscription writer ended: {exc}")


async def _receive_loop(ws: WebSocket, session: _Session) -> None:
    """Handle client frames until the socket closes.

    Args:
        ws: The socket.
        session: The authenticated session.
    """
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=IDLE_TIMEOUT)
            except TimeoutError:
                await ws.close(code=CLOSE_IDLE_TIMEOUT, reason="Idle timeout")
                return
            if raw.strip().lower() == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
                continue
            message = _parse(raw)
            if message is None:
                await _send(ws, _error("INVALID_MESSAGE"))
                continue
            await _handle(ws, session, message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # a protocol error must not take the server down
        logger.debug(f"data subscription receive loop ended: {exc}")


async def _handle(ws: WebSocket, session: _Session, message: dict[str, Any]) -> None:
    """Dispatch one client action.

    Args:
        ws: The socket.
        session: The authenticated session.
        message: The parsed frame.
    """
    action = message.get("action")
    if action == "ping":
        await _send(ws, {"type": "pong"})
    elif action == "subscribe":
        await _subscribe(ws, session, message)
    elif action == "unsubscribe":
        await _unsubscribe(ws, session, message)
    elif action == "auth":
        await _send(ws, _error("UNKNOWN_ACTION", detail="already authenticated"))
    else:
        await _send(ws, _error("UNKNOWN_ACTION", detail=str(action)))


async def _subscribe(ws: WebSocket, session: _Session, message: dict[str, Any]) -> None:
    """Handle a ``subscribe`` action, replaying when asked.

    Args:
        ws: The socket.
        session: The authenticated session.
        message: The parsed frame.
    """
    domain = message.get("domain")
    layer = message.get("layer", "dwd")
    payload = message.get("payload", "meta")
    symbols = message.get("symbols") or []
    if (
        not isinstance(domain, str)
        or layer not in SUBSCRIBABLE_LAYERS
        or payload not in PAYLOAD_MODES
        or not isinstance(symbols, list)
    ):
        await _send(ws, _error("INVALID_SUBSCRIPTION"))
        return
    if not _domain_exists(domain):
        await _send(ws, _error("INVALID_SUBSCRIPTION", detail=f"unknown domain {domain!r}"))
        return
    if not session.principal.allows_domain(domain):
        await _send(ws, _error("DOMAIN_FORBIDDEN", domain=domain))
        return
    if domain not in session.subscriptions and len(session.subscriptions) >= (
        MAX_SUBSCRIPTIONS_PER_CONNECTION
    ):
        await _send(ws, _error("SUBSCRIPTION_LIMIT", domain=domain))
        return
    session.subscriptions[domain] = Subscription(
        domain=domain,
        layer=layer,
        payload=payload,
        symbols=tuple(str(item) for item in symbols),
    )
    replayed = await _replay(ws, session, domain, layer, message.get("since_batch_id"))
    await _send(
        ws,
        {
            "type": "subscribe.ok",
            "domain": domain,
            "layer": layer,
            "payload": payload,
            "symbols": list(symbols),
            "replayed": replayed,
        },
    )


async def _unsubscribe(ws: WebSocket, session: _Session, message: dict[str, Any]) -> None:
    """Handle an ``unsubscribe`` action.

    Args:
        ws: The socket.
        session: The authenticated session.
        message: The parsed frame.
    """
    domain = message.get("domain")
    if not isinstance(domain, str):
        await _send(ws, _error("INVALID_SUBSCRIPTION"))
        return
    session.subscriptions.pop(domain, None)
    await _send(ws, {"type": "unsubscribe.ok", "domain": domain})


async def _replay(
    ws: WebSocket,
    session: _Session,
    domain: str,
    layer: str,
    since_batch_id: object,
) -> int:
    """Replay the batches a reconnecting client missed.

    Replays are always ``meta`` frames: the watermark is a notification
    log, not a row store, and the client pulls the batches it decides
    it wants over REST.

    Args:
        ws: The socket.
        session: The authenticated session.
        domain: Domain being subscribed.
        layer: Layer filter of the subscription.
        since_batch_id: Last batch the client processed, if any.

    Returns:
        Number of events replayed (0 when nothing was missed).
    """
    try:
        engine = warehouse_engine_factory()
        if isinstance(since_batch_id, str) and since_batch_id:
            batches = await asyncio.to_thread(
                replay_since, engine, domain=domain, since_batch_id=since_batch_id
            )
        else:
            batches = await asyncio.to_thread(
                latest_batches, engine, domain=domain, limit=DEFAULT_REPLAY_LIMIT
            )
    except Exception as exc:  # warehouse not migrated, or unreachable
        logger.warning(f"watermark replay unavailable for {domain}: {exc}")
        await _send(ws, _error("REPLAY_UNAVAILABLE", domain=domain))
        return 0
    replayed = [batch for batch in batches if batch.layer == layer]
    for batch in replayed:
        await _send(ws, _replay_message(batch))
    session.replays[domain] = len(replayed)
    return len(replayed)


def _replay_message(batch: BatchWatermark) -> dict[str, Any]:
    """Render one replayed batch as a meta frame.

    Args:
        batch: The recorded batch.

    Returns:
        The ``data.update`` frame, carrying ``replayed: true`` so a
        client can tell catch-up traffic from live traffic.
    """
    return {"type": "data.update", "payload": "meta", "replayed": True, **batch.to_event()}


def _domain_exists(domain: str) -> bool:
    """Report whether a domain is registered.

    Args:
        domain: Domain identifier.

    Returns:
        True when the domain registry knows it.
    """
    from opendata.data.domains import require_domain

    try:
        require_domain(domain)
    except LookupError:
        return False
    return True


async def _send(ws: WebSocket, message: dict[str, Any]) -> None:
    """Send one frame, tolerating a socket that just closed.

    Args:
        ws: The socket.
        message: The message to send.
    """
    try:
        await ws.send_text(json.dumps(message, default=str, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.debug(f"data subscription send failed: {exc}")


async def _fail(ws: WebSocket, code: str, close_code: int) -> None:
    """Send an error frame and close the socket.

    Args:
        ws: The socket.
        code: Error code from :data:`ERROR_TEXT`.
        close_code: WebSocket close code.
    """
    await _send(ws, _error(code))
    try:
        await ws.close(code=close_code, reason=code)
    except RuntimeError as exc:  # already closed
        logger.debug(f"data subscription close failed: {exc}")


def _parse(raw: str) -> dict[str, Any] | None:
    """Parse a client frame.

    Args:
        raw: The raw text frame.

    Returns:
        The parsed object, or None when it is not a JSON object.
    """
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return message if isinstance(message, dict) else None
