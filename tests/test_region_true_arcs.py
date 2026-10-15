# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A footprint region can hold a true arc.

lib_add_footprint_regions drew straight edges only, so a slot end or a
rounded paste window had to be sent as a run of short lines. A point given
as [x, y, sweep] now makes the edge to the next point an arc, sent as its
circle and Altium's angles and built from shape segments, which keep it.
"""

from __future__ import annotations

import math

import pytest

from eda_agent.tools.library import _edge_arc, _regions_payload
from tests.test_footprint_from_package_tools import _pascal_routine
from tests.test_handlers_verify_their_outcome import _decommented


def _on_circle(x, y, cx, cy, r):
    return math.isclose(math.hypot(x - cx, y - cy), r, rel_tol=1e-9)


def _ccw_span(a1, a2):
    return (a2 - a1) % 360


@pytest.mark.parametrize("sweep", [90, -90, 180, -180, 270, -270, 30, -330])
def test_the_arc_runs_from_start_to_end_through_the_sweep(sweep):
    x1, y1, x2, y2 = 1.0, 2.0, 5.0, 3.0
    cx, cy, r, a1, a2 = _edge_arc(x1, y1, x2, y2, sweep)
    assert _on_circle(x1, y1, cx, cy, r) and _on_circle(x2, y2, cx, cy, r)
    assert math.isclose(_ccw_span(a1, a2), abs(sweep), abs_tol=1e-9)
    # Altium's angles run counterclockwise, so a clockwise edge starts at a2.
    start, end = (a1, a2) if sweep > 0 else (a2, a1)
    assert math.isclose(cx + r * math.cos(math.radians(start)), x1, abs_tol=1e-9)
    assert math.isclose(cy + r * math.sin(math.radians(start)), y1, abs_tol=1e-9)
    assert math.isclose(cx + r * math.cos(math.radians(end)), x2, abs_tol=1e-9)
    assert math.isclose(cy + r * math.sin(math.radians(end)), y2, abs_tol=1e-9)


def test_a_positive_sweep_turns_counterclockwise():
    # From (0,0) to (2,0) counterclockwise by 90: the centre is above.
    cx, cy, r, a1, a2 = _edge_arc(0, 0, 2, 0, 90)
    assert (round(cx, 9), round(cy, 9)) == (1, 1)
    assert (round(a1, 9), round(a2, 9)) == (225, 315)
    cx, cy, *_ = _edge_arc(0, 0, 2, 0, -90)
    assert (round(cx, 9), round(cy, 9)) == (1, -1)


@pytest.mark.parametrize("sweep", [360, -360, 400, float("nan"), float("inf")])
def test_a_sweep_that_is_not_an_arc_is_refused(sweep):
    payload, problems = _regions_payload(
        [{"layer": "TopPaste", "points": [[0, 0, sweep], [1, 0], [0, 1]]}])
    assert payload == "" and "sweep" in problems[0]


def test_an_arc_with_no_length_is_refused():
    payload, problems = _regions_payload(
        [{"layer": "TopPaste", "points": [[0, 0, 90], [0, 0], [1, 1]]}])
    assert payload == "" and "apart" in problems[0]


def test_a_slot_goes_out_with_both_ends_as_arcs():
    payload, problems = _regions_payload([{"layer": "TopPaste", "points": [
        [0, 0], [4, 0, 180], [4, 2], [0, 2, 180]]}])
    assert not problems
    op = dict(f.split("=", 1) for f in payload.split(";"))
    assert op["points"] == "0,0/4,0/4,2/0,2"
    # 1-based, so index 2 is the edge leaving the second point.
    assert op["arcs"] == "2,4,1,1,270,90/4,0,1,1,90,270"


def test_the_closing_edge_can_be_an_arc():
    payload, _ = _regions_payload([{"layer": "Mechanical1", "points": [
        [0, 0], [2, 0], [2, 2, 90]]}])
    op = dict(f.split("=", 1) for f in payload.split(";"))
    index, cx, cy, r, a1, a2 = op["arcs"].split(",")
    assert index == "3"
    assert _on_circle(2, 2, float(cx), float(cy), float(r))
    assert _on_circle(0, 0, float(cx), float(cy), float(r))


def test_a_region_of_straight_edges_sends_no_arcs_field():
    payload, _ = _regions_payload([{"layer": "TopPaste", "points": [
        [0, 0], [1, 0, 0], [1, 1], [0, 1]]}])
    assert "arcs=" not in payload


def test_the_handler_builds_arcs_from_shape_segments():
    body = _decommented(_pascal_routine("Lib_AddFootprintRegions"))
    assert "GetBatchField(Op, 'arcs')" in body
    # Arcs go through the shape, which keeps them; straight regions keep
    # the contour route they had.
    shape = body[body.index("Seg := TPolySegment;"):]
    assert "Seg.Kind := ePolySegmentArc;" in shape
    assert "Region.ShapeSegments[I - 1] := Seg;" in shape
    assert "Region.UpdateContourFromShape(True);" in shape
    assert body.index("Region.SetOutlineContour(Contour);") < body.index(
        "Seg := TPolySegment;")
    # The arc fields are read with the same 1-based index as the vertex.
    for k, field in enumerate(["Seg.cx", "Seg.cy", "Seg.Radius",
                               "Seg.Angle1", "Seg.Angle2"]):
        line = shape[shape.index(field + " :="):]
        assert f"RegionArcField(ArcsStr, I, {k})" in line[:200], field
    # A malformed arc refuses the region instead of drawing a zero centre.
    assert "IsFloatStr(RegionArcField(ArcsStr, I, K))" in body
    assert '"arc_edges":' in body


def test_the_reader_reports_arc_edges():
    body = _decommented(_pascal_routine("Lib_GetFootprintPrimitives"))
    assert "Region.ShapeSegments[SegI].Kind = ePolySegmentArc" in body
    assert '"arc_edges":' in body
