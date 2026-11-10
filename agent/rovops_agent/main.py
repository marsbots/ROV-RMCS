"""Agent entrypoint.

    python -m rovops_agent.main
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .client import AgentClient
from .config import AgentConfig
from .health import HealthServer
from .mavlink_bridge import MavlinkBridge

log = logging.getLogger("rovops.agent")


async def _main() -> int:
    config = AgentConfig()
    if not config.vehicle_token:
        log.error("VEHICLE_TOKEN is required (device token provisioned on the server)")
        return 2

    bridge = MavlinkBridge(config)
    bridge.start()
    client = AgentClient(config, bridge)
    health = HealthServer(config.health_port, client.status)
    health.start()

    log.info("ROVOps agent %s starting", config.agent_id)
    log.info("config: %s", config.redacted())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):
            # Windows event loop does not support add_signal_handler.
            pass

    runner = asyncio.create_task(client.run_forever())
    stopper = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc:
            log.error("agent stopped with error: %s", exc, exc_info=exc)
            health.stop()
            bridge.stop()
            return 1

    health.stop()
    bridge.stop()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
