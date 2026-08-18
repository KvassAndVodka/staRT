"""
WebSocket Connection Manager and Event Stream Router.
Dispatches real-time session events conforming to Section 9.2 of the spec.
Validates client Origin headers against settings.CORS_ORIGINS.
"""
import json
import asyncio
from typing import Dict, Set, Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from app.config import settings

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        # session_id -> Set[WebSocket]
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        # Monotonic sequence counter per session
        self.sequence_counters: Dict[str, int] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and origin not in settings.CORS_ORIGINS and "*" not in settings.CORS_ORIGINS:
            # Reject untrusted origins
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False

        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = set()
            self.sequence_counters[session_id] = 0
        self.active_connections[session_id].add(websocket)
        return True

    def disconnect(self, session_id: str, websocket: WebSocket):
        if session_id in self.active_connections:
            self.active_connections[session_id].discard(websocket)
            if not self.active_connections[session_id]:
                del self.active_connections[session_id]

    async def broadcast_event(self, session_id: str, event_type: str, payload: Dict[str, Any]):
        """Broadcast an event payload to all connected clients of a session."""
        if session_id not in self.active_connections:
            return

        seq = self.sequence_counters.get(session_id, 0) + 1
        self.sequence_counters[session_id] = seq

        event_msg = {
            "session_id": session_id,
            "type": event_type,
            "sequence": seq,
            "payload": payload,
            "version": "1.0"
        }
        
        dead_sockets = set()
        for ws in list(self.active_connections[session_id]):
            try:
                await ws.send_json(event_msg)
            except Exception:
                dead_sockets.add(ws)
                
        for ws in dead_sockets:
            self.disconnect(session_id, ws)

ws_manager = ConnectionManager()

@router.websocket("/api/sessions/{session_id}/events")
async def session_events_websocket(websocket: WebSocket, session_id: str):
    connected = await ws_manager.connect(session_id, websocket)
    if not connected:
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(session_id, websocket)
    except Exception:
        ws_manager.disconnect(session_id, websocket)
