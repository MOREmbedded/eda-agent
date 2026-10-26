# SPDX-License-Identifier: Apache-2.0
"""Python-side coverage for document open/close/reload MCP tools."""

from __future__ import annotations

import pytest


class _DummyMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True}


def _tools(monkeypatch):
    from eda_agent.tools import application

    bridge = _Bridge()
    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    mcp = _DummyMcp()
    application.register_application_tools(mcp)
    return mcp.tools, bridge


@pytest.mark.asyncio
async def test_open_document_infers_kind_in_live_script(monkeypatch):
    tools, bridge = _tools(monkeypatch)
    await tools["app_open_document"]("C:\\p\\board.PcbDoc")
    assert bridge.calls[-1] == (
        "application.open_document",
        {"file_path": "C:\\p\\board.PcbDoc"},
    )


@pytest.mark.asyncio
async def test_close_document_refuses_unsaved_changes_by_default(monkeypatch):
    tools, bridge = _tools(monkeypatch)
    await tools["app_close_document"]("C:\\p\\main.SchDoc")
    assert bridge.calls[-1][0] == "application.close_document"
    assert bridge.calls[-1][1] == {"file_path": "C:\\p\\main.SchDoc"}


@pytest.mark.asyncio
async def test_reload_document_defaults_to_disk_without_discard(monkeypatch):
    tools, bridge = _tools(monkeypatch)
    await tools["app_reload_document"]("C:\\p\\main.SchDoc", kind="SCH")
    assert bridge.calls[-1] == (
        "application.reload_document",
        {
            "file_path": "C:\\p\\main.SchDoc",
            "save_before_close": "false",
            "discard_changes": "false",
            "kind": "SCH",
        },
    )


@pytest.mark.asyncio
async def test_restart_mcp_server_schedules_delayed_exec(monkeypatch):
    from eda_agent.tools import application

    started = []

    class _Thread:
        def __init__(self, target, name, daemon):
            started.append((target, name, daemon))

        def start(self):
            started.append("started")

    monkeypatch.setattr(application.threading, "Thread", _Thread)
    tools, _ = _tools(monkeypatch)
    out = await tools["app_restart_mcp_server"](delay_ms=750)
    assert out["restart_scheduled"] is True
    assert out["command"][-2:] == ["-m", "eda_agent"]
    assert started[-1] == "started"
