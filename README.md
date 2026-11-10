# ROVOps

Remote teleoperation platform for tethered ROVs (ArduSub + BlueOS + Pixhawk/Raspberry Pi).

## What this is

- **Control server** (`server/`): FastAPI app that brokers telemetry and validated pilot commands between vehicles and pilots. Vehicles connect outbound (`/ws/vehicle`), pilots connect over WebSocket to a session (`/ws/pilot/{session_id}`).
- **Vehicle agent** (`agent/`): Runs on the vehicle (BlueOS Extension). Opens an outbound connection to the server, bridges MAVLink to/from the flight controller, and enforces a local command watchdog/E-stop as a backstop.
- **Protocol**: JSON control plane + binary MAVLink passthrough gated by a single-controller lease. Server enforces rate limits, command TTL, neutral bursts on timeout/disconnect/lease loss, and a latched E-stop.

## Quick start (development)

```powershell
# Server (from repo root)
cd D:\1\rovops
$env:JWT_SECRET = "change-me-at-least-32-characters-long"
$env:VEHICLES_FILE = "server/config/vehicles.json"
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir server --reload --port 8080
```

```powershell
# Agent (on vehicle / SITL)
cd D:\1\rovops\agent
$env:SERVER_URL = "http://your-server:8080"
$env:VEHICLE_TOKEN = "dev-device-token-rov-01"
$env:MAVLINK_ENDPOINT = "udpin:0.0.0.0:14599"
..\.venv\Scripts\python.exe -m rovops_agent.main
```

Login: `demo/demo` or `admin/admin` (change before exposure). Provision device tokens in `server/config/vehicles.json` or via environment (`token_env`).

## Safety

- Server is a **broker**, not in the inner control loop; the vehicle agent and ArduSub failsafes remain authoritative.
- Only the lease holder can send motion/arm/mode commands; E-stop is latched server-side and re-asserted locally by the agent.
- On disconnect/timeout/lease loss the server sends neutral bursts; the agent forces neutral if no command arrives within `COMMAND_TTL_MS`.
- Audit log in `data/audit.jsonl` records auth, sessions, lease, arm/mode, E-stop, and command rejections.

## Project layout

```
rovops/
  server/        # FastAPI control server
    app/         # routes, hubs, auth, mavlink, safety
    config/      # vehicle registry
    app/static/  # pilot UI
  agent/         # BlueOS Extension vehicle agent
    rovops_agent/ # config, mavlink bridge, websocket client
  data/          # audit logs
  docs/          # design notes
  scripts/       # helpers (fake_autopilot.py, e2e_smoke.py)
```

## End-to-end smoke test

```powershell
.venv\Scripts\python.exe scripts\e2e_smoke.py
```

Starts a real server, fake autopilot and agent, then drives a pilot session over the WebSocket API.

## License / IP notes

If you expose Cockpit as a network service or fork it, **AGPL** implications may apply. This broker is designed to integrate with existing GCS flows without bundling a heavy GPL UI by default.
