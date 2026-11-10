# Security and provisioning

## Tokens
- **User tokens** (REST): scope `user`, short TTL, issued by `/api/auth/login`.
- **Session tokens** (pilot WebSocket): scope `session`, bound to one session id.
- **Device tokens** (vehicle WebSocket): scope `device`, provisioned per vehicle.

Device tokens can be stored in `vehicles.json` (`token`) or referenced via
`token_env` so the registry never contains plaintext secrets.

## Device authentication
`/ws/vehicle?token=...` is authenticated with constant-time comparison
(`hmac.compare_digest`) against every provisioned vehicle token.

## Sessions and lease
- One session per vehicle (reused across pilots).
- A single controller lease; handover is explicit.
- E-stop is latched server-side and re-asserted to the vehicle.

## Audit
Append-only JSONL: auth, sessions, lease changes, arm/mode, E-stop, command
rejections, vehicle online/offline. Audit writes are synchronous to preserve
ordering.
