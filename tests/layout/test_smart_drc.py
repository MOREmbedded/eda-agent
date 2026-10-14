# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Temporary-rule checks find what the board's own rules leave out."""

from __future__ import annotations

from eda_agent.layout.model import (Component, Layer, LayoutBoard, Pad,
                                    PadCopper, Track)
from eda_agent.layout.smart_drc import net_pair_gaps, smart_drc

PINS = {"U1": {"1": "VIN", "2": "SW", "3": "GND", "4": "FB", "5": "EN"}}


def _pad(comp, name, x, y, net):
    return Pad(comp, name, x, y, net=net,
               copper=[PadCopper("Top", "rect", 20, 20)])


def _buck(fb_track_y: float) -> LayoutBoard:
    """A buck whose FB track runs parallel to the SW track at a given y."""
    comps = [Component(r) for r in ("U1", "L1", "C1", "C2", "R1", "R2")]
    pads = [
        _pad("U1", "1", 0, 0, "VIN"), _pad("U1", "2", 0, -100, "SW"),
        _pad("U1", "3", 0, -200, "GND"), _pad("U1", "4", 0, -300, "FBN"),
        _pad("U1", "5", 0, -400, "EN"),
        _pad("L1", "1", 600, -100, "SW"), _pad("L1", "2", 800, -100, "VOUT"),
        _pad("C1", "1", -200, 0, "VIN"), _pad("C1", "2", -200, -200, "GND"),
        _pad("C2", "1", 900, -100, "VOUT"), _pad("C2", "2", 900, -200, "GND"),
        _pad("R1", "1", 800, -300, "VOUT"), _pad("R1", "2", 600, -300, "FBN"),
        _pad("R2", "1", 600, -400, "FBN"), _pad("R2", "2", 600, -500, "GND"),
    ]
    tracks = [Track("Top", 0, -100, 600, -100, 20, net="SW"),
              Track("Top", 0, -300, 0, fb_track_y, 8, net="FBN"),
              Track("Top", 0, fb_track_y, 600, fb_track_y, 8, net="FBN"),
              Track("Top", 600, fb_track_y, 600, -300, 8, net="FBN")]
    return LayoutBoard(name="t", layers=[Layer("Top", order=0)],
                       components=comps, pads=pads, tracks=tracks)


def test_feedback_routed_beside_the_switch_node_is_caught():
    close = smart_drc(_buck(-125), pin_names=PINS, spacing_mils=40)
    assert close["counts"]["spacing_violations"] >= 1
    v = close["spacing"][0]
    assert {v["net_a"], v["net_b"]} == {"FBN", "SW"}
    assert v["gap_mils"] < 40


def test_feedback_kept_away_passes_and_still_ranks_the_gap():
    far = smart_drc(_buck(-200), pin_names=PINS, spacing_mils=40, probe_mils=150)
    assert far["counts"]["spacing_violations"] == 0
    assert far["tightest_pairs"] and far["tightest_pairs"][0]["gap_mils"] >= 40


def test_gap_matches_the_geometry():
    """SW track centre y=-100, 20 wide; FB track centre y=-125, 8 wide:
    edge to edge 25 - 10 - 4 = 11 mil."""
    (p,) = net_pair_gaps(_buck(-125), {"FBN"}, {"SW"}, 100)[:1]
    assert abs(p["gap_mils"] - 11.0) < 0.1


def test_hot_loop_width_from_the_current():
    out = smart_drc(_buck(-200), pin_names=PINS, currents={"SW": 3.0})
    (w,) = out["hot_loop_width"]
    assert w["net"] == "SW" and w["min_width_mils"] > 20
