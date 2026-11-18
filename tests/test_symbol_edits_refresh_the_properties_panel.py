# SPDX-License-Identifier: Apache-2.0
"""Scripted SchLib edits bring Altium's Properties panel up to date.

Live on Altium 26.11 the panel kept showing the values from before
lib_create_ic_symbol and lib_batch_set_params until the symbol was
selected again by hand. The panel follows the selection, so after those
edits the component announces a modify and is selected again. The Pascal
half is a source guard, as it cannot run here.
"""
from __future__ import annotations

import asyncio
import re

from tests.test_handlers_verify_their_outcome import _body, _decommented, _src
from tests.test_link_footprint_reads_the_link_back import _tools


def _library() -> str:
    return _decommented(_src("Library.pas"))


def _procedure(text: str, name: str) -> str:
    start = text.index(f"Procedure {name}(")
    return text[start:text.index("\nEnd;", start)]


def test_the_refresh_announces_a_modify_and_selects_the_component_again():
    body = _procedure(_library(), "RefreshSchLibPanels")
    assert "SchBeginModify(Comp)" in body and "SchEndModify(Comp)" in body
    assert "GraphicallyInvalidate" in body
    # Off and back on, with the messages processed in between.
    off = body.index("SchLib.CurrentSchComponent := Other")
    back = body.index("SchLib.CurrentSchComponent := Comp")
    assert off < body.index("Application.ProcessMessages", off) < back


def test_batch_set_params_refreshes_after_its_edits():
    body = _body(_library(), "Lib_BatchSetParams")
    end_edit = body.rindex("SchEndEdit(SchLib")
    refresh = body.index("RefreshSchLibPanels(SchLib")
    assert end_edit < refresh < body.index("BuildSuccessResponse", refresh)


def test_the_refresh_command_is_dispatched():
    text = _library()
    assert re.search(r"'refresh_view'\s*:\s*Result := Lib_RefreshView\(", text)
    assert "RefreshSchLibPanels(SchLib, Comp)" in _body(text, "Lib_RefreshView")


class _Bridge:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, dict(params or {})))
        if command == "application.get_active_document":
            return {"file_path": r"C:\lib\Parts.SchLib"}
        if command == "application.get_open_documents":
            return [{"file_path": r"C:\lib\Parts.SchLib"}]
        if command == "library.get_pin_list":
            return {"pins": [{"designator": "1"}, {"designator": "2"}]}
        return {"success": True, "added": 2}


def test_ic_symbol_refreshes_the_panel_after_the_pins(monkeypatch):
    from eda_agent import preferences
    monkeypatch.setattr(preferences, "_read",
                        lambda workspace=None: {"checks_off": "symbol_pins"})
    b = _Bridge()
    out = asyncio.run(_tools(monkeypatch, b)["lib_create_ic_symbol"](
        name="U_TEST", left_pins=[{"designator": "1", "name": "A"}],
        right_pins=[{"designator": "2", "name": "B"}], body="rectangle"))
    names = [c for c, _ in b.calls]
    assert out["panel"] == "refreshed"
    refresh = names.index("library.refresh_view")
    assert names.index("library.add_pins") < refresh
    assert b.calls[refresh][1] == {"component_name": "U_TEST"}
