# Development Guide (HQ macOS)

## Environment

- Project root: `/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)` (SMB mount)
- Python venv: `~/.venvs/rmcs` (Python 3.14, local disk — required because the repo's `.venv` is Windows-only)
- Never use the in-repo `.venv` on macOS; it is Windows (C:\Python313).

## Setup (once)

```bash
python3 -m venv ~/.venvs/rmcs
~/.venvs/rmcs/bin/pip install -r server/requirements.txt
~/.venvs/rmcs/bin/pip install -r server/requirements-dev.txt
```

## Run server

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
export JWT_SECRET="<long-random-secret>"
export VEHICLES_FILE="server/config/vehicles.json"
~/.venvs/rmcs/bin/python -m uvicorn app.main:app --app-dir server --reload --port 8080
```

## Run agent (SITL)

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)/agent"
export SERVER_URL="http://localhost:8080"
export VEHICLE_TOKEN="<provisioned-device-token>"
export MAVLINK_ENDPOINT="udpin:0.0.0.0:14599"
~/.venvs/rmcs/bin/python -m rovops_agent.main
```

## Tests

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
~/.venvs/rmcs/bin/python -m pytest server/tests/ agent/tests/ -q
```

## End-to-end smoke test

```bash
cd "/Volumes/DataHub/Mars/Mutec/RMCS (Remote Monitoring and Control System)"
~/.venvs/rmcs/bin/python scripts/e2e_smoke.py
```

Starts a real server, fake autopilot and agent, then drives a pilot session over the WebSocket API.

## Storage notes

- SMB mount (`/Volumes/DataHub`) is NOT `noexec` on macOS — binaries can run from it.
- The in-repo `.venv` was created on Windows and cannot run on macOS — use `~/.venvs/rmcs`.
- WebDAV mount (`/Volumes/file.heo.best`) IS `noexec` — do not place executables there.
- `.DS_Store`, `세션내용.json`, `._DAV`, `._*` are gitignored (macOS/NAS artifacts).
