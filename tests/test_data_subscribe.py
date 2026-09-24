"""WS data subscription tests (AC-11/FR-16, design §10.2).

Three layers:

* framing - the row/byte ceilings, chunking and the meta downgrade,
  exercised as pure functions;
* hub - routing by domain/layer/symbol and the backpressure ladder
  (full -> meta -> drop), driven through the transport-agnostic sink;
* socket - the real ``/ws/data/subscribe`` route through a
  ``TestClient``, proving first-frame auth, subscribe/unsubscribe,
  live ``data.update`` delivery and ``since_batch_id`` replay.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from opendata.api import data_subscribe as subscribe_api
from opendata.main import app
from opendata.pipeline.subscription import (
    MAX_FRAME_ROWS,
    MAX_FULL_ROWS,
    BatchEvent,
    Subscription,
    SubscriptionHub,
    diff_alert_message,
    frame_full,
)
from opendata.pipeline.watermark import BatchWatermark

BATCH = "5d2c8a41-9e3b-4c7f-8a1b-6f0d2e9c4b73"
CREATED = datetime(2026, 9, 24, 8, 30, tzinfo=timezone.utc)


def _watermark(
    *,
    domain: str = "stock_daily",
    source: str = "ths",
    layer: str = "ods",
    rows: int = 3,
    seq: int = 1,
    batch_id: str = BATCH,
) -> BatchWatermark:
    return BatchWatermark(
        batch_id=batch_id,
        domain=domain,
        source=source,
        layer=layer,
        window_start=date(2026, 9, 23),
        window_end=date(2026, 9, 23),
        rows=rows,
        created_at=CREATED,
        seq=seq,
    )


def _event(**kwargs) -> BatchEvent:
    return BatchEvent(watermark=_watermark(**kwargs), symbols=("600519", "000001"))


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


class TestFraming:
    def test_meta_message_carries_the_batch_facts(self):
        message = _event().meta_message()

        assert message["type"] == "data.update"
        assert message["payload"] == "meta"
        assert message["domain"] == "stock_daily"
        assert message["source"] == "ths"
        assert message["batch_id"] == BATCH
        assert message["rows"] == 3
        assert message["window"] == {"start": "2026-09-23", "end": "2026-09-23"}
        assert "truncated" not in message

    def test_truncated_meta_explains_the_remedy(self):
        message = _event().meta_message(truncated=True)

        assert message["truncated"] is True
        assert "REST" in message["reason"]

    def test_small_full_payload_is_a_single_frame(self):
        rows = [{"symbol": "600519", "trade_date": date(2026, 9, 23), "close": 1253.8}]

        frames = frame_full(_event(), rows, chunk_id="chunk-1")

        assert len(frames) == 1
        frame = frames[0]
        assert frame["payload"] == "full"
        assert frame["chunk"] == {"chunk_id": "chunk-1", "seq": 1, "total": 1}
        assert frame["data"][0]["trade_date"] == "2026-09-23"  # dates become ISO strings

    def test_empty_payload_still_emits_one_frame(self):
        frames = frame_full(_event(), [], chunk_id="chunk-1")

        assert len(frames) == 1
        assert frames[0]["data"] == []

    def test_payload_over_the_row_ceiling_is_chunked(self):
        rows = [{"symbol": f"{index:06d}", "close": 1.0} for index in range(MAX_FRAME_ROWS + 10)]

        frames = frame_full(_event(), rows, chunk_id="chunk-1")

        assert len(frames) == 2
        assert [frame["chunk"]["seq"] for frame in frames] == [1, 2]
        assert all(frame["chunk"]["total"] == 2 for frame in frames)
        assert len(frames[0]["data"]) == MAX_FRAME_ROWS
        assert len(frames[1]["data"]) == 10

    def test_payload_over_the_total_ceiling_degrades_to_meta(self):
        rows = [{"symbol": f"{index:06d}", "close": 1.0} for index in range(MAX_FULL_ROWS + 1)]

        frames = frame_full(_event(), rows, chunk_id="chunk-1")

        assert len(frames) == 1
        assert frames[0]["payload"] == "meta"
        assert frames[0]["truncated"] is True
        assert "data" not in frames[0]

    def test_diff_alert_message_shape(self):
        message = diff_alert_message(
            domain="stock_daily",
            source_a="akshare",
            source_b="ths",
            batch_id=BATCH,
            mismatch_ratio=0.012,
            mismatches=3,
            compared=250,
        )

        assert message["type"] == "data.diff_alert"
        assert message["mismatch_ratio"] == 0.012
        assert message["compared"] == 250


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


class _Sink:
    """Collects what the hub delivers to one subscriber."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, message: dict) -> None:
        self.messages.append(message)


