"""Teleop sessions, participants and the single-controller lease."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings


@dataclass
class PilotConn:
    id: str
    user: str
    ws: Any
    joined_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(payload)


@dataclass
class Session:
    id: str
    vehicle_id: str
    created_at: float
    expires_at: float
    created_by: str
    pilots: dict[str, PilotConn] = field(default_factory=dict)
    lease_holder: str | None = None
    lease_acquired_at: float | None = None
    estop: bool = False
    estop_by: str | None = None
    last_command_at: float = 0.0
    has_command: bool = False
    neutral_task: asyncio.Task | None = None
    estop_task: asyncio.Task | None = None

    def state_for(self, pilot: PilotConn, command_ttl_ms: int) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "vehicle_id": self.vehicle_id,
            "you": pilot.id,
            "user": pilot.user,
            "lease_holder": self.lease_holder,
            "you_have_lease": self.lease_holder == pilot.id,
            "estop": self.estop,
            "estop_by": self.estop_by,
            "pilots": [
                {
                    "id": item.id,
                    "user": item.user,
                    "is_controller": item.id == self.lease_holder,
                }
                for item in self.pilots.values()
            ],
            "expires_at": self.expires_at,
            "command_ttl_ms": command_ttl_ms,
        }


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._by_id: dict[str, Session] = {}
        self._by_vehicle: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------

    def get_or_create(self, vehicle_id: str, user: str) -> tuple[Session, bool]:
        existing_id = self._by_vehicle.get(vehicle_id)
        if existing_id:
            existing = self._by_id.get(existing_id)
            if existing and existing.expires_at > time.time():
                return existing, False
            if existing:
                self.close(existing.id)
        now = time.time()
        session = Session(
            id=secrets.token_hex(8),
            vehicle_id=vehicle_id,
            created_at=now,
            expires_at=now + self.settings.session_ttl_s,
            created_by=user,
        )
        self._by_id[session.id] = session
        self._by_vehicle[vehicle_id] = session.id
        return session, True

    def get(self, session_id: str) -> Session | None:
        session = self._by_id.get(session_id)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self.close(session.id)
            return None
        return session

    def by_vehicle(self, vehicle_id: str) -> Session | None:
        session_id = self._by_vehicle.get(vehicle_id)
        if session_id is None:
            return None
        return self.get(session_id)

    def active(self) -> list[Session]:
        """All live sessions (expired ones are swept by the expiry loop)."""
        return list(self._by_id.values())

    def close(self, session_id: str) -> Session | None:
        session = self._by_id.pop(session_id, None)
        if session is None:
            return None
        if self._by_vehicle.get(session.vehicle_id) == session_id:
            del self._by_vehicle[session.vehicle_id]
        for task in (session.neutral_task, session.estop_task):
            if task and not task.done():
                task.cancel()
        return session

    def expire(self) -> list[Session]:
        now = time.time()
        expired = [session for session in self._by_id.values() if session.expires_at <= now]
        for session in expired:
            self.close(session.id)
        return expired

    # -- participation ----------------------------------------------------

    def attach(self, session: Session, pilot: PilotConn) -> None:
        session.pilots[pilot.id] = pilot

    def detach(self, session: Session, pilot: PilotConn) -> bool:
        session.pilots.pop(pilot.id, None)
        if session.lease_holder == pilot.id:
            session.lease_holder = None
            session.lease_acquired_at = None
            session.has_command = False
            return True
        return False

    # -- control lease ----------------------------------------------------

    def acquire(self, session: Session, pilot: PilotConn) -> tuple[bool, str | None]:
        if session.lease_holder in (None, pilot.id):
            session.lease_holder = pilot.id
            session.lease_acquired_at = time.time()
            session.last_command_at = 0.0
            session.has_command = False
            return True, None
        return False, session.lease_holder

    def release(self, session: Session, pilot: PilotConn) -> bool:
        if session.lease_holder != pilot.id:
            return False
        session.lease_holder = None
        session.lease_acquired_at = None
        session.has_command = False
        return True

    def can_command(self, session: Session, pilot: PilotConn) -> bool:
        return session.lease_holder == pilot.id and not session.estop

    def mark_command(self, session: Session) -> None:
        session.last_command_at = time.time()
        session.has_command = True

    # -- fan-out ----------------------------------------------------------

    async def broadcast_json(self, session: Session, payload: dict[str, Any]) -> None:
        if not session.pilots:
            return
        await asyncio.gather(
            *(pilot.send_json(payload) for pilot in list(session.pilots.values())),
            return_exceptions=True,
        )

    async def broadcast_bytes(self, session: Session, payload: bytes) -> None:
        if not session.pilots:
            return
        await asyncio.gather(
            *(pilot.send_bytes(payload) for pilot in list(session.pilots.values())),
            return_exceptions=True,
        )
