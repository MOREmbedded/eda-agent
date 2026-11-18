# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""app_restart_altium_bridge answers in seconds, whatever the loop is doing.

REPORTED from outside: after each crash the restart tool hung too, and a
person had to run StartMCPServer by hand. Its two preparing calls rode the
session's single executor worker, which a crashed call holds through its
whole recovery, and queued behind the recovery's maintenance marker.
"""
from __future__ import annotations

import asyncio
import time

import pytest


class _DummyMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class _StuckBridge:
    """A loop held by a crashed call: nothing it is sent comes back."""

    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None,
                                 max_extensions=None):
        self.calls.append((command, max_extensions))
        await asyncio.sleep(3600)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from eda_agent.config import configure
    from eda_agent.tools import application

    configure(workspace_dir=tmp_path)
    bridge = _StuckBridge()
    started = []

    class _Thread:
        def __init__(self, target, name, daemon):
            started.append(name)

        def start(self):
            started.append("started")

    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    monkeypatch.setattr(application, "_get_ui_inspector", lambda: object())
    monkeypatch.setattr(application.threading, "Thread", _Thread)
    monkeypatch.setattr(application, "_RESTART_PREPARE_CAP_S", 0.2)
    mcp = _DummyMcp()
    application.register_application_tools(mcp)
    return mcp.tools["app_restart_altium_bridge"], bridge, started, tmp_path


@pytest.mark.asyncio
async def test_a_loop_that_does_not_answer_does_not_hold_the_tool(setup):
    restart, bridge, started, _ = setup
    began = time.monotonic()
    out = await restart(launch_hotkey="F9")
    assert time.monotonic() - began < 2.0
    assert out["restart_scheduled"] is True
    assert started[-1] == "started"
    assert "did not answer" in out["prepared_script_document"]["error"]
    assert bridge.calls[0] == ("application.get_open_documents", 0)


@pytest.mark.asyncio
async def test_a_restart_already_under_way_is_not_started_twice(setup):
    from eda_agent.bridge.altium_bridge import MAINTENANCE_FILE

    restart, bridge, started, workspace = setup
    (workspace / MAINTENANCE_FILE).write_text("{}")
    out = await restart(launch_hotkey="F9")
    assert out["success"] is False and out["restart_in_progress"] is True
    assert "relaunch --restart" in out["reason"]
    assert started == [] and bridge.calls == []
