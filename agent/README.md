# ROVOps vehicle agent

Runs on the vehicle (normally as a BlueOS Extension). Opens an **outbound**
WebSocket to the control server, bridges the local MAVLink link, and enforces
a local command watchdog and latched E-stop as a backstop.

## Run locally / SITL
```powershell
cd D:\1\rovops\agent
$env:SERVER_URL = "http://127.0.0.1:8080"
$env:VEHICLE_TOKEN = "<provisioned-device-token>"
$env:MAVLINK_ENDPOINT = "udpin:0.0.0.0:14599"
..\.venv\Scripts\python.exe -m rovops_agent.main
```

Health endpoint: `http://127.0.0.1:8081/healthz`

## BlueOS Extension
Build and install from `agent/Dockerfile`:
```bash
docker build -t rovops-agent:0.1.0 .
```
In BlueOS, add a custom extension pointing at the image. The agent needs
host networking so it can bind the MAVLink UDP endpoint
(`udpin:0.0.0.0:14599`). Add a MAVLink router endpoint in BlueOS (Client
mode, `127.0.0.1:14599`) pointing at the agent.

## Safety
- Only `MANUAL_CONTROL`, arm/disarm and mode changes are accepted from the
  server; everything else is dropped.
- Frames are re-targeted to the vehicle's actual system ID.
- While E-stop is latched, motion commands are dropped and hard-neutral frames
  are sent continuously.
- If no manual command arrives within `COMMAND_TTL_MS`, neutral frames are
  sent continuously.
- ArduSub failsafes (pilot timeout, etc.) remain the ultimate backstop.
