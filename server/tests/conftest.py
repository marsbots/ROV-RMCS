"""Shared pytest fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import os
import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-hs256")
os.environ.setdefault("DEMO_USER", "demo")
os.environ.setdefault("DEMO_PASSWORD", "demo")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "agent"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

DEVICE_TOKEN = "test-device-token-rov-01"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    registry = {
        "vehicles": [
            {
                "id": "rov-01",
                "name": "Test ROV",
                "model": "BlueROV2",
                "autopilot": "ArduSub",
                "site": "test-tank",
                "sysid": 1,
                "token": DEVICE_TOKEN,
            }
        ]
    }
    vehicles_file = tmp_path / "vehicles.json"
    vehicles_file.write_text(json.dumps(registry), encoding="utf-8")
    return Settings(
        jwt_secret="test-secret-that-is-long-enough-for-hs256",
        jwt_ttl_s=600,
        session_ttl_s=600,
        vehicles_file=str(vehicles_file),
        audit_file=str(tmp_path / "audit.jsonl"),
        demo_user="demo",
        demo_password="demo",
        admin_user="admin",
        admin_password="admin",
        command_ttl_ms=300,
        neutral_hz=20.0,
        estop_repeat_s=0.5,
        max_command_hz=50.0,
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
