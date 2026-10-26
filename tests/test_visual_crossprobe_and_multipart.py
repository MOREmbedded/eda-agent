# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for visual cross-probe and multipart symbol composition."""

from __future__ import annotations

import pytest


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True, "command": command}


@pytest.mark.asyncio
async def test_visual_cross_probe_captures_result_without_live_altium(monkeypatch):
    from eda_agent.bridge import windows_ui
    from eda_agent.tools import project

    bridge = _Bridge()

    class Inspector:
        def main_window_handle(self):
            return 101

        def capture_window(self, handle, path):
            return {"handle": handle, "path": path, "width": 1200}

        def list_dialogs(self):
            return [{"title": "Compiler", "handle": 202}]

    monkeypatch.setattr(project, "get_bridge", lambda: bridge)
    monkeypatch.setattr(
        windows_ui, "get_altium_ui_inspector", lambda candidate: Inspector()
    )
    mcp = _Mcp()
    project.register_project_tools(mcp)

    result = await mcp.tools["proj_visual_cross_probe"](
        "U7", "D:/captures/U7.bmp", target="pcb", settle_ms=0
    )

    assert bridge.calls == [
        ("project.cross_probe", {"designator": "U7", "target": "pcb"})
    ]
    assert result["capture"]["handle"] == 101
    assert result["dialogs"][0]["handle"] == 202


@pytest.mark.asyncio
async def test_multipart_symbol_assigns_parts_and_shared_pins(monkeypatch):
    from eda_agent.tools import library

    bridge = _Bridge()
    monkeypatch.setattr(library, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    library.register_library_tools(mcp)

    result = await mcp.tools["lib_create_multipart_symbol"](
        "DUAL_AMP",
        parts=[
            {
                "name": "A",
                "left_pins": [{"designator": "2", "name": "INA"}],
                "right_pins": [{"designator": "1", "name": "OUTA"}],
            },
            {
                "name": "B",
                "left_pins": [{"designator": "6", "name": "INB"}],
                "right_pins": [{"designator": "7", "name": "OUTB"}],
            },
        ],
        shared_pins=[
            {"designator": "8", "name": "V+", "x": 0, "y": 400}
        ],
    )

    assert result["part_count"] == 2
    assert bridge.calls[0][1]["part_count"] == "2"
    pins = bridge.calls[1][1]["pins"]
    assert "owner_part_id=1" in pins
    assert "owner_part_id=2" in pins
    assert "owner_part_id=0" in pins
    assert [c for c in bridge.calls if c[0] == "library.set_active_part"] == [
        ("library.set_active_part", {"part_id": 1}),
        ("library.set_active_part", {"part_id": 2}),
        ("library.set_active_part", {"part_id": 1}),
    ]


@pytest.mark.asyncio
async def test_multipart_validates_before_mutating(monkeypatch):
    from eda_agent.tools import library

    bridge = _Bridge()
    monkeypatch.setattr(library, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    library.register_library_tools(mcp)

    result = await mcp.tools["lib_create_multipart_symbol"]("BAD", parts=[{}])
    assert result == {"ok": False, "reason": "part 1 has no pins"}
    assert bridge.calls == []
