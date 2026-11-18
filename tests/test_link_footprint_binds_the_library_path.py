# SPDX-License-Identifier: Apache-2.0
"""lib_link_footprint stores the full path of the footprint's .PcbLib.

Live on Altium 26.11 the demo project's Footprints.PcbLib shared its name
with a library of another open project. The link left the model's library
location empty, the ECO resolved the other project's library and reported
"Footprint Not Found". The tool also left the PcbLib in front although its
description said the SchLib came forward.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from eda_agent.bridge.exceptions import AltiumError
from tests.test_link_footprint_reads_the_link_back import _tools

_SCH = r"C:\work\demo\Lib\Demo.SchLib"
_MINE = r"C:\work\demo\Lib\Footprints.PcbLib"
_OTHER_SCH = r"C:\work\other\Other.SchLib"
_THEIRS = r"C:\work\other\Footprints.PcbLib"
_ROOT = Path(__file__).resolve().parents[1]


class _Bridge:
    def __init__(self, front=_SCH):
        self.front = front
        self.read: list[str] = []
        self.sent: dict = {}

    async def send_command_async(self, command, params=None, timeout=None):
        if command == "application.get_open_documents":
            return {"documents": [{"file_path": p} for p in
                                  (_THEIRS, _OTHER_SCH, _SCH, _MINE)]}
        if command == "application.get_active_document":
            return {"file_path": self.front}
        if command == "application.set_active_document":
            self.front = params["file_path"]
            return {"success": True}
        if command == "library.get_pad_geometry":
            self.read.append(params["library_path"])
            self.front = params["library_path"]
            return {"name": "FP1", "pads": [{"name": "1"}, {"name": "2"}]}
        if command == "library.get_pin_list":
            return {"pins": [{"designator": "1"}, {"designator": "2"}]}
        if command == "library.link_footprint":
            assert self.front == _SCH, self.front
            self.sent = dict(params)
            return {"success": True, "footprint": "FP1", "library_path": _SCH,
                    "footprint_library_path": params["footprint_library_path"]}
        if command == "library.get_component_details":
            # Something after the link left a PcbLib in front.
            self.front = _MINE
            return {"models": [{"model_name": "FP1", "model_type": "PCBLIB"}]}
        raise AltiumError(command)


def test_the_full_library_path_is_sent_and_the_twin_is_named(monkeypatch):
    b = _Bridge()
    out = asyncio.run(_tools(monkeypatch, b)["lib_link_footprint"](
        component_name="U1", footprint_name="FP1"))
    assert out["success"]
    assert b.sent["footprint_library_path"] == _MINE
    assert b.read == [_MINE]
    assert any(_THEIRS in w for w in out["warnings"])


def test_a_named_library_path_is_the_only_one_read(monkeypatch):
    b = _Bridge()
    out = asyncio.run(_tools(monkeypatch, b)["lib_link_footprint"](
        component_name="U1", footprint_name="FP1", footprint_library=_THEIRS))
    assert out["success"]
    assert b.read == [_THEIRS]
    assert b.sent["footprint_library_path"] == _THEIRS


def test_the_schlib_comes_forward_after_the_link(monkeypatch):
    b = _Bridge()
    asyncio.run(_tools(monkeypatch, b)["lib_link_footprint"](
        component_name="U1", footprint_name="FP1"))
    assert b.front == _SCH


def test_the_pascal_writes_the_path_in_place_not_through_add_datafile_link():
    src = (_ROOT / "scripts" / "altium" / "Library.pas").read_text(encoding="utf-8")
    start = src.index("Function Lib_LinkFootprint(")
    body = src[start:src.index("\nEnd;", start)]
    assert "ExtractJsonValue(Params, 'footprint_library_path')" in body
    assert re.search(r"DatafileLink\[0\]\.Location\s*:=\s*FpLibPath", body)
    # A path given to AddDataFileLink wedges AD26.
    for call in re.findall(r"AddDataFileLink\(([^)]*)\)", body):
        assert "FpLibPath" not in call
    assert '"library_path"' in body and '"footprint_library_path"' in body
