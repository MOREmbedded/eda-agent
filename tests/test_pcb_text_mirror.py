# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A free PCB text can be mirrored and un-mirrored through the generic layer.

Requested for size labels drawn mirrored on mechanical layers: obj_query did
not list MirrorFlag and obj_modify had no branch for it, so the only route
was delete and re-place, which loses the font and the line breaks. Altium
flips a text about its anchor, so the write moves it back by the shift of
its bounding box and the text stays where it was.
"""

from __future__ import annotations

import re
from pathlib import Path

PCBGENERIC = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "PCBGeneric.pas"


def _code() -> str:
    return PCBGENERIC.read_text(encoding="ascii")


def _body(code: str, header: str) -> str:
    start = code.index(header)
    return code[start:start + code[start:].index("\nEnd;")]


def test_mirror_flag_is_read_for_a_text():
    body = _body(_code(), "Function GetPCBProperty(")
    m = re.search(r"PropName = 'MirrorFlag' Then\s+Begin\s+(.*?)\s+End", body, re.DOTALL)
    assert m and "Oid = eTextObject" in m.group(1) and "Txt.MirrorFlag" in m.group(1)


def test_mirror_flag_is_a_known_and_advertised_property():
    code = _code()
    assert "(PropName = 'MirrorFlag')" in _body(code, "Function IsKnownPCBProperty(")
    assert re.search(r"'Height, StrokeWidth, IsDesignator, IsComment, UseTTFonts, MirrorFlag'", code)


def test_mirror_flag_is_written_in_place_and_only_on_a_text():
    body = _body(_code(), "Function SetPCBProperty(")
    start = body.index("PropName = 'MirrorFlag'")
    branch = body[start:start + body[start:].index("\n        Else If PropName")]
    assert "If Oid <> eTextObject Then\n                Matched := False" in branch
    assert "Refused := True" in branch            # anything but true/false
    assert "SetTextMirrorInPlace(Txt," in branch
    assert "Txt.MirrorFlag :=" not in branch      # never the bare flip


def test_the_text_is_moved_back_by_its_bounding_box_shift():
    body = _body(_code(), "Procedure SetTextMirrorInPlace(")
    before = body.index("Before := Txt.BoundingRectangle;")
    flip = body.index("Txt.MirrorFlag := Mirror;")
    after = body.index("After := Txt.BoundingRectangle;")
    move = body.index("Txt.MoveByXY(Before.Left - After.Left, Before.Bottom - After.Bottom);")
    assert before < flip < after < move
    assert body.index("Txt.BeginModify;") < flip < body.index("Txt.EndModify;") < after
