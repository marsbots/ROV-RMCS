"""Safety-focused WebSocket tests."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.mavlink import MAV_CMD_COMPONENT_ARM_DISARM
from test_api import auth, create_session, login
from test_ws_control import recv_json

DEVICE_TOKEN = "test-device-token-rov-01"


def setup(client: TestClient) -> tuple[str, dict]:
    token = login(client)["token"]
    session = create_session(client, token)
    return token, session


def test_manual_range_is_rejected(client: TestClient) -> None:
    _, session = setup(client)
    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1})
        with client.websocket_connect(
            f"/ws/pilot/{session['session_id']}?token={session['token']}"
        ) as pilot:
            recv_json(pilot, "session_state")
            recv_json(pilot, "pilot_joined")
            recv_json(pilot, "state")
            pilot.send_json({"type": "lease_acquire"})
            recv_json(pilot, "lease")
            recv_json(pilot, "state")
            pilot.send_json({"type": "manual", "x": 2000, "y": 0, "z": 500, "r": 0})
            error = recv_json(pilot, "error")
            assert error["code"] == "bad_manual"


def test_unknown_mode_is_rejected(client: TestClient) -> None:
    _, session = setup(client)
    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1})
        with client.websocket_connect(
            f"/ws/pilot/{session['session_id']}?token={session['token']}"
        ) as pilot:
            recv_json(pilot, "session_state")
            recv_json(pilot, "pilot_joined")
            recv_json(pilot, "state")
            pilot.send_json({"type": "lease_acquire"})
            recv_json(pilot, "lease")
            recv_json(pilot, "state")
            pilot.send_json({"type": "mode", "mode": "NOT_A_MODE"})
            error = recv_json(pilot, "error")
            assert error["code"] == "bad_mode"


def test_arm_blocked_while_estop_latched(client: TestClient) -> None:
    _, session = setup(client)
    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1})
        with client.websocket_connect(
            f"/ws/pilot/{session['session_id']}?token={session['token']}"
        ) as pilot:
            recv_json(pilot, "session_state")
            recv_json(pilot, "pilot_joined")
            recv_json(pilot, "state")
            pilot.send_json({"type": "lease_acquire"})
            recv_json(pilot, "lease")
            recv_json(pilot, "state")
            pilot.send_json({"type": "estop"})
            recv_json(pilot, "estop")
            recv_json(pilot, "state")
            pilot.send_json({"type": "arm", "arm": True})
            error = recv_json(pilot, "error")
            assert error["code"] == "estop_active"


def test_estop_reset_requires_lease(client: TestClient) -> None:
    token, session = setup(client)
    second = create_session(client, token)
    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1})
        with client.websocket_connect(
            f"/ws/pilot/{session['session_id']}?token={session['token']}"
        ) as first:
            recv_json(first, "session_state")
            recv_json(first, "pilot_joined")
            recv_json(first, "state")
            first.send_json({"type": "lease_acquire"})
            recv_json(first, "lease")
            recv_json(first, "state")
            first.send_json({"type": "estop"})
            recv_json(first, "estop")
            recv_json(first, "state")
            with client.websocket_connect(
                f"/ws/pilot/{session['session_id']}?token={second['token']}"
            ) as second_ws:
                recv_json(second_ws, "session_state")
                recv_json(second_ws, "pilot_joined")
                recv_json(second_ws, "state")
                second_ws.send_json({"type": "estop_reset"})
                error = recv_json(second_ws, "error")
                assert error["code"] == "no_lease"