class TestHub:
    async def test_delivers_only_to_matching_subscriptions(self):
        hub = SubscriptionHub()
        matching, other_domain, other_layer = _Sink(), _Sink(), _Sink()
        await hub.register(matching.send)
        subscriber = await hub.register(other_domain.send)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="dwd")
        subscriber2 = await hub.register(other_layer.send)
        subscriber2.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")

        first = hub._subscribers[0]
        first.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")
        delivered = await hub.publish_batch(_event())

        assert delivered == 2
        assert len(matching.messages) == 1
        assert other_domain.messages == []  # layer mismatch
        assert len(other_layer.messages) == 1

    async def test_symbol_filter_selects_the_matching_events(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", symbols=("600519",)
        )

        await hub.publish_batch(_event())
        await hub.publish_batch(BatchEvent(watermark=_watermark(), symbols=("000002",)))

        assert len(sink.messages) == 1

    async def test_replay_events_without_symbols_reach_every_subscriber(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", symbols=("600519",)
        )

        # A replayed event carries no symbol set; dropping it would lose
        # a real update, so it is delivered and the client filters.
        await hub.publish_batch(BatchEvent(watermark=_watermark()))

        assert len(sink.messages) == 1

    async def test_full_payload_is_only_built_for_subscribers_that_want_it(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", payload="full"
        )
        rows = [{"symbol": "600519", "close": 1.0}]

        assert hub.wants_full(_event()) is True
        await hub.publish_batch(_event(), rows=rows)

        assert sink.messages[0]["payload"] == "full"
        assert sink.messages[0]["data"] == rows

    async def test_meta_subscriber_ignores_supplied_rows(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")

        assert hub.wants_full(_event()) is False
        await hub.publish_batch(_event(), rows=[{"symbol": "600519"}])

        assert sink.messages[0]["payload"] == "meta"

    async def test_backlog_degrades_full_then_drops(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(
            domain="stock_daily", layer="ods", payload="full"
        )
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        hub.attach_queue(subscriber, queue)
        rows = [{"symbol": "600519", "close": 1.0}]

        await hub.publish_batch(_event(), rows=rows)  # room: stays full
        await hub.publish_batch(_event(), rows=rows)  # half full: degrades to meta
        await hub.publish_batch(_event(), rows=rows)  # no room even for meta: dropped

        assert queue.get_nowait()["payload"] == "full"
        downgraded = queue.get_nowait()
        assert downgraded["payload"] == "meta"
        assert downgraded["truncated"] is True
        assert subscriber.downgraded == 1
        assert subscriber.dropped == 1

    async def test_diff_alert_reaches_the_domain_subscribers(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")

        delivered = await hub.publish_diff_alert(
            diff_alert_message(
                domain="stock_daily",
                source_a="akshare",
                source_b="ths",
                batch_id=BATCH,
                mismatch_ratio=0.1,
                mismatches=1,
                compared=10,
            )
        )

        assert delivered == 1
        assert sink.messages[0]["type"] == "data.diff_alert"

    async def test_unregister_stops_delivery(self):
        hub = SubscriptionHub()
        sink = _Sink()
        subscriber = await hub.register(sink.send)
        subscriber.subscriptions["stock_daily"] = Subscription(domain="stock_daily", layer="ods")

        await hub.unregister(subscriber)
        delivered = await hub.publish_batch(_event())

        assert delivered == 0
        assert sink.messages == []


# ---------------------------------------------------------------------------
# Socket protocol
# ---------------------------------------------------------------------------


class _Principal:
    """Minimal stand-in for the auth dependency's Principal."""

    class _User:
        username = "tester"

    user = _User()
    scopes = None

    def allows_domain(self, domain: str) -> bool:
        return True


class _ScopedPrincipal(_Principal):
    scopes = ("other_domain",)

    def allows_domain(self, domain: str) -> bool:
        return domain in self.scopes


@pytest.fixture
def ws_client():
    """TestClient with the auth seam stubbed and an empty watermark."""
    with (
        patch.object(subscribe_api, "principal_resolver", _resolver(_Principal())),
        patch.object(subscribe_api, "warehouse_engine_factory", lambda: object()),
        patch.object(subscribe_api, "latest_batches", lambda engine, *, domain, limit=None: []),
        patch.object(
            subscribe_api,
            "replay_since",
            lambda engine, *, domain, since_batch_id, limit=None: [],
        ),
        TestClient(app) as client,
    ):
        yield client


@pytest.fixture
def ws_client_no_warehouse():
    """TestClient whose watermark store is unreachable."""
    with (
        patch.object(subscribe_api, "principal_resolver", _resolver(_Principal())),
        patch.object(subscribe_api, "warehouse_engine_factory", _no_warehouse),
        TestClient(app) as client,
    ):
        yield client


def _resolver(principal):
    async def resolve(token: str):
        return principal if token == "good-token" else None

    return resolve


def _no_warehouse():
    raise RuntimeError("no warehouse in this test")


def _auth(ws) -> dict:
    ws.send_text(json.dumps({"action": "auth", "token": "good-token"}))
    return ws.receive_json()


class TestSocketAuth:
    def test_first_frame_must_be_auth(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            ws.send_text(json.dumps({"action": "subscribe", "domain": "stock_daily"}))

            frame = ws.receive_json()

        assert frame["type"] == "error"
        assert frame["code"] == "AUTH_REQUIRED"
        assert frame["suggestion"]  # business wording carries a remedy

    def test_garbage_first_frame_is_rejected(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            ws.send_text("not json")

            frame = ws.receive_json()

        assert frame["code"] == "AUTH_REQUIRED"

    def test_bad_token_is_rejected(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            ws.send_text(json.dumps({"action": "auth", "token": "wrong"}))

            frame = ws.receive_json()

        assert frame["code"] == "AUTH_FAILED"

    def test_valid_token_is_acknowledged(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            frame = _auth(ws)

        assert frame["type"] == "auth.ok"
        assert frame["user"] == "tester"

    def test_second_auth_frame_is_refused(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "auth", "token": "good-token"}))

            frame = ws.receive_json()

        assert frame["type"] == "error"
        assert frame["code"] == "UNKNOWN_ACTION"


class TestSocketSubscription:
    def test_subscribe_acknowledges_and_reports_the_replay_count(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "subscribe", "domain": "stock_daily"}))

            frame = ws.receive_json()

        assert frame["type"] == "subscribe.ok"
        assert frame["domain"] == "stock_daily"
        assert frame["payload"] == "meta"
        assert frame["replayed"] == 0

    def test_unknown_domain_is_refused(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "subscribe", "domain": "not_a_domain"}))

            frame = ws.receive_json()

        assert frame["code"] == "INVALID_SUBSCRIPTION"

    def test_bad_layer_is_refused(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(
                json.dumps({"action": "subscribe", "domain": "stock_daily", "layer": "raw"})
            )

            frame = ws.receive_json()

        assert frame["code"] == "INVALID_SUBSCRIPTION"

    def test_unsubscribe_acknowledges(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "subscribe", "domain": "stock_daily"}))
            ws.receive_json()
            ws.send_text(json.dumps({"action": "unsubscribe", "domain": "stock_daily"}))

            frame = ws.receive_json()

        assert frame == {"type": "unsubscribe.ok", "domain": "stock_daily"}

    def test_ping_gets_a_pong(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "ping"}))

            frame = ws.receive_json()

        assert frame == {"type": "pong"}

    def test_unknown_action_is_reported(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "delete_everything"}))

            frame = ws.receive_json()

        assert frame["code"] == "UNKNOWN_ACTION"

    def test_replay_failure_is_reported_but_subscription_stands(self, ws_client_no_warehouse):
        with ws_client_no_warehouse.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"action": "subscribe", "domain": "stock_daily"}))

            first = ws.receive_json()
            second = ws.receive_json()

        assert first["code"] == "REPLAY_UNAVAILABLE"
        assert second["type"] == "subscribe.ok"

    def test_scoped_key_cannot_subscribe_outside_its_domains(self):
        with (
            patch.object(subscribe_api, "principal_resolver", _resolver(_ScopedPrincipal())),
            patch.object(subscribe_api, "warehouse_engine_factory", _no_warehouse),
            TestClient(app) as client,
            client.websocket_connect("/ws/data/subscribe") as ws,
        ):
            _auth(ws)
            ws.send_text(json.dumps({"action": "subscribe", "domain": "stock_daily"}))

            frame = ws.receive_json()

        assert frame["code"] == "DOMAIN_FORBIDDEN"


