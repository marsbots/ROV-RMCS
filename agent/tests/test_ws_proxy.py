"""Regression test: the agent must ignore ambient HTTP(S)_PROXY for the control plane.

`websockets` 17.x honours ``HTTP_PROXY``/``HTTPS_PROXY`` by default.  On a host
that runs an egress proxy (CI runners, corporate laptops, the OpenClaw gateway
host) that default silently routes ``ws://127.0.0.1:<port>`` through the proxy,
and every handshake fails with ``InvalidMessage: did not receive a valid HTTP
response`` even though the server is healthy.

The agent therefore passes an explicit ``proxy`` setting: ``AGENT_WS_PROXY``
(empty = never use a proxy).  These tests pin that contract without needing a
live proxy.
"""

from __future__ import annotations

import pytest

from rovops_agent.config import AgentConfig


def test_ws_proxy_defaults_to_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_WS_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:pass@127.0.0.1:49555")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@127.0.0.1:49555")

    assert AgentConfig().ws_proxy is None


def test_ws_proxy_can_be_enabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WS_PROXY", "http://proxy.internal:3128")

    assert AgentConfig().ws_proxy == "http://proxy.internal:3128"


def test_ws_proxy_auto_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENT_WS_PROXY=auto`` opts back into environment-proxy discovery."""
    monkeypatch.setenv("AGENT_WS_PROXY", "auto")

    assert AgentConfig().ws_proxy is True
