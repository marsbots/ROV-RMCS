# Video

Video is out of band by design. The control plane only carries a
`whep_url` / `rtsp_url` advertised by the agent in its `hello` message.

## MediaMTX (recommended)
Run MediaMTX on the vehicle or a relay. Configure the agent with
`VIDEO_WHEP_URL=http://<host>:8889/<stream>/whep`. The pilot UI plays it via
WebRTC (WHEP).

## LiveKit
Alternative when you need TURN, multi-party viewing or recording. Advertise
the LiveKit WHEP/egress URL the same way.

## AGPL / Cockpit
If you expose QGroundControl/Cockpit (GPL/AGPL) as a network service, the
AGPL network-service clause may apply. ROVOps keeps the heavy GCS UI out of
the default stack and integrates with existing GCS flows instead.
