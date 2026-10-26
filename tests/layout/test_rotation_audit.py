# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A part facing the wrong way is told which quarter turn shortens it.

R1 sits between U1 pin A (left) and U1 pin B (right) with its pads the
wrong way round, so both of its connections cross over it. Turned 180
degrees, each pad faces the pin it connects to.
"""
from __future__ import annotations

from eda_agent.layout.audit import CHECKS, run_audits
from eda_agent.layout.model import BOTTOM, Component, LayoutBoard, Pad
from eda_agent.layout.rotation_audit import rotation_audit


def _rect(cx, cy, w, h):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
            (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def _board(r1_side="top", neighbour=False):
    comps = [
        Component(ref="U1", x=0, y=0, courtyard=_rect(0, -300, 200, 100),
                  courtyard_source="courtyard"),
        Component(ref="R1", x=0, y=0, side=r1_side,
                  courtyard=_rect(0, 0, 160, 60),
                  courtyard_source="courtyard"),
    ]
    if neighbour:
        # Sits across R1's turned body only if R1 were turned 90 degrees.
        comps.append(Component(ref="C9", x=0, y=110,
                               courtyard=_rect(0, 110, 40, 60),
                               courtyard_source="courtyard"))
    pads = [
        Pad(comp="U1", name="A", x=-400, y=0, net="SIG_A"),
        Pad(comp="U1", name="B", x=400, y=0, net="SIG_B"),
        # Backwards: pad 1 (SIG_A, which is on the LEFT) sits on the right.
        Pad(comp="R1", name="1", x=50, y=0, net="SIG_A"),
        Pad(comp="R1", name="2", x=-50, y=0, net="SIG_B"),
    ]
    if neighbour:
        pads += [Pad(comp="C9", name="1", x=0, y=90, net="N9"),
                 Pad(comp="C9", name="2", x=0, y=130, net="N9b")]
    return LayoutBoard(name="t", components=comps, pads=pads)


def test_a_backwards_part_is_told_to_turn_half_way():
    out = rotation_audit(_board())
    r1 = next(s for s in out["suggestions"] if s["ref"] == "R1")
    assert r1["turn_by"] == 180.0
    assert r1["mils_saved"] == 200.0          # 450 + 450 down to 350 + 350
    assert r1["new_rotation"] == 180.0
    assert set(r1["nets"]) == {"SIG_A", "SIG_B"}


def test_a_turn_into_a_neighbour_is_not_suggested():
    """A neighbour above R1 only rules out a turn that reaches it."""
    out = rotation_audit(_board(neighbour=True))
    r1 = next(s for s in out["suggestions"] if s["ref"] == "R1")
    assert r1["turn_by"] == 180.0, "180 keeps R1's body where it was"


def test_a_part_already_facing_right_is_left_alone():
    board = _board()
    board.pads[2].x, board.pads[3].x = -50, 50
    out = rotation_audit(board)
    assert not [s for s in out["suggestions"] if s["ref"] == "R1"]


def test_a_bottom_part_is_scored_and_given_a_rotation_value():
    """Measured, not assumed.

    The rotation value was withheld on the bottom side while it was
    unknown how a mirrored part's angle is counted. Measured against
    Altium 26.11.1.5 on 2026-10-25: four parts flipped with
    `pcb_flip_component` and read back turn their pads in world
    coordinates by exactly the delta applied to `Rotation`, the same as a
    top-side part, at 45 degrees as well as at right angles. So the same
    arithmetic holds and the value can be given on either side.
    """
    top = rotation_audit(_board())
    bottom = rotation_audit(_board(r1_side=BOTTOM))
    t1 = next(s for s in top["suggestions"] if s["ref"] == "R1")
    r1 = next(s for s in bottom["suggestions"] if s["ref"] == "R1")

    assert r1["turn_by"] == 180.0
    assert r1["new_rotation"] == t1["new_rotation"]
    assert r1["side"] == BOTTOM


def test_a_locked_part_is_not_touched():
    board = _board()
    board.components[1].locked = True
    out = rotation_audit(board)
    assert not [s for s in out["suggestions"] if s["ref"] == "R1"]
    # U1 is free, and turning IT shortens the same two connections.
    assert [s["ref"] for s in out["suggestions"]] == ["U1"]


def test_it_runs_as_an_advisory_layout_check():
    assert "rotation_audit" in CHECKS
    out = run_audits(_board(), ["rotation_audit"])
    assert out["pass"] and out["summary"]["rotation_audit"]["suggested"] >= 1
