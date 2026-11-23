"""End-to-end smoke test for the ROVOps control plane.

Starts three real processes:

* ``uvicorn`` running the control server,
* ``scripts/fake_autopilot.py`` acting as the ArduSub autopilot,
* ``rovops_agent`` connecting outbound to the server and bridging MAVLink.

Then it drives a pilot session over the real WebSocket API and asserts the
safety behaviour that matters: lease enforcement, arm/mode forwarding, E-stop
neutral + disarm, E-stop reset, and neutral output when the pilot disappears.

This is not a substitute for SITL/hardware testing, but it catches wiring,
protocol and lifecycle bugs that unit tests cannot.

    python scripts/e2e_smoke.py

Exit code 0 means every step passed.  On failure the child process logs are
printed so the cause is visible in CI output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
AGENT_DIR = ROOT / "agent"

DEVICE_TOKEN = "e2e-device-token"
VEHICLE_ID = "rov-01"


class SmokeError(AssertionError):
    pass


def say(message: str) -> None:
    print(f"[e2e] {message}", flush=True)


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


class Child:
    def __init__(self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str], log: Path) -> None:
        self.name = name
        self.log_path = log
        self._handle = log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=self._handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        self._handle.close()

    def tail(self, lines: int = 40) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "<no log>"
        return "\n".join(content[-lines:])


def child_env(base: dict[str, str], **overrides: Any) -> dict[str, str]:
    env = dict(base)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------


async def wait_for(
    factory: Callable[[], Any],
    *,
    what: str,
    timeout: float = 20.0,
    interval: float = 0.1,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            result = factory()
            if asyncio.iscoroutine(result):
                result = await result
            last = result
            if result:
                return result
        except Exception as exc:  # keep polling; surface the last error on timeout
            last = exc
        await asyncio.sleep(interval)
    raise SmokeError(f"timed out waiting for {what}; last={last!r}")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


async def wait_for_log(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    start: int = 0,
    what: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        for record in read_jsonl(path)[start:]:
            if predicate(record):
                return record
        return None

    found = await wait_for(check, what=what, timeout=timeout)
    return found


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------


async def recv_type(
    ws: Any,
    kind: str,
    *,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeError(f"timed out waiting for {kind!r}; saw types={seen[-20:]}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        seen.append(str(message.get("type")))
        if message.get("type") == kind and (predicate is None or predicate(message)):
            return message


async def send(ws: Any, **payload: Any) -> None:
    payload.setdefault("type", "")
    await ws.send(json.dumps(payload))


# ---------------------------------------------------------------------------
# The actual scenario
# ---------------------------------------------------------------------------


async def run_smoke(
    *,
    server: Child,
    agent: Child,
    autopilot_log: Path,
    audit_file: Path,
    http_url: str,
    keep: bool,
) -> None:
    async with httpx.AsyncClient(base_url=http_url, timeout=10.0) as client:
        # 1. Server is alive.
        async def server_health() -> dict[str, Any]:
            response = await client.get("/healthz")
            return response.json()

        health = await wait_for(server_health, what="server /healthz")
        if not health.get("ok"):
            raise SmokeError(f"unhealthy server: {health}")
        say(f"server healthy: {health}")

        # 2. Pilot login and session creation.
        response = await client.post("/api/auth/login", json={"username": "demo", "password": "demo"})
        response.raise_for_status()
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Wait for the agent to connect and MAVLink telemetry to flow.
        async def vehicle_online() -> dict[str, Any] | None:
            data = await client.get("/api/vehicles", headers=headers)
            for vehicle in data.json().get("vehicles", []):
                telemetry = vehicle.get("telemetry") or {}
                if vehicle.get("online") and telemetry.get("mavlink_online"):
                    return vehicle
            return None

        vehicle = await wait_for(vehicle_online, what="vehicle online with MAVLink telemetry", timeout=25)
        say(
            "vehicle online: "
            f"mode={vehicle['telemetry'].get('mode')} "
            f"depth={vehicle['telemetry'].get('depth_m')} "
            f"battery={vehicle['telemetry'].get('battery')}"
        )
        if vehicle["telemetry"].get("mode") != "STABILIZE":
            raise SmokeError(f"unexpected initial mode: {vehicle['telemetry'].get('mode')}")

        response = await client.post("/api/sessions", json={"vehicle_id": VEHICLE_ID}, headers=headers)
        response.raise_for_status()
        session = response.json()
        pilot_url = f"{http_url.replace('http', 'ws', 1)}{session['ws_url']}?token={session['token']}"
        say(f"session {session['session_id']} created")

        # proxy=None: never route the local pilot link through an ambient
        # HTTP(S)_PROXY (websockets >= 14 discovers them by default).
        async with websockets.connect(pilot_url, max_size=None, proxy=None) as pilot:
            initial = await recv_type(pilot, "session_state")
            if initial["session"]["session_id"] != session["session_id"]:
                raise SmokeError("pilot joined the wrong session")
            if not initial.get("vehicle", {}).get("online"):
                await recv_type(pilot, "vehicle", predicate=lambda m: bool(m.get("online")))
            await recv_type(pilot, "pilot_joined")

            # 4. Commands are rejected without the lease.
            await send(pilot, type="manual", x=100, y=0, z=500, r=0)
            rejected = await recv_type(pilot, "error")
            if rejected.get("code") != "no_lease":
                raise SmokeError(f"expected no_lease, got {rejected}")
            say("unleased command rejected")

            # 5. Take the lease.
            await send(pilot, type="lease_acquire")
            lease = await recv_type(pilot, "lease", predicate=lambda m: bool(m.get("granted")))
            if lease["holder"] != lease["you"]:
                raise SmokeError(f"lease holder mismatch: {lease}")
            await recv_type(pilot, "state")
            say(f"lease granted to {lease['holder']}")

            # 6. Manual control reaches the fake autopilot.
            mark = len(read_jsonl(autopilot_log))
            await send(pilot, type="manual", x=500, y=-250, z=650, r=100)
            manual = await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "manual" and record.get("x") == 500,
                start=mark,
                what="forwarded MANUAL_CONTROL",
            )
            if (manual["y"], manual["z"], manual["r"]) != (-250, 650, 100):
                raise SmokeError(f"manual payload changed in transit: {manual}")
            say("manual control forwarded intact")

            # 7. Arm and mode change are forwarded and reflected in telemetry.
            mark = len(read_jsonl(autopilot_log))
            await send(pilot, type="arm", arm=True)
            await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "armed",
                start=mark,
                what="arm command",
            )
            armed_telemetry = await recv_type(
                pilot, "telemetry", predicate=lambda m: bool((m.get("data") or {}).get("armed"))
            )
            if not armed_telemetry["data"]["armed"]:
                raise SmokeError("telemetry did not report armed")
            say("arm forwarded and confirmed by telemetry")

            mark = len(read_jsonl(autopilot_log))
            await send(pilot, type="mode", mode="ALT_HOLD")
            mode_record = await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "mode",
                start=mark,
                what="mode change",
            )
            if int(mode_record.get("mode", -1)) != 2:
                raise SmokeError(f"ALT_HOLD mapped to wrong custom mode: {mode_record}")
            await recv_type(
                pilot, "telemetry", predicate=lambda m: (m.get("data") or {}).get("mode") == "ALT_HOLD"
            )
            say("mode change forwarded and confirmed by telemetry")

            # 8. E-stop latches, drives neutral and disarms locally.
            mark = len(read_jsonl(autopilot_log))
            await send(pilot, type="estop")
            await recv_type(pilot, "estop", predicate=lambda m: m.get("active") is True)
            await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "neutral",
                start=mark,
                what="neutral burst after E-stop",
            )
            await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "disarmed",
                start=mark,
                what="local disarm after E-stop",
                timeout=10,
            )
            await wait_for(
                lambda: _estop_active(client, headers, session["session_id"]),
                what="server E-stop latch",
            )
            say("E-stop latched, neutral + disarm observed")

            await send(pilot, type="manual", x=100, y=0, z=500, r=0)
            rejected = await recv_type(pilot, "error")
            if rejected.get("code") != "estop_active":
                raise SmokeError(f"expected estop_active, got {rejected}")
            say("motion blocked while E-stop is latched")

            # 9. Reset and send a fresh manual command.
            mark = len(read_jsonl(autopilot_log))
            await send(pilot, type="estop_reset")
            await recv_type(pilot, "estop", predicate=lambda m: m.get("active") is False)
            await send(pilot, type="manual", x=-300, y=0, z=500, r=0)
            reset_manual = await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "manual" and record.get("x") == -300,
                start=mark,
                what="manual control after E-stop reset",
            )
            say(f"E-stop reset; motion restored ({reset_manual['x']})")

            # 10. Dropping the pilot socket forces neutral output.
            mark = len(read_jsonl(autopilot_log))
            await pilot.close()
            await wait_for_log(
                autopilot_log,
                lambda record: record.get("kind") == "neutral",
                start=mark,
                what="neutral burst after pilot disconnect",
                timeout=10,
            )
            async def lease_released() -> bool:
                holder = await _lease_holder(client, headers, session["session_id"])
                return holder is None

            await wait_for(lease_released, what="lease release on pilot disconnect")
            say("pilot disconnect released the lease and forced neutral")

    # 11. The audit trail records the safety-relevant events.
    events = [record.get("event") for record in read_jsonl(audit_file)]
    required = {
        "login",
        "session_create",
        "vehicle_online",
        "vehicle_hello",
        "pilot_join",
        "lease_acquire",
        "arm",
        "set_mode",
        "estop",
        "estop_reset",
        "lease_release",
    }
    missing = required.difference(events)
    if missing:
        raise SmokeError(f"audit log missing events: {sorted(missing)}")
    say(f"audit log contains all {len(required)} required events")

    # 12. Agent shutdown marks the vehicle offline.
    agent.stop()
    async with httpx.AsyncClient(base_url=http_url, timeout=10.0) as client:
        response = await client.post("/api/auth/login", json={"username": "demo", "password": "demo"})
        headers = {"Authorization": f"Bearer {response.json()['token']}"}

        async def vehicle_offline() -> bool:
            state = await client.get("/api/vehicles", headers=headers)
            return not any(vehicle["online"] for vehicle in state.json()["vehicles"])

        await wait_for(vehicle_offline, what="vehicle offline after agent shutdown", timeout=15)
    say("agent shutdown marked the vehicle offline")


async def _estop_active(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> bool:
    response = await client.get(f"/api/sessions/{session_id}", headers=headers)
    return bool(response.json().get("estop"))


async def _lease_holder(client: httpx.AsyncClient, headers: dict[str, str], session_id: str) -> str | None:
    response = await client.get(f"/api/sessions/{session_id}", headers=headers)
    return response.json().get("lease_holder")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="rovops-e2e-"))
    if args.keep:
        say(f"keeping artifacts in {workdir}")
    vehicles_file = workdir / "vehicles.json"
    vehicles_file.write_text(
        json.dumps(
            {
                "vehicles": [
                    {
                        "id": VEHICLE_ID,
                        "name": "E2E ROV",
                        "model": "BlueROV2",
                        "autopilot": "ArduSub",
                        "site": "smoke-tank",
                        "sysid": 1,
                        "token": DEVICE_TOKEN,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    audit_file = workdir / "audit.jsonl"
    autopilot_log = workdir / "autopilot.jsonl"

    http_port = free_port()
    mavlink_port = free_port(socket.SOCK_DGRAM)
    health_port = free_port()
    http_url = f"http://127.0.0.1:{http_port}"

    python = sys.executable
    base_env = dict(os.environ)
    base_env.pop("VEHICLE_TOKEN", None)

    server = Child(
        "server",
        [
            python,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(SERVER_DIR),
            "--host",
            "127.0.0.1",
            "--port",
            str(http_port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=child_env(
            base_env,
            JWT_SECRET="e2e-secret-that-is-long-enough-for-hs256",
            DEMO_USER="demo",
            DEMO_PASSWORD="demo",
            ADMIN_USER="admin",
            ADMIN_PASSWORD="admin",
            VEHICLES_FILE=str(vehicles_file),
            AUDIT_FILE=str(audit_file),
            COMMAND_TTL_MS=300,
            NEUTRAL_HZ=20,
            ESTOP_REPEAT_S=0.5,
        ),
        log=workdir / "server.log",
    )
    autopilot = Child(
        "autopilot",
        [
            python,
            str(ROOT / "scripts" / "fake_autopilot.py"),
            "--port",
            str(mavlink_port),
            "--duration",
            "180",
            "--log",
            str(autopilot_log),
        ],
        cwd=ROOT,
        env=base_env,
        log=workdir / "autopilot.log",
    )
    agent = Child(
        "agent",
        [python, "-m", "rovops_agent.main"],
        cwd=AGENT_DIR,
        env=child_env(
            base_env,
            SERVER_URL=http_url,
            VEHICLE_TOKEN=DEVICE_TOKEN,
            MAVLINK_ENDPOINT=f"udpin:0.0.0.0:{mavlink_port}",
            AGENT_HEALTH_PORT=health_port,
            TELEMETRY_HZ=4,
            COMMAND_TTL_MS=300,
            NEUTRAL_HZ=10,
            ESTOP_HZ=20,
            RECONNECT_MIN_S=0.5,
            RECONNECT_MAX_S=2,
            PYTHONPATH=str(AGENT_DIR),
        ),
        log=workdir / "agent.log",
    )

    try:
        await run_smoke(
            server=server,
            agent=agent,
            autopilot_log=autopilot_log,
            audit_file=audit_file,
            http_url=http_url,
            keep=args.keep,
        )
    except Exception:
        print("\n--- smoke test failed ---", file=sys.stderr)
        for child in (server, agent, autopilot):
            print(f"\n[{child.name}] log tail:\n{child.tail()}", file=sys.stderr)
        raise
    finally:
        for child in (agent, autopilot, server):
            child.stop()

    say("PASS")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep temporary logs")
    parsed = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(parsed)))
    except KeyboardInterrupt:
        raise SystemExit(130)
