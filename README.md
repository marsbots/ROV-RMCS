# ROVOps — Remote Monitoring & Control System

> 한국어 문서: [README.ko.md](README.ko.md)

Remote teleoperation platform for tethered ROVs (ArduSub + BlueOS + Pixhawk/Raspberry Pi).

## What this is

- **Control server** (`server/`): FastAPI app that brokers telemetry and validated pilot commands between vehicles and pilots. Vehicles connect outbound (`/ws/vehicle`), pilots connect over WebSocket to a session (`/ws/pilot/{session_id}`).
- **Vehicle agent** (`agent/`): Runs on the vehicle (BlueOS Extension). Opens an outbound connection to the server, bridges MAVLink to/from the flight controller, and enforces a local command watchdog/E-stop as a backstop.
- **Protocol**: JSON control plane + binary MAVLink passthrough gated by a single-controller lease. Server enforces rate limits, command TTL, neutral bursts on timeout/disconnect/lease loss, and a latched E-stop.

## Development environment (HQ macOS)

This project is developed on the HQ server (macOS, Apple Silicon).

### Prerequisites

- Python 3.11+ (tested with 3.14)
- A local virtualenv (see below)

### Setup

```bash
# Create venv on local disk (NOT on NAS/SMB — see note below)
python3 -m venv ~/.venvs/rmcs
~/.venvs/rmcs/bin/pip install -r server/requirements.txt
~/.venvs/rmcs/bin/pip install -r server/requirements-dev.txt  # if present
```

### Run the server

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
export JWT_SECRET="change-me-at-least-32-characters-long"
export VEHICLES_FILE="server/config/vehicles.json"
~/.venvs/rmcs/bin/python -m uvicorn app.main:app --app-dir server --reload --port 8080
```

### Run the agent (on vehicle / SITL)

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)/agent"
export SERVER_URL="http://localhost:8080"
export VEHICLE_TOKEN="dev-device-token-rov-01"
export MAVLINK_ENDPOINT="udpin:0.0.0.0:14599"
~/.venvs/rmcs/bin/python -m rovops_agent.main
```

### Tests

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
~/.venvs/rmcs/bin/python -m pytest server/tests/ agent/tests/ -q
```

### End-to-end smoke test

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
~/.venvs/rmcs/bin/python scripts/e2e_smoke.py
```

## Storage note (important)

- The project lives on an **SMB mount** (`/Volumes/DataHub`, `smbfs`). SMB on macOS is **not** `noexec` — binaries can run from it.
- However, the `.venv` committed in this repo's history was created on **Windows** (`C:\Python313`, `D:\1\rovops\.venv`) and cannot run on macOS. **Do not use it.** Create a fresh venv on local disk (`~/.venvs/rmcs`) as shown above.
- The WebDAV mount (`/Volumes/file.heo.best`) **is** `noexec` — do not place executables there.

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
  data/          # audit logs (runtime-generated)
  docs/          # design notes
  scripts/       # helpers (fake_autopilot.py, e2e_smoke.py)
```

## Login

`demo/demo` or `admin/admin` (change before exposure). Provision device tokens in `server/config/vehicles.json` or via environment (`token_env`).

## License / IP notes

If you expose Cockpit as a network service or fork it, **AGPL** implications may apply. This broker is designed to integrate with existing GCS flows without bundling a heavy GPL UI by default.
