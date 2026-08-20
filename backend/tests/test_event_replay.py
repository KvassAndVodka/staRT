"""Durable WebSocket replay, cursor validation, and compaction contracts."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import asc, select

from app.adapters.storage.database import AsyncSessionLocal
from app.api.websocket import ConnectionManager
from app.application.event_stream import allocate_event_sequences, compact_session_events
from app.domain.models import OutboxEventModel, SessionModel


class FakeWebSocket:
    def __init__(self, origin: str = "http://localhost:3000") -> None:
        self.headers = {"origin": origin}
        self.accepted = False
        self.closed_code = None
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code=None) -> None:
        self.closed_code = code

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


async def _create_session(session_id: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(SessionModel(
            id=session_id,
            title="Replay",
            source_url="https://example.com/audio",
            status="live",
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_two_clients_replay_ordered_events_without_duplicate_live_delivery():
    session_id = "multi-client-replay"
    await _create_session(session_id)
    manager = ConnectionManager(replay_limit=10)
    first_client = FakeWebSocket()
    assert await manager.connect(session_id, first_client, 0)

    await manager.broadcast_event(session_id, "session.status", {"status": "live"})
    await manager.broadcast_event(session_id, "transcript.partial", {"text": "one"})
    assert [message["sequence"] for message in first_client.messages] == [1, 2]

    manager.disconnect(session_id, first_client)
    await manager.broadcast_event(session_id, "turn.upsert", {"turns": []})

    late_client = FakeWebSocket()
    assert await manager.connect(session_id, late_client, 1)
    assert [message["sequence"] for message in late_client.messages] == [2, 3]
    assert all(message["replayed"] is True for message in late_client.messages)

    reconnected_client = FakeWebSocket()
    assert await manager.connect(session_id, reconnected_client, 2)
    assert [message["sequence"] for message in reconnected_client.messages] == [3]

    await manager.broadcast_event(session_id, "session.status", {"status": "finalizing"})
    assert [message["sequence"] for message in late_client.messages] == [2, 3, 4]
    assert [message["sequence"] for message in reconnected_client.messages] == [3, 4]

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        result = await db.execute(
            select(OutboxEventModel)
            .where(OutboxEventModel.session_id == session_id)
            .order_by(asc(OutboxEventModel.sequence))
        )
        events = result.scalars().all()
        assert session.event_sequence == 4
        assert session.event_replay_floor == 1
        assert [event.sequence for event in events] == [1, 2, 3, 4]
        assert len({event.id for event in events}) == 4


@pytest.mark.asyncio
async def test_expired_missing_and_ahead_cursors_require_a_snapshot():
    session_id = "bounded-replay"
    await _create_session(session_id)
    manager = ConnectionManager(replay_limit=2)
    for sequence in range(1, 5):
        await manager.broadcast_event(session_id, "test.event", {"value": sequence})

    async with AsyncSessionLocal() as db:
        session = await db.get(SessionModel, session_id)
        result = await db.execute(
            select(OutboxEventModel.sequence)
            .where(OutboxEventModel.session_id == session_id)
            .order_by(asc(OutboxEventModel.sequence))
        )
        assert session.event_sequence == 4
        assert session.event_replay_floor == 3
        assert result.scalars().all() == [3, 4]

    expired = FakeWebSocket()
    assert await manager.connect(session_id, expired, 0)
    assert expired.messages[0]["type"] == "stream.snapshot_required"
    assert expired.messages[0]["payload"] == {
        "reason": "cursor_expired",
        "latest_sequence": 4,
        "replay_floor": 3,
    }

    missing = FakeWebSocket()
    assert await manager.connect(session_id, missing, None)
    assert missing.messages[0]["payload"]["reason"] == "cursor_required"

    ahead = FakeWebSocket()
    assert await manager.connect(session_id, ahead, 5)
    assert ahead.messages[0]["payload"]["reason"] == "cursor_ahead"

    valid = FakeWebSocket()
    assert await manager.connect(session_id, valid, 2)
    assert [message["sequence"] for message in valid.messages] == [3, 4]


@pytest.mark.asyncio
async def test_persisted_outbox_event_keeps_its_transactional_sequence():
    session_id = "transactional-outbox"
    await _create_session(session_id)
    async with AsyncSessionLocal() as db:
        sequence = next(iter(await allocate_event_sequences(db, session_id)))
        db.add(OutboxEventModel(
            id="durable-event",
            session_id=session_id,
            window_id=None,
            idempotency_key="durable:event",
            event_type="transcript.partial",
            sequence=sequence,
            payload={"text": "durable"},
        ))
        await db.commit()

    manager = ConnectionManager(replay_limit=10)
    client = FakeWebSocket()
    assert await manager.connect(session_id, client, 0)
    assert client.messages[0]["payload"]["event_id"] == "durable-event"
    manager.disconnect(session_id, client)

    await manager.broadcast_event(
        session_id,
        "transcript.partial",
        {"text": "durable", "event_id": "durable-event"},
    )
    async with AsyncSessionLocal() as db:
        event = await db.get(OutboxEventModel, "durable-event")
        session = await db.get(SessionModel, session_id)
        assert event.sequence == 1
        assert event.published_at is not None
        assert session.event_sequence == 1


@pytest.mark.asyncio
async def test_sequence_reservation_rolls_back_with_its_transaction():
    session_id = "transactional-sequence"
    await _create_session(session_id)
    async with AsyncSessionLocal() as db:
        assert list(await allocate_event_sequences(db, session_id, 2)) == [1, 2]
        await db.rollback()

    async with AsyncSessionLocal() as db:
        assert list(await allocate_event_sequences(db, session_id)) == [1]
        await db.commit()


@pytest.mark.asyncio
async def test_compaction_never_deletes_unpublished_outbox_work():
    session_id = "safe-compaction"
    await _create_session(session_id)
    published = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        sequences = iter(await allocate_event_sequences(db, session_id, 3))
        for event_id, published_at in (
            ("pending-old-event", None),
            ("published-old-event", published),
            ("published-tail-event", published),
        ):
            db.add(OutboxEventModel(
                id=event_id,
                session_id=session_id,
                window_id=None,
                idempotency_key=f"safe:{event_id}",
                event_type="test.event",
                sequence=next(sequences),
                payload={},
                published_at=published_at,
            ))
        await db.commit()

    async with AsyncSessionLocal() as db:
        await compact_session_events(db, session_id, retain=1)
        await db.commit()
        result = await db.execute(
            select(OutboxEventModel.id)
            .where(OutboxEventModel.session_id == session_id)
            .order_by(asc(OutboxEventModel.sequence))
        )
        session = await db.get(SessionModel, session_id)
        assert result.scalars().all() == ["pending-old-event", "published-tail-event"]
        assert session.event_replay_floor == 1


@pytest.mark.asyncio
async def test_untrusted_origin_is_rejected_before_acceptance():
    manager = ConnectionManager(replay_limit=10)
    websocket = FakeWebSocket(origin="https://attacker.example")
    assert not await manager.connect("unknown", websocket, 0)
    assert websocket.accepted is False
    assert websocket.closed_code == 1008
