"""Control/data-plane orchestration.

The hub is the only place that turns a pilot action into bytes on the wire.
Keeping it in one module makes the safety rules auditable:

* only the lease holder may send motion/arm/mode commands,
* no motion while an emergency stop is latched,
* stale commands are stopped by both the server (neutral burst) and the
  vehicle agent (local watchdog),
* every state change is written to the audit log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .audit import AuditLog
from .config import Settings
from .mavlink import NEUTRAL, MavlinkCodec, MavlinkError
from .registry import VehicleRegistry
from .sessions import PilotConn, Session, SessionManager

log = logging.getLogger(__name__)

MAX_ABS_XY = 1000
Z_MIN, Z_MAX = 0, 1000
BUTTONS_MAX = 65535


class CommandRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Hub:
    def __init__(
        self,
        settings: Settings,
        registry: VehicleRegistry,
        sessions: SessionManager,
        audit: AuditLog,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sessions = sessions
        self.audit = audit
        self.codec = MavlinkCodec(settings.gcs_sysid, settings.gcs_compid)
        self._tasks: set[asyncio.Task] = set()

    # -- low level --------------------------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def send_to_vehicle(self, vehicle_id: str, payload: bytes) -> bool:
        conn = self.registry.get_connection(vehicle_id)
        if conn is None:
            log.debug("no live connection for vehicle %s", vehicle_id)
            return False
        try:
            await conn.ws.send_bytes(payload)
            log.debug("sent %d bytes to vehicle %s", len(payload), vehicle_id)
            return True
        except Exception:
            log.exception("failed to send to vehicle %s", vehicle_id)
            return False

    async def send_json_to_vehicle(self, vehicle_id: str, payload: dict[str, Any]) -> bool:
        conn = self.registry.get_connection(vehicle_id)
        if conn is None:
            return False
        try:
            await conn.ws.send_json(payload)
            return True
        except Exception:
            log.exception("failed to send JSON to vehicle %s", vehicle_id)
            return False

    async def broadcast_state(self, session: Session, reason: str) -> None:
        for pilot in list(session.pilots.values()):
            try:
                await pilot.send_json(
                    {
                        "type": "state",
                        "reason": reason,
                        "session": session.state_for(pilot, self.settings.command_ttl_ms),
                    }
                )
            except Exception:
                log.debug("state broadcast failed for pilot %s", pilot.id, exc_info=True)

    # -- commands ---------------------------------------------------------

    @staticmethod
    def _require_control(session: Session, pilot: PilotConn) -> None:
        if session.lease_holder != pilot.id:
            raise CommandRejected("no_lease", "you do not hold the control lease")
        if session.estop:
            raise CommandRejected("estop_active", "emergency stop is latched")

    async def manual(self, session: Session, pilot: PilotConn, payload: dict[str, Any]) -> None:
        self._require_control(session, pilot)
        try:
            x = int(payload.get("x", 0))
            y = int(payload.get("y", 0))
            z = int(payload.get("z", NEUTRAL["z"]))
            r = int(payload.get("r", 0))
            buttons = int(payload.get("buttons", 0))
            buttons2 = int(payload.get("buttons2", 0))
        except (TypeError, ValueError) as exc:
            raise CommandRejected("bad_manual", "manual control fields must be integers") from exc

        if not (-MAX_ABS_XY <= x <= MAX_ABS_XY):
            raise CommandRejected("bad_manual", "x out of range")
        if not (-MAX_ABS_XY <= y <= MAX_ABS_XY):
            raise CommandRejected("bad_manual", "y out of range")
        if not (Z_MIN <= z <= Z_MAX):
            raise CommandRejected("bad_manual", "z out of range")
        if not (-MAX_ABS_XY <= r <= MAX_ABS_XY):
            raise CommandRejected("bad_manual", "r out of range")
        if not (0 <= buttons <= BUTTONS_MAX and 0 <= buttons2 <= BUTTONS_MAX):
            raise CommandRejected("bad_manual", "buttons out of range")

        conn = self.registry.get_connection(session.vehicle_id)
        if conn is None:
            raise CommandRejected("vehicle_offline", "vehicle is offline")

        frame = self.codec.manual_control(
            conn.sysid,
            x=x,
            y=y,
            z=z,
            r=r,
            buttons=buttons,
            buttons2=buttons2,
        )
        if not await self.send_to_vehicle(session.vehicle_id, frame):
            raise CommandRejected("vehicle_offline", "vehicle link is not available")

        self.sessions.mark_command(session)
        pilot.last_seen = time.time()

        # Manual frames are high-rate; audit only periodically to keep the log
        # readable.  Arm/mode/estop are always audited.
        now = time.time()
        if now - getattr(pilot, "_last_manual_audit", 0.0) > 5.0:
            setattr(pilot, "_last_manual_audit", now)
            await self.audit.log(
                "manual_control",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                values={"x": x, "y": y, "z": z, "r": r, "buttons": buttons},
            )

    async def arm(self, session: Session, pilot: PilotConn, *, arm: bool) -> None:
        # Disarming is always allowed for the lease holder, even while the
        # E-stop is latched.  Arming is not.
        if session.lease_holder != pilot.id:
            raise CommandRejected("no_lease", "you do not hold the control lease")
        if session.estop and arm:
            raise CommandRejected("estop_active", "reset the emergency stop first")
        conn = self.registry.get_connection(session.vehicle_id)
        if conn is None:
            raise CommandRejected("vehicle_offline", "vehicle is offline")
        frame = self.codec.arm(conn.sysid, arm=arm, target_component=conn.compid)
        if not await self.send_to_vehicle(session.vehicle_id, frame):
            raise CommandRejected("vehicle_offline", "vehicle link is not available")
        self.sessions.mark_command(session)
        await self.audit.log(
            "arm" if arm else "disarm",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=pilot.user,
        )

    async def set_mode(self, session: Session, pilot: PilotConn, mode: str) -> None:
        self._require_control(session, pilot)
        conn = self.registry.get_connection(session.vehicle_id)
        if conn is None:
            raise CommandRejected("vehicle_offline", "vehicle is offline")
        try:
            frame = self.codec.set_mode(conn.sysid, mode, target_component=conn.compid)
        except MavlinkError as exc:
            raise CommandRejected("bad_mode", str(exc)) from exc
        if not await self.send_to_vehicle(session.vehicle_id, frame):
            raise CommandRejected("vehicle_offline", "vehicle link is not available")
        self.sessions.mark_command(session)
        await self.audit.log(
            "set_mode",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=pilot.user,
            mode=mode.upper(),
        )

    # -- lease ------------------------------------------------------------

    async def acquire_lease(self, session: Session, pilot: PilotConn) -> None:
        ok, holder = self.sessions.acquire(session, pilot)
        if ok:
            if session.neutral_task and not session.neutral_task.done():
                session.neutral_task.cancel()
            await self.audit.log(
                "lease_acquire",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                pilot_id=pilot.id,
            )
        else:
            await self.audit.log(
                "lease_denied",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                holder=holder,
            )
        await pilot.send_json(
            {
                "type": "lease",
                "granted": ok,
                "holder": session.lease_holder,
                "you": pilot.id,
                "reason": None if ok else "held_by_another_pilot",
            }
        )
        await self.broadcast_state(session, "lease")

    async def release_lease(self, session: Session, pilot: PilotConn) -> None:
        released = self.sessions.release(session, pilot)
        if released:
            await self.audit.log(
                "lease_release",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                pilot_id=pilot.id,
            )
            await self.neutral_burst(session.vehicle_id, seconds=0.5)
        await pilot.send_json(
            {
                "type": "lease",
                "granted": False,
                "holder": session.lease_holder,
                "you": pilot.id,
                "reason": "released" if released else "not_holder",
            }
        )
        await self.broadcast_state(session, "lease")

    async def lease_disconnected(self, session: Session, pilot: PilotConn) -> None:
        if self.sessions.detach(session, pilot):
            await self.audit.log(
                "lease_release",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                pilot_id=pilot.id,
                reason="disconnect",
            )
            await self.neutral_burst(session.vehicle_id, seconds=0.5)
            await self.broadcast_state(session, "pilot_disconnected")

    # -- emergency stop ---------------------------------------------------

    async def estop(self, session: Session, pilot: PilotConn) -> None:
        if not session.estop:
            session.estop = True
            session.estop_by = pilot.user
            await self.audit.log(
                "estop",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                pilot_id=pilot.id,
            )
        if session.estop_task and not session.estop_task.done():
            session.estop_task.cancel()
        session.estop_task = self._spawn(self._estop_loop(session))
        await self.send_json_to_vehicle(
            session.vehicle_id,
            {"type": "estop", "active": True, "by": pilot.user, "ts": time.time()},
        )
        await self.sessions.broadcast_json(
            session,
            {"type": "estop", "active": True, "by": pilot.user},
        )
        await self.broadcast_state(session, "estop")

    async def reset_estop(self, session: Session, pilot: PilotConn) -> None:
        # Only the lease holder may clear an E-stop, but the latch itself must
        # not block the reset (that would make the stop unrecoverable).
        if session.lease_holder != pilot.id:
            raise CommandRejected("no_lease", "you do not hold the control lease")
        session.estop = False
        session.estop_by = None
        if session.estop_task and not session.estop_task.done():
            session.estop_task.cancel()
        await self.send_json_to_vehicle(
            session.vehicle_id,
            {"type": "estop", "active": False, "ts": time.time()},
        )
        await self.audit.log(
            "estop_reset",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=pilot.user,
        )
        await self.sessions.broadcast_json(
            session,
            {"type": "estop", "active": False, "by": None},
        )
        await self.broadcast_state(session, "estop_reset")

    async def _estop_loop(self, session: Session) -> None:
        """Keep the vehicle in a hard-neutral state while E-stop is latched."""

        try:
            while session.estop:
                await self.neutral_burst(session.vehicle_id, seconds=0.2, hz=10)
                # Re-assert the text flag in case the vehicle reconnected.
                await self.send_json_to_vehicle(
                    session.vehicle_id,
                    {"type": "estop", "active": True, "by": session.estop_by, "ts": time.time()},
                )
                await asyncio.sleep(self.settings.estop_repeat_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("estop loop failed for session %s", session.id)

    # -- neutral / watchdog ----------------------------------------------

    async def neutral_burst(self, vehicle_id: str, *, seconds: float = 0.5, hz: float | None = None) -> None:
        conn = self.registry.get_connection(vehicle_id)
        if conn is None:
            log.debug("neutral burst skipped: vehicle %s is offline", vehicle_id)
            return
        rate = hz or self.settings.neutral_hz
        count = max(1, int(seconds * rate))
        frame = self.codec.manual_control(conn.sysid, **NEUTRAL)
        log.debug("neutral burst to %s: %d frames at %.1f Hz", vehicle_id, count, rate)
        for _ in range(count):
            if not await self.send_to_vehicle(vehicle_id, frame):
                return
            await asyncio.sleep(1.0 / rate)

    def start_neutral_watchdog(self, session: Session) -> None:
        """Optional server-side watchdog.

        The authoritative watchdog lives on the vehicle.  This server-side
        helper adds a second layer for the case where the pilot's browser dies
        while the WebSocket remains half-open.
        """

        if session.neutral_task and not session.neutral_task.done():
            session.neutral_task.cancel()
        session.neutral_task = self._spawn(self._neutral_watchdog(session))

    async def _neutral_watchdog(self, session: Session) -> None:
        try:
            while session.lease_holder:
                await asyncio.sleep(0.1)
                if not session.has_command:
                    continue
                age = time.time() - session.last_command_at
                if age > self.settings.command_ttl_ms / 1000.0:
                    await self.neutral_burst(session.vehicle_id, seconds=0.2, hz=20)
                    session.has_command = False
                    await self.audit.log(
                        "command_timeout",
                        session_id=session.id,
                        vehicle_id=session.vehicle_id,
                        age_ms=round(age * 1000),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("neutral watchdog failed for session %s", session.id)
