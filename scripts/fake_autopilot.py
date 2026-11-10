"""Minimal fake ArduSub autopilot for end-to-end smoke tests.

Listens for the agent's MAVLink traffic on a local UDP port, emits a small
telemetry stream, and logs the commands it receives as JSON lines.

    python scripts/fake_autopilot.py --port 14599 --duration 60 --log fake.jsonl

This is not a simulator: it only exercises the wire protocol and safety paths.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from pathlib import Path

from pymavlink import mavutil

MAV = mavutil.mavlink
ARDUSUB = 12  # MAV_TYPE_SUBMARINE
ARDUPILOT = 3  # MAV_AUTOPILOT_ARDUPILOTMEGA
CUSTOM_MODE = 0  # STABILIZE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=14599)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--log", type=Path, default=Path("fake-autopilot.jsonl"))
    parser.add_argument("--depth", type=float, default=3.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    dest = ("127.0.0.1", args.port)
    parser_mav = MAV.MAVLink(None)
    tx_mav = MAV.MAVLink(None, srcSystem=1, srcComponent=1)
    args.log.write_text("", encoding="utf-8")

    armed = False
    mode = CUSTOM_MODE
    started = time.monotonic()
    next_heartbeat = 0.0
    next_telemetry = 0.0
    next_attitude = 0.0

    def log(kind: str, **fields: object) -> None:
        record = {"ts": time.time(), "kind": kind, **fields}
        with args.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()

    print(f"fake autopilot -> 127.0.0.1:{args.port}", flush=True)
    while time.monotonic() - started < args.duration:
        now = time.monotonic()

        if now >= next_heartbeat:
            next_heartbeat = now + 0.5
            base_mode = MAV.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0
            sock.sendto(
                MAV.MAVLink_heartbeat_message(
                    ARDUSUB, ARDUPILOT, base_mode, mode, 4, 3
                ).pack(tx_mav),
                dest,
            )

        if now >= next_telemetry:
            next_telemetry = now + 0.5
            sock.sendto(
                MAV.MAVLink_sys_status_message(
                    0,
                    0,
                    0,
                    500,
                    12000,
                    -1,
                    75,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ).pack(tx_mav),
                dest,
            )
            pressure = 1013.25 - (args.depth / 0.010197)
            sock.sendto(
                MAV.MAVLink_scaled_pressure_message(0, pressure, 0.0, 20).pack(tx_mav),
                dest,
            )
            sock.sendto(
                MAV.MAVLink_vfr_hud_message(0.0, 0.0, 123, 0, 0.0, 0.0).pack(tx_mav),
                dest,
            )

        if now >= next_attitude:
            next_attitude = now + 0.2
            sock.sendto(
                MAV.MAVLink_attitude_message(
                    int(now * 1000) & 0xFFFFFFFF, 0.02, -0.01, 1.2, 0.0, 0.0, 0.0
                ).pack(tx_mav),
                dest,
            )

        try:
            while True:
                data, _addr = sock.recvfrom(4096)
                messages = parser_mav.parse_buffer(data) or []
                for msg in messages:
                    kind = msg.get_type()
                    if kind == "MANUAL_CONTROL":
                        x, y, z, r = int(msg.x), int(msg.y), int(msg.z), int(msg.r)
                        if (x, y, z, r) == (0, 0, 500, 0):
                            log("neutral", target=int(msg.target))
                        else:
                            log("manual", x=x, y=y, z=z, r=r, target=int(msg.target))
                    elif kind == "COMMAND_LONG":
                        command = int(msg.command)
                        if command == MAV.MAV_CMD_COMPONENT_ARM_DISARM:
                            armed = float(msg.param1) >= 0.5
                            log("armed" if armed else "disarmed", target=int(msg.target_system))
                        elif command == MAV.MAV_CMD_DO_SET_MODE:
                            mode = int(msg.param2)
                            log("mode", mode=mode)
        except (BlockingIOError, ConnectionResetError):
            # Windows can report WSAECONNRESET on an unconnected UDP socket
            # after an ICMP port-unreachable; keep the loop alive.
            pass

        time.sleep(0.02)

    print("fake autopilot stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
