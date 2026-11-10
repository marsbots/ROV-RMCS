# Deployment

## Server
- Run with uvicorn behind a reverse proxy (nginx/Caddy) with TLS.
- Set `JWT_SECRET` to a long random value; change demo/admin credentials.
- Provision device tokens via `server/config/vehicles.json` or `token_env`.
- Audit log: `data/audit.jsonl`; ship to durable storage in production.

## Video plane (out of band)
The control server never carries video. The agent advertises `VIDEO_WHEP_URL` /
`VIDEO_RTSP_URL`; the pilot UI plays WHEP. Recommended: MediaMTX serving WHEP
from the vehicle or a relay. LiveKit is an alternative for multi-party /
TURN-heavy deployments.

## Networking
- Vehicles dial out to `/ws/vehicle` (NAT/firewall friendly).
- Pilots connect to `/ws/pilot/{session_id}`.
- CORS defaults to `*`; restrict in production.
