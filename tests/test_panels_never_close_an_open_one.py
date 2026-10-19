# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""app_list_panels answers, and app_open_panel never leaves a panel closed.

MEASURED on AD 26.10.1.6: app_list_panels raised KeyError 'holder_class'
on every call with a panel open, because the scan no longer returns that
key. Reported on AD 25: the scan found no panel while Properties was open,
and app_open_panel clicked the View > Panels entry, which is a toggle, and
closed it. Nothing here touches Altium: the scan and the menu are fakes.
"""

from __future__ import annotations

import asyncio

import pytest

from eda_agent.ui import panels


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(monkeypatch):
    from eda_agent.tools import uiauto
    monkeypatch.setattr(uiauto, "_altium_pid", lambda: (4242, None))
    mcp = _FakeMcp()
    uiauto.register_uiauto_tools(mcp)
    return mcp.tools


#: Exactly the keys panels.list_open builds.
SCANNED = {"name": "Properties", "button_hwnd": 77, "rect": [10, 20, 30, 400],
           "content": [{"hwnd": 5, "class": "TdxTreeList", "rect": [0, 0, 1, 1]}]}


def test_list_panels_reports_what_the_scan_returns(monkeypatch):
    tools = _tools(monkeypatch)
    monkeypatch.setattr(panels, "available", lambda: True)
    monkeypatch.setattr(panels, "list_open", lambda pid: [dict(SCANNED)])
    out = asyncio.run(tools["app_list_panels"]())
    assert out["ok"] is True and out["count"] == 1
    assert out["panels"] == [{"name": "Properties", "rect": [10, 20, 30, 400],
                              "button_hwnd": 77, "content_count": 1}]


def test_an_empty_scan_says_what_it_does_not_prove(monkeypatch):
    tools = _tools(monkeypatch)
    monkeypatch.setattr(panels, "available", lambda: True)
    monkeypatch.setattr(panels, "list_open", lambda pid: [])
    out = asyncio.run(tools["app_list_panels"]())
    assert out["ok"] is True and out["count"] == 0
    assert "does not prove" in out["note"]


class _Toggle:
    """A View > Panels entry and a scan that may or may not see the panel."""

    def __init__(self, open_=False, visible=True, second_click_ok=True):
        self.open = open_
        self.visible = visible
        self.second_click_ok = second_click_ok
        self.clicks = []

    def install(self, monkeypatch):
        monkeypatch.setattr(panels, "available", lambda: True)
        monkeypatch.setattr(panels.win, "wait_until",
                            lambda check, timeout, poll=0.02: check())

        def find(pid, name):
            if self.open and self.visible:
                return {"name": name, "rect": [1, 2, 3, 4]}
            return None

        def click_path(pid, path, settle=1.2, **kw):
            self.clicks.append(path)
            if len(self.clicks) > 1 and not self.second_click_ok:
                return {"ok": False, "reason": "menu bar unavailable"}
            self.open = not self.open
            return {"ok": True, "path": path}

        monkeypatch.setattr(panels, "find", find)
        monkeypatch.setattr(panels.menu, "click_path", click_path)


def test_an_open_panel_that_is_seen_is_not_clicked(monkeypatch):
    t = _Toggle(open_=True)
    t.install(monkeypatch)
    out = panels.open_panel(1, "Properties")
    assert out["ok"] is True and out["opened"] is False
    assert t.clicks == []


def test_a_closed_panel_is_opened_and_confirmed(monkeypatch):
    t = _Toggle(open_=False)
    t.install(monkeypatch)
    out = panels.open_panel(1, "Properties")
    assert out["ok"] is True and out["opened"] is True
    assert t.clicks == ["View|Panels|Properties"]


def test_an_open_panel_the_scan_cannot_see_is_left_open(monkeypatch):
    """The reported case: open, no tab the scan reads, so find misses."""
    t = _Toggle(open_=True, visible=False)
    t.install(monkeypatch)
    out = panels.open_panel(1, "Properties")
    assert t.open is True, "the user's panel was left closed"
    assert out["ok"] is False and out["toggled_back"] is True
    assert len(t.clicks) == 2


def test_a_failed_toggle_back_is_reported(monkeypatch):
    t = _Toggle(open_=True, visible=False, second_click_ok=False)
    t.install(monkeypatch)
    out = panels.open_panel(1, "Properties")
    assert out["ok"] is False and out["toggled_back"] is False
    assert "may now be closed" in out["reason"]


@pytest.mark.parametrize("panel", [dict(SCANNED), {"name": "Messages"}])
def test_describe_never_raises_on_a_missing_key(panel):
    out = panels.describe(panel)
    assert set(out) == {"name", "rect", "button_hwnd", "content_count"}
