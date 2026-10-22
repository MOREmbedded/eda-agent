# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A footprint read live from a library can be drawn and compared.

The replies are in the shape lib_get_pad_geometry, lib_get_footprint_
primitives and lib_get_mech_layers return (taken from a live read of a
scratch library on AD 26.11), so the conversion is tested against what
Altium actually sends.
"""
from __future__ import annotations

import copy

from eda_agent.design.landpattern.live import (compare_live, layer_roles,
                                               model_from_live, role_summary,
                                               silk_over_copper)
from eda_agent.design.landpattern.model import Circle
from eda_agent.design.landpattern.svg import render_svg


def _pad(name, x):
    return {"name": name, "x_mm": x, "y_mm": 0, "copper_x_mm": x,
            "copper_y_mm": 0, "w_mm": 0.762, "h_mm": 0.889,
            "shape": "roundrectangle", "corner_pct": 25, "rotation": 0,
            "hole_mm": 0, "hole_w_mm": 0, "plated": True,
            "paste_top_enabled": True, "paste_expansion_mm": 0,
            "mask_expansion_mm": 0}


def _track(layer, x1, y1, x2, y2, w=0.15):
    return {"layer": layer, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": w}


PADS = {"name": "C0603", "pads": [_pad("1", -0.762), _pad("2", 0.762)]}
PRIMS = {"name": "C0603", "height_mm": 0.9, "tracks": [
    _track("TopOverlay", -0.3, 0.6, 0.3, 0.6),
    _track("Mechanical1", -1.4, -0.7, 1.4, -0.7, 0.1),
    _track("Mechanical1", -1.4, 0.7, 1.4, 0.7, 0.1),
    _track("Mechanical13", -0.8, -0.4, 0.8, -0.4, 0.1)],
    "arcs": [{"layer": "TopOverlay", "xc": -1.5, "yc": 0, "r": 0.1,
              "start": 0, "end": 360, "width": 0.1}]}
MECH = [{"layer": "Mechanical1", "name": "Courtyard", "kind": "Courtyard Top"},
        {"layer": "Mechanical13", "name": "Mechanical 13", "kind": "Not Set"}]


def test_layers_take_their_role_from_altiums_kind():
    roles = layer_roles(MECH)
    assert roles["mechanical1"] == "courtyard"
    assert "mechanical13" not in roles, "an unset kind is not guessed"
    assert layer_roles(MECH, assembly_layer="Mechanical13")[
        "mechanical13"] == "assembly"


def test_the_live_model_carries_pads_and_roles():
    model = model_from_live(PADS, PRIMS, MECH)
    assert [(p.number, p.shape) for p in model.pads] == \
        [("1", "roundrect"), ("2", "roundrect")]
    assert abs(model.pads[0].corner_radius - 0.762 * 0.25 / 2) < 1e-9
    summary = role_summary(model)
    assert summary["courtyard"]["count"] == 2
    assert summary["silk"]["count"] == 2
    assert summary["other"]["count"] == 1
    assert model.body.z1 == 0.9
    assert model.courtyard.points, "the courtyard extent is kept"


def test_a_whole_circle_is_drawn_as_a_circle():
    """An SVG arc whose ends meet draws nothing; a pin 1 dot vanished."""
    model = model_from_live(PADS, PRIMS, MECH)
    assert any(isinstance(g, Circle) and g.layer == "silk"
               for g in model.graphics)
    svg = render_svg(model)
    assert "<circle" in svg and 'id="other-layers"' in svg


def test_silkscreen_over_a_pad_is_found():
    clean = model_from_live(PADS, PRIMS, MECH)
    assert silk_over_copper(clean) == []
    bad = copy.deepcopy(PRIMS)
    bad["tracks"].append(_track("TopOverlay", -0.81, -0.4, -0.81, 0.4))
    hits = silk_over_copper(model_from_live(PADS, bad, MECH))
    assert [h["pad"] for h in hits] == ["1"]


def test_compare_names_what_changed_and_nothing_else():
    a = model_from_live(PADS, PRIMS, MECH)
    assert compare_live(a, a)["same"]
    pads = copy.deepcopy(PADS)
    pads["pads"][1]["w_mm"] = 0.8
    pads["pads"].append(_pad("3", 0))
    prims = copy.deepcopy(PRIMS)
    prims["tracks"][1]["y1"] = prims["tracks"][1]["y2"] = -0.9
    out = compare_live(a, model_from_live(pads, prims, MECH))
    assert not out["same"]
    assert out["only_in_b"] == ["3"]
    assert out["pads_differing"][0]["pad"] == "2"
    assert out["pads_differing"][0]["width"] == [0.762, 0.8]
    assert list(out["layers_differing"]) == ["courtyard"]


def test_an_empty_footprint_draws_rather_than_failing():
    """A library's blank PCBComponent_1 raised 'min() arg is empty'."""
    svg = render_svg(model_from_live({"pads": []}, {"tracks": []}, []))
    assert svg.startswith("<svg")


def test_pad_numbers_sort_like_a_person_reads_them():
    pads = {"pads": [_pad(n, i) for i, n in enumerate(["10", "2", "1"])]}
    other = copy.deepcopy(pads)
    for p in other["pads"]:
        p["w_mm"] = 1.0
    out = compare_live(model_from_live(pads, {}, []),
                       model_from_live(other, {}, []))
    assert [d["pad"] for d in out["pads_differing"]] == ["1", "2", "10"]
