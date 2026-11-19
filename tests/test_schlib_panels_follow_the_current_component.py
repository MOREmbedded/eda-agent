# SPDX-License-Identifier: Apache-2.0
"""The SCH Library list and the Properties panel follow a scripted edit.

Live on Altium 26.11.1 (2026-11-19) setting CurrentSchComponent and sending
the modify broadcast moved the canvas, but the list and the Properties
panel kept the previous component until Tools > Mode was run. A source
guard, as the Pascal cannot run here.
"""
from __future__ import annotations

import re

from tests.test_handlers_verify_their_outcome import _decommented, _src


def _lib(name: str) -> str:
    """One Pascal routine of Library.pas, up to the next routine."""
    text = _src("Library.pas")
    start = re.search(rf"^(?:Function|Procedure) {name}\(", text, re.M).start()
    nxt = re.compile(r"^(?:Function|Procedure) ", re.M).search(text, start + 1)
    return _decommented(text[start:nxt.start() if nxt else len(text)])


def test_the_nudge_runs_the_editors_own_mode_process_and_keeps_the_mode():
    body = _lib("NudgeSchLibPanels")
    assert body.count("RunProcess('Sch:ToggleComponentDisplayMode')") == 3
    assert "'Mode0'" in body and "'Previous'" in body and "'Next'" in body
    assert "Comp.DisplayMode" in body


def test_every_refresh_and_selection_ends_with_the_nudge():
    assert "NudgeSchLibPanels(Comp)" in _lib("RefreshSchLibPanels")
    assert "NudgeSchLibPanels(Component)" in _lib("Lib_SetCurrentComponent")
    assert "NudgeSchLibPanels(SchLib.CurrentSchComponent)" in _lib(
        "Lib_SetComponentDescription")
