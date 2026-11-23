"""Raw MAVLink byte passthrough over /ws/pilot (M2-A).

MARSUB Remote Mode needs to push pre-encoded MAVLink frames through the pilot
socket for features the JSON command surface does not model yet.  The contract
(``docs/api-contract.md`` §6) allows binary frames on the pilot socket, but only
for the lease holder and only within the command rate limit.

These tests pin that behaviour: binary bytes reach the vehicle agent verbatim,
are refused without the lease, and are refused once the E-stop is latched.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.mavlink import MavlinkCodec, decode_frames
from test_api import create_session, login
from test_ws_control import DEVICE_TOKEN, recv_json

_CODEC = MavlinkCodec()


def encode_manual(x: int = 111) -> bytes:
    """Encode a single MANUAL_CONTROL frame with the server-side codec."""
    return _CODEC.manual_control(1, x=x, y=0, z=500, r=0)


def _open_session(client: TestClient) -> tuple[dict[str, Any], str]:
    token = login(client)["token"]
    session = create_session(client, token)
    return session, session["token"]


def _join_pilot(client: TestClient, session: dict[str, Any], token: str) -> Any:
    return client.websocket_connect(f"/ws/pilot/{session['session_id']}?token={token}")


def _drain_pilot(ws: Any, limit: int = 60) -> None:
    """Consume the join-time broadcasts so later reads see fresh messages."""
    for _ in range(limit):
        message = ws.receive()
        if message.get("text") is None:
            continue
        payload = json.loads(message["text"])
        if payload.get("type") == "state":
            return


def test_raw_bytes_are_forwarded_to_vehicle(client: TestClient) -> None:
    session, session_token = _open_session(client)

    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1, "agent": {"name": "raw-test"}})

        with _join_pilot(client, session, session_token) as pilot:
            recv_json(pilot, "session_state")
            _drain_pilot(pilot)

            pilot.send_json({"type": "lease_acquire"})
            assert recv_json(pilot, "lease")["granted"] is True
            _drain_pilot(pilot)

            raw = encode_manual(111)
            pilot.send_bytes(raw)

            # The vehicle receives the exact bytes the pilot sent.
            for _ in range(100):
                message = vehicle.receive()
                payload = message.get("bytes")
                if payload is None:
                    continue
                assert payload == raw
                frames = decode_frames(payload)
                assert any(int(getattr(f, "x", -1)) == 111 for f in frames)
                break
            else:
                raise AssertionError("vehicle never received the raw frame")


def test_raw_bytes_require_lease(client: TestClient) -> None:
    session, session_token = _open_session(client)

    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1, "agent": {"name": "raw-test"}})

        with _join_pilot(client, session, session_token) as pilot:
            recv_json(pilot, "session_state")
            _drain_pilot(pilot)

            pilot.send_bytes(encode_manual(222))
            error = recv_json(pilot, "error")
            assert error["code"] == "no_lease"


def test_raw_bytes_blocked_while_estop_latched(client: TestClient) -> None:
    session, session_token = _open_session(client)

    with client.websocket_connect(f"/ws/vehicle?token={DEVICE_TOKEN}") as vehicle:
        recv_json(vehicle, "welcome")
        vehicle.send_json({"type": "hello", "sysid": 1, "compid": 1, "agent": {"name": "raw-test"}})

        with _join_pilot(client, session, session_token) as pilot:
            recv_json(pilot, "session_state")
            _drain_pilot(pilot)

            pilot.send_json({"type": "lease_acquire"})
            assert recv_json(pilot, "lease")["granted"] is True
            _drain_pilot(pilot)

            pilot.send_json({"type": "estop"})
            recv_json(pilot, "estop")
            _drain_pilot(pilot)

            pilot.send_bytes(encode_manual(333))
            error = recv_json(pilot, "error")
            assert error["code"] == "estop_active"
