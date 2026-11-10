"""Outbound WebSocket client for the ROVOps control server."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from . import __version__
from .config import AgentConfig
from .mavlink_bridge import MavlinkBridge

log = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, config: AgentConfig, bridge: MavlinkBridge) -> None:
        self.config = config
        self.bridge = bridge
        self._connected = False
        self._connected_since: float | None = None
        self._last_connected_at: float | None = None
        self._last_error: str | None = None
        self._last_hello_sysid: int | None = None

    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.config.agent_id,
            "version": __version__,
            "server": self.config.redacted()["server"],
            "connected": self._connected,
            "connected_since": self._connected_since,
            "last_connected_at": self._last_connected_at,
            "last_error": self._last_error,
            "mavlink": self.bridge.status(),
            "ts": time.time(),
        }

    async def run_forever(self) -> None:
        delay = self.config.reconnect_min_s
        while True:
            try:
                log.info("connecting to %s", self.config.redacted()["server"])
                async with connect(
                    self.config.ws_url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=None,
                ) as ws:
                    self._connected = True
                    self._connected_since = time.time()
                    self._last_connected_at = self._connected_since
                    self._last_error = None
                    delay = self.config.reconnect_min_s
                    log.info("connected to control server")
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("control link lost: %s", self._last_error)
            finally:
                self._connected = False
                self._connected_since = None

            sleep_for = min(self.config.reconnect_max_s, delay) * (0.8 + random.random() * 0.4)
            log.info("reconnecting in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, self.config.reconnect_max_s)

    async def _session(self, ws: ClientConnection) -> None:
        await self._send_hello(ws)
        sender = asyncio.create_task(self._telemetry_loop(ws), name="telemetry")
        receiver = asyncio.create_task(self._receive_loop(ws), name="receive")
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, ConnectionClosed):
                raise exc

    async def _send_hello(self, ws: ClientConnection) -> None:
        snapshot = self.bridge.snapshot()
        video: dict[str, Any] = {}
        if self.config.video_whep_url:
            video["whep_url"] = self.config.video_whep_url
        if self.config.video_rtsp_url:
            video["rtsp_url"] = self.config.video_rtsp_url
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "agent": {
                        "name": "rovops-agent",
                        "version": __version__,
                        "id": self.config.agent_id,
                        "mavlink_endpoint": self.config.mavlink_endpoint,
                    },
                    "sysid": snapshot["sysid"],
                    "compid": snapshot["compid"],
                    "telemetry": snapshot,
                    "video": video,
                }
            )
        )
        self._last_hello_sysid = int(snapshot["sysid"])

    async def _telemetry_loop(self, ws: ClientConnection) -> None:
        interval = 1.0 / max(0.2, self.config.telemetry_hz)
        heartbeat_at = 0.0
        while True:
            now = time.time()
            snapshot = self.bridge.snapshot()
            await ws.send(json.dumps({"type": "telemetry", "data": snapshot}))
            if now - heartbeat_at >= 2.0:
                heartbeat_at = now
                await ws.send(
                    json.dumps(
                        {
                            "type": "heartbeat",
                            "data": {
                                "mavlink_online": snapshot["mavlink_online"],
                                "estop": snapshot["estop"],
                                "mode": snapshot["mode"],
                                "armed": snapshot["armed"],
                            },
                        }
                    )
                )
            if self._last_hello_sysid != int(snapshot["sysid"]):
                await self._send_hello(ws)
            await asyncio.sleep(interval)

    async def _receive_loop(self, ws: ClientConnection) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                self.bridge.submit_frame(message)
                continue
            if not isinstance(message, str):
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                log.warning("ignoring malformed server JSON")
                continue
            await self._handle(payload, ws)

    async def _handle(self, payload: dict[str, Any], ws: ClientConnection) -> None:
        kind = payload.get("type")
        if kind == "ping":
            await ws.send(json.dumps({"type": "pong", "ts": payload.get("ts")}))
            return

        if kind == "welcome":
            log.info(
                "server welcome: vehicle=%s protocol=%s command_ttl_ms=%s",
                payload.get("vehicle_id"),
                payload.get("protocol"),
                payload.get("command_ttl_ms"),
            )
            return

        if kind == "estop":
            active = bool(payload.get("active"))
            self.bridge.set_estop(active)
            await ws.send(
                json.dumps(
                    {
                        "type": "estop_ack",
                        "active": active,
                        "ts": time.time(),
                    }
                )
            )
            return

        if kind == "error":
            log.error("server error: %s (%s)", payload.get("message"), payload.get("code"))
            return

        log.debug("unhandled server message: %s", kind)


async def run(config: AgentConfig, bridge: MavlinkBridge) -> None:
    client = AgentClient(config, bridge)
    await client.run_forever()
