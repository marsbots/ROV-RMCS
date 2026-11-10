"""End-to-end WebSocket tests for lease, E-stop and neutral behaviour."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.mavlink import MAV_CMD_COMPONENT_ARM_DISARM, decode_frames
from test_api import create_session, login

DEVICE_TOKEN = "test-device-token-rov-01"


def recv_json(ws, kind: str, predicate: Callable[[dict], bool] | None = None, limit: int = 200) -> dict[str, Any]:
    for _ in range(limit):
        message = ws.receive()
        text = message.get("text")
        if text is None:
            continue
        data = json.loads(text)
        if data.get("type") == kind and (predicate is None or predicate(data)):
            return data
    raise AssertionError(f"did not receive message type {kind!r}")


def recv_frames(
    ws,
    predicate: Callable[[Any], bool] | None = None,
    limit: int = 200,
) -> list[Any]:
    for _ in range(limit):
        message = ws.receive()
        raw = message.get("bytes")
        if raw is None:
            continue
        frames = decode_frames(raw)
        if predicate is None or any(predicate(frame) for frame in frames):
            return frames
    raise AssertionError("did not receive matching MAVLink frames")


def is_manual(x: int) -> Callable[[Any], bool]:
    return lambda msg: msg.get_type() == "MANUAL_CONTROL" and int(msg.x) == x


def setup_session(client: TestClient) -> tuple[dict, dict]:
    token = login(client)["token"]
    session = create_session(client, token)
    return token, session


def test_device_auth_rejects_bad_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/vehicle?token=not-a-token"):
            pass


def test_pilot_token_is_bound_to_session(client: TestClient) -> None:
    _, session = setup_session(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/pilot/not-the-session?token={session['token']}"):
            pass


def test_full_control_flow(client: TestClient) -> None:
    _, session = setup_session(client)
    vehicle_url = f"/ws/vehicle?token={DEVICE_TOKEN}"

    with client.websocket_connect(vehicle_url) as vehicle:
        welcome = recv_json(vehicle, "welcome")
        assert welcome["vehicle_id"] == "rov-01"
        vehicle.send_json(
            {
                "type": "hello",
                "sysid": 1,
                "compid": 1,
                "agent": {"name": "pytest-agent"},
                "video": {"whep_url": "http://media.local/rov-01/whep"},
                "telemetry": {"mode": "STABILIZE"},
            }
        )

        with client.websocket_connect(f"/ws/pilot/{session['session_id']}?token={session['token']}") as pilot:
            initial = recv_json(pilot, "session_state")
            assert initial["session"]["session_id"] == session["session_id"]
            assert initial["video"]["whep_url"].endswith("/rov-01/whep")
            recv_json(pilot, "pilot_joined")
            recv_json(pilot, "state")

            # Commands before the lease are rejected.
            pilot.send_json({"type": "manual", "x": 100, "y": 0, "z": 500, "r": 0})
            error = recv_json(pilot, "error")
            assert error["code"] == "no_lease"

            pilot.send_json({"type": "arm", "arm": True})
            error = recv_json(pilot, "error")
            assert error["code"] == "no_lease"

            pilot.send_json({"type": "lease_acquire"})
            lease = recv_json(pilot, "lease")
            assert lease["granted"] is True
            assert lease["holder"] == lease["you"]
            recv_json(pilot, "state")

            # A leased manual command reaches the vehicle.
            pilot.send_json({"type": "manual", "x": 500, "y": -250, "z": 650, "r": 100})
            frames = recv_frames(vehicle, is_manual(500))
            manual = frames[0]
            assert manual.get_type() == "MANUAL_CONTROL"
            assert int(manual.target) == 1
            assert int(manual.y) == -250
            assert int(manual.z) == 650
            assert int(manual.r) == 100

            # Arm is forwarded as COMMAND_LONG.
            pilot.send_json({"type": "arm", "arm": True})
            frames = recv_frames(
                vehicle,
                lambda msg: msg.get_type() == "COMMAND_LONG"
                and int(msg.command) == MAV_CMD_COMPONENT_ARM_DISARM,
            )
            assert float(frames[0].param1) == 1.0

            # E-stop is latched, forwarded to the vehicle, and blocks motion.
            pilot.send_json({"type": "estop"})
            estop = recv_json(vehicle, "estop")
            assert estop["active"] is True
            assert recv_json(pilot, "estop")["active"] is True
            recv_json(pilot, "state")

            recv_frames(vehicle, is_manual(0))

            pilot.send_json({"type": "manual", "x": 100, "y": 0, "z": 500, "r": 0})
            error = recv_json(pilot, "error")
            assert error["code"] == "estop_active"

            # Disarming remains possible while the E-stop is latched.
            pilot.send_json({"type": "arm", "arm": False})
            frames = recv_frames(
                vehicle,
                lambda msg: msg.get_type() == "COMMAND_LONG"
                and int(msg.command) == MAV_CMD_COMPONENT_ARM_DISARM
                and float(msg.param1) == 0.0,
            )
            assert float(frames[0].param1) == 0.0

            # Reset requires the lease and clears the latch.
            pilot.send_json({"type": "estop_reset"})
            reset = recv_json(vehicle, "estop", lambda msg: msg.get("active") is False)
            assert reset["active"] is False
            assert recv_json(pilot, "estop")["active"] is False
            recv_json(pilot, "state")

            pilot.send_json({"type": "manual", "x": 250, "y": 0, "z": 500, "r": 0})
            recv_frames(vehicle, is_manual(250))

            # Releasing the lease sends neutral frames.
            pilot.send_json({"type": "lease_release"})
            release = recv_json(pilot, "lease")
            assert release["granted"] is False
            assert release["reason"] == "released"
            recv_json(pilot, "state")
            recv_frames(vehicle, is_manual(0))

            # Without the lease motion is rejected again.
            pilot.send_json({"type": "manual", "x": 1, "y": 0, "z": 500, "r": 0})
            assert recv_json(pilot, "error")["code"] == "no_lease"


def test_second_pilot_cannot_steal_lease(client: TestClient) -> None:
    token, session = setup_session(client)
    second = create_session(client, token)
    assert second["session_id"] == session["session_id"]

    with client.websocket_connect(f"/ws/pilot/{session['session_id']}?token={session['token']}") as first:
        recv_json(first, "session_state")
        recv_json(first, "pilot_joined")
        recv_json(first, "state")

        with client.websocket_connect(f"/ws/pilot/{session['session_id']}?token={second['token']}") as second_ws:
            recv_json(second_ws, "session_state")
            recv_json(second_ws, "pilot_joined")
            recv_json(second_ws, "state")

            first.send_json({"type": "lease_acquire"})
            assert recv_json(first, "lease")["granted"] is True
            recv_json(first, "state")

            second_ws.send_json({"type": "lease_acquire"})
            denied = recv_json(second_ws, "lease")
            assert denied["granted"] is False
            assert denied["reason"] == "held_by_another_pilot"
            recv_json(second_ws, "state")

            second_ws.send_json({"type": "manual", "x": 100, "y": 0, "z": 500, "r": 0})
            assert recv_json(second_ws, "error")["code"] == "no_lease"


def test_lease_holder_disconnect_forces_neutral(client: TestClient) -> None:
    _, session = setup_session(client)
    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1})

        with client.websocket_connect(f"/ws/pilot/{session['session_id']}?token={session['token']}") as pilot:
            recv_json(pilot, "session_state")
            recv_json(pilot, "pilot_joined")
            recv_json(pilot, "state")
            pilot.send_json({"type": "lease_acquire"})
            recv_json(pilot, "lease")
            recv_json(pilot, "state")

            pilot.send_json({"type": "manual", "x": 900, "y": 0, "z": 500, "r": 0})
            recv_frames(vehicle, is_manual(900))

        # Leaving the context closes the pilot socket; the server must send a
        # neutral burst because the lease holder disappeared.
        time.sleep(0.8)
        recv_frames(vehicle, is_manual(0))
