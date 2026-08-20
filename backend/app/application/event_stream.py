"""Durable event sequence allocation and bounded replay storage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OutboxEventModel, SessionModel


class EventStreamError(RuntimeError):
    """The durable event stream cannot satisfy its ordering contract."""


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    session_id: str
    event_type: str
    sequence: int
    payload: dict[str, Any]

    def to_message(self, *, replayed: bool = False) -> dict[str, Any]:
        payload = dict(self.payload)
        payload["event_id"] = self.event_id
        return {
            "session_id": self.session_id,
            "type": self.event_type,
            "sequence": self.sequence,
            "payload": payload,
            "version": "1.0",
            "replayed": replayed,
        }


async def allocate_event_sequences(
    db: AsyncSession,
    session_id: str,
    count: int = 1,
) -> range:
    """Reserve a contiguous sequence range inside the caller's transaction."""
    if count <= 0:
        raise ValueError("event sequence count must be positive")
    result = await db.execute(
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(event_sequence=SessionModel.event_sequence + count)
        .returning(SessionModel.event_sequence)
    )
    final_sequence = result.scalar_one_or_none()
    if final_sequence is None:
        raise EventStreamError(f"Session {session_id} does not exist")
    first_sequence = int(final_sequence) - count + 1
    return range(first_sequence, int(final_sequence) + 1)


def envelope_from_model(event: OutboxEventModel) -> EventEnvelope:
    if event.sequence is None or event.sequence <= 0:
        raise EventStreamError(f"Event {event.id} has no durable sequence")
    return EventEnvelope(
        event_id=event.id,
        session_id=event.session_id,
        event_type=event.event_type,
        sequence=event.sequence,
        payload=dict(event.payload),
    )


async def compact_session_events(
    db: AsyncSession,
    session_id: str,
    retain: int,
) -> int:
    """Keep a bounded published tail and return the new replay floor."""
    if retain <= 0:
        raise ValueError("event replay retention must be positive")
    session = await db.get(SessionModel, session_id)
    if session is None:
        raise EventStreamError(f"Session {session_id} does not exist")

    cutoff = max(0, session.event_sequence - retain)
    if cutoff:
        await db.execute(
            delete(OutboxEventModel)
            .where(OutboxEventModel.session_id == session_id)
            .where(OutboxEventModel.sequence.is_not(None))
            .where(OutboxEventModel.sequence <= cutoff)
            .where(OutboxEventModel.published_at.is_not(None))
        )
    minimum_result = await db.execute(
        select(func.min(OutboxEventModel.sequence))
        .where(OutboxEventModel.session_id == session_id)
        .where(OutboxEventModel.sequence.is_not(None))
    )
    minimum = minimum_result.scalar_one()
    floor = int(minimum) if minimum is not None else session.event_sequence + 1
    session.event_replay_floor = floor
    return floor
