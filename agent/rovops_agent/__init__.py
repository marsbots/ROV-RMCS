"""ROVOps vehicle agent.

The agent runs aboard the vehicle (normally as a BlueOS Extension).  It opens
one **outbound** WebSocket to the control server, bridges the local MAVLink
link, and keeps its own safety watchdog so a lost server never leaves the
thrusters running.
"""

__version__ = "0.1.0"
