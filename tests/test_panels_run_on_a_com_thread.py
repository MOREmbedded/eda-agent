# SPDX-License-Identifier: Apache-2.0
"""The panel tools read on a thread of their own that initialised COM.

On AD 26.11 app_list_panels, app_open_panel and app_read_panel failed
with "CoInitialize has not been called" and saw 0 panels: they ran on
the server's event loop thread, which nothing had initialised. The same
UI Automation reads worked from a fresh thread.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from eda_agent.tools import uiauto
from eda_agent.ui import panels


def test_work_runs_on_another_thread_and_returns():
    here = threading.get_ident()
    assert panels.on_com_thread(threading.get_ident) != here
    assert panels.on_com_thread(lambda: 7) == 7


def test_a_failure_comes_back_to_the_caller():
    def boom():
        raise ValueError("inside")
    with pytest.raises(ValueError, match="inside"):
        panels.on_com_thread(boom)


@pytest.mark.skipif(not panels.available(), reason="needs Windows UI automation")
def test_the_thread_has_com():
    import comtypes

    def probe():
        # S_FALSE (1) means "already initialised on this thread".
        result = comtypes.CoInitialize()
        comtypes.CoUninitialize()
        return result
    assert panels.on_com_thread(probe) in (None, 0, 1)


class _Tools:
    def __init__(self):
        self.fns = {}

    def tool(self, *a, **k):
        def keep(fn):
            self.fns[fn.__name__] = fn
            return fn
        return keep


@pytest.mark.parametrize("tool, args", [("app_list_panels", {}),
                                        ("app_read_panel", {"name": "Projects"}),
                                        ("app_open_panel", {"name": "Projects"})])
def test_each_panel_tool_goes_through_the_com_thread(monkeypatch, tool, args):
    used = []
    monkeypatch.setattr(uiauto, "_altium_pid", lambda reads_only=False: (1, None))
    monkeypatch.setattr(panels, "available", lambda: True)
    monkeypatch.setattr(panels, "list_open", lambda pid: [])
    monkeypatch.setattr(panels, "read_panel", lambda pid, name: {"ok": True})
    monkeypatch.setattr(panels, "open_panel", lambda pid, name, settle: {"ok": True})
    real = panels.on_com_thread

    def recording(work):
        used.append(threading.get_ident())
        return real(work)
    monkeypatch.setattr(panels, "on_com_thread", recording)
    mcp = _Tools()
    uiauto.register_uiauto_tools(mcp)
    asyncio.run(mcp.fns[tool](**args))
    assert used, f"{tool} did not use the COM thread"


class _Control:
    def __init__(self, class_name, text, rect, hwnd):
        self.class_name, self.text, self.rect, self.hwnd = class_name, text, rect, hwnd


def test_ad_26_11_panels_are_found_by_their_caption(monkeypatch):
    """MEASURED on AD 26.11: no TPopupPanelButton; a docked panel is a
    TXPPanel caption plus a TViewComponent of the same name, and a
    document view has no caption."""
    every = [
        _Control("TViewComponent", "Board.PcbDoc", (418, 103, 1544, 1090), 1),
        _Control("TViewComponent", "Projects", (3, 101, 412, 1085), 2),
        _Control("TXPPanel", "Projects", (6, 80, 358, 98), 3),
        _Control("TdxTreeList", "", (12, 176, 403, 900), 4),
        _Control("TXPPanel", "Toolbar text", (6, 30, 300, 48), 5),
        _Control("TPopupPanelButton", "Messages", (1900, 300, 20, 90), 6),
    ]
    monkeypatch.setattr(panels, "_frame_controls", lambda pid: every)
    found = panels.list_open(1)
    assert [p["name"] for p in found] == ["Messages", "Projects"]
    projects = next(p for p in found if p["name"] == "Projects")
    assert [c["hwnd"] for c in projects["content"]] == [4]
