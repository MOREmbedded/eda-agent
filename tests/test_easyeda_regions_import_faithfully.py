# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EasyEDA regions reach Altium as the source draws them (#39.22).

A field report imported a part and got Mechanical1 primitives at
(-39897, 30000) mils and silkscreen circles over the pads. Both came from
SOLIDREGION handling. Its SVG path was read as a point list, so an arc's
radius and flags became coordinates: 10.3 and 0, moved to the origin at
canvas (4000, 3000) in 10 mil units, are exactly -39897 and 30000. And
every region was drawn as an outline through the TopOverlay fallback,
including the per-pad cutouts on layers 99 and 100, which became rings
over the pads. The KiCad emitter already skipped those.
"""

from __future__ import annotations

import json
from pathlib import Path

from eda_agent.libimport.easyeda import parse_component
from eda_agent.libimport.easyeda.altium import build_altium_plan
from eda_agent.libimport.easyeda.shapes import (
    EePolyline, parse_footprint_shapes, parse_path_points)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "easyeda_soic8.json"


def test_an_arc_in_a_region_path_is_followed_not_read_as_points():
    pts = parse_path_points(
        "M 4010 3000 L 4020 3000 A 10.3 10.3 0 0 1 4030.3 3010.3 L 4010 3010 Z")
    assert all(4009 <= x <= 4031 and 2999 <= y <= 3011 for x, y in pts), pts
    assert (4030.3, 3010.3) in pts
    assert len(pts) > 4, "the arc was not followed"


def test_relative_and_plain_point_lists_still_read():
    assert parse_path_points("M 4000 3000 h 10 v 10 h -10 z") == [
        (4000.0, 3000.0), (4010.0, 3000.0), (4010.0, 3010.0), (4000.0, 3010.0)]
    assert parse_path_points("4000 3000 4010 3000") == [(4000.0, 3000.0), (4010.0, 3000.0)]


def test_a_solidregion_record_uses_the_path_reader():
    [region] = parse_footprint_shapes(
        "SOLIDREGION~1~~M 4000 3000 L 4010 3000 A 5 5 0 0 1 4015 3005 L 4000 3005 Z~solid~gge1~~0")
    assert all(x >= 3999 for x, _ in region.points), region.points


def _plan_with(*regions):
    comp = parse_component(json.loads(FIXTURE.read_text(encoding="utf-8")))
    comp.footprint.shapes.extend(regions)
    return build_altium_plan(comp, "T.SchLib", "T.PcbLib")


def test_cutouts_and_unmapped_regions_are_not_drawn():
    plan = _plan_with(
        EePolyline(kind="solid_region", layer=100, fill="cutout", closed=True,
                   points=[(0, 0), (40, 0), (40, 40), (0, 40)]),
        EePolyline(kind="solid_region", layer=99, fill="solid", closed=True,
                   points=[(0, 0), (40, 0), (40, 40)]))
    tracks = [t for st in plan["steps"] if st["tool"] == "lib_add_footprint_tracks"
              for t in st["args"]["tracks"]]
    assert not any(t["x2"] == 40 and t["y2"] in (0, 40) for t in tracks)
    assert not any(st["tool"] == "lib_add_footprint_regions" for st in plan["steps"])
    assert any("2 region(s) not drawn" in w for w in plan["warnings"])


def test_a_real_region_is_placed_filled_on_its_layer():
    plan = _plan_with(EePolyline(kind="solid_region", layer=1, fill="solid", closed=True,
                                 points=[(0, 0), (40, 0), (40, 40)]))
    [step] = [st for st in plan["steps"] if st["tool"] == "lib_add_footprint_regions"]
    assert step["args"]["regions"] == [
        {"layer": "TopLayer", "points": [[0, 0], [40, 0], [40, 40]]}]
