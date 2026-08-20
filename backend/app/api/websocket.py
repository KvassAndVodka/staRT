"""Durable, bounded WebSocket event replay for transcription sessions."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import asc, select

from app.adapters.storage.database import AsyncSessionLocal
from app.application.event_stream import (
    EventEnvelope,
    EventStreamError,
    allocate_event_sequences,
    compact_session_events,
    envelope_from_model,
)
from app.config import settings
from app.domain.models import OutboxEventModel, SessionModel


router = APIRouter()


class ConnectionManager:
    """Persist events before delivery and replay a bounded ordered tail."""

    def __init__(self, *, session_factory=None, replay_limit: Optional[int] = None):
        self.session_factory = session_factory or AsyncSessionLocal
        self.replay_limit = (
            settings.EVENT_REPLAY_LIMIT if replay_limit is None else replay_limit
        )
        if self.replay_limit <= 0:
            raise ValueError("event replay limit must be positive")
        self.active_connections: dict[str, dict[WebSocket, int]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _snapshot_required_message(
        session_id: str,
        *,
        latest_sequence: int,
        replay_floor: int,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "type": "stream.snapshot_required",
            "sequence": latest_sequence,
            "payload": {
                "reason": reason,
                "latest_sequence": latest_sequence,
                "replay_floor": replay_floor,
            },
            "version": "1.0",
            "replayed": False,
        }

    async def _load_replay(
        self,
        session_id: str,
        since_sequence: Optional[int],
    ) -> tuple[SessionModel, list[EventEnvelope], Optional[str]]:
        async with self.session_factory() as db:
            session = await db.get(SessionModel, session_id)
            if session is None:
                raise EventStreamError(f"Session {session_id} does not exist")
            if since_sequence is None:
                return session, [], "cursor_required"
            if since_sequence > session.event_sequence:
                return session, [], "cursor_ahead"
            if since_sequence < session.event_replay_floor - 1:
                return session, [], "cursor_expired"
            result = await db.execute(
                select(OutboxEventModel)
                .where(OutboxEventModel.session_id == session_id)
                .where(OutboxEventModel.sequence > since_sequence)
                .order_by(asc(OutboxEventModel.sequence))
            )
            events = [envelope_from_model(event) for event in result.scalars().all()]
            expected = since_sequence + 1
            for event in events:
                if event.sequence != expected:
                    return session, [], "replay_gap"
                expected += 1
            if expected - 1 != session.event_sequence:
                return session, [], "replay_gap"
            return session, events, None

    async def connect(
        self,
        session_id: str,
        websocket: WebSocket,
        since_sequence: Optional[int],
    ) -> bool:
        origin = websocket.headers.get("origin")
        if origin and origin not in settings.CORS_ORIGINS and "*" not in settings.CORS_ORIGINS:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        await websocket.accept()
        async with self._lock_for(session_id):
            try:
                session, events, snapshot_reason = await self._load_replay(
                    session_id,
                    since_sequence,
                )
            except EventStreamError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return False
            try:
                if snapshot_reason is not None:
                    await websocket.send_json(self._snapshot_required_message(
                        session_id,
                        latest_sequence=session.event_sequence,
                        replay_floor=session.event_replay_floor,
                        reason=snapshot_reason,
                    ))
                else:
                    for event in events:
                        await websocket.send_json(event.to_message(replayed=True))
            except Exception:
                await websocket.close()
                return False

            self.active_connections.setdefault(session_id, {})[websocket] = (
                session.event_sequence
            )
        return True

    def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        connections = self.active_connections.get(session_id)
        if connections is None:
            return
        connections.pop(websocket, None)
        if not connections:
            self.active_connections.pop(session_id, None)

    async def _prepare_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        supplied_event_id = payload.get("event_id")
        event_id = supplied_event_id if isinstance(supplied_event_id, str) else str(uuid.uuid4())
        stored_payload = dict(payload)
        stored_payload.pop("event_id", None)
        async with self.session_factory() as db:
            event = await db.get(OutboxEventModel, event_id)
            if event is not None:
                if event.session_id != session_id or event.event_type != event_type:
                    raise EventStreamError(f"Event identity collision for {event_id}")
                if event.sequence is None:
                    event.sequence = next(
                        iter(await allocate_event_sequences(db, session_id))
                    )
            else:
                sequence = next(iter(await allocate_event_sequences(db, session_id)))
                event = OutboxEventModel(
                    id=event_id,
                    session_id=session_id,
                    window_id=None,
                    idempotency_key=f"broadcast:{event_id}",
                    event_type=event_type,
                    sequence=sequence,
                    payload=stored_payload,
                )
                db.add(event)
            if event.published_at is None:
                event.published_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(event)
            return envelope_from_model(event)

    async def broadcast_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist one event, then deliver it once per connection sequence."""
        async with self._lock_for(session_id):
            event = await self._prepare_event(session_id, event_type, payload)
            connections = self.active_connections.get(session_id, {})
            dead_sockets: set[WebSocket] = set()
            for websocket, cursor in list(connections.items()):
                if event.sequence <= cursor:
                    continue
                try:
                    if event.sequence != cursor + 1:
                        await websocket.send_json(self._snapshot_required_message(
                            session_id,
                            latest_sequence=event.sequence,
                            replay_floor=max(1, event.sequence - self.replay_limit + 1),
                            reason="delivery_gap",
                        ))
                    else:
                        await websocket.send_json(event.to_message())
                    connections[websocket] = event.sequence
                except Exception:
                    dead_sockets.add(websocket)
            for websocket in dead_sockets:
                self.disconnect(session_id, websocket)

            async with self.session_factory() as db:
                await compact_session_events(db, session_id, self.replay_limit)
                await db.commit()


ws_manager = ConnectionManager()


@router.websocket("/api/sessions/{session_id}/events")
async def session_events_websocket(websocket: WebSocket, session_id: str):
    raw_cursor = websocket.query_params.get("since_sequence")
    try:
        since_sequence = int(raw_cursor) if raw_cursor is not None else None
        if since_sequence is not None and since_sequence < 0:
            raise ValueError
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    connected = await ws_manager.connect(session_id, websocket, since_sequence)
    if not connected:
        return
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id, websocket)
    except Exception:
        ws_manager.disconnect(session_id, websocket)
