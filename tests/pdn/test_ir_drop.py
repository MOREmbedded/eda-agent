# SPDX-License-Identifier: Apache-2.0
"""DC IR drop against closed-form references."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Region, Track, Via
from eda_agent.pdn import ir_drop as ir
from eda_agent.pdn import physics as ph

from .boards import STACKUP, plane_board, smd

L2 = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
RHO = ph.copper_resistivity(25.0)
RS = ph.sheet_resistance(1.4, RHO)
SRC = [{"ref": "J1", "volts": 1.0}]
SNK = [{"ref": "U1", "amps": 1.0}]


def _board(w=1400, h=400):
    return LayoutBoard(outline=[(0, 0), (w, 0), (w, h), (0, h)],
                       layers=[Layer(l.name, l.kind, l.order) for l in L2])


def _r(board, net="P", **kw):
    kw.setdefault("check_convergence", False)
    res = ir.analyse_net(board, net, None, SRC, SNK, **kw)
    return res["sinks"][0]["drop_mv"] / 1e3, res


def test_copper_resistivity_and_sheet():
    assert ph.copper_resistivity(20.0) == pytest.approx(1.7241e-8)
    assert ph.copper_resistivity(100.0) / ph.copper_resistivity(20.0) == pytest.approx(1.3144)
    # 1 oz copper (1.37 mil) is the textbook 0.5 mohm per square at 20 C
    assert ph.sheet_resistance(1.37) == pytest.approx(0.495e-3, rel=0.01)


def test_via_barrel_resistance_closed_form():
    # 10 mil hole, 1 mil plating, 62 mil long
    r = ph.via_barrel_resistance(62, 10, 1.0)
    area = math.pi * (6 ** 2 - 5 ** 2) * ph.MIL ** 2
    assert r == pytest.approx(ph.RHO_CU_20C * 62 * ph.MIL / area)
    assert 1.2e-3 < r < 1.3e-3


def test_track_is_rho_l_over_wt():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40)]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    r, res = _r(b, cell_mils=5)
    # the track is a resistor between the pad edges (20 mil inside each pad)
    exact = RHO * 960 * ph.MIL / (10 * ph.MIL * 1.4 * ph.MIL)
    assert r == pytest.approx(exact, rel=0.003)
    t = res["tracks"][0]
    assert t["amps"] == pytest.approx(1.0, rel=1e-6)
    assert t["j_a_per_mm2"] == pytest.approx(1.0 / (10 * 1.4 * 0.0254 ** 2), rel=1e-3)
    assert res["loss_w"] == pytest.approx(r, rel=1e-3)   # P = I^2 R at 1 A


def test_temperature_raises_the_drop():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40)]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    cold, _ = _r(b, cell_mils=5, temp_c=20.0)
    hot, _ = _r(b, cell_mils=5, temp_c=100.0)
    assert hot / cold == pytest.approx(1.3144, rel=1e-3)


def test_pour_strip_is_squares_times_sheet_resistance():
    b = _board(2400, 400)
    b.regions.append(Region("TopLayer", [(100, 100), (2300, 100), (2300, 300), (100, 300)],
                            [], "P", "pour"))
    b.pads += [smd("J1", "1", 120, 200, "P", 40, 200), smd("U1", "1", 2280, 200, "P", 40, 200)]
    r, res = _r(b, cell_mils=10, sink_model="equipotential")
    # between the pads' inner edges: a soldered pad is an equipotential
    exact = RS * 2120 / 200
    assert r == pytest.approx(exact, rel=0.01)
    # uniform flow: J = I / (w t) everywhere in the middle of the strip
    J = res["_maps"]["J"]["TopLayer"]
    mid = J[15:25, 100:140]
    assert np.nanmean(mid) == pytest.approx(1.0 / (200 * 1.4 * 0.0254 ** 2), rel=0.02)


def test_two_discs_on_a_sheet_spreading_resistance():
    # R = Rs / pi * acosh(D / 2a) between two discs of radius a, centres D
    # apart, on an infinite sheet; the 8 inch sheet is within a few percent.
    b = _board(8000, 8000)
    b.regions.append(Region("TopLayer", list(b.outline), [], "P", "pour"))
    b.pads += [Pad("J1", "1", 3500, 4000, 0, "P", [PadCopper("TopLayer", "round", 200, 200)]),
               Pad("U1", "1", 4500, 4000, 0, "P", [PadCopper("TopLayer", "round", 200, 200)])]
    r, _ = _r(b, cell_mils=20)
    exact = RS / math.pi * math.acosh(1000 / 200)
    assert r == pytest.approx(exact, rel=0.05)


def test_track_inside_its_own_pour_is_not_counted_twice():
    b = _board(2400, 400)
    b.regions.append(Region("TopLayer", [(100, 100), (2300, 100), (2300, 300), (100, 300)],
                            [], "P", "pour"))
    b.pads += [smd("J1", "1", 120, 200, "P", 40, 200), smd("U1", "1", 2280, 200, "P", 40, 200)]
    r0, _ = _r(b, cell_mils=10, sink_model="equipotential")
    b.tracks.append(Track("TopLayer", 120, 200, 2280, 200, 50, "P"))
    r1, res = _r(b, cell_mils=10, sink_model="equipotential")
    assert r1 == pytest.approx(r0, rel=0.005)
    assert res["network"]["track_parts_inside_pours"] >= 1


def test_parallel_tracks_divide_the_current_by_width():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 200), smd("U1", "1", 1200, 200, "P", 40, 200)]
    b.tracks += [Track("TopLayer", 200, 150, 1200, 150, 10, "P"),
                 Track("TopLayer", 200, 250, 1200, 250, 30, "P")]
    r, res = _r(b, cell_mils=5, sink_model="equipotential")
    amps = sorted(t["amps"] for t in res["tracks"])
    assert amps[0] == pytest.approx(0.25, rel=0.01) and amps[1] == pytest.approx(0.75, rel=0.01)
    exact = RHO * 960 * ph.MIL / (40 * ph.MIL * 1.4 * ph.MIL)
    assert r == pytest.approx(exact, rel=0.01)


def test_t_junction_splits_the_trunk():
    # trunk J1 -> U1 with a stub teed off its middle to U2
    b = _board(1400, 800)
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40),
               smd("U2", "1", 700, 600, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 200, 1200, 200, 10, "P"),
                 Track("TopLayer", 700, 200, 700, 600, 10, "P")]
    res = ir.analyse_net(b, "P", None, SRC, [{"ref": "U1", "amps": 1.0}, {"ref": "U2", "amps": 2.0}],
                         cell_mils=5, check_convergence=False)
    per_mil = RHO / (10 * 1.4 * ph.MIL)
    d1 = 3.0 * per_mil * 480 + 1.0 * per_mil * 480
    d2 = 3.0 * per_mil * 480 + 2.0 * per_mil * 380
    got = {s["sink"]: s["drop_mv"] / 1e3 for s in res["sinks"]}
    assert got["U1"] == pytest.approx(d1, rel=0.01)
    assert got["U2"] == pytest.approx(d2, rel=0.01)


def test_via_joins_the_layers_through_its_barrel():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40),
               Pad("U1", "1", 1200, 200, 0, "P", [PadCopper("BottomLayer", "rect", 40, 40)])]
    b.tracks += [Track("TopLayer", 200, 200, 700, 200, 10, "P"),
                 Track("BottomLayer", 700, 200, 1200, 200, 10, "P")]
    b.vias.append(Via(700, 200, 24, 12, "TopLayer", "BottomLayer", "P"))
    r, res = _r(b, cell_mils=3)
    barrel = ph.via_barrel_resistance(62.99, 12, 0.79, RHO)
    # track from the pad edge to the via land edge on each layer
    tracks = 2 * RHO * (500 - 20 - 12) * ph.MIL / (10 * ph.MIL * 1.4 * ph.MIL)
    assert r == pytest.approx(tracks + barrel, rel=0.02)
    assert res["vias"][0]["amps"] == pytest.approx(1.0, rel=1e-6)
    assert res["vias"][0]["loss_w"] == pytest.approx(barrel, rel=1e-3)
    # without the via the sink is cut off, and that is an error, not a number
    b.vias.clear()
    with pytest.raises(ir.IRDropError, match="not connected"):
        _r(b, cell_mils=3)


def test_arc_length_counts():
    b = _board(1400, 1400)
    b.pads += [smd("J1", "1", 1000, 500, "P", 20, 20), smd("U1", "1", 500, 1000, "P", 20, 20)]
    from eda_agent.layout.model import Arc
    b.arcs.append(Arc("TopLayer", 500, 500, 500, 0, 90, 10, "P"))
    r, _ = _r(b, cell_mils=2.5)
    exact = RHO * (math.pi * 250 - 20) * ph.MIL / (10 * ph.MIL * 1.4 * ph.MIL)
    assert r == pytest.approx(exact, rel=0.01)


def test_energy_balance_and_heat_map_on_a_plane_board():
    b = plane_board()
    st = stack_from_stackup(STACKUP, b.layers)
    res = ir.analyse(b, "+1V0", st, [{"ref": "U1", "volts": 1.0}], [{"ref": "U2", "amps": 5.0}],
                     return_net="GND", cell_mils=8, check_convergence=False)
    sup, ret = res["supply"], res["return"]
    # Joule loss equals the power the sinks lose: sum of I * drop per pin
    lost = sum(5.0 / len(s["pins"]) * (1.0 - p["volts"]) for s in sup["sinks"] for p in s["pins"])
    assert sup["loss_w"] == pytest.approx(lost, rel=1e-4)
    heat = ir.power_density(sup)
    assert sum(float(h.sum()) for h in heat.values()) == pytest.approx(sup["loss_w"], rel=1e-6)
    # the rail is the source less both drops
    rail = res["rails"][0]
    assert rail["total_drop_mv"] == pytest.approx(
        sup["sinks"][0]["drop_mv"] + ret["sinks"][0]["drop_mv"], rel=1e-4)
    # all 5 A cross the regulator's single via: flagged
    assert sup["vias"][0]["amps"] == pytest.approx(5.0, rel=1e-4)
    assert sup["vias"][0]["over_capacity"]
    # the plane carries it: the plane layer holds most of the area loss
    assert {l["layer"] for l in sup["layers"]} >= {"TopLayer", "InternalPlane2"}


def test_convergence_estimate_is_reported_and_small_on_a_strip():
    b = _board(2400, 400)
    b.regions.append(Region("TopLayer", [(100, 100), (2300, 100), (2300, 300), (100, 300)],
                            [], "P", "pour"))
    b.pads += [smd("J1", "1", 120, 200, "P", 40, 200), smd("U1", "1", 2280, 200, "P", 40, 200)]
    res = ir.analyse_net(b, "P", None, SRC, SNK, cell_mils=5, sink_model="equipotential")
    c = res["convergence"]
    assert c["coarse_cell_mils"] == 10 and c["estimated_error_pct_of_drop"] < 1.0


def test_bad_inputs_are_errors():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P"), smd("U1", "1", 1200, 200, "P")]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    with pytest.raises(ir.IRDropError, match="no pads"):
        ir.analyse_net(b, "P", None, [{"ref": "J9", "volts": 1}], SNK)
    with pytest.raises(ir.IRDropError, match="sink_model"):
        ir.analyse_net(b, "P", None, SRC, SNK, sink_model="x")
