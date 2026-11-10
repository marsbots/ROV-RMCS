"""Local MAVLink bridge with an authoritative safety watchdog.

Everything that can move the vehicle passes through this module.  The design
is intentionally conservative:

* only ``MANUAL_CONTROL``, arm/disarm and mode-change commands are accepted
  from the server; any other frame is dropped,
* frames are re-targeted to the vehicle's actual system ID,
* while an E-stop is latched every motion command is dropped and hard-neutral
  frames are sent continuously,
* if no manual command arrives within ``COMMAND_TTL_MS`` the bridge keeps
  sending neutral frames, so a dead server or pilot cannot leave thrust on.

The bridge runs in a dedicated thread because pymavlink's file objects are
blocking.  The asyncio side only pushes frames into a queue and reads immutable
telemetry snapshots.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from pymavlink import mavutil

from .config import AgentConfig

log = logging.getLogger(__name__)

NEUTRAL = {"x": 0, "y": 0, "z": 500, "r": 0, "buttons": 0, "buttons2": 0}

# Keep in sync with server/app/mavlink.py (the server validates mode names).
ARDUSUB_MODES = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    7: "CIRCLE",
    9: "SURFACE",
    16: "POSHOLD",
    19: "MANUAL",
    20: "MOTOR_DETECT",
    21: "SURFTRAK",
}

ALLOWED_COMMANDS = {
    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
    mavutil.mavlink.MAV_CMD_DO_SET_MODE,
}

ARMED_FLAG = mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED


class MavlinkBridge:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._commands: queue.Queue[bytes] = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._estop = threading.Event()

        self._link: Any = None
        self._parser = mavutil.mavlink.MAVLink(None)

        self._connected = False
        self._last_link_error: str | None = None
        self._last_vehicle_heartbeat = 0.0
        self._last_manual = 0.0
        self._had_manual = False
        self._estop_sent_at = 0.0
        self._last_heartbeat_tx = 0.0
        self._last_neutral_tx = 0.0
        self._dropped_frames = 0
        self._accepted_frames = 0
        self._vehicle_sysid = config.vehicle_sysid
        self._vehicle_compid = config.vehicle_compid

        self._telemetry: dict[str, Any] = {
            "armed": False,
            "mode": "UNKNOWN",
            "mode_num": None,
            "attitude": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "heading": 0.0,
            "depth_m": None,
            "battery": {"voltage": None, "current": None, "remaining": None},
            "throttle": 0.0,
            "rssi": None,
            "system_status": None,
        }

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mavlink-bridge", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        self._close_link()

    # -- public API -------------------------------------------------------

    def submit_frame(self, raw: bytes) -> bool:
        """Queue a raw MAVLink frame from the server for validation."""
        try:
            self._commands.put_nowait(raw)
            return True
        except queue.Full:
            self._dropped_frames += 1
            # Drop the oldest frame so the newest command still gets through.
            try:
                self._commands.get_nowait()
                self._commands.put_nowait(raw)
            except queue.Empty:
                pass
            return False

    def set_estop(self, active: bool) -> None:
        if active:
            self._estop.set()
            log.warning("E-STOP latched locally; forcing neutral")
        else:
            self._estop.clear()
            self._had_manual = False
            self._last_manual = 0.0
            log.warning("E-STOP reset; waiting for a fresh manual command")

    @property
    def estop(self) -> bool:
        return self._estop.is_set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = {
                **self._telemetry,
                "attitude": dict(self._telemetry["attitude"]),
                "battery": dict(self._telemetry["battery"]),
            }
        now = time.time()
        heartbeat_age = now - self._last_vehicle_heartbeat if self._last_vehicle_heartbeat else None
        manual_age = now - self._last_manual if self._last_manual else None
        data.update(
            {
                "sysid": self._vehicle_sysid,
                "compid": self._vehicle_compid,
                "mavlink_online": bool(heartbeat_age is not None and heartbeat_age < 5.0),
                "heartbeat_age_s": round(heartbeat_age, 3) if heartbeat_age is not None else None,
                "command_age_ms": round(manual_age * 1000) if manual_age is not None else None,
                "estop": self._estop.is_set(),
                "frames_accepted": self._accepted_frames,
                "frames_dropped": self._dropped_frames,
                "ts": now,
            }
        )
        return data

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "mavlink_endpoint": self.config.mavlink_endpoint,
            "last_error": self._last_link_error,
            "estop": self._estop.is_set(),
            "telemetry": self.snapshot(),
        }

    # -- thread -----------------------------------------------------------

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            if self._link is None:
                if not self._open_link():
                    self._sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
                    continue
                backoff = 0.5

            try:
                self._drain_commands()
                self._read_messages()
                self._send_heartbeat()
                self._run_watchdog()
                self._sleep(0.005)
            except Exception as exc:  # link failure -> reopen
                self._last_link_error = f"{type(exc).__name__}: {exc}"
                log.warning("MAVLink link error: %s", self._last_link_error)
                self._close_link()
                self._sleep(0.5)

        self._close_link()

    def _open_link(self) -> bool:
        try:
            log.info("opening MAVLink link %s", self.config.mavlink_endpoint)
            self._link = mavutil.mavlink_connection(
                self.config.mavlink_endpoint,
                source_system=self.config.gcs_sysid,
                source_component=self.config.gcs_compid,
                autoreconnect=True,
                robust_parsing=True,
            )
            self._connected = True
            self._last_link_error = None
            log.info("MAVLink link ready")
            return True
        except Exception as exc:
            self._connected = False
            self._last_link_error = f"{type(exc).__name__}: {exc}"
            log.warning("could not open MAVLink link: %s", self._last_link_error)
            self._link = None
            return False

    def _close_link(self) -> None:
        link, self._link = self._link, None
        self._connected = False
        if link is not None:
            try:
                link.close()
            except Exception:
                pass

    def _drain_commands(self) -> None:
        while True:
            try:
                raw = self._commands.get_nowait()
            except queue.Empty:
                return
            self._handle_command_frame(raw)

    def _handle_command_frame(self, raw: bytes) -> None:
        messages = self._parser.parse_buffer(raw) or []
        if not messages:
            self._dropped_frames += 1
            return
        for msg in messages:
            kind = msg.get_type()
            if kind == "MANUAL_CONTROL":
                if self._estop.is_set():
                    self._dropped_frames += 1
                    continue
                msg.target = self._vehicle_sysid
                self._write(msg)
                self._last_manual = time.monotonic()
                self._had_manual = True
                self._accepted_frames += 1
                continue

            if kind == "COMMAND_LONG":
                if int(msg.command) not in ALLOWED_COMMANDS:
                    self._dropped_frames += 1
                    log.warning("dropping unsupported command %s", msg.command)
                    continue
                if (
                    int(msg.command) == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
                    and float(msg.param1) >= 0.5
                    and self._estop.is_set()
                ):
                    self._dropped_frames += 1
                    log.warning("dropping arm command while E-stop is latched")
                    continue
                msg.target_system = self._vehicle_sysid
                msg.target_component = self._vehicle_compid
                self._write(msg)
                self._accepted_frames += 1
                continue

            self._dropped_frames += 1
            log.debug("dropping unexpected frame type %s", kind)

    def _write(self, msg: Any) -> None:
        if self._link is None:
            raise ConnectionError("MAVLink link is not open")
        self._link.write(msg.pack(self._link.mav))

    def _read_messages(self) -> None:
        assert self._link is not None
        for _ in range(200):
            msg = self._link.recv_match(blocking=False)
            if msg is None:
                return
            self._handle_message(msg)

    def _handle_message(self, msg: Any) -> None:
        kind = msg.get_type()
        now = time.time()
        with self._lock:
            if kind == "HEARTBEAT":
                # Ignore heartbeats from other GCSs (our own heartbeat has type
                # GCS, and MAV_TYPE_GCS is 6).
                if int(msg.type) == mavutil.mavlink.MAV_TYPE_GCS:
                    return
                self._last_vehicle_heartbeat = now
                self._vehicle_sysid = int(msg.get_srcSystem())
                self._vehicle_compid = int(msg.get_srcComponent())
                self._telemetry["armed"] = bool(int(msg.base_mode) & ARMED_FLAG)
                self._telemetry["mode_num"] = int(msg.custom_mode)
                self._telemetry["mode"] = ARDUSUB_MODES.get(int(msg.custom_mode), f"MODE_{msg.custom_mode}")
                self._telemetry["system_status"] = int(msg.system_status)
                return

            if kind == "ATTITUDE":
                self._telemetry["attitude"] = {
                    "roll": round(float(msg.roll) * 57.2958, 2),
                    "pitch": round(float(msg.pitch) * 57.2958, 2),
                    "yaw": round(float(msg.yaw) * 57.2958, 2),
                }
                return

            if kind == "VFR_HUD":
                self._telemetry["heading"] = round(float(msg.heading), 1)
                self._telemetry["throttle"] = round(float(msg.throttle), 1)
                return

            if kind == "SYS_STATUS":
                battery = self._telemetry["battery"]
                if int(msg.voltage_battery) not in (0, 65535):
                    battery["voltage"] = round(int(msg.voltage_battery) / 1000.0, 2)
                if int(msg.current_battery) != -1:
                    battery["current"] = round(int(msg.current_battery) / 100.0, 2)
                if int(msg.battery_remaining) != -1:
                    battery["remaining"] = int(msg.battery_remaining)
                return

            if kind == "BATTERY_STATUS":
                battery = self._telemetry["battery"]
                voltages = [v for v in msg.voltages if v not in (0, 65535)]
                if voltages:
                    battery["voltage"] = round(sum(voltages) / 1000.0, 2)
                if int(msg.current_battery) != -1:
                    battery["current"] = round(int(msg.current_battery) / 100.0, 2)
                if int(msg.battery_remaining) != -1:
                    battery["remaining"] = int(msg.battery_remaining)
                return

            if kind == "SCALED_PRESSURE":
                # 1 hPa of water column is ~1.0197 cm at 4 C / 1.0 g/cm^3.
                depth = (self.config.surface_pressure_hpa - float(msg.press_abs)) * 0.010197
                self._telemetry["depth_m"] = round(max(0.0, depth), 2)
                return

            if kind == "AHRS2" and self._telemetry["depth_m"] is None:
                altitude = float(msg.altitude)
                if altitude < 0:
                    self._telemetry["depth_m"] = round(-altitude, 2)
                return

            if kind == "RADIO_STATUS":
                self._telemetry["rssi"] = int(msg.rssi)
                return

    def _send_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat_tx < 1.0:
            return
        self._last_heartbeat_tx = now
        if self._link is None:
            return
        self._write(
            mavutil.mavlink.MAVLink_heartbeat_message(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,
                0,
                0,
                3,
            )
        )

    def _run_watchdog(self) -> None:
        if self._link is None:
            return
        now = time.monotonic()
        if self._estop.is_set():
            # Latch is local and does not depend on the server staying alive.
            if self.config.estop_disarm and now - self._estop_sent_at > 2.0:
                self._estop_sent_at = now
                self._write(
                    mavutil.mavlink.MAVLink_command_long_message(
                        target_system=self._vehicle_sysid,
                        target_component=self._vehicle_compid,
                        command=mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                        confirmation=0,
                        param1=0.0,
                        param2=0.0,
                        param3=0.0,
                        param4=0.0,
                        param5=0.0,
                        param6=0.0,
                        param7=0.0,
                    )
                )
            interval = 1.0 / max(1.0, self.config.estop_hz)
            if now - self._last_neutral_tx >= interval:
                self._send_neutral()
                self._last_neutral_tx = now
            return

        if not self._had_manual:
            return

        age_ms = (now - self._last_manual) * 1000.0
        if age_ms > self.config.command_ttl_ms:
            interval = 1.0 / max(1.0, self.config.neutral_hz)
            if now - self._last_neutral_tx >= interval:
                self._send_neutral()
                self._last_neutral_tx = now

    def _send_neutral(self) -> None:
        if self._link is None:
            return
        self._write(
            mavutil.mavlink.MAVLink_manual_control_message(
                target=self._vehicle_sysid,
                x=NEUTRAL["x"],
                y=NEUTRAL["y"],
                z=NEUTRAL["z"],
                r=NEUTRAL["r"],
                buttons=NEUTRAL["buttons"],
            )
        )

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)
