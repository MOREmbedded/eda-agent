# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Closing a project none of whose documents is loaded.

MEASURED 2026-11-18 on AD 26.11.1: proj_close of such a project stopped
Altium's script engine with an access violation in its script engine,
and the loop did not come back. The close handler loaded a member itself,
inside its walk of the project's documents. It no longer loads anything:
it refuses with ``no_loaded_documents``, and the tool loads the sheets in
a command of their own before closing again.
"""
from __future__ import annotations

import asyncio

from eda_agent.tools import project

PRJ = r"C:\work\Scratch\Scratch.PrjPcb"
SCH = r"C:\work\Scratch\Sheet1.SchDoc"


class _Bridge:
    def __init__(self, closes, loaded=1):
        self.sent = []
        self._closes = list(closes)
        self._loaded = loaded

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append(command)
        if command == "project.get_documents":
            return [{"file_path": SCH}]
        if command == "project.load_project_sheets":
            return {"total_sheets": 1, "loaded": self._loaded,
                    "already_loaded": 0, "failed": 0}
        if command == "project.close":
            return self._closes.pop(0)
        raise AssertionError(command)


def _close(monkeypatch, bridge, **kwargs):
    monkeypatch.setattr(project, "get_bridge", lambda: bridge)
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    project.register_project_tools(_Mcp())
    return asyncio.run(captured["proj_close"](**kwargs))


REFUSED = {"success": False, "closed": False, "project_path": PRJ,
           "discarded": [], "attempts": 0, "closed_instead": [],
           "no_loaded_documents": True, "reason": "not focused"}
CLOSED = {"success": True, "closed": True, "project_path": PRJ, "saved": True,
          "discarded": [], "attempts": 1, "closed_instead": []}


def test_the_sheets_are_loaded_by_their_own_call_then_the_close_is_sent_again(monkeypatch):
    bridge = _Bridge([REFUSED, CLOSED])
    out = _close(monkeypatch, bridge, project_path=PRJ)
    assert bridge.sent == ["project.get_documents", "project.close",
                           "project.load_project_sheets", "project.close"]
    assert out["closed"] is True and out["loaded_sheets_to_close"] == 1


def test_nothing_to_load_is_refused_without_a_second_close(monkeypatch):
    bridge = _Bridge([REFUSED], loaded=0)
    out = _close(monkeypatch, bridge, project_path=PRJ)
    assert bridge.sent.count("project.close") == 1
    assert out["closed"] is False and "app_open_document" in out["reason"]


def test_a_project_refused_for_another_reason_is_not_loaded(monkeypatch):
    other = {**REFUSED, "no_loaded_documents": False}
    bridge = _Bridge([other])
    out = _close(monkeypatch, bridge, project_path=PRJ)
    assert "project.load_project_sheets" not in bridge.sent
    assert out["closed"] is False
