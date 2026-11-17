"""Runtime configuration for the ROVOps control server.

All values can be overridden with environment variables.

``JWT_SECRET`` is mandatory: the server refuses to start without it.  Demo and
admin credentials have no built-in defaults and must be provided via
``DEMO_USER``/``DEMO_PASSWORD`` and ``ADMIN_USER``/``ADMIN_PASSWORD``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None:
        return default if default is not None else ""
    return value


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} environment variable is required")
    return value


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _project_root() -> Path:
    # server/app/config.py -> server/app -> server -> rovops
    return Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))

    jwt_secret: str = field(default_factory=lambda: _required_env("JWT_SECRET"))
    jwt_ttl_s: int = field(default_factory=lambda: _env_int("JWT_TTL_S", 3600))
    session_ttl_s: int = field(default_factory=lambda: _env_int("SESSION_TTL_S", 7200))

    demo_user: str = field(default_factory=lambda: _env("DEMO_USER"))
    demo_password: str = field(default_factory=lambda: _env("DEMO_PASSWORD"))
    admin_user: str = field(default_factory=lambda: _env("ADMIN_USER"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD"))

    vehicles_file: str = field(
        default_factory=lambda: _env("VEHICLES_FILE", str(_project_root() / "server" / "config" / "vehicles.json"))
    )
    audit_file: str = field(
        default_factory=lambda: _env("AUDIT_FILE", str(_project_root() / "data" / "audit.jsonl"))
    )

    # Teleop safety parameters, mirrored to the vehicle agent on connect.
    command_ttl_ms: int = field(default_factory=lambda: _env_int("COMMAND_TTL_MS", 500))
    neutral_hz: float = field(default_factory=lambda: _env_float("NEUTRAL_HZ", 10.0))
    estop_repeat_s: float = field(default_factory=lambda: _env_float("ESTOP_REPEAT_S", 1.0))
    max_command_hz: float = field(default_factory=lambda: _env_float("MAX_COMMAND_HZ", 50.0))

    # Public URLs advertised to pilots / agents.
    public_ws_base: str = field(default_factory=lambda: _env("PUBLIC_WS_BASE", ""))
    public_whep_base: str = field(default_factory=lambda: _env("PUBLIC_WHEP_BASE", ""))
    public_rtsp_base: str = field(default_factory=lambda: _env("PUBLIC_RTSP_BASE", ""))

    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", "https://rmcs.marsbot.kr"))

    # Server-side MAVLink identity used when encoding pilot commands.
    gcs_sysid: int = field(default_factory=lambda: _env_int("GCS_SYSID", 255))
    gcs_compid: int = field(default_factory=lambda: _env_int("GCS_COMPID", 190))

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]

    def load_vehicles(self) -> list[dict[str, Any]]:
        path = Path(self.vehicles_file)
        if not path.exists():
            raise FileNotFoundError(f"vehicle registry not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("vehicles", [])
        if not isinstance(data, list):
            raise ValueError("vehicle registry must be a list or an object with a 'vehicles' list")
        return data
