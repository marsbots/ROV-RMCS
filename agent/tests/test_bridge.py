"""Unit tests for the agent's local safety logic."""

from __future__ import annotations

import time
from typing import Any

from pymavlink import mavutil

from rovops_agent.config import AgentConfig
from rovops_agent.mavlink_bridge import MavlinkBridge

MAV = mavutil.mavlink


class FakeLink:
    def __init__(self) -> None:
        self.mav = MAV.MAVLink(None, srcSystem=255, srcComponent=190)
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


def make_bridge(**overrides: Any) -> MavlinkBridge:
    config = AgentConfig(vehicle_token="test", agent_id="test-agent", **overrides)
    bridge = MavlinkBridge(config)
    bridge._link = FakeLink()
    bridge._vehicle_sysid = 2
    bridge._vehicle_compid = 1
    return bridge


def pack(message: Any) -> bytes:
    return message.pack(MAV.MAVLink(None, srcSystem=255, srcComponent=190))


def decode(raw: bytes) -> Any:
    parser = MAV.MAVLink(None)
    messages = parser.parse_buffer(raw)
    assert messages
    return messages[0]


def manual(target: int = 1, x: int = 300) -> bytes:
    return pack(
        MAV.MAVLink_manual_control_message(
            target=target, x=x, y=10, z=600, r=20, buttons=0
        )
    )


def command(command_id: int, param1: float) -> bytes:
    return pack(
        MAV.MAVLink_command_long_message(
            target_system=1,
            target_component=1,
            command=command_id,
            confirmation=0,
            param1=param1,
            param2=0,
            param3=0,
            param4=0,
            param5=0,
            param6=0,
            param7=0,
        )
    )


def test_manual_is_retargeted_to_vehicle_sysid() -> None:
    bridge = make_bridge()
    bridge._handle_command_frame(manual(target=1, x=300))

    assert len(bridge._link.written) == 1
    message = decode(bridge._link.written[0])
    assert message.get_type() == "MANUAL_CONTROL"
    assert int(message.target) == 2
    assert int(message.x) == 300
    assert bridge._last_manual > 0
    assert bridge._had_manual is True


def test_disarm_is_allowed_but_arm_is_dropped_during_estop() -> None:
    bridge = make_bridge()
    bridge.set_estop(True)

    bridge._handle_command_frame(command(MAV.MAV_CMD_COMPONENT_ARM_DISARM, 1.0))
    assert bridge._link.written == []
    assert bridge._dropped_frames == 1

    bridge._handle_command_frame(command(MAV.MAV_CMD_COMPONENT_ARM_DISARM, 0.0))
    assert len(bridge._link.written) == 1
    disarm = decode(bridge._link.written[0])
    assert disarm.get_type() == "COMMAND_LONG"
    assert float(disarm.param1) == 0.0


def test_motion_is_dropped_during_estop() -> None:
    bridge = make_bridge()
    bridge.set_estop(True)
    before = bridge._last_manual

    bridge._handle_command_frame(manual(x=800))

    assert bridge._link.written == []
    assert bridge._last_manual == before
    assert bridge._dropped_frames == 1


def test_unsupported_commands_are_dropped() -> None:
    bridge = make_bridge()
    # MAV_CMD_NAV_TAKEOFF is not on the allow-list.
    bridge._handle_command_frame(command(22, 0.0))
    assert bridge._link.written == []
    assert bridge._dropped_frames == 1


def test_watchdog_sends_neutral_after_ttl() -> None:
    bridge = make_bridge(command_ttl_ms=300, neutral_hz=10)
    bridge._had_manual = True
    bridge._last_manual = time.monotonic() - 1.0
    bridge._last_neutral_tx = 0.0

    bridge._run_watchdog()

    assert bridge._link.written
    neutral = decode(bridge._link.written[0])
    assert neutral.get_type() == "MANUAL_CONTROL"
    assert int(neutral.x) == 0
    assert int(neutral.y) == 0
    assert int(neutral.z) == 500
    assert int(neutral.target) == 2


def test_watchdog_does_not_send_before_ttl() -> None:
    bridge = make_bridge(command_ttl_ms=5000, neutral_hz=10)
    bridge._had_manual = True
    bridge._last_manual = time.monotonic()
    bridge._run_watchdog()
    assert bridge._link.written == []


def test_estop_forces_neutral_regardless_of_commands() -> None:
    bridge = make_bridge(estop_hz=20)
    bridge.set_estop(True)
    bridge._estop_sent_at = time.monotonic()  # skip the immediate disarm
    bridge._last_neutral_tx = 0.0

    bridge._run_watchdog()

    assert bridge._link.written
    neutral = decode(bridge._link.written[0])
    assert neutral.get_type() == "MANUAL_CONTROL"
    assert int(neutral.x) == 0
    assert int(neutral.z) == 500


def test_ws_url_hides_path_and_appends_token() -> None:
    config = AgentConfig(
        server_url="https://rov.example.com:8443/base",
        ws_path="/ws/vehicle",
        vehicle_token="secret-token",
    )
    assert config.ws_url == "wss://rov.example.com:8443/ws/vehicle?token=secret-token"
    assert "secret-token" not in config.redacted()["server"]
