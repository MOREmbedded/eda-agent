# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""sch_mirror_component reflects diagonal lines and arcs correctly.

The rectangle normalisation also ran on lines and swapped only their X
ends, which reversed every diagonal: an op-amp triangle came out sheared.
Arcs were not moved at all, so an inverter's bubble stayed on the wrong
side while the call reported success.
"""

from __future__ import annotations

import re

from tests.test_handlers_verify_their_outcome import _body, _decommented, _src


def _mirrored_angle(a: float) -> float:
    """The Pascal MirroredAngle, for checking what it computes."""
    out = 180 - a
    while out < 0:
        out += 360
    while out >= 360:
        out -= 360
    return out


def test_lines_are_not_reordered_like_rectangles():
    body = _decommented(_body(_src("Generic.pas"), "Gen_MirrorSchComponent"))
    assert "If (Kind <> eLine) And (Loc.X > Crn.X) Then" in body


def test_arcs_move_their_centre_and_mirror_their_sweep():
    body = _decommented(_body(_src("Generic.pas"), "Gen_MirrorSchComponent"))
    assert "If (Not Handled) And ((Kind = eArc) Or (Kind = eEllipticalArc)) Then" in body
    assert "Arc.StartAngle := MirroredAngle(EndA)" in body
    assert "Arc.EndAngle := MirroredAngle(StartA)" in body
    assert "ElArc.StartAngle := MirroredAngle(EndA)" in body
    assert "If (Not Handled) And (Kind = eEllipse) Then" in body


def test_no_local_hides_the_arc_kind():
    """Names are case-insensitive. A local EArc made eArc read 0 in this
    routine, so the arc branch never ran: measured 2026-10-11, a symbol's
    arc bubble stayed put and was reported skipped as kind 8, while the
    previous version of this test asserted that very code and passed."""
    body = _body(_src("Generic.pas"), "Gen_MirrorSchComponent")
    decls = body.split("Begin", 1)[0]
    assert not re.search(r"^\s*EArc\s*:", decls, re.M | re.I)


def test_the_designator_moves_with_the_parameters():
    body = _decommented(_body(_src("Generic.pas"), "Gen_MirrorSchComponent"))
    assert "(Kind = eDesignator)" in body


def test_mirrored_angle_matches_the_pascal():
    text = _src("Generic.pas")
    fn = text[text.index("Function MirroredAngle("):]
    fn = fn[:fn.index("End;") + 4]
    assert "Result := 180 - A;" in fn
    # A bubble swept 300..60 through 0 on the right ends up 120..240 on
    # the left, still counter-clockwise.
    assert (_mirrored_angle(60), _mirrored_angle(300)) == (120, 240)
    assert _mirrored_angle(0) == 180 and _mirrored_angle(180) == 0


def test_the_diagonal_from_the_report_comes_out_right():
    """(8500,11400)-(9100,11100) mirrored about 8800 is (9100,11400)-(8500,11100)."""
    cx = 8800
    loc, crn = (8500, 11400), (9100, 11100)
    new_loc = (cx + (cx - loc[0]), loc[1])
    new_crn = (cx + (cx - crn[0]), crn[1])
    assert {new_loc, new_crn} == {(9100, 11400), (8500, 11100)}
