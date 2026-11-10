"""Thin MAVLink helpers for the control server.

The server does not inspect the vehicle's full telemetry stream (the vehicle
agent already reduces that to JSON for the dashboard).  It only needs to
*encode* the small set of commands a pilot is allowed to send, and to decode
raw frames for legacy passthrough clients and tests.

Using a single codec object per process keeps the MAVLink sequence number
monotonic.  A lock makes the object safe to use from multiple coroutines.
"""

from __future__ import annotations

import threading
from typing import Any

from pymavlink.dialects.v20 import common as mavlink2

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_DO_SET_MODE = 176
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1

# ArduSub custom mode numbers.  Keep this list in sync with the vehicle's
# firmware; unknown names are rejected instead of guessed.
ARDUSUB_MODES: dict[str, int] = {
    "STABILIZE": 0,
    "ACRO": 1,
    "ALT_HOLD": 2,
    "AUTO": 3,
    "GUIDED": 4,
    "CIRCLE": 7,
    "SURFACE": 9,
    "POSHOLD": 16,
    "MANUAL": 19,
    "SURFTRAK": 21,
}

NEUTRAL = {"x": 0, "y": 0, "z": 500, "r": 0, "buttons": 0, "buttons2": 0}


class MavlinkError(ValueError):
    pass


class MavlinkCodec:
    def __init__(self, source_system: int = 255, source_component: int = 190) -> None:
        self._mav = mavlink2.MAVLink(None, srcSystem=source_system, srcComponent=source_component)
        self._lock = threading.Lock()

    def _pack(self, message: Any) -> bytes:
        with self._lock:
            return message.pack(self._mav)

    def heartbeat(self, *, vehicle_type: int = 2, autopilot: int = 3) -> bytes:
        msg = mavlink2.MAVLink_heartbeat_message(
            type=vehicle_type,
            autopilot=autopilot,
            base_mode=0,
            custom_mode=0,
            system_status=4,
            mavlink_version=3,
        )
        return self._pack(msg)

    def manual_control(
        self,
        target_system: int,
        *,
        x: int = 0,
        y: int = 0,
        z: int = 500,
        r: int = 0,
        buttons: int = 0,
        buttons2: int = 0,
    ) -> bytes:
        msg = mavlink2.MAVLink_manual_control_message(
            target=target_system,
            x=x,
            y=y,
            z=z,
            r=r,
            buttons=buttons,
            buttons2=buttons2,
        )
        return self._pack(msg)

    def arm(self, target_system: int, *, arm: bool, target_component: int = 1) -> bytes:
        msg = mavlink2.MAVLink_command_long_message(
            target_system=target_system,
            target_component=target_component,
            command=MAV_CMD_COMPONENT_ARM_DISARM,
            confirmation=0,
            param1=1.0 if arm else 0.0,
            param2=0.0,
            param3=0.0,
            param4=0.0,
            param5=0.0,
            param6=0.0,
            param7=0.0,
        )
        return self._pack(msg)

    def set_mode(self, target_system: int, mode: str, *, target_component: int = 1) -> bytes:
        custom_mode = ARDUSUB_MODES.get(mode.upper())
        if custom_mode is None:
            raise MavlinkError(f"unsupported ArduSub mode: {mode}")
        msg = mavlink2.MAVLink_command_long_message(
            target_system=target_system,
            target_component=target_component,
            command=MAV_CMD_DO_SET_MODE,
            confirmation=0,
            param1=float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
            param2=float(custom_mode),
            param3=0.0,
            param4=0.0,
            param5=0.0,
            param6=0.0,
            param7=0.0,
        )
        return self._pack(msg)


def decode_frames(data: bytes) -> list[Any]:
    """Best-effort decode of one or more MAVLink frames.

    Returns an empty list for unsupported dialects or malformed data.  This is
    used by tests and by the optional raw-passthrough path, never on the hot
    telemetry path.
    """

    parser = mavlink2.MAVLink(None)
    try:
        return parser.parse_buffer(data) or []
    except Exception:
        return []
