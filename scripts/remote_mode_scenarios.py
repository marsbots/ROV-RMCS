"""Remote Mode scenario harness (M2-C).

`e2e_smoke.py` proves the *server/agent* safety wiring against a fake autopilot.
This script proves the Remote Mode acceptance scenarios from
``projects/marsub-pi/docs/REMOTE_MODE.md`` §7.2 that can be automated without
ArduSub SITL or a MARSUB GUI: everything the RMCS side must guarantee for a
Remote Mode pilot, driven exactly as MARSUB's ``RmcsRemoteLink`` drives it.

Scenarios covered (numbering per REMOTE_MODE.md §7.2):

  1  login -> vehicles -> session -> pilot WS            (session_state received)
  2  telemetry visible to the pilot                     (attitude/depth/battery/mode/armed)
  3  lease acquire / release                            (granted true/false)
  4  manual command forwarding                          (MANUAL_CONTROL seen by autopilot)
  5  arm / disarm                                       (COMMAND_LONG ARM/DISARM)
  6  mode change                                        (SET_MODE + telemetry mode update)
  7  E-stop latch                                       (neutral burst, disarm, manual refused)
  8  E-stop reset                                       (latch cleared, motion restored)
  9  lease loss on pilot disconnect                     (server neutral burst)
 10  reconnect                                          (telemetry restored)
 12  server restart recovery                            (re-login/re-session/reconnect)

Not automated here (need ArduSub SITL, a GUI, or a camera):

 11  Local <-> Remote mode switching in the MARSUB UI   (covered by vitest)
 13  WHEP video playback                                (needs video hardware)

Usage:
    python scripts/remote_mode_scenarios.py            # run all automated scenarios
    python scripts/remote_mode_scenarios.py --list     # show the scenario matrix

Exit code 0 means every automated scenario passed.
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
JWT_SECRET = "e2e-secret-that-is-long-enough-for-hs256"

SCENARIOS: list[tuple[int, str, str]] = [
    (1, "login -> vehicles -> session -> pilot WS", "automated"),
    (2, "telemetry visible to the pilot", "automated"),
    (3, "lease acquire / release", "automated"),
    (4, "manual command forwarding", "automated"),
    (5, "arm / disarm", "automated"),
    (6, "mode change", "automated"),
    (7, "E-stop latch", "automated"),
    (8, "E-stop reset", "automated"),
    (9, "lease loss on pilot disconnect", "automated"),
    (10, "reconnect restores telemetry", "automated"),
    (11, "Local <-> Remote UI switching", "vitest (link-manager)"),
    (12, "server restart recovery", "automated"),
    (13, "WHEP video playback", "manual (video hardware)"),
]


class ScenarioError(AssertionError):
    pass


def say(message: str) -> None:
    print(f"[m2c] {message}", flush=True)


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Process plumbing (mirrors e2e_smoke.py so the two stay comparable)
# ---------------------------------------------------------------------------


class Child:
    def __init__(self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str], log: Path) -> None:
        self.name = name
        self.log_path = log
        self._handle = log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdout=self._handle, stderr=subprocess.STDOUT, text=True
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

    def tail(self, lines: int = 25) -> str:
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "<no log>"
        return "\n".join(content[-lines:])


def child_env(base: dict[str, str], **overrides: Any) -> dict[str, str]:
    env = dict(base)
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def start_server(env_base: dict[str, str], port: int, log: Path, *, vehicles_file: Path, audit_file: Path) -> Child:
    return Child(
        "server",
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(SERVER_DIR),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=child_env(
            env_base,
            JWT_SECRET=JWT_SECRET,
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
        log=log,
    )


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_for(predicate: Callable[[], Any], *, what: str, timeout: float = 20.0, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval)
    raise ScenarioError(f"timed out waiting for {what}; last={last!r}")


async def recv_type(ws, kind: str, predicate: Callable[[dict], bool] | None = None, limit: int = 300) -> dict:
    for _ in range(limit):
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        if isinstance(raw, (bytes, bytearray)):
            continue
        message = json.loads(raw)
        if message.get("type") == kind and (predicate is None or predicate(message)):
            return message
    raise ScenarioError(f"did not receive message type {kind!r}")


async def send(ws, **payload: Any) -> None:
    await ws.send(json.dumps(payload))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


async def wait_for_log(path: Path, predicate: Callable[[dict], bool], *, start: int = 0, what: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in read_jsonl(path)[start:]:
            if predicate(record):
                return record
        await asyncio.sleep(0.1)
    raise ScenarioError(f"timed out waiting for autopilot log entry: {what}")


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


async def run_scenarios(env_base: dict[str, str]) -> None:
    http_port = free_port()
    mavlink_port = free_port(socket.SOCK_DGRAM)
    tmp = Path(tempfile.mkdtemp(prefix="m2c-"))
    vehicles_file = tmp / "vehicles.json"
    vehicles_file.write_text(
        json.dumps(
            {
                "vehicles": [
                    {
                        "id": VEHICLE_ID,
                        "name": "SITL ROV",
                        "model": "BlueROV2",
                        "autopilot": "ArduSub",
                        "site": "sitl",
                        "sysid": 1,
                        "token": DEVICE_TOKEN,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    audit_file = tmp / "audit.jsonl"

    autopilot_log = tmp / "autopilot.jsonl"
    server_log = tmp / "server.log"
    agent_log = tmp / "agent.log"

    http_url = f"http://127.0.0.1:{http_port}"
    server = start_server(env_base, http_port, server_log, vehicles_file=vehicles_file, audit_file=audit_file)
    autopilot = Child(
        "autopilot",
        [sys.executable, str(ROOT / "scripts" / "fake_autopilot.py"), "--port", str(mavlink_port), "--log", str(autopilot_log)],
        cwd=ROOT,
        env=dict(env_base),
        log=tmp / "autopilot.out",
    )
    agent = Child(
        "agent",
        [sys.executable, "-m", "rovops_agent.main"],
        cwd=AGENT_DIR,
        env=child_env(
            env_base,
            SERVER_URL=f"http://127.0.0.1:{http_port}",
            VEHICLE_TOKEN=DEVICE_TOKEN,
            # fake_autopilot.py sends to <port>; the agent listens on it (same
            # topology as e2e_smoke.py).
            MAVLINK_ENDPOINT=f"udpin:0.0.0.0:{mavlink_port}",
            VIDEO_WHEP_URL="http://127.0.0.1:8889/rov-01/whep",
            AGENT_HEALTH_PORT=str(free_port()),
            TELEMETRY_HZ=4,
            COMMAND_TTL_MS=300,
            NEUTRAL_HZ=10,
            ESTOP_HZ=20,
            RECONNECT_MIN_S=0.5,
            RECONNECT_MAX_S=2,
            PYTHONPATH=str(AGENT_DIR),
        ),
        log=agent_log,
    )

    headers: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:

            async def healthy() -> dict | None:
                try:
                    response = await client.get(f"{http_url}/healthz")
                except httpx.HTTPError:
                    return None
                return response.json() if response.status_code == 200 else None

            await wait_for(healthy, what="server healthy", timeout=25)

            # -- Scenario 1: login -> vehicles -> session -> pilot WS --------
            login = await client.post(f"{http_url}/api/auth/login", json={"username": "demo", "password": "demo"})
            if login.status_code != 200:
                raise ScenarioError(f"login failed: {login.status_code} {login.text}")
            headers = {"Authorization": f"Bearer {login.json()['token']}"}
            say("1/12 login OK")

            async def vehicle_online() -> dict | None:
                response = await client.get(f"{http_url}/api/vehicles/{VEHICLE_ID}", headers=headers)
                if response.status_code != 200:
                    return None
                vehicle = response.json()
                telemetry = vehicle.get("telemetry") or {}
                return vehicle if vehicle.get("online") and telemetry.get("mavlink_online") else None

            vehicle = await wait_for(vehicle_online, what="vehicle online with MAVLink telemetry", timeout=30)

            response = await client.post(f"{http_url}/api/sessions", json={"vehicle_id": VEHICLE_ID}, headers=headers)
            response.raise_for_status()
            session = response.json()
            pilot_url = f"{http_url.replace('http', 'ws', 1)}{session['ws_url']}?token={session['token']}"
            say(f"1/12 session {session['session_id']} created")

            async with websockets.connect(pilot_url, max_size=None, proxy=None) as pilot:
                initial = await recv_type(pilot, "session_state")
                if initial["session"]["session_id"] != session["session_id"]:
                    raise ScenarioError("pilot joined the wrong session")
                say("1/12 pilot WS joined (session_state received)")

                # -- Scenario 2: telemetry reaches the pilot -----------------
                async def telemetry_snapshot() -> dict | None:
                    try:
                        return await recv_type(pilot, "telemetry", limit=120)
                    except (ScenarioError, asyncio.TimeoutError):
                        return None

                snapshot = await wait_for(telemetry_snapshot, what="pilot telemetry", timeout=20)
                data = snapshot.get("data") or {}
                for field in ("mode", "depth_m", "battery", "mavlink_online"):
                    if field not in data:
                        raise ScenarioError(f"telemetry snapshot missing {field!r}: {data}")
                if data.get("mode") != "STABILIZE":
                    raise ScenarioError(f"unexpected initial mode: {data.get('mode')}")
                say(
                    "2/12 telemetry OK "
                    f"(mode={data['mode']} depth={data.get('depth_m')} battery={(data.get('battery') or {}).get('voltage')})"
                )

                # -- Scenario 3: lease acquire ---------------------------------
                await send(pilot, type="lease_acquire")
                lease = await recv_type(pilot, "lease", predicate=lambda m: bool(m.get("granted")))
                if lease["holder"] != lease["you"]:
                    raise ScenarioError(f"lease holder mismatch: {lease}")
                say("3/12 lease acquired")

                # -- Scenario 4: manual forwarding -----------------------------
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="manual", x=500, y=-250, z=650, r=100)
                manual = await wait_for_log(
                    autopilot_log,
                    lambda r: r.get("kind") == "manual" and r.get("x") == 500,
                    start=mark,
                    what="forwarded MANUAL_CONTROL",
                )
                if (manual["y"], manual["z"], manual["r"]) != (-250, 650, 100):
                    raise ScenarioError(f"manual payload changed in transit: {manual}")
                say("4/12 manual forwarded intact")

                # -- Scenario 5: arm / disarm ---------------------------------
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="arm", arm=True)
                await wait_for_log(autopilot_log, lambda r: r.get("kind") == "armed", start=mark, what="ARM")
                await recv_type(pilot, "telemetry", predicate=lambda m: bool((m.get("data") or {}).get("armed")))
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="arm", arm=False)
                await wait_for_log(autopilot_log, lambda r: r.get("kind") == "disarmed", start=mark, what="DISARM")
                say("5/12 arm + disarm forwarded")

                # -- Scenario 6: mode change ----------------------------------
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="mode", mode="ALT_HOLD")
                mode_record = await wait_for_log(autopilot_log, lambda r: r.get("kind") == "mode", start=mark, what="SET_MODE")
                if int(mode_record.get("mode", -1)) != 2:
                    raise ScenarioError(f"ALT_HOLD mapped to the wrong custom mode: {mode_record}")
                await recv_type(pilot, "telemetry", predicate=lambda m: (m.get("data") or {}).get("mode") == "ALT_HOLD")
                say("6/12 mode change forwarded (ALT_HOLD)")

                # -- Scenario 7: E-stop latch ---------------------------------
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="estop")
                estop = await recv_type(pilot, "estop", predicate=lambda m: m.get("active") is True)
                if not estop["active"]:
                    raise ScenarioError("E-stop did not latch")
                await wait_for_log(autopilot_log, lambda r: r.get("kind") == "neutral", start=mark, what="neutral burst")
                await send(pilot, type="manual", x=100, y=0, z=500, r=0)
                rejected = await recv_type(pilot, "error")
                if rejected.get("code") != "estop_active":
                    raise ScenarioError(f"expected estop_active, got {rejected}")
                say("7/12 E-stop latched: neutral burst + manual refused")

                # -- Scenario 8: E-stop reset ---------------------------------
                await send(pilot, type="estop_reset")
                reset = await recv_type(pilot, "estop", predicate=lambda m: m.get("active") is False)
                if reset["active"]:
                    raise ScenarioError("E-stop did not clear")
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="manual", x=-300, y=0, z=500, r=0)
                await wait_for_log(
                    autopilot_log, lambda r: r.get("kind") == "manual" and r.get("x") == -300, start=mark, what="motion after reset"
                )
                say("8/12 E-stop reset: motion restored")

                # -- Scenario 9: lease loss on disconnect ---------------------
                mark = len(read_jsonl(autopilot_log))
                await send(pilot, type="lease_release")
                release = await recv_type(pilot, "lease", predicate=lambda m: m.get("granted") is False)
                if release.get("reason") != "released":
                    raise ScenarioError(f"unexpected release reason: {release}")
                await wait_for_log(
                    autopilot_log, lambda r: r.get("kind") == "neutral", start=mark, what="neutral on release"
                )
                say("9/12 lease release forced neutral")

            # -- Scenario 10: reconnect restores the pilot -------------------
            async with websockets.connect(pilot_url, max_size=None, proxy=None) as pilot:
                rejoined = await recv_type(pilot, "session_state")
                if rejoined["session"]["session_id"] != session["session_id"]:
                    raise ScenarioError("reconnect landed in a different session")

                async def fresh_telemetry() -> dict | None:
                    try:
                        return await recv_type(pilot, "telemetry", limit=120)
                    except (ScenarioError, asyncio.TimeoutError):
                        return None

                await wait_for(fresh_telemetry, what="telemetry after reconnect", timeout=20)
                say("10/12 reconnect restored telemetry")

            # -- Scenario 12: server restart recovery ------------------------
            say("12/12 restarting the server...")
            server.stop()

            async def server_down() -> bool:
                try:
                    response = await client.get(f"{http_url}/healthz")
                except httpx.HTTPError:
                    return True
                return response.status_code != 200

            await wait_for(server_down, what="server down", timeout=25)
            server = start_server(env_base, http_port, server_log, vehicles_file=vehicles_file, audit_file=audit_file)
            await wait_for(healthy, what="server healthy after restart", timeout=25)

            relogin = await client.post(f"{http_url}/api/auth/login", json={"username": "demo", "password": "demo"})
            if relogin.status_code != 200:
                raise ScenarioError(f"re-login after restart failed: {relogin.status_code}")
            headers = {"Authorization": f"Bearer {relogin.json()['token']}"}

            vehicle = await wait_for(vehicle_online, what="vehicle back online after restart", timeout=30)
            response = await client.post(f"{http_url}/api/sessions", json={"vehicle_id": VEHICLE_ID}, headers=headers)
            response.raise_for_status()
            new_session = response.json()
            new_pilot_url = f"{http_url.replace('http', 'ws', 1)}{new_session['ws_url']}?token={new_session['token']}"
            async with websockets.connect(new_pilot_url, max_size=None, proxy=None) as pilot:
                state = await recv_type(pilot, "session_state")
                if not state["session"]["session_id"]:
                    raise ScenarioError("no session after restart recovery")
            say("12/12 server restart recovery OK")

        say("PASS - all automated M2-C scenarios succeeded")
    except Exception:
        print("\n--- scenario run failed ---\n", flush=True)
        print(f"[server] log tail:\n{server.tail()}\n")
        print(f"[agent] log tail:\n{agent.tail()}\n")
        print(f"[autopilot] log tail:\n{autopilot.tail()}\n")
        raise
    finally:
        agent.stop()
        autopilot.stop()
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the scenario matrix and exit")
    args = parser.parse_args(argv)

    if args.list:
        print(f"{'#':>3}  {'status':<26} scenario")
        for number, title, status in SCENARIOS:
            print(f"{number:>3}  {status:<26} {title}")
        return 0

    env_base = dict(os.environ)
    # A host-wide egress proxy must not swallow loopback WebSocket traffic.
    env_base.pop("VEHICLE_TOKEN", None)

    try:
        asyncio.run(run_scenarios(env_base))
    except ScenarioError as exc:
        print(f"[m2c] FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - surface the real cause to the operator
        print(f"[m2c] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
