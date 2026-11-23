# ROVOps API Contract

Single source of truth for the control-plane interface between the RMCS server,
pilot clients, and vehicle agents. Both `mars-rmcs` and `mars-gcs` track changes
against this document.

- Base URL (production): `https://rmcs.marsbot.kr`
- Base URL (local dev): `http://127.0.0.1:8080`
- Auth: JWT (HS256) for users; static device token for vehicles; session token
  for the pilot WebSocket.
- Control plane: JSON over REST + WebSocket. Binary MAVLink frames travel on the
  same WebSocket as raw bytes (separate from JSON text frames).

---

## 1. Authentication

### POST /api/auth/login

Request body (`LoginRequest`):

```json
{"username": "string", "password": "string"}
```

Response 200 (`LoginResponse`):

```json
{
  "token": "<jwt>",
  "user": "string",
  "role": "pilot" | "admin",
  "expires_in": 3600
}
```

Errors: `401 invalid credentials` (audited as `login_failed`).

JWT payload (`user` scope):

```json
{
  "sub": "<username>",
  "role": "pilot" | "admin",
  "scope": "user",
  "iat": <unix>,
  "exp": <unix>
}
```

### GET /api/me

Headers: `Authorization: Bearer <user-token>`.

Response 200:

```json
{"user": "string", "role": "pilot" | "admin", "exp": <unix>}
```

Errors: `401` (missing/invalid/wrong-scope token).

### Authorization header

All REST endpoints except `/healthz` and `/api/auth/login` require
`Authorization: Bearer <user JWT with scope=user>`.

---

## 2. Vehicles

### GET /api/vehicles

Headers: `Authorization: Bearer <user-token>`.

Response 200:

```json
{
  "vehicles": [
    {
      "id": "rov-01",
      "name": "BlueROV2 #1",
      "model": "BlueROV2 Heavy",
      "autopilot": "ArduSub",
      "site": "test-tank",
      "sysid": 1,
      "tags": ["tether", "blueos"],
      "online": true,
      "telemetry": {"mode": "STABILIZE", "...": "..."}
    }
  ]
}
```

Notes: device tokens are never returned. `online` reflects the live
`/ws/vehicle` connection. `telemetry` is the last snapshot pushed by the agent.

### GET /api/vehicles/{vehicle_id}

Same item shape as above (single object). `404` if unknown.

---


## 3. Sessions

### POST /api/sessions

Headers: `Authorization: Bearer <user-token>`.

Request body (`CreateSessionRequest`):

```json
{"vehicle_id": "rov-01"}
```

Response 200 (`CreateSessionResponse`):

```json
{
  "session_id": "<hex16>",
  "token": "<session jwt>",
  "ws_url": "/ws/pilot/{session_id}" ,
  "expires_at": <unix float>,
  "vehicle": { "...vehicle state..." },
  "command_ttl_ms": 500
}
```

Notes:
- One session per vehicle; repeated calls for the same vehicle/user reuse the
  existing session while it is alive.
- `token` is a `session`-scoped JWT bound to `sid=<session_id>` and
  `vid=<vehicle_id>`; it cannot be used for REST.
- `ws_url` is prefixed with `PUBLIC_WS_BASE` when configured.

Session JWT payload:

```json
{
  "sub": "<username>",
  "role": "pilot" | "admin",
  "scope": "session",
  "sid": "<session_id>",
  "vid": "<vehicle_id>",
  "iat": <unix>,
  "exp": <unix>
}
```

### GET /api/sessions/{session_id}

Headers: `Authorization: Bearer <user-token>`.

Response 200:

```json
{
  "session_id": "...",
  "vehicle_id": "rov-01",
  "created_at": <unix>,
  "expires_at": <unix>,
  "created_by": "demo",
  "lease_holder": "<pilot id or null>",
  "estop": false,
  "estop_by": null,
  "pilots": [{"id": "...", "user": "demo", "is_controller": false}],
  "vehicle": { "...vehicle state..." }
}
```

### DELETE /api/sessions/{session_id}

Headers: `Authorization: Bearer <user-token>`.

- Allowed for the session creator or an admin.
- On success broadcasts `{"type":"error","code":"session_closed",...}` to pilots,
  sends a neutral burst, closes pilot sockets (code 4001), and closes the session.

Response 200: `{"ok": true}`.

### GET /api/sessions/{session_id}/audit

Headers: `Authorization: Bearer <user-token>`.

Query: `limit` (default 200, clamped 1..500).

Response 200: `{"events": [ ...audit records filtered by session_id... ]}`.

### GET /api/audit (admin only)

Headers: `Authorization: Bearer <admin-token>`.

Query: `limit` (default 200, clamped 1..500).

Response 200: `{"events": [ ...recent audit records... ]}`.

Errors: `403` if not admin.

---

## 4. Health

### GET /healthz

No auth.

Response 200:

```json
{
  "ok": true,
  "time": <unix float>,
  "vehicles_online": 0,
  "sessions": 0
}
```

---

## 5. WebSocket /ws/vehicle

Device WebSocket. Auth via `?token=<device-token>` (or `Authorization: Bearer`).

