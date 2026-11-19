# SPDX-License-Identifier: Apache-2.0
"""lib_reload_library puts the document that was in front back in front.

Live on Altium 26.11 (2026-11-19) a reload with a sheet in front left the
reopened SchLib in front, so the next tool that acts on the focused
document acted on the library.
"""
from __future__ import annotations

import asyncio

from tests.test_link_footprint_reads_the_link_back import _tools

_SHEET = r"C:\work\Sheet.SchDoc"
_LIB = r"C:\work\Parts.SchLib"


class _Bridge:
    def __init__(self, front):
        self.front = front
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params))
        if command == "application.get_active_document":
            return {"file_path": self.front}
        if command == "library.reload_library":
            self.front = _LIB
            return {"reloaded": True, "library_path": _LIB,
                    "active_document_changed": {"from": _SHEET, "to": _LIB}}
        if command == "application.set_active_document":
            self.front = params["file_path"]
            return {"success": True}
        raise AssertionError(command)


def test_the_sheet_in_front_is_focused_again(monkeypatch):
    b = _Bridge(_SHEET)
    out = asyncio.run(_tools(monkeypatch, b)["lib_reload_library"](library_path=_LIB))
    assert out["reloaded"] and out["front_restored"] == _SHEET
    assert "active_document_changed" not in out
    assert b.front == _SHEET


def test_the_library_itself_in_front_stays(monkeypatch):
    b = _Bridge(_LIB)
    out = asyncio.run(_tools(monkeypatch, b)["lib_reload_library"](library_path=_LIB))
    assert out["reloaded"] and "front_restored" not in out
    assert not any(c == "application.set_active_document" for c, _ in b.calls)
