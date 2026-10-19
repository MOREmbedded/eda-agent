# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Audit an existing footprint against its package, offline.

The footprint comes from ``readback``: the Altium plan the package tool
would send, turned into what ``library.get_pad_geometry`` and
``library.get_footprint_primitives`` read back (pads in mm from the
footprint origin; tracks, arcs and region boxes with their layers). A
footprint built that way from each datasheet case must audit clean, and
one with a defect put in must not.
"""

from __future__ import annotations

import copy

import pytest

from eda_agent.design.landpattern import build_footprint
from eda_agent.design.landpattern.audit import (
    audit_footprint_vs_package,
    classify_layer,
    terminal_outlines,
)
from eda_agent.design.landpattern.emit import altium_plan
from tests.design import _landpattern_cases as cases

LAYERS = {"silk": {"layer": "TopOverlay"}, "paste": {"layer": "TopPaste"},
          "assembly": {"layer": "Mechanical13"},
          "courtyard": {"layer": "Mechanical15"}}
MECH = [{"layer": "Mechanical13", "name": "Top Assembly", "kind": "Assembly Top"},
        {"layer": "Mechanical15", "name": "Top Courtyard", "kind": "Courtyard Top"}]
_SHAPE = {"roundrect": "roundrectangle", "rectangular": "rectangular",
          "round": "round", "octagonal": "octagonal"}


def readback(model) -> tuple[list[dict], dict]:
    """What the two Altium reads return for the footprint the package tool
    writes from ``model``."""
    plan = altium_plan(model, LAYERS)
    pads = []
    for p in plan["pads"]:
        pads.append({
            "name": p["designator"], "x_mm": p["x"], "y_mm": p["y"],
            "w_mm": p["x_size"], "h_mm": p["y_size"],
            "shape": _SHAPE[p["shape"]], "rotation": p["rotation"],
            "hole_mm": p["hole_size"], "layer": "top" if p["hole_size"] == 0
            else "multi",
            "paste_expansion_mm": p.get("paste_expansion", 0.0),
            "paste_expansion_source": "manual" if "paste_expansion" in p else "rule",
            "mask_expansion_mm": p.get("mask_expansion", 0.0),
            "mask_expansion_source": "manual" if "mask_expansion" in p else "rule"})
    prims: dict = {"tracks": [dict(t) for t in plan["tracks"]], "arcs": [],
                   "regions": [], "fills": [], "texts": [],
                   "height_mm": plan["height_mm"], "bodies": 0}
    for a in plan["arcs"]:
        prims["arcs"].append({"layer": a["layer"], "xc": a["x_center"],
                              "yc": a["y_center"], "r": a["radius"],
                              "start": a["start_angle"], "end": a["end_angle"],
                              "width": a["width"]})
    for rg in plan["regions"]:
        pts = rg["points"]
        area = abs(sum(pts[k][0] * pts[(k + 1) % len(pts)][1]
                       - pts[(k + 1) % len(pts)][0] * pts[k][1]
                       for k in range(len(pts)))) / 2.0
        prims["regions"].append({
            "layer": rg["layer"], "x1": min(p[0] for p in pts),
            "y1": min(p[1] for p in pts), "x2": max(p[0] for p in pts),
            "y2": max(p[1] for p in pts), "area": area})
    return pads, prims


def _audit(spec, pads, prims, model=None, **kw):
    return audit_footprint_vs_package(spec, pads, model=model, primitives=prims,
                                      mech_layers=MECH, **kw)


def _hits(r: dict, check: str, severity: str = "error") -> list[dict]:
    found = [f for f in r["findings"]
             if f["check"] == check and f["severity"] == severity]
    assert found, (check, severity, r["findings"])
    return found


@pytest.fixture
def soic():
    spec = cases.soic8()
    model = build_footprint(spec)
    pads, prims = readback(model)
    return spec, model, pads, prims


ALL_CHECKS = ["pad_count", "numbering", "pin1", "pad_position", "pad_size",
              "terminal_coverage", "holes", "pitch", "span", "origin", "paste",
              "mask", "courtyard", "silk", "assembly", "height"]


@pytest.mark.parametrize("name", sorted(cases.ALL_CASES))
def test_the_footprint_the_engine_writes_audits_clean(name):
    spec = cases.ALL_CASES[name]()
    model = build_footprint(spec)
    pads, prims = readback(model)
    r = _audit(spec, pads, prims, model)
    assert r["ok"] and r["findings"] == [], r["findings"]
    assert r["examined"] == ALL_CHECKS
    skipped = {s["check"] for s in r["not_examined"]}
    assert skipped <= {"terminal_coverage", "height"}, r["not_examined"]


def test_a_footprint_drawn_to_the_vendor_pattern_passes(soic):
    """TI D0008A example board layout: lands 1.55 x 0.6 on 5.4."""
    spec, _, pads, _ = soic
    vendor = copy.deepcopy(pads)
    for p in vendor:
        p["x_mm"] = 2.7 if p["x_mm"] > 0 else -2.7
        p["w_mm"] = 1.55
    r = audit_footprint_vs_package(spec, vendor)
    assert r["ok"] and r["findings"] == [], r["findings"]
    assert r["metrics"]["footprint"]["span"] == pytest.approx(5.4)


def test_wrong_pitch(soic):
    spec, model, pads, prims = soic
    bad = copy.deepcopy(pads)
    for p in bad:
        p["y_mm"] *= 1.25 / 1.27
    f = _hits(_audit(spec, bad, prims, model), "pitch")[0]
    assert "1.25 mm apart" in f["message"] and "pitch is 1.27 mm" in f["message"]
    assert "p. 54" in f["message"]


def test_a_mirror_image_footprint_is_named_as_one(soic):
    spec, model, pads, prims = soic
    bad = copy.deepcopy(pads)
    for p in bad:
        p["x_mm"] = -p["x_mm"]
    r = _audit(spec, bad, prims, model)
    assert r["alignment"]["mirrored"] is True
    assert "MIRROR IMAGE" in _hits(r, "numbering")[0]["message"]
    assert "pin 8" in _hits(r, "pin1")[0]["message"]


def test_pin_one_on_the_wrong_corner():
    """TI BQA0014A puts pin 1 at the left of the top side; a footprint that
    starts the count at the top of the left side has every pin moved."""
    spec = cases.qfn14_rect()
    model = build_footprint(spec)
    wrong = cases.qfn14_rect()
    wrong.pin1 = "left_top"
    pads, prims = readback(build_footprint(wrong))
    r = _audit(spec, pads, prims, model)
    assert "pad 1 is where the package has pin 2" in _hits(r, "pin1")[0]["message"]
    assert "14 pad(s)" in _hits(r, "numbering")[0]["message"]


def test_a_turned_footprint_is_compared_turned_back(soic):
    spec, model, pads, prims = soic
    turned = copy.deepcopy(pads)
    for p in turned:
        p["x_mm"], p["y_mm"] = -p["y_mm"], p["x_mm"]
        p["w_mm"], p["h_mm"] = p["h_mm"], p["w_mm"]
    r = audit_footprint_vs_package(spec, turned, model=model)
    assert r["ok"], r["findings"]
    assert r["alignment"]["rotation_deg"] == 90
    assert any("turned 90 degrees" in n for n in r["notes"])


def test_courtyard_missing(soic):
    spec, model, pads, prims = soic
    pr = copy.deepcopy(prims)
    pr["tracks"] = [t for t in pr["tracks"] if t["layer"] != "Mechanical15"]
    assert "no courtyard" in _hits(_audit(spec, pads, pr, model),
                                   "courtyard")[0]["message"]


def test_courtyard_that_does_not_enclose_the_lands(soic):
    spec, model, pads, prims = soic
    pr = copy.deepcopy(prims)
    for t in pr["tracks"]:
        if t["layer"] == "Mechanical15":
            for k in ("x1", "x2"):
                t[k] *= 0.8
    f = _hits(_audit(spec, pads, pr, model), "courtyard")[0]
    assert "falls short on the left" in f["message"] and "right" in f["message"]


def test_courtyard_found_by_layer_name_when_no_kind_is_set(soic):
    spec, model, pads, prims = soic
    named = [{"layer": "Mechanical13", "name": "Assembly", "kind": "Not Set"},
             {"layer": "Mechanical15", "name": "Courtyard", "kind": "Not Set"}]
    r = audit_footprint_vs_package(spec, pads, model=model, primitives=prims,
                                   mech_layers=named)
    assert r["ok"], r["findings"]
    r = audit_footprint_vs_package(spec, pads, model=model, primitives=prims,
                                   mech_layers=[])
    assert "no courtyard" in _hits(r, "courtyard")[0]["message"]
    r = audit_footprint_vs_package(spec, pads, model=model, primitives=prims,
                                   mech_layers=[], courtyard_layer="Mechanical15",
                                   assembly_layer="Mechanical13")
    assert r["ok"], r["findings"]


def test_silk_over_copper(soic):
    spec, model, pads, prims = soic
    pr = copy.deepcopy(prims)
    pr["tracks"].append({"layer": "TopOverlay", "x1": -3.0, "y1": 1.905,
                         "x2": -1.5, "y2": 1.905, "width": 0.2})
    f = _hits(_audit(spec, pads, pr, model), "silk")[0]
    assert "pad(s) 1 (by" in f["message"]


def test_silk_closer_than_the_drafting_default_is_a_warning(soic):
    spec, model, pads, prims = soic
    pr = copy.deepcopy(prims)
    # Pad 1 is 1.95 x 0.6 at y 1.905, so its top edge is at 2.205; a 0.2 wide
    # line at 2.405 leaves 0.1 mm.
    pr["tracks"].append({"layer": "TopOverlay", "x1": -3.0, "y1": 2.405,
                         "x2": -2.0, "y2": 2.405, "width": 0.2})
    r = _audit(spec, pads, pr, model)
    assert r["ok"]
    assert "1 (0.1 mm)" in _hits(r, "silk", "warning")[0]["message"]


def test_silk_arc_over_copper_is_seen():
    spec = cases.qfn16()
    model = build_footprint(spec)
    pads, prims = readback(model)
    pr = copy.deepcopy(prims)
    pr["arcs"].append({"layer": "TopOverlay", "xc": 0.0, "yc": 0.0, "r": 0.5,
                       "start": 0, "end": 360, "width": 0.15})
    assert "pad(s) 17" in _hits(_audit(spec, pads, pr, model), "silk")[0]["message"]


def test_exposed_pad_with_no_paste():
    spec = cases.qfn16()
    model = build_footprint(spec)
    pads, prims = readback(model)
    pr = copy.deepcopy(prims)
    pr["regions"] = []
    f = _hits(_audit(spec, pads, pr, model), "paste")[0]
    assert "exposed pad 17" in f["message"] and "no solder" in f["message"]


def test_exposed_pad_with_full_paste_and_with_too_much_window():
    spec = cases.qfn16()
    model = build_footprint(spec)
    pads, prims = readback(model)
    full = copy.deepcopy(pads)
    for p in full:
        if p["name"] == "17":
            p["paste_expansion_source"], p["paste_expansion_mm"] = "rule", 0.0
    pr = copy.deepcopy(prims)
    pr["regions"] = []
    assert "full paste" in _hits(_audit(spec, full, pr, model), "paste",
                                 "warning")[0]["message"]
    big = copy.deepcopy(prims)
    for rg in big["regions"]:
        rg["area"] *= 1.45
    w = _hits(_audit(spec, pads, big, model), "paste", "warning")[0]
    assert "outside the 50 to 80 %" in w["message"]


def test_a_land_with_its_paste_closed():
    spec = cases.soic8()
    model = build_footprint(spec)
    pads, prims = readback(model)
    pads[2]["paste_expansion_source"] = "manual"
    pads[2]["paste_expansion_mm"] = -1.95
    assert "pad 3 has its paste opening closed" in _hits(
        _audit(spec, pads, prims, model), "paste")[0]["message"]


def test_mask_openings_that_merge(soic):
    spec, model, pads, prims = soic
    bad = copy.deepcopy(pads)
    for p in bad:
        p["mask_expansion_source"], p["mask_expansion_mm"] = "manual", 0.4
    assert "no mask web" in _hits(_audit(spec, bad, prims, model), "mask",
                                  "warning")[0]["message"]


def test_origin_away_from_the_centroid(soic):
    spec, model, pads, _ = soic
    moved = copy.deepcopy(pads)
    for p in moved:
        p["x_mm"] += 0.5
    r = audit_footprint_vs_package(spec, moved, model=model)
    assert r["ok"]
    assert "0.5 mm from the package centroid" in _hits(r, "origin",
                                                       "warning")[0]["message"]


def test_a_missing_pad_and_an_extra_one(soic):
    spec, model, pads, prims = soic
    r = _audit(spec, pads[:-1], prims, model)
    assert any("7 pads" in f["message"] for f in _hits(r, "pad_count"))
    extra = copy.deepcopy(pads) + [dict(pads[0], name="9", x_mm=0.0, y_mm=0.0)]
    assert any("9" in f["message"]
               for f in _hits(_audit(spec, extra, prims, model), "pad_count"))


def test_lands_moved_off_their_terminals(soic):
    spec, model, pads, prims = soic
    out = copy.deepcopy(pads)
    for p in out:
        p["x_mm"] += 1.3 if p["x_mm"] > 0 else -1.3
    r = audit_footprint_vs_package(spec, out, model=model)
    _hits(r, "span")
    _hits(r, "pad_position")
    assert len(_hits(r, "terminal_coverage")) == 8


def test_a_footprint_drawn_in_mils_as_mm(soic):
    spec, model, pads, prims = soic
    mils = copy.deepcopy(pads)
    for p in mils:
        for k in ("x_mm", "y_mm", "w_mm", "h_mm"):
            p[k] /= 0.0254
    r = audit_footprint_vs_package(spec, mils, model=model)
    assert not r["ok"]
    assert {"pitch", "span", "pad_size"} <= {f["check"] for f in r["findings"]}


def test_a_hole_smaller_than_the_lead():
    """Wurth 61300411121: square post 0.64 +/-0.15, diagonal 1.117 max."""
    spec = cases.header_1x4()
    model = build_footprint(spec)
    pads, prims = readback(model)
    pads[0]["hole_mm"] = 0.9
    f = _hits(_audit(spec, pads, prims, model), "holes")[0]
    assert "smaller than the lead" in f["message"]


def test_height_missing_and_height_too_low(soic):
    spec, model, pads, prims = soic
    pr = dict(prims, height_mm=0.0)
    assert "no height" in _hits(_audit(spec, pads, pr, model), "height",
                                "warning")[0]["message"]
    pr = dict(prims, height_mm=1.0)
    assert "below the package maximum 1.75" in _hits(
        _audit(spec, pads, pr, model), "height", "warning")[0]["message"]


def test_unread_primitives_are_listed_not_passed(soic):
    spec, model, pads, _ = soic
    r = audit_footprint_vs_package(spec, pads, model=model,
                                   primitives_reason="not read here")
    assert r["ok"]
    skipped = {s["check"]: s["reason"] for s in r["not_examined"]}
    assert set(skipped) == {"courtyard", "silk", "assembly", "paste_windows",
                            "height"}
    assert skipped["silk"] == "not read here"
    assert "silk" not in r["examined"]


def test_no_pads_at_all(soic):
    spec, model, _, _ = soic
    r = audit_footprint_vs_package(spec, [], model=model)
    assert not r["ok"] and r["findings"][0]["check"] == "pad_count"


def test_terminal_outlines_follow_the_drawing():
    """TI D0008A nominal E 5.995, L 0.84, b 0.41: the right-hand terminal
    runs from 5.995 / 2 - 0.84 to 5.995 / 2."""
    spec = cases.soic8()
    model = build_footprint(spec)
    t = {x["number"]: x for x in terminal_outlines(spec, model)}
    assert len(t) == 8
    assert t["5"]["x1"] == pytest.approx(5.995 / 2)
    assert t["5"]["x0"] == pytest.approx(5.995 / 2 - 0.84)
    assert t["5"]["y1"] - t["5"]["y0"] == pytest.approx(0.41)
    bga = cases.bga80()
    balls = terminal_outlines(bga, build_footprint(bga))
    assert len(balls) == 80 and {b["shape"] for b in balls} == {"circle"}
    assert terminal_outlines(cases.dpak(), build_footprint(cases.dpak())) == []


@pytest.mark.parametrize("name,mech,expect", [
    ("TopOverlay", {}, "silk"),
    ("TopPaste", {}, "paste"),
    ("Mechanical15", {"mechanical15": {"kind": "Courtyard Top"}}, "courtyard"),
    ("Mechanical15", {"mechanical15": {"kind": "Courtyard Bottom"}}, "other"),
    ("Mechanical2", {"mechanical2": {"kind": "Not Set",
                                     "name": "Assembly Top"}}, "assembly"),
    ("Mechanical3", {"mechanical3": {"kind": "", "name": "Bottom Courtyard"}},
     "other"),
    ("Mechanical7", {}, "other"),
])
def test_layers_are_classified_by_kind_then_name(name, mech, expect):
    assert classify_layer(name, mech) == expect
