# SPDX-License-Identifier: Apache-2.0
"""Capability discovery must work without starting Altium."""

from pathlib import Path

import pytest

from eda_agent.bridge.capabilities import inspect_script_capabilities


ROOT = Path(__file__).resolve().parents[1]


def test_script_capabilities_parse_case_and_if_dispatchers():
    report = inspect_script_capabilities(ROOT / "scripts" / "altium")
    assert report["complete_bundle"] is True
    assert "ping" in report["categories"]["application"]
    assert "cross_probe" in report["categories"]["project"]
    assert "place_via" in report["categories"]["pcb"]
    assert "find_signal_vias_without_return" in report["categories"]["audit"]
    assert report["command_count"] > 200


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.mark.asyncio
async def test_application_probe_defaults_to_offline(monkeypatch):
    from eda_agent.tools import application

    monkeypatch.setattr(
        application, "get_bridge",
        lambda: (_ for _ in ()).throw(AssertionError("must stay offline")),
    )
    mcp = _Mcp()
    application.register_meta_tools(mcp)
    result = await mcp.tools["app_capability_probe"]()
    assert result["live_probed"] is False
    assert result["command_count"] > 200