### Server -> vehicle: welcome

```json
{
  "type": "welcome",
  "protocol": 1,
  "server_time": <unix>,
  "vehicle_id": "rov-01",
  "command_ttl_ms": 500,
  "neutral_hz": 10.0
}
```

### Server -> vehicle: ping (every 5s)

```json
{"type": "ping", "ts": <unix>, "seq": <int>}
```

### Vehicle -> server: hello

```json
{
  "type": "hello",
  "sysid": 1,
  "compid": 1,
  "agent": {"name": "rovops-agent", "version": "...", "id": "...", "mavlink_endpoint": "..."},
  "video": {"whep_url": "...", "rtsp_url": "..."},
  "telemetry": {"mode": "STABILIZE", "armed": false, "...": "..."}
}
```

### Vehicle -> server: telemetry

```json
{"type": "telemetry", "data": {"mode": "...", "armed": false, "...": "..."}}
```

Server broadcasts to pilots: `{"type":"telemetry","vehicle_id":"rov-01","ts":<unix>,"data":{...}}`.

### Vehicle -> server: heartbeat

```json
{"type": "heartbeat", "data": {"mavlink_online": true, "estop": false, "mode": "...", "armed": false}}
```

Server broadcasts: `{"type":"vehicle_heartbeat","vehicle_id":"rov-01","ts":<unix>,"rtt_ms":<number>,"data":{...}}`.

### Vehicle -> server: pong

```json
{"type": "pong", "ts": <unix echo>}
```

### Vehicle -> server: log

```json
{"type": "log", "level": "info", "message": "..."}
```

### Vehicle -> server: estop_ack

```json
{"type": "estop_ack", "active": true, "ts": <unix>}
```

Server broadcasts to pilots: `{"type":"estop_ack","vehicle_id":"rov-01","ts":<unix>}`.

### Server -> vehicle: estop (JSON)

```json
{"type": "estop", "active": true, "by": "demo", "ts": <unix>}
```

Sent on E-stop latch and repeatedly while latched (every `ESTOP_REPEAT_S`).

### Server -> vehicle: raw binary MAVLink

Binary WebSocket frames carry MAVLink v2 frames (manual control, arm/disarm,
mode change, neutral). These are produced by the server from pilot commands.

### Vehicle -> server: raw binary MAVLink

Binary frames from the vehicle are broadcast to all pilots in the session
(telemetry passthrough).

---

## 6. WebSocket /ws/pilot/{session_id}

Auth via `?token=<session-jwt>` (or `Authorization: Bearer`). The token must have
`scope=session` and `sid=<session_id>`.

### Server -> pilot: session_state (on connect)

```json
{
  "type": "session_state",
  "session": {
    "session_id": "...",
    "vehicle_id": "rov-01",
    "you": "<pilot id>",
    "user": "demo",
    "lease_holder": null,
    "you_have_lease": false,
    "estop": false,
    "estop_by": null,
    "pilots": [{"id": "...", "user": "demo", "is_controller": false}],
    "expires_at": <unix>,
    "command_ttl_ms": 500
  },
  "vehicle": { "...vehicle state..." },
  "video": {"whep_url": "...", "rtsp_url": "..."},
  "server_time": <unix>
}
```

### Server -> pilot: pilot_joined / pilot_left

```json
{"type": "pilot_joined", "pilot": {"id": "...", "user": "demo"}}
{"type": "pilot_left", "pilot": {"id": "...", "user": "demo"}}
```

### Server -> pilot: state (broadcast)

```json
{
  "type": "state",
  "reason": "lease" | "arm" | "mode" | "estop" | "estop_reset" | "pilot_joined" | "pilot_left" | "pilot_disconnected",
  "session": { "...session state for this pilot..." }
}
```

### Server -> pilot: lease

```json
{"type": "lease", "granted": true, "holder": "<pilot id>", "you": "<pilot id>", "reason": null}
{"type": "lease", "granted": false, "holder": "<other pilot id>", "you": "<pilot id>", "reason": "held_by_another_pilot"}
{"type": "lease", "granted": false, "holder": null, "you": "<pilot id>", "reason": "released"}
{"type": "lease", "granted": false, "holder": null, "you": "<pilot id>", "reason": "not_holder"}
```

### Server -> pilot: estop

```json
{"type": "estop", "active": true, "by": "demo"}
{"type": "estop", "active": false, "by": null}
```

### Server -> pilot: telemetry / vehicle / vehicle_heartbeat (forwarded)

See section 5.

### Server -> pilot: error

```json
{"type": "error", "code": "<code>", "message": "<text>"}
```

Common codes: `bad_json`, `unknown_type`, `no_lease`, `estop_active`,
`rate_limited`, `vehicle_offline`, `bad_manual`, `bad_mode`, `session_closed`,
`session_expired`, `server_error`.

### Pilot -> server: JSON messages

