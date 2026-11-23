"""ROVOps control server.

Run locally:

    uvicorn app.main:app --app-dir server --reload --port 8080

The server is deliberately split into a control plane (REST + WebSocket JSON)
and a byte plane (binary MAVLink frames over the same WebSocket).  Media is
out of band: see ``docs/04-deployment.md`` for the MediaMTX / LiveKit options.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audit import AuditLog
from .auth import (
    AuthError,
    authenticate_pilot,
    authenticate_vehicle,
    create_token,
    current_claims,
    decode_token,
)
from .config import Settings
from .hub import CommandRejected, Hub
from .models import (
    CreateSessionRequest,
    CreateSessionResponse,
    LoginRequest,
    LoginResponse,
    VehicleConfig,
    VehicleConnection,
)
from .registry import VehicleRegistry
from .sessions import PilotConn, Session, SessionManager

log = logging.getLogger("rovops.server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.audit.start()
        task = asyncio.create_task(_expiry_loop(app))
        log.info("ROVOps server started (vehicles=%d)", len(app.state.registry.states()))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            log.info("ROVOps server stopped")

    app = FastAPI(title="ROVOps Control Server", version="0.1.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.registry = VehicleRegistry(cfg)
    app.state.sessions = SessionManager(cfg)
    app.state.audit = AuditLog(cfg.audit_file)
    app.state.hub = Hub(cfg, app.state.registry, app.state.sessions, app.state.audit)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- HTTP ------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "time": time.time(),
            "vehicles_online": sum(1 for v in app.state.registry.states() if v["online"]),
            "sessions": len(app.state.sessions.active()),
        }

    @app.get("/")
    async def index() -> FileResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=404, detail="pilot UI not built")
        return FileResponse(index_file)

    @app.post("/api/auth/login", response_model=LoginResponse)
    async def login(body: LoginRequest) -> LoginResponse:
        role = None
        if body.username == cfg.admin_user and body.password == cfg.admin_password:
            role = "admin"
        elif body.username == cfg.demo_user and body.password == cfg.demo_password:
            role = "pilot"
        if role is None:
            await app.state.audit.log("login_failed", user=body.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
        token = create_token(cfg, subject=body.username, role=role, scope="user")
        await app.state.audit.log("login", user=body.username, role=role)
        return LoginResponse(token=token, user=body.username, role=role, expires_in=cfg.jwt_ttl_s)

    @app.get("/api/me")
    async def me(claims: dict[str, Any] = Depends(current_claims)) -> dict[str, Any]:
        return {"user": claims["sub"], "role": claims.get("role"), "exp": claims.get("exp")}

    @app.get("/api/vehicles")
    async def list_vehicles(_: dict[str, Any] = Depends(current_claims)) -> dict[str, Any]:
        return {"vehicles": app.state.registry.states()}

    @app.get("/api/vehicles/{vehicle_id}")
    async def get_vehicle(vehicle_id: str, _: dict[str, Any] = Depends(current_claims)) -> dict[str, Any]:
        state = app.state.registry.state(vehicle_id)
        if state is None:
            raise HTTPException(status_code=404, detail="vehicle not found")
        return state

    @app.post("/api/sessions", response_model=CreateSessionResponse)
    async def create_session(
        body: CreateSessionRequest,
        request: Request,
        claims: dict[str, Any] = Depends(current_claims),
    ) -> CreateSessionResponse:
        config = app.state.registry.get_config(body.vehicle_id)
        if config is None:
            raise HTTPException(status_code=404, detail="vehicle not found")
        session, created = app.state.sessions.get_or_create(body.vehicle_id, claims["sub"])
        token = create_token(
            cfg,
            subject=claims["sub"],
            role=claims.get("role", "pilot"),
            scope="session",
            ttl_s=min(cfg.session_ttl_s, 3600),
            sid=session.id,
            vid=session.vehicle_id,
        )
        ws_path = f"/ws/pilot/{session.id}"
        ws_url = f"{cfg.public_ws_base.rstrip('/')}{ws_path}" if cfg.public_ws_base else ws_path
        await app.state.audit.log(
            "session_create" if created else "session_join",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=claims["sub"],
            client=str(request.client.host) if request.client else None,
        )
        return CreateSessionResponse(
            session_id=session.id,
            token=token,
            ws_url=ws_url,
            expires_at=session.expires_at,
            vehicle=app.state.registry.state(body.vehicle_id) or config.public(False),
            command_ttl_ms=cfg.command_ttl_ms,
        )

    @app.get("/api/sessions/{session_id}")
    async def get_session(
        session_id: str,
        _: dict[str, Any] = Depends(current_claims),
    ) -> dict[str, Any]:
        session = app.state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "session_id": session.id,
            "vehicle_id": session.vehicle_id,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
            "created_by": session.created_by,
            "lease_holder": session.lease_holder,
            "estop": session.estop,
            "estop_by": session.estop_by,
            "pilots": [
                {"id": p.id, "user": p.user, "is_controller": p.id == session.lease_holder}
                for p in session.pilots.values()
            ],
            "vehicle": app.state.registry.state(session.vehicle_id),
        }

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(
        session_id: str,
        claims: dict[str, Any] = Depends(current_claims),
    ) -> dict[str, Any]:
        session = app.state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if claims.get("role") != "admin" and session.created_by != claims["sub"]:
            raise HTTPException(status_code=403, detail="only the creator or an admin may end the session")
        await app.state.sessions.broadcast_json(
            session, {"type": "error", "code": "session_closed", "message": "session ended by operator"}
        )
        if session.lease_holder:
            await app.state.hub.neutral_burst(session.vehicle_id, seconds=0.5)
        await _close_pilot_sockets(session, code=4001, reason="session closed")
        app.state.sessions.close(session.id)
        await app.state.audit.log(
            "session_close", session_id=session.id, vehicle_id=session.vehicle_id, user=claims["sub"]
        )
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/audit")
    async def session_audit(
        session_id: str,
        limit: int = 200,
        _: dict[str, Any] = Depends(current_claims),
    ) -> dict[str, Any]:
        records = [r for r in app.state.audit.recent(2000) if r.get("session_id") == session_id]
        return {"events": records[-max(1, min(limit, 500)):]}

    @app.get("/api/audit")
    async def global_audit(
        limit: int = 200,
        claims: dict[str, Any] = Depends(current_claims),
    ) -> dict[str, Any]:
        if claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
        return {"events": app.state.audit.recent(max(1, min(limit, 500)))}

    # -- vehicle WebSocket ----------------------------------------------

    @app.websocket("/ws/vehicle")
    async def vehicle_socket(ws: WebSocket) -> None:
        try:
            claims, config = authenticate_vehicle(ws)
        except AuthError as exc:
            await ws.close(code=4401, reason=str(exc))
            return

        await ws.accept()
        conn = VehicleConnection(config=config, ws=ws, sysid=config.sysid)
        previous = app.state.registry.attach(conn)
        if previous is not None:
            try:
                await previous.ws.close(code=4000, reason="replaced by a newer agent connection")
            except Exception:
                pass

        await ws.send_json(
            {
                "type": "welcome",
                "protocol": 1,
                "server_time": time.time(),
                "vehicle_id": config.id,
                "command_ttl_ms": cfg.command_ttl_ms,
                "neutral_hz": cfg.neutral_hz,
            }
        )
        await app.state.audit.log(
            "vehicle_online",
            vehicle_id=config.id,
            user=claims["sub"],
            remote=str(ws.client.host) if ws.client else None,
        )
        session = app.state.sessions.by_vehicle(config.id)
        if session:
            await _broadcast_vehicle_state(app, session, config.id, True)

        ping_task = asyncio.create_task(_vehicle_ping_loop(app, conn))
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is not None:
                    await _handle_vehicle_bytes(app, conn, raw)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                await _handle_vehicle_json(app, conn, payload)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("vehicle socket failed for %s", config.id)
        finally:
            ping_task.cancel()
            app.state.registry.detach(config.id, conn)
            session = app.state.sessions.by_vehicle(config.id)
            if session:
                await _broadcast_vehicle_state(app, session, config.id, False)
            await app.state.audit.log("vehicle_offline", vehicle_id=config.id)

    # -- pilot WebSocket --------------------------------------------------

    @app.websocket("/ws/pilot/{session_id}")
    async def pilot_socket(ws: WebSocket, session_id: str) -> None:
        try:
            claims = authenticate_pilot(ws, session_id)
        except AuthError as exc:
            await ws.close(code=4401, reason=str(exc))
            return
        session = app.state.sessions.get(session_id)
        if session is None:
            await ws.close(code=4404, reason="session not found or expired")
            return

        await ws.accept()
        pilot = PilotConn(id=secrets.token_hex(4), user=claims["sub"], ws=ws)
        app.state.sessions.attach(session, pilot)
        await app.state.audit.log(
            "pilot_join",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=pilot.user,
            pilot_id=pilot.id,
        )
        await pilot.send_json(
            {
                "type": "session_state",
                "session": session.state_for(pilot, cfg.command_ttl_ms),
                "vehicle": app.state.registry.state(session.vehicle_id),
                "video": _video_info(app, session.vehicle_id),
                "server_time": time.time(),
            }
        )
        await app.state.sessions.broadcast_json(
            session,
            {"type": "pilot_joined", "pilot": {"id": pilot.id, "user": pilot.user}},
        )
        await app.state.hub.broadcast_state(session, "pilot_joined")

        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is not None:
                    await _handle_pilot_bytes(app, session, pilot, raw)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await _send_error(pilot, "bad_json", "message is not valid JSON")
                    continue
                await _handle_pilot_json(app, session, pilot, payload)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("pilot socket failed for session %s pilot %s", session.id, pilot.id)
        finally:
            await app.state.hub.lease_disconnected(session, pilot)
            await app.state.audit.log(
                "pilot_leave",
                session_id=session.id,
                vehicle_id=session.vehicle_id,
                user=pilot.user,
                pilot_id=pilot.id,
            )
            await app.state.sessions.broadcast_json(
                session,
                {"type": "pilot_left", "pilot": {"id": pilot.id, "user": pilot.user}},
            )
            await app.state.hub.broadcast_state(session, "pilot_left")

    return app


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------


async def _handle_vehicle_json(app: FastAPI, conn: VehicleConnection, payload: dict[str, Any]) -> None:
    cfg: Settings = app.state.settings
    kind = payload.get("type")
    now = time.time()
    conn.last_seen = now

    if kind == "hello":
        conn.info = dict(payload.get("agent") or {})
        conn.video = dict(payload.get("video") or {})
        conn.sysid = int(payload.get("sysid") or conn.config.sysid)
        conn.compid = int(payload.get("compid") or 1)
        conn.telemetry = dict(payload.get("telemetry") or conn.telemetry)
        app.state.registry.update_telemetry(conn.config.id, conn.telemetry)
        await app.state.audit.log(
            "vehicle_hello",
            vehicle_id=conn.config.id,
            sysid=conn.sysid,
            agent=conn.info,
            video=conn.video,
        )
        session = app.state.sessions.by_vehicle(conn.config.id)
        if session:
            await _broadcast_vehicle_state(app, session, conn.config.id, True)
        return

    if kind == "telemetry":
        data = payload.get("data")
        if isinstance(data, dict):
            app.state.registry.update_telemetry(conn.config.id, data)
            session = app.state.sessions.by_vehicle(conn.config.id)
            if session:
                await app.state.sessions.broadcast_json(
                    session,
                    {
                        "type": "telemetry",
                        "vehicle_id": conn.config.id,
                        "ts": now,
                        "data": data,
                    },
                )
        return

    if kind == "pong":
        sent_at = payload.get("ts")
        if isinstance(sent_at, (int, float)):
            conn.rtt_ms = max(0.0, (now - float(sent_at)) * 1000.0)
        return

    if kind == "heartbeat":
        session = app.state.sessions.by_vehicle(conn.config.id)
        if session:
            await app.state.sessions.broadcast_json(
                session,
                {
                    "type": "vehicle_heartbeat",
                    "vehicle_id": conn.config.id,
                    "ts": now,
                    "rtt_ms": conn.rtt_ms,
                    "data": payload.get("data") or {},
                },
            )
        return

    if kind == "log":
        log.info(
            "vehicle %s [%s] %s",
            conn.config.id,
            payload.get("level", "info"),
            payload.get("message", ""),
        )
        return

    if kind == "estop_ack":
        session = app.state.sessions.by_vehicle(conn.config.id)
        if session:
            await app.state.sessions.broadcast_json(
                session,
                {"type": "estop_ack", "vehicle_id": conn.config.id, "ts": now},
            )
        return

    log.debug("unhandled vehicle message %s: %s", conn.config.id, kind)


async def _handle_vehicle_bytes(app: FastAPI, conn: VehicleConnection, raw: bytes) -> None:
    session = app.state.sessions.by_vehicle(conn.config.id)
    if session:
        await app.state.sessions.broadcast_bytes(session, raw)


async def _handle_pilot_bytes(app: FastAPI, session: Session, pilot: PilotConn, raw: bytes) -> None:
    """Legacy/raw MAVLink passthrough for an existing GCS client.

    The client must hold the lease.  Raw mode is powerful; only enable it for
    trusted clients.  Use the JSON command API for new applications so the
    server can validate ranges and audit intent.
    """

    # Distinguish "E-stop latched" from "no lease": raw bytes must never reach
    # the vehicle while the E-stop is latched, and the client needs the real
    # reason to react (clear the latch instead of grabbing the lease).
    if session.estop:
        await _send_error(pilot, "estop_active", "emergency stop is latched")
        return
    if not app.state.sessions.can_command(session, pilot):
        await _send_error(pilot, "no_lease", "raw MAVLink requires the control lease")
        return
    if not _rate_ok(pilot, app.state.settings.max_command_hz):
        await _send_error(pilot, "rate_limited", "too many MAVLink frames")
        return
    if not await app.state.hub.send_to_vehicle(session.vehicle_id, raw):
        await _send_error(pilot, "vehicle_offline", "vehicle is offline")
        return
    app.state.sessions.mark_command(session)


async def _handle_pilot_json(app: FastAPI, session: Session, pilot: PilotConn, payload: dict[str, Any]) -> None:
    cfg: Settings = app.state.settings
    kind = payload.get("type")
    hub: Hub = app.state.hub
    try:
        if kind == "ping":
            await pilot.send_json(
                {
                    "type": "pong",
                    "ts": payload.get("ts"),
                    "server_time": time.time(),
                }
            )
            return

        if kind == "lease_acquire":
            await hub.acquire_lease(session, pilot)
            hub.start_neutral_watchdog(session)
            return

        if kind == "lease_release":
            await hub.release_lease(session, pilot)
            return

        if kind == "manual":
            if not _rate_ok(pilot, cfg.max_command_hz):
                await _send_error(pilot, "rate_limited", "too many control frames")
                return
            await hub.manual(session, pilot, payload)
            return

        if kind == "arm":
            await hub.arm(session, pilot, arm=bool(payload.get("arm", True)))
            await hub.broadcast_state(session, "arm")
            return

        if kind == "mode":
            mode = str(payload.get("mode") or "")
            await hub.set_mode(session, pilot, mode)
            await hub.broadcast_state(session, "mode")
            return

        if kind == "estop":
            await hub.estop(session, pilot)
            return

        if kind == "estop_reset":
            await hub.reset_estop(session, pilot)
            return

        await _send_error(pilot, "unknown_type", f"unknown message type: {kind}")
    except CommandRejected as exc:
        await app.state.audit.log(
            "command_rejected",
            session_id=session.id,
            vehicle_id=session.vehicle_id,
            user=pilot.user,
            code=exc.code,
            message=exc.message,
        )
        await _send_error(pilot, exc.code, exc.message)
    except Exception:
        log.exception("command handler failed")
        await _send_error(pilot, "server_error", "command failed")


async def _close_pilot_sockets(session: Session, *, code: int, reason: str) -> None:
    for pilot in list(session.pilots.values()):
        try:
            await pilot.ws.close(code=code, reason=reason)
        except Exception:
            log.debug("could not close pilot socket %s", pilot.id, exc_info=True)


async def _broadcast_vehicle_state(
    app: FastAPI, session: Session, vehicle_id: str, online: bool
) -> None:
    await app.state.sessions.broadcast_json(
        session,
        {
            "type": "vehicle",
            "vehicle_id": vehicle_id,
            "online": online,
            "vehicle": app.state.registry.state(vehicle_id),
        },
    )


async def _vehicle_ping_loop(app: FastAPI, conn: VehicleConnection) -> None:
    try:
        while True:
            await asyncio.sleep(5.0)
            conn.ping_seq += 1
            conn.last_ping_at = time.time()
            try:
                await conn.ws.send_json(
                    {"type": "ping", "ts": conn.last_ping_at, "seq": conn.ping_seq}
                )
            except Exception:
                return
    except asyncio.CancelledError:
        raise


async def _expiry_loop(app: FastAPI) -> None:
    try:
        while True:
            await asyncio.sleep(5.0)
            for session in app.state.sessions.active():
                if session.expires_at > time.time():
                    continue
                try:
                    await app.state.sessions.broadcast_json(
                        session,
                        {"type": "error", "code": "session_expired", "message": "session expired"},
                    )
                    if session.lease_holder:
                        await app.state.hub.neutral_burst(session.vehicle_id, seconds=0.5)
                    await _close_pilot_sockets(session, code=4001, reason="session expired")
                finally:
                    app.state.sessions.close(session.id)
                    await app.state.audit.log(
                        "session_expired", session_id=session.id, vehicle_id=session.vehicle_id
                    )
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _video_info(app: FastAPI, vehicle_id: str) -> dict[str, Any]:
    conn = app.state.registry.get_connection(vehicle_id)
    if conn and conn.video:
        return conn.video
    cfg: Settings = app.state.settings
    info: dict[str, Any] = {}
    if cfg.public_whep_base:
        info["whep_url"] = f"{cfg.public_whep_base.rstrip('/')}/{vehicle_id}/whep"
    if cfg.public_rtsp_base:
        info["rtsp_url"] = f"{cfg.public_rtsp_base.rstrip('/')}/{vehicle_id}"
    return info


def _rate_ok(pilot: PilotConn, max_hz: float) -> bool:
    now = time.time()
    times = getattr(pilot, "_cmd_times", None)
    if times is None:
        times = deque()
        setattr(pilot, "_cmd_times", times)
    while times and now - times[0] > 1.0:
        times.popleft()
    if len(times) >= max_hz:
        return False
    times.append(now)
    return True


async def _send_error(pilot: PilotConn, code: str, message: str) -> None:
    try:
        await pilot.send_json({"type": "error", "code": code, "message": message})
    except Exception:
        pass


app = create_app()
