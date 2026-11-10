"""Provisioned vehicle registry and live connection table."""

from __future__ import annotations

import hmac
import time
from typing import Any

from .config import Settings
from .models import VehicleConfig, VehicleConnection


class VehicleRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._configs: dict[str, VehicleConfig] = {}
        self._connections: dict[str, VehicleConnection] = {}
        self.reload()

    # -- provisioning -----------------------------------------------------

    def reload(self) -> None:
        configs: dict[str, VehicleConfig] = {}
        for raw in self.settings.load_vehicles():
            cfg = VehicleConfig.from_dict(raw)
            if cfg.id in configs:
                raise ValueError(f"duplicate vehicle id: {cfg.id}")
            configs[cfg.id] = cfg
        self._configs = configs

    def get_config(self, vehicle_id: str) -> VehicleConfig | None:
        return self._configs.get(vehicle_id)

    def authenticate_device(self, token: str) -> VehicleConfig | None:
        if not token:
            return None
        # Constant-time compare against every provisioned vehicle token so a
        # timing side channel cannot be used to enumerate devices.
        match: VehicleConfig | None = None
        for cfg in self._configs.values():
            if cfg.token and hmac.compare_digest(token, cfg.token):
                match = cfg
        return match

    # -- live connections -------------------------------------------------

    def get_connection(self, vehicle_id: str) -> VehicleConnection | None:
        return self._connections.get(vehicle_id)

    def attach(self, conn: VehicleConnection) -> VehicleConnection | None:
        previous = self._connections.get(conn.config.id)
        self._connections[conn.config.id] = conn
        return previous

    def detach(self, vehicle_id: str, conn: VehicleConnection | None = None) -> bool:
        current = self._connections.get(vehicle_id)
        if current is None:
            return False
        if conn is not None and current is not conn:
            return False
        del self._connections[vehicle_id]
        return True

    def is_online(self, vehicle_id: str) -> bool:
        return vehicle_id in self._connections

    def update_telemetry(self, vehicle_id: str, data: dict[str, Any]) -> None:
        conn = self._connections.get(vehicle_id)
        if conn is None:
            return
        conn.telemetry = data
        conn.last_seen = time.time()

    def states(self) -> list[dict[str, Any]]:
        states: list[dict[str, Any]] = []
        for cfg in sorted(self._configs.values(), key=lambda item: item.id):
            conn = self._connections.get(cfg.id)
            states.append(cfg.public(online=conn is not None, telemetry=conn.telemetry if conn else None))
        return states

    def state(self, vehicle_id: str) -> dict[str, Any] | None:
        cfg = self._configs.get(vehicle_id)
        if cfg is None:
            return None
        conn = self._connections.get(vehicle_id)
        return cfg.public(online=conn is not None, telemetry=conn.telemetry if conn else None)
