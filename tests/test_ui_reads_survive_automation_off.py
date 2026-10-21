# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Turning UI input off leaves the UI readable; letter shortcuts are keys.

With EDA_AGENT_UI_AUTOMATION=0 every app_ tool refused, the screenshot
and the dialog list included, so nothing could say why Altium was stuck
(issue #94). docs/ui-automation.md already promised reading stays
available. And send_keys documented "ctrl+s" while its key table had no
letters, so every letter chord was refused.
"""
from __future__ import annotations

import re
from pathlib import Path

from eda_agent.tools import uiauto
from eda_agent.ui import windows

_UIAUTO = Path(uiauto.__file__)

# Tools that only look. Each must pass reads_only=True, unconditionally.
READS = ("app_list_open_dialogs", "app_read_panel", "app_screenshot",
         "app_wait_for_dialog", "app_ui_snapshot", "app_read_window")
# Tools that read in one mode and act in another.
SOMETIMES = {"app_list_panels": "reads_only=not offered_too",
             "app_toolbar": "reads_only=not name",
             "app_dialog_tab": "reads_only=not name"}
# Tools that send input: never reads_only.
ACTS = ("app_send_keys", "app_type_text", "app_click_menu",
        "app_press_dialog_button", "app_canvas", "app_set_dialog_control")


def _body(name: str) -> str:
    text = _UIAUTO.read_text(encoding="utf-8")
    start = text.index(f"async def {name}(")
    nxt = text.find("\n    @mcp.tool()", start)
    return text[start:nxt if nxt > 0 else len(text)]


def test_each_read_tool_asks_as_a_read():
    for name in READS:
        assert "_altium_pid(reads_only=True)" in _body(name), name


def test_a_tool_that_can_act_asks_as_a_read_only_when_it_will_not():
    for name, call in SOMETIMES.items():
        assert f"_altium_pid({call})" in _body(name), name


def test_input_tools_still_honour_the_switch():
    for name in ACTS:
        body = _body(name)
        assert "_altium_pid()" in body and "reads_only" not in body, name


class _Bridge:
    def get_altium_status(self):
        return {"running": True, "pid": 4242}


def test_with_automation_off_a_read_gets_a_pid(monkeypatch):
    monkeypatch.setenv(windows.UI_AUTOMATION_ENV, "0")
    monkeypatch.setattr("eda_agent.bridge.get_bridge", lambda: _Bridge())
    monkeypatch.setattr(windows, "_make_dpi_aware", lambda: None)
    pid, problem = uiauto._altium_pid(reads_only=True)
    assert (pid, problem) == (4242, None)
    pid, problem = uiauto._altium_pid()
    assert pid is None and "disabled" in problem["reason"]


def test_letter_and_digit_chords_have_keys():
    keys = windows._KEYS
    assert keys["s"] == 0x53 and keys["a"] == 0x41 and keys["z"] == 0x5A
    assert keys["0"] == 0x30 and keys["9"] == 0x39
    assert "a-z" in windows._KEYS_HELP


def test_rectangles_are_read_in_physical_pixels():
    """At 200% a DPI-unaware process reads every rectangle at half size,
    and a click aimed by one lands elsewhere."""
    text = Path(windows.__file__).read_text(encoding="utf-8")
    require = text[text.index("def _require():"):text.index("_DPI_DONE = False")]
    assert "_make_dpi_aware()" in require
    assert re.search(r"_win\._make_dpi_aware\(\)", _UIAUTO.read_text(encoding="utf-8"))
