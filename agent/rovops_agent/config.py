"""Agent configuration, read from environment variables."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit, urlunsplit


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass
class AgentConfig:
    # Control plane
    server_url: str = field(default_factory=lambda: _env("SERVER_URL", "ws://127.0.0.1:8080"))
    ws_path: str = field(default_factory=lambda: _env("WS_PATH", "/ws/vehicle"))
    vehicle_token: str = field(default_factory=lambda: _env("VEHICLE_TOKEN", ""))

    # Local MAVLink link.  With BlueOS, add a UDP endpoint to the MAVLink
    # router that points at this port (Client mode, 127.0.0.1:14599).
    mavlink_endpoint: str = field(default_factory=lambda: _env("MAVLINK_ENDPOINT", "udpin:0.0.0.0:14599"))
    vehicle_sysid: int = field(default_factory=lambda: _env_int("VEHICLE_SYSID", 1))
    vehicle_compid: int = field(default_factory=lambda: _env_int("VEHICLE_COMPID", 1))
    gcs_sysid: int = field(default_factory=lambda: _env_int("GCS_SYSID", 255))
    gcs_compid: int = field(default_factory=lambda: _env_int("GCS_COMPID", 190))

    # Safety watchdog (authoritative local backstop)
    command_ttl_ms: int = field(default_factory=lambda: _env_int("COMMAND_TTL_MS", 500))
    neutral_hz: float = field(default_factory=lambda: _env_float("NEUTRAL_HZ", 10.0))
    estop_hz: float = field(default_factory=lambda: _env_float("ESTOP_HZ", 20.0))
    estop_disarm: bool = field(default_factory=lambda: _env_bool("ESTOP_DISARM", True))
    require_manual_lease: bool = field(default_factory=lambda: _env_bool("REQUIRE_MANUAL_LEASE", True))

    # Telemetry / health
    telemetry_hz: float = field(default_factory=lambda: _env_float("TELEMETRY_HZ", 2.0))
    surface_pressure_hpa: float = field(
        default_factory=lambda: _env_float("SURFACE_PRESSURE_HPA", 1013.25)
    )
    health_port: int = field(default_factory=lambda: _env_int("AGENT_HEALTH_PORT", 8081))

    # Video is out of band.  Advertise the URLs the pilot should use.
    video_whep_url: str = field(default_factory=lambda: _env("VIDEO_WHEP_URL", ""))
    video_rtsp_url: str = field(default_factory=lambda: _env("VIDEO_RTSP_URL", ""))

    # Outbound HTTP proxy for the control-plane WebSocket.
    # ``websockets`` >= 14 discovers HTTP_PROXY/HTTPS_PROXY from the environment
    # by default, which breaks loopback deployments on hosts that run an egress
    # proxy.  The control link is a point-to-point connection to the RMCS server,
    # so a proxy is opt-in: empty (default) = no proxy, ``auto`` = environment
    # discovery, any other value = that proxy URL.
    ws_proxy_raw: str = field(default_factory=lambda: _env("AGENT_WS_PROXY", ""))

    # Reconnect behaviour
    reconnect_min_s: float = field(default_factory=lambda: _env_float("RECONNECT_MIN_S", 1.0))
    reconnect_max_s: float = field(default_factory=lambda: _env_float("RECONNECT_MAX_S", 20.0))

    agent_id: str = field(default_factory=lambda: _env("AGENT_ID", uuid.uuid4().hex[:12]))

    @property
    def ws_proxy(self) -> str | bool | None:
        """Value for ``websockets.connect(proxy=...)``.

        ``None`` disables proxy use, ``True`` enables environment discovery.
        """
        raw = (self.ws_proxy_raw or "").strip()
        if not raw:
            return None
        if raw.lower() in {"auto", "env", "1", "true"}:
            return True
        return raw

    @property
    def ws_url(self) -> str:
        parts = urlsplit(self.server_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = self.ws_path if self.ws_path.startswith("/") else "/" + self.ws_path
        query = dict()
        if parts.query:
            from urllib.parse import parse_qsl

            query.update(parse_qsl(parts.query))
        if self.vehicle_token:
            query["token"] = self.vehicle_token
        return urlunsplit((scheme, parts.netloc, path, urlencode(query), ""))

    def redacted(self) -> dict[str, object]:
        return {
            "server": urlsplit(self.ws_url)._replace(query="token=***").geturl(),
            "mavlink_endpoint": self.mavlink_endpoint,
            "vehicle_sysid": self.vehicle_sysid,
            "command_ttl_ms": self.command_ttl_ms,
            "neutral_hz": self.neutral_hz,
            "estop_hz": self.estop_hz,
            "agent_id": self.agent_id,
        }
