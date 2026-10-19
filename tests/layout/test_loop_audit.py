# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Loops are measured on the placement, through their pads in path order."""

from __future__ import annotations

import math

from eda_agent.layout.audit import CHECKS, run_audits
from eda_agent.layout.loop_audit import analyze_loops, loop_inductance_nh
from eda_agent.layout.model import Component, LayoutBoard, Pad


def _board(cap_offset: float) -> LayoutBoard:
    """An IC with VDD/VSS pins and a decoupling cap ``cap_offset`` mils away."""
    comps = [Component("U1", x=0, y=0), Component("C1", x=cap_offset, y=0)]
    pads = [
        Pad("U1", "1", 0, 50, net="VDD"), Pad("U1", "2", 0, -50, net="GND"),
        Pad("U1", "3", -50, 0, net="SIG"), Pad("U1", "4", -50, 50, net="SIG2"),
        Pad("U1", "5", -50, -50, net="SIG3"),
        Pad("C1", "1", cap_offset, 25, net="VDD"),
        Pad("C1", "2", cap_offset, -25, net="GND"),
    ]
    return LayoutBoard(name="t", components=comps, pads=pads)


def test_a_decoupling_loop_is_measured_and_grows_with_distance():
    near = analyze_loops(_board(100))
    far = analyze_loops(_board(600))
    (n,) = [m for m in near if m["kind"] == "decoupling"]
    (f,) = [m for m in far if m["kind"] == "decoupling"]
    assert 0 < n["area_sq_mils"] < f["area_sq_mils"]
    assert n["inductance_nh_est"] < f["inductance_nh_est"]
    assert not n["missing_pads"]


def test_loop_audit_is_a_registered_advisory_check():
    assert "loop_audit" in CHECKS
    result = run_audits(_board(100), ["loop_audit"])
    check = result["checks"]["loop_audit"]
    assert check["pass"] and check["advisory"]
    assert check["counts"]["loops"] >= 1


def test_inductance_estimate_is_monotonic_and_plausible():
    # A 100 mil square loop of 10 mil trace is a few nH (circular-loop
    # approximation, Grover 1946).
    small = loop_inductance_nh(100 * 100)
    big = loop_inductance_nh(500 * 500)
    assert 2 < small < 10 < big < 80
    assert loop_inductance_nh(0) == 0.0
    assert math.isfinite(big)


def test_a_cap_on_a_shared_rail_pairs_with_the_nearest_ic_only():
    """Pairing a cap with every IC on a shared rail counted it many times."""
    b = _board(100)
    b.components.append(Component("U2", x=3000, y=0))
    b.pads += [Pad("U2", "1", 3000, 50, net="VDD"),
               Pad("U2", "2", 3000, -50, net="GND"),
               Pad("U2", "3", 2950, 0, net="SIGA"),
               Pad("U2", "4", 2950, 50, net="SIGB"),
               Pad("U2", "5", 2950, -50, net="SIGC")]
    dec = [m for m in analyze_loops(b) if m["kind"] == "decoupling"]
    assert len(dec) == 1 and dec[0]["parts"] == ["C1", "U1"]


def test_routed_measure_follows_the_copper_not_the_straight_line():
    """A supply trace that detours out and back encloses far more area than
    the pad-to-pad outline suggests; the routed measure must see it."""
    from eda_agent.layout.model import Layer, PadCopper, Track

    b = _board(100)
    b.layers = [Layer("Top", order=0)]
    for p in b.pads:
        p.copper = [PadCopper("Top", "rect", 30, 30)]
    # VDD: U1.1 (0,50) out to (0,600), across, down to C1.1 (100,25).
    for x1, y1, x2, y2 in ((0, 50, 0, 600), (0, 600, 100, 600),
                           (100, 600, 100, 25)):
        b.tracks.append(Track("Top", x1, y1, x2, y2, 10, net="VDD"))
    # GND straight: U1.2 (0,-50) to C1.2 (100,-25).
    b.tracks.append(Track("Top", 0, -50, 100, -25, 10, net="GND"))
    (dec,) = [m for m in analyze_loops(b) if m["kind"] == "decoupling"]
    assert dec["routed_hops"] == "2/2"
    assert dec["routed_area_sq_mils"] > 5 * dec["area_sq_mils"]


def test_an_unrouted_hop_is_reported_not_guessed():
    from eda_agent.layout.model import Layer, PadCopper, Track

    b = _board(100)
    b.layers = [Layer("Top", order=0)]
    for p in b.pads:
        p.copper = [PadCopper("Top", "rect", 30, 30)]
    b.tracks.append(Track("Top", 0, -50, 100, -25, 10, net="GND"))
    (dec,) = [m for m in analyze_loops(b) if m["kind"] == "decoupling"]
    assert dec["routed_hops"] == "1/2" and "routed_area_sq_mils" not in dec
