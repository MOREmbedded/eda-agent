# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A board object moved by script is found where it is drawn.

Measured on AD 26.10 with a spatial iterator on a scratch board: a text
moved by obj_modify X/Y, and a 3D body moved into place by
pcb_place_3d_body, were drawn at the new position while the board's
spatial index still held the old one. The mouse picks through that index,
so the text could be seen and not selected (#39.14). Inside the object's
BeginModify/EndModify both were found at the new position and no longer
at the old one.
"""

from __future__ import annotations

import re
from pathlib import Path

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _body(file: str, name: str) -> str:
    text = (ALTIUM / file).read_text(encoding="utf-8-sig")
    start = re.search(rf"^(Function|Procedure) {name}\(", text, re.M).start()
    return text[start:text.index("\nEnd;\n", start)]


def test_obj_modify_writes_inside_the_objects_modify_bracket():
    body = _body("PCBGeneric.pas", "ProcessPCBBoardObjects")
    apply = body.index("ApplySetPropertiesPCB(FoundObj, SetStr)")
    assert body.rindex("FoundObj.BeginModify;", 0, apply) < apply
    assert apply < body.index("FoundObj.EndModify;", apply)


def test_a_placed_body_is_moved_inside_its_modify_bracket():
    body = _body("PCB.pas", "PCB_Place3DBody")
    move = body.index("Body.MoveByXY(")
    assert body.index("Board.AddPCBObject(Body)") < body.rindex("Body.BeginModify;", 0, move)
    assert move < body.index("Body.EndModify;", move)
