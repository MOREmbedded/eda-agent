# SPDX-License-Identifier: Apache-2.0
"""A board edited by script reads modified from Altium's title.

Measured live on 2026-11-14: after tracks, pads, parts and a dielectric
were placed on BoosterTest.PcbDoc by the tools, application.get_active_
document said "modified": false while the window title read
"BoosterTest.PcbDoc * - Altium Designer Professional (26.11.1)".
"""
from __future__ import annotations

import pytest

from eda_agent.ui import title_state

DIRTY = "BoosterTest.PcbDoc * - Altium Designer Professional (26.11.1)"
CLEAN = "BoosterTest.PcbDoc - Altium Designer Professional (26.11.1)"
PATH = r"D:\x\test-project\BoosterTest.PcbDoc"


def test_the_title_is_read_with_and_without_the_star():
    assert title_state.parse_title(DIRTY) == ("BoosterTest.PcbDoc", True)
    assert title_state.parse_title(CLEAN) == ("BoosterTest.PcbDoc", False)
    assert title_state.parse_title("EDA Agent MCP") is None
    assert title_state.parse_title("") is None


def test_only_the_document_the_title_names_is_answered_for():
    assert title_state.front_document_dirty(PATH, [DIRTY]) is True
    assert title_state.front_document_dirty(PATH, [CLEAN]) is False
    assert title_state.front_document_dirty(r"D:\x\other.PcbDoc", [DIRTY]) is None
    assert title_state.front_document_dirty(PATH, ["EDA Agent MCP"]) is None
    assert title_state.front_document_dirty(PATH.lower(), [DIRTY]) is True


def test_the_title_only_adds_a_modified_it_never_clears_one():
    row = {"file_path": PATH, "modified": False}
    assert title_state.mark_modified(row, [DIRTY]) == {
        **row, "modified": True, "modified_from": "window title"}
    assert title_state.mark_modified(row, [CLEAN]) == row
    already = {"file_path": PATH, "modified": True}
    assert title_state.mark_modified(already, [CLEAN]) == already
    assert title_state.mark_modified({}, [DIRTY]) == {}


class _DummyMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class _Bridge:
    def __init__(self, replies):
        self.calls, self.replies = [], replies

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return self.replies.get(command, {"success": True})


def _tools(monkeypatch, replies, titles):
    from eda_agent.tools import application
    bridge = _Bridge(replies)
    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    monkeypatch.setattr(title_state, "_titles", lambda: titles)
    mcp = _DummyMcp()
    application.register_application_tools(mcp)
    return mcp.tools, bridge


@pytest.mark.asyncio
async def test_the_active_document_reads_modified_from_the_title(monkeypatch):
    doc = {"file_name": "BoosterTest.PcbDoc", "file_path": PATH, "modified": False}
    tools, _ = _tools(monkeypatch, {"application.get_active_document": doc}, [DIRTY])
    out = await tools["app_get_active_document"]()
    assert out["modified"] is True and out["modified_from"] == "window title"


@pytest.mark.asyncio
async def test_the_document_list_marks_only_the_document_in_front(monkeypatch):
    rows = [{"file_name": "BoosterTest.PcbDoc", "file_path": PATH, "modified": False},
            {"file_name": "other.PcbDoc", "file_path": r"D:\x\other.PcbDoc", "modified": False}]
    tools, _ = _tools(monkeypatch, {"application.get_open_documents": rows}, [DIRTY])
    out = await tools["app_list_documents"]()
    assert [r["modified"] for r in out] == [True, False]


@pytest.mark.asyncio
async def test_closing_an_edited_board_is_refused_before_the_save_prompt(monkeypatch):
    tools, bridge = _tools(monkeypatch, {}, [DIRTY])
    out = await tools["app_close_document"](PATH)
    assert out["closed"] is False and "discard_changes" in out["error"]
    assert bridge.calls == [], "nothing may reach Altium, or its modal save prompt opens"
    out = await tools["app_close_document"](PATH, discard_changes=True)
    assert bridge.calls[-1] == ("application.close_document",
                                {"file_path": PATH, "discard": "true"})


@pytest.mark.asyncio
async def test_a_clean_title_lets_the_close_through(monkeypatch):
    tools, bridge = _tools(monkeypatch, {}, [CLEAN])
    await tools["app_close_document"](PATH)
    assert bridge.calls[-1] == ("application.close_document", {"file_path": PATH})
