# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Placing a part: side first, then rotation, then the move, then read back.

Measured 2026-10-23 on a 529-part board against the pads read back: a side
change mirrors a part about its bounding box and negates a rotation set
before it (bottom parts at 90/270 came out 180 off, a PowerPAK moved
29 mil), a rotation moves an asymmetric part's origin (an SO8 by 23 mil),
and a part already on the bottom was flipped again.
"""
from __future__ import annotations

from pathlib import Path

from tests.pascal_source import load

_ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _pcb():
    return load(_ALTIUM / "PCB.pas")


def test_import_flips_then_turns_then_moves():
    body = _pcb()["PCB_ImportPlacement"]
    flip = body.index("Comp.Layer := TargetLayer;")
    turn = body.index("If RotStr <> '' Then Comp.Rotation := StrToFloatDef(RotStr, 0);")
    move = body.index("Comp.MoveByXY(DeltaX, DeltaY);")
    assert flip < turn < move
    # The delta is taken after the flip and the turn, from where they left it.
    assert turn < body.index("DeltaX := WantX - Comp.x;") < move


def test_a_part_given_no_position_keeps_its_origin():
    body = _pcb()["PCB_ImportPlacement"]
    keep = body.index("WantX := Comp.x;")
    assert keep < body.index("If XStr <> '' Then WantX := MilsToCoordF(StrToFloatDef(XStr, 0));")
    assert keep < body.index("Comp.Layer := TargetLayer;")


def test_the_side_is_compared_as_top_or_bottom():
    body = _pcb()["PCB_ImportPlacement"]
    assert "If Pos('BOTTOM', UpperCase(LayerStr)) > 0 Then TargetLayer := eBottomLayer" in body
    assert "If (TargetLayer <> eNoLayer) And (Comp.Layer <> TargetLayer) Then" in body


def test_every_part_is_read_back():
    body = _pcb()["PCB_ImportPlacement"]
    back = body[body.index("Bad := '';"):]
    assert "(Abs(Comp.x - WantX) > 1) Or (Abs(Comp.y - WantY) > 1)" in back
    assert "SameAngle(Comp.Rotation, StrToFloatDef(RotStr, 0))" in back
    assert '"mismatched":' in body and '"mismatches":[' in body


def test_the_single_move_turns_before_it_moves():
    pcb = _pcb()
    body = pcb["PCB_MoveComponent"] if "PCB_MoveComponent" in pcb else None
    if body is None:
        text = (_ALTIUM / "PCB.pas").read_text(encoding="utf-8")
        i = text.index("If HasRot Then Comp.Rotation := NewRot;")
        body = text[i - 3000:i + 1500]
    turn = body.index("If HasRot Then Comp.Rotation := NewRot;")
    move = body.index("Comp.MoveByXY(DeltaX, DeltaY);", turn)
    assert turn < move


def test_a_new_part_gets_its_origin_last():
    text = (_ALTIUM / "PCB.pas").read_text(encoding="utf-8")
    # Two placement sites, and they no longer name the same layer. The
    # SINGLE placement commits its own transaction, so assigning the
    # caller's side there mirrors the primitives. The BATCH shares one
    # PreProcess/PostProcess across the whole loop, where the same
    # assignment sets the field and leaves the pads in top-side geometry
    # (measured on AD 26.11.1.5, 2026-10-25), so it loads on the top
    # layer and the tool flips afterwards. Either way the origin is
    # assigned after the turn, which is what this test is about.
    # Only the placement sites: PCB_FlipComponent also assigns a layer,
    # but it turns nothing and has no origin to set.
    sites = [i for i in range(len(text))
             if (text.startswith("Comp.Layer := CompLayer;", i)
                 or text.startswith("Comp.Layer := eTopLayer;", i))
             and "Comp.Rotation := Rotation;" in text[i:i + 300]]
    assert len(sites) == 2, f"expected two placement sites, found {len(sites)}"
    for start in sites:
        chunk = text[start:start + 300]
        assert chunk.index("Comp.Rotation := Rotation;") < chunk.index("Comp.x := MilsToCoordF(X);")