class _FakeSocket:
    """Minimal socket stand-in for the writer tests."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.fail_after = fail_after

    async def send_text(self, text: str) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("socket gone")
        self.sent.append(text)


class TestHelpers:
    def test_parse_rejects_non_object_json(self):
        assert subscribe_api._parse("[1, 2]") is None
        assert subscribe_api._parse("not json") is None
        assert subscribe_api._parse('{"action": "ping"}') == {"action": "ping"}

    def test_unknown_error_code_still_carries_wording(self):
        frame = subscribe_api._error("SOMETHING_NEW")

        assert frame["type"] == "error"
        assert frame["code"] == "SOMETHING_NEW"
        assert frame["suggestion"]

    def test_replay_message_is_flagged_as_replay(self):

        frame = subscribe_api._replay_message(_watermark())

        assert frame["type"] == "data.update"
        assert frame["replayed"] is True
        assert frame["payload"] == "meta"

    async def test_send_sink_serializes_with_iso_dates(self):
        socket = _FakeSocket()
        send = subscribe_api._make_send(socket)

        await send({"type": "x", "when": date(2026, 9, 23)})

        assert json.loads(socket.sent[0]) == {"type": "x", "when": "2026-09-23"}

    async def test_writer_stops_on_the_shutdown_sentinel(self):
        socket = _FakeSocket()
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(None)

        await asyncio.wait_for(subscribe_api._writer(socket, queue), timeout=1)

        assert socket.sent == []

    async def test_writer_heartbeats_when_the_queue_is_idle(self, monkeypatch):
        socket = _FakeSocket()
        queue: asyncio.Queue = asyncio.Queue()
        monkeypatch.setattr(subscribe_api, "PING_INTERVAL", 0.01)

        task = asyncio.create_task(subscribe_api._writer(socket, queue))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert socket.sent and json.loads(socket.sent[0]) == {"type": "ping"}

    async def test_writer_gives_up_when_the_socket_dies(self):
        socket = _FakeSocket(fail_after=0)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "data.update"})

        await asyncio.wait_for(subscribe_api._writer(socket, queue), timeout=1)

        assert socket.sent == []

    async def test_domain_exists_uses_the_registry(self):
        assert subscribe_api._domain_exists("stock_daily") is True
        assert subscribe_api._domain_exists("not_a_domain") is False


class TestCredentialResolution:
    """The real resolver behind the auth frame (not just the seam)."""

    @pytest.fixture
    def session_maker(self, test_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def _user(self, session_maker, *, active: bool = True):
        from opendata.core.security import hash_password
        from opendata.models.user import User

        async with session_maker() as db:
            user = User(
                username="socket-user",
                email="socket@example.com",
                hashed_password=hash_password("Password123!"),
                is_active=active,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    async def test_jwt_resolves_to_its_user(self, session_maker):
        from opendata.core.security import create_access_token

        user = await self._user(session_maker)
        token = create_access_token(data={"sub": str(user.id)})

        principal = await subscribe_api._resolve_principal(token, session_maker=session_maker)

        assert principal is not None
        assert principal.user.id == user.id
        assert principal.scopes is None  # JWT callers are not domain-restricted

    async def test_garbage_jwt_is_refused(self, session_maker):
        assert (
            await subscribe_api._resolve_principal("not-a-token", session_maker=session_maker)
            is None
        )

    async def test_revoked_jwt_is_refused(self, session_maker):
        from opendata.core.security import create_access_token
        from opendata.core.token_blacklist import token_blacklist

        user = await self._user(session_maker)
        token = create_access_token(data={"sub": str(user.id)})
        token_blacklist.revoke(token)

        assert await subscribe_api._resolve_principal(token, session_maker=session_maker) is None

    async def test_disabled_user_is_refused(self, session_maker):
        from opendata.core.security import create_access_token

        user = await self._user(session_maker, active=False)
        token = create_access_token(data={"sub": str(user.id)})

        assert await subscribe_api._resolve_principal(token, session_maker=session_maker) is None

    async def test_api_key_resolves_with_its_scopes(self, session_maker):
        from opendata.services.api_key_service import ApiKeyService

        user = await self._user(session_maker)
        async with session_maker() as db:
            issued = await ApiKeyService(db).issue(
                owner=user, name="consumer", scopes=["stock_daily"]
            )

        principal = await subscribe_api._resolve_principal(
            issued.plaintext, session_maker=session_maker
        )

        assert principal is not None
        assert principal.uses_api_key
        assert principal.allows_domain("stock_daily")
        assert not principal.allows_domain("stock_action")

    async def test_unknown_api_key_is_refused(self, session_maker):
        await self._user(session_maker)

        assert (
            await subscribe_api._resolve_principal("od-unknown", session_maker=session_maker)
            is None
        )

    async def test_revoked_api_key_is_refused(self, session_maker):
        from opendata.services.api_key_service import ApiKeyService

        user = await self._user(session_maker)
        async with session_maker() as db:
            service = ApiKeyService(db)
            issued = await service.issue(owner=user, name="consumer", scopes=["stock_daily"])
            await service.revoke(issued.record)

        assert (
            await subscribe_api._resolve_principal(issued.plaintext, session_maker=session_maker)
            is None
        )


class TestSocketDelivery:
    def test_a_published_batch_reaches_the_subscriber(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(
                json.dumps({"action": "subscribe", "domain": "stock_daily", "layer": "ods"})
            )
            ws.receive_json()

            async def publish() -> int:
                from opendata.pipeline.subscription import hub

                return await hub.publish_batch(_event())

            delivered = ws.portal.call(publish)
            frame = ws.receive_json()

        assert delivered == 1
        assert frame["type"] == "data.update"
        assert frame["batch_id"] == BATCH
        assert frame["payload"] == "meta"

    def test_unsubscribed_domain_stops_receiving(self, ws_client):
        with ws_client.websocket_connect("/ws/data/subscribe") as ws:
            _auth(ws)
            ws.send_text(
                json.dumps({"action": "subscribe", "domain": "stock_daily", "layer": "ods"})
            )
            ws.receive_json()
            ws.send_text(json.dumps({"action": "unsubscribe", "domain": "stock_daily"}))
            ws.receive_json()

            async def publish() -> int:
                from opendata.pipeline.subscription import hub

                return await hub.publish_batch(_event())

            assert ws.portal.call(publish) == 0
            ws.send_text(json.dumps({"action": "ping"}))

            frame = ws.receive_json()

        assert frame == {"type": "pong"}  # nothing was pushed in between


# ---------------------------------------------------------------------------
# Replay from the watermark (socket + stubbed store)
# ---------------------------------------------------------------------------


class TestSocketReplay:
    def test_since_batch_id_replays_the_missed_batches(self):
        missed = [
            _watermark(seq=2),
            _watermark(seq=3, batch_id="7c9e6679-7425-40de-944b-e07fc1f90ae7"),
        ]
        with (
            patch.object(subscribe_api, "principal_resolver", _resolver(_Principal())),
            patch.object(subscribe_api, "warehouse_engine_factory", lambda: object()),
            patch.object(
                subscribe_api,
                "replay_since",
                lambda engine, *, domain, since_batch_id, limit=None: missed,
            ),
            TestClient(app) as client,
            client.websocket_connect("/ws/data/subscribe") as ws,
        ):
            _auth(ws)
            ws.send_text(
                json.dumps(
                    {
                        "action": "subscribe",
                        "domain": "stock_daily",
                        "layer": "ods",
                        "since_batch_id": BATCH,
                    }
                )
            )

            frames = [ws.receive_json() for _ in range(3)]

        assert [frame["type"] for frame in frames] == [
            "data.update",
            "data.update",
            "subscribe.ok",
        ]
        assert all(frame.get("replayed") is True for frame in frames[:2])
        assert frames[2]["replayed"] == 2

    def test_subscribe_without_watermark_reads_the_tail(self):
        with (
            patch.object(subscribe_api, "principal_resolver", _resolver(_Principal())),
            patch.object(subscribe_api, "warehouse_engine_factory", lambda: object()),
            patch.object(
                subscribe_api,
                "latest_batches",
                lambda engine, *, domain, limit=None: [_watermark()],
            ),
            TestClient(app) as client,
            client.websocket_connect("/ws/data/subscribe") as ws,
        ):
            _auth(ws)
            ws.send_text(
                json.dumps({"action": "subscribe", "domain": "stock_daily", "layer": "ods"})
            )

            first = ws.receive_json()
            second = ws.receive_json()

        assert first["type"] == "data.update"
        assert second["replayed"] == 1

    def test_replayed_batches_of_other_layers_are_filtered_out(self):
        batches = [_watermark(seq=2, layer="dwd"), _watermark(seq=3, layer="ods")]
        with (
            patch.object(subscribe_api, "principal_resolver", _resolver(_Principal())),
            patch.object(subscribe_api, "warehouse_engine_factory", lambda: object()),
            patch.object(
                subscribe_api,
                "replay_since",
                lambda engine, *, domain, since_batch_id, limit=None: batches,
            ),
            TestClient(app) as client,
            client.websocket_connect("/ws/data/subscribe") as ws,
        ):
            _auth(ws)
            ws.send_text(
                json.dumps(
                    {
                        "action": "subscribe",
                        "domain": "stock_daily",
                        "layer": "ods",
                        "since_batch_id": BATCH,
                    }
                )
            )

            first = ws.receive_json()
            second = ws.receive_json()

        assert first["replayed"] is True
        assert first["layer"] == "ods"
        assert second["replayed"] == 1


class TestWebSocketMounts:
    """The socket paths the deployment actually uses (regression).

    nginx proxies ``location /ws/`` straight through and the frontend
    dials ``/ws/executions``, so both sockets must be mounted at the
    application root rather than under the versioned REST prefix.
    """

    def test_both_sockets_are_mounted_at_the_root(self):
        from opendata.main import app

        paths = {route.path for route in app.routes if "websocket" in type(route).__name__.lower()}

        assert "/ws/data/subscribe" in paths
        assert "/ws/executions" in paths

    def test_the_versioned_prefix_does_not_carry_sockets(self):
        from opendata.main import app

        paths = {route.path for route in app.routes}

        assert "/api/v1/ws/data/subscribe" not in paths
        assert "/api/v1/ws/executions" not in paths

    def test_executions_socket_still_refuses_a_missing_token(self):
        from opendata.main import app

        with (
            TestClient(app) as client,
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect("/ws/executions"),
        ):
            pass

        assert caught.value.code == 4001  # authenticated before accept