- `{"type":"ping","ts":<any>}` -> `{"type":"pong","ts":<echo>,"server_time":<unix>}`.
- `{"type":"lease_acquire"}`: request the control lease.
- `{"type":"lease_release"}`: release the control lease (sends neutral burst).
- `{"type":"manual","x":int,"y":int,"z":int,"r":int,"buttons":int,"buttons2":int}`:
  manual control. Ranges: x,y,r in [-1000,1000]; z in [0,1000]; buttons in
  [0,65535]. Only the lease holder; blocked while E-stop latched.
- `{"type":"arm","arm":bool}`: arm/disarm. Arming blocked while E-stop latched;
  disarming always allowed for the lease holder.
- `{"type":"mode","mode":"STABILIZE"}`: set flight mode. Allowed modes:
  STABILIZE, ACRO, ALT_HOLD, AUTO, GUIDED, CIRCLE, SURFACE, POSHOLD, MANUAL,
  SURFTRAK.
- `{"type":"estop"}`: latch E-stop (any pilot in the session).
- `{"type":"estop_reset"}`: clear E-stop (lease holder only).

### Pilot -> server: raw binary MAVLink

Raw MAVLink frames are forwarded to the vehicle only if the sender holds the
lease. Rate-limited by `MAX_COMMAND_HZ`.

---

## 7. Lease semantics

- One session per vehicle; multiple pilots may join the same session.
- A single control lease per session. Only the lease holder may send motion,
  arm, or mode commands.
- `lease_acquire` grants the lease if it is free or already held by the
  requester; otherwise denied (`held_by_another_pilot`).
- Releasing or disconnecting as lease holder releases the lease and triggers a
  neutral burst.
- The server neutralizes automatically when no command arrives within
  `COMMAND_TTL_MS` (neutral watchdog), and on lease loss / disconnect / session
  close / expiry.

## 8. E-stop (latched) semantics

- Any pilot in the session may latch E-stop (`estop`).
- While latched: motion commands are rejected (`estop_active`); arming is
  rejected; disarming remains allowed.
- Only the lease holder may reset (`estop_reset`).
- While latched the server repeatedly sends neutral frames and re-asserts the
  E-stop flag to the vehicle (every `ESTOP_REPEAT_S`).
- The vehicle agent also enforces E-stop locally: motion dropped, hard-neutral
  sent continuously.

## 9. Neutral burst conditions

A neutral burst (MANUAL_CONTROL with x=y=r=0, z=500) is sent when:

- lease is released or its holder disconnects,
- session is closed or expires (if a lease was held),
- command TTL expires (neutral watchdog),
- E-stop is latched (continuously).

## 10. External access path

- Public hostname: `rmcs.marsbot.kr` (TLS via Caddy reverse proxy).
- Caddy terminates TLS and proxies to the uvicorn server (127.0.0.1:8080).
- Server runs under LaunchAgent `ai.openclaw.rmcs-server` (auto-start on boot,
  restart on crash), via `~/.openclaw/scripts/rmcs-server-start.sh`.
- Vehicle agents dial out to `wss://rmcs.marsbot.kr/ws/vehicle?token=...`.
- Pilots use `https://rmcs.marsbot.kr` (and `wss://.../ws/pilot/{session_id}`).
- CORS: only `https://rmcs.marsbot.kr` is allowed (configurable via
  `CORS_ORIGINS`).
- Secrets are injected via environment variables (LaunchAgent
  `EnvironmentVariables`); no default secrets in code.

## 11. Environment variables

See `.env.example`. Required: `JWT_SECRET`. User credentials
(`DEMO_USER`/`DEMO_PASSWORD`/`ADMIN_USER`/`ADMIN_PASSWORD`) have no defaults.
Device tokens are referenced via `token_env` in `vehicles.json`.

`PUBLIC_WS_BASE` — absolute WebSocket base URL advertised to pilots.
When set, `POST /api/sessions` returns `ws_url` as
`${PUBLIC_WS_BASE}/ws/pilot/{session_id}` (e.g. `wss://rmcs.marsbot.kr`);
otherwise the relative path `/ws/pilot/{session_id}` is returned. Clients
(MARSUB Remote Mode) must join a relative `ws_url` with the server base URL.
Behind a TLS terminator set `PUBLIC_WS_BASE=wss://<host>`.

## 12. Audit log retention policy

- Source of truth: append-only JSONL at `AUDIT_FILE` (default `data/audit.jsonl`);
  records are also kept in a bounded in-memory ring for recent queries.
- **Minimum retention (production):** keep audit logs for at least 90 days online
  and 1 year in durable/archive storage (or per applicable safety/regulatory
  policy if stricter). Do not delete or rewrite log lines in place.
- **Rotation/archival:** rotate by day or size (recommended: daily or >=100 MB)
  with compressed archives (e.g. `audit.YYYY-MM-DD.jsonl.gz`), preserve ordering,
  and ship copies to tamper-evident durable storage (S3/object lock, WORM, or
  centralized log backend) outside the host.
- **Access/control:** restrict read access to authorized operators/admins; treat
  logs as sensitive operational data. Monitor ingest/ship failures and alert.
- **Verification:** `GET /api/audit` (admin) and
  `GET /api/sessions/{session_id}/audit` expose recent records; long-term review
  must use the archived/shipped copies.
