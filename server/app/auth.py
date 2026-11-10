"""JWT authentication helpers.

Two token scopes are used:

* ``user``    – issued by ``POST /api/auth/login``.  Used for REST calls.
* ``session`` – short-lived token bound to one teleop session.  Used for the
  pilot WebSocket.  A browser cannot set an ``Authorization`` header on a
  WebSocket handshake, so this token is passed as a query parameter.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi import HTTPException, Request, WebSocket, status

from .config import Settings


class AuthError(Exception):
    pass


def create_token(
    settings: Settings,
    *,
    subject: str,
    role: str = "pilot",
    scope: str = "user",
    ttl_s: int | None = None,
    **claims: Any,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "scope": scope,
        "iat": now,
        "exp": now + (ttl_s if ttl_s is not None else settings.jwt_ttl_s),
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("invalid token") from exc
    if not claims.get("sub"):
        raise AuthError("token has no subject")
    return claims


def _bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def settings_from_request(request: Request) -> Settings:
    return request.app.state.settings


def current_claims(request: Request) -> dict[str, Any]:
    token = _bearer_from_header(request.headers.get("authorization"))
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(settings_from_request(request), token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if claims.get("scope") != "user":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="wrong token scope")
    return claims


def require_admin(request: Request) -> dict[str, Any]:
    claims = current_claims(request)
    if claims.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return claims


def websocket_token(ws: WebSocket) -> str | None:
    token = _bearer_from_header(ws.headers.get("authorization"))
    if token:
        return token
    return ws.query_params.get("token")


def authenticate_vehicle(ws: WebSocket) -> tuple[dict[str, Any], Any]:
    token = websocket_token(ws)
    if not token:
        raise AuthError("missing device token")
    registry = ws.app.state.registry
    config = registry.authenticate_device(token)
    if config is None:
        raise AuthError("unknown device token")
    return {"sub": config.id, "scope": "device", "role": "vehicle"}, config


def authenticate_pilot(ws: WebSocket, session_id: str) -> dict[str, Any]:
    token = websocket_token(ws)
    if not token:
        raise AuthError("missing session token")
    claims = decode_token(ws.app.state.settings, token)
    if claims.get("scope") != "session":
        raise AuthError("wrong token scope")
    if claims.get("sid") != session_id:
        raise AuthError("token does not belong to this session")
    return claims
