"""Data models shared by the HTTP API and the WebSocket hubs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Provisioned vehicle registry
# ---------------------------------------------------------------------------


@dataclass
class VehicleConfig:
    id: str
    name: str
    token: str
    model: str = "unknown"
    autopilot: str = "ArduSub"
    site: str = ""
    sysid: int = 1
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VehicleConfig":
        vehicle_id = str(raw["id"]).strip()
        if not vehicle_id:
            raise ValueError("vehicle id must not be empty")
        # Prefer an environment variable for the device secret so production
        # registries do not need to contain plaintext tokens.
        token_env = raw.get("token_env")
        token = os.environ.get(str(token_env), "") if token_env else ""
        token = token or str(raw.get("token") or "")
        return cls(
            id=vehicle_id,
            name=str(raw.get("name") or vehicle_id),
            token=token,
            model=str(raw.get("model") or raw.get("type") or "unknown"),
            autopilot=str(raw.get("autopilot") or "ArduSub"),
            site=str(raw.get("site") or ""),
            sysid=int(raw.get("sysid") or 1),
            tags=list(raw.get("tags") or []),
            meta=dict(raw.get("meta") or {}),
        )

    def public(self, online: bool, telemetry: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "autopilot": self.autopilot,
            "site": self.site,
            "sysid": self.sysid,
            "tags": self.tags,
            "online": online,
            "telemetry": telemetry or {},
        }


@dataclass
class VehicleConnection:
    config: VehicleConfig
    ws: Any
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    info: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    video: dict[str, Any] = field(default_factory=dict)
    sysid: int = 1
    compid: int = 1
    rtt_ms: float | None = None
    last_ping_at: float | None = None
    ping_seq: int = 0


# ---------------------------------------------------------------------------
# HTTP request bodies
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: str
    role: str
    expires_in: int


class CreateSessionRequest(BaseModel):
    vehicle_id: str


class CreateSessionResponse(BaseModel):
    session_id: str
    token: str
    ws_url: str
    expires_at: float
    vehicle: dict[str, Any]
    command_ttl_ms: int
