"""HTTP authentication and session API tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

DEVICE_TOKEN = "test-device-token-rov-01"


def login(client: TestClient, user: str = "demo", password: str = "demo") -> dict:
    response = client.post("/api/auth/login", json={"username": user, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_session(client: TestClient, token: str) -> dict:
    response = client.post("/api/sessions", json={"vehicle_id": "rov-01"}, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_login_failure_is_audited(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": "demo", "password": "nope"})
    assert response.status_code == 401


def test_login_and_rest_scopes(client: TestClient) -> None:
    body = login(client)
    assert body["user"] == "demo"
    assert login(client, "admin", "admin")["role"] == "admin"

    me = client.get("/api/me", headers=auth(body["token"]))
    assert me.status_code == 200
    assert me.json()["user"] == "demo"

    vehicles = client.get("/api/vehicles", headers=auth(body["token"]))
    assert vehicles.status_code == 200
    assert vehicles.json()["vehicles"][0]["id"] == "rov-01"


def test_session_token_cannot_be_used_for_rest(client: TestClient) -> None:
    token = login(client)["token"]
    session = create_session(client, token)
    response = client.get("/api/me", headers=auth(session["token"]))
    assert response.status_code == 401
    assert "scope" in response.json()["detail"]


def test_session_is_reused_per_vehicle(client: TestClient) -> None:
    token = login(client)["token"]
    first = create_session(client, token)
    second = create_session(client, token)
    assert first["session_id"] == second["session_id"]

    state = client.get(f"/api/sessions/{first['session_id']}", headers=auth(token))
    assert state.status_code == 200
    assert state.json()["vehicle_id"] == "rov-01"


def test_session_delete_permissions(client: TestClient) -> None:
    token = login(client)["token"]
    session = create_session(client, token)
    response = client.delete(f"/api/sessions/{session['session_id']}", headers=auth(token))
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_audit_requires_admin(client: TestClient) -> None:
    pilot = login(client)["token"]
    admin = login(client, "admin", "admin")["token"]
    assert client.get("/api/audit", headers=auth(pilot)).status_code == 403
    response = client.get("/api/audit", headers=auth(admin))
    assert response.status_code == 200
    assert any(event["event"] == "login" for event in response.json()["events"])


def test_unknown_vehicle(client: TestClient) -> None:
    token = login(client)["token"]
    response = client.post("/api/sessions", json={"vehicle_id": "nope"}, headers=auth(token))
    assert response.status_code == 404


def test_device_token_is_not_exposed(client: TestClient) -> None:
    token = login(client)["token"]
    response = client.get("/api/vehicles", headers=auth(token))
    serialized = response.text
    assert DEVICE_TOKEN not in serialized
    assert "token" not in response.json()["vehicles"][0]
