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
    snk = res["sinks"][0]
    assert snk["drop_mv"] == pytest.approx(r * 1e3, rel=1e-6)
    assert snk["drop_pct"] == pytest.approx(100 * r / 1.0, rel=1e-3)      # of the 1 V source
    assert res["worst"]["drop_pct"] == snk["drop_pct"]


def test_ipc2221_capacity_by_hand_and_halved_on_an_inner_layer():
    # I = k dT^0.44 A^0.725, A in square mils; k = 0.048 outside, 0.024 inside.
    # 10 mil x 1.4 mil at 10 K: 0.048 * 2.7542 * 6.776 = 0.896 A
    assert ph.ipc2221_current_capacity(14.0, 10.0) == pytest.approx(0.896, rel=2e-3)
    assert ph.ipc2221_current_capacity(14.0, 10.0, external=False) == pytest.approx(0.448, rel=2e-3)
    assert ph.ipc2221_current_capacity(14.0, 20.0) / ph.ipc2221_current_capacity(14.0, 10.0) \
        == pytest.approx(2 ** 0.44)
    assert ph.ipc2221_current_capacity(0.0, 10.0) == 0.0

    def track_on(layer):
        b = _board()
        b.layers = [Layer("TopLayer", "signal", 0), Layer("MidLayer1", "signal", 1),
                    Layer("BottomLayer", "signal", 2)]
        b.pads += [smd("J1", "1", 200, 200, "P", 40, 40, layer), smd("U1", "1", 1200, 200, "P", 40, 40, layer)]
        b.tracks.append(Track(layer, 200, 200, 1200, 200, 10, "P"))
        res = _r(b, cell_mils=5)[1]
        area = 10 * ir._layer_thickness(b, None, layer)
        return res["tracks"][0], area
    top, a_top = track_on("TopLayer")
    mid, a_mid = track_on("MidLayer1")
    assert top["ipc2221_capacity_a"] == pytest.approx(0.048 * 10 ** 0.44 * a_top ** 0.725, abs=1e-3)
    assert mid["ipc2221_capacity_a"] == pytest.approx(0.024 * 10 ** 0.44 * a_mid ** 0.725, abs=1e-3)
    # 1 A is over what a 10 mil inner track may carry at 10 K, whatever its foil
    assert mid["over_capacity"] is True


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
    assert r == pytest.approx(exact, rel=1e-3)
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
    # it is the same board solved on the grid twice as coarse, and the
    # change between the two, whole, is the error reported
    coarse = ir.analyse_net(b, "P", None, SRC, SNK, cell_mils=10, sink_model="equipotential",
                            check_convergence=False)
    fine_mv, coarse_mv = res["worst"]["drop_mv"], coarse["worst"]["drop_mv"]
    assert c["coarse_worst_drop_mv"] == pytest.approx(coarse_mv, abs=2e-4)
    assert c["estimated_error_mv"] == pytest.approx(abs(fine_mv - coarse_mv), abs=2e-4)
    assert c["estimated_error_pct_of_drop"] == pytest.approx(
        100 * abs(fine_mv - coarse_mv) / fine_mv, abs=0.011)
    assert "convergence" not in coarse


def test_bad_inputs_are_errors():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P"), smd("U1", "1", 1200, 200, "P")]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    with pytest.raises(ir.IRDropError, match="no pads"):
        ir.analyse_net(b, "P", None, [{"ref": "J9", "volts": 1}], SNK)
    with pytest.raises(ir.IRDropError, match="sink_model"):
        ir.analyse_net(b, "P", None, SRC, SNK, sink_model="x")


# --- contacts: a pad's edge, a barrel's wall, on any grid ---------------------

def test_lattice_green_function_and_the_radius_of_a_cell_set():
    assert ph.lattice_potential(0, 0) == 0.0
    assert ph.lattice_potential(1, 0) == pytest.approx(0.25, abs=1e-12)
    assert ph.lattice_potential(0, -1) == pytest.approx(0.25, abs=1e-12)
    assert ph.lattice_potential(1, 1) == pytest.approx(1 / math.pi, abs=1e-12)
    assert ph.lattice_potential(2, 0) == pytest.approx(1 - 2 / math.pi, abs=1e-12)
    far = math.log(40 / ph.PEACEMAN) / (2 * math.pi)
    assert ph.lattice_potential(40, 0) == pytest.approx(far, abs=2e-5)
    assert ph.lattice_potential(24, 32) == pytest.approx(far, abs=2e-5)
    assert ph.lattice_capacity_radius([(3, 7)]) == pytest.approx(ph.PEACEMAN)
    # two nodes: exp(2 pi U(1,0) / 2) times one node, by symmetry
    assert ph.lattice_capacity_radius([(0, 0), (1, 0)]) == pytest.approx(
        ph.PEACEMAN * math.exp(math.pi / 4), rel=1e-9)
    disc = [(i, j) for i in range(-9, 10) for j in range(-9, 10) if i * i + j * j <= 64]
    assert ph.lattice_capacity_radius(disc) == pytest.approx(8 - 0.39, abs=0.05)
    with pytest.raises(ValueError):
        ph.lattice_capacity_radius([])


def _pair(cell, shift=0.0, *, d=0.0, hole=0.0, sep=500.0):
    """Ohms between two round contacts ``sep`` apart on a 2000 x 1600 sheet:
    surface pads of diameter ``d`` on a pour, or through-hole pins of drill
    ``hole`` on a plane."""
    w, h = 2000.0, 1600.0
    if hole:
        b = LayoutBoard(outline=[(0, 0), (w, 0), (w, h), (0, h)],
                        layers=[Layer("TopLayer", "signal", 0),
                                Layer("InternalPlane1", "plane", 1, plane_net="P"),
                                Layer("BottomLayer", "signal", 2)])
        cu = [PadCopper("TopLayer", "round", hole + 24, hole + 24),
              PadCopper("BottomLayer", "round", hole + 24, hole + 24)]
    else:
        b = _board(w, h)
        b.regions.append(Region("TopLayer", list(b.outline), [], "P", "pour"))
        cu = [PadCopper("TopLayer", "round", d, d)]
    for ref, x in (("J1", w / 2 - sep / 2 + shift), ("U1", w / 2 + sep / 2 + shift)):
        b.pads.append(Pad(ref, "1", x, h / 2 + 0.6 * shift, 0, "P", list(cu), hole=hole))
    return _r(b, cell_mils=cell)[0]


def test_round_pad_on_a_pour_reads_the_same_on_any_grid():
    # 13.58 mil pad: from five cells across down to two thirds of a cell
    got = [_pair(c, s, d=13.58) for c, s in ((2.5, 0), (5, 0), (5, 1.85), (10, 0),
                                             (10, 3.7), (20, 0), (20, 10))]
    assert max(got) / min(got) - 1 < 0.004
    # and two pad sizes differ by Rs / pi ln(a2 / a1), whatever the sheet
    small, big = _pair(5, 1.85, d=13.58), _pair(5, 1.85, d=37.58)
    exact = RS / math.pi * (math.acosh(500 / 13.58) - math.acosh(500 / 37.58))
    assert small - big == pytest.approx(exact, rel=0.01)


@pytest.mark.parametrize("hole", [8.0, 12.0, 36.0, 60.0])
def test_through_hole_pin_meets_the_plane_at_its_hole_wall(hole):
    # the wall is hole / 2 + plating; a surface pad of that radius on a
    # pour of the same sheet is the reference (same finite-sheet term)
    ref = _pair(2.5, d=hole + 2 * 0.79)
    for cell, shift in ((5, 0), (5, 1.85), (10, 3.7), (10, 5), (20, 0), (20, 7.4), (20, 10)):
        assert _pair(cell, shift, hole=hole) == pytest.approx(ref, rel=0.004), (cell, shift)


def test_a_barrel_wall_is_narrower_than_its_land_into_a_pour():
    # A via's land is the same sheet of copper as the pour round it: the
    # current reaches the barrel at the hole wall, not at the land's edge.
    def run(land):
        b = _board(2000, 1600)
        b.layers = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
        for layer in ("TopLayer", "BottomLayer"):
            b.regions.append(Region(layer, list(b.outline), [], "P", "pour"))
        b.pads += [smd("J1", "1", 400, 800, "P", 40, 400),
                   Pad("U1", "1", 1600, 800, 0, "P", [PadCopper("BottomLayer", "rect", 40, 400)])]
        # the pours are joined only by this via: cut the top pour right of it
        b.regions[0] = Region("TopLayer", [(0, 0), (1100, 0), (1100, 1600), (0, 1600)], [], "P", "pour")
        b.regions[1] = Region("BottomLayer", [(900, 0), (2000, 0), (2000, 1600), (900, 1600)], [], "P", "pour")
        b.vias.append(Via(1000, 800, land, 10, "TopLayer", "BottomLayer", "P"))
        return _r(b, cell_mils=5)[0]
    assert run(20) == pytest.approx(run(40), rel=1e-3)


def test_pad_edge_sets_the_strip_length_off_the_grid():
    # pads whose edges fall between cell centres: the strip between them
    # is as long as the copper between their edges
    for x_src, x_snk in ((120, 2280), (123, 2277), (127.5, 2271)):
        b = _board(2400, 400)
        b.regions.append(Region("TopLayer", [(100, 100), (2300, 100), (2300, 300), (100, 300)],
                                [], "P", "pour"))
        b.pads += [smd("J1", "1", x_src, 200, "P", 40, 200), smd("U1", "1", x_snk, 200, "P", 40, 200)]
        r, _ = _r(b, cell_mils=10, sink_model="equipotential")
        assert r == pytest.approx(RS * (x_snk - x_src - 40) / 200, rel=1e-3)


def test_through_hole_sink_pin_is_one_conductor_on_every_layer():
    b = _board(1400, 400)
    for layer in ("TopLayer", "BottomLayer"):
        b.regions.append(Region(layer, [(100, 100), (1300, 100), (1300, 300), (100, 300)],
                                [], "P", "pour"))
    cu = [PadCopper("TopLayer", "rect", 60, 200), PadCopper("BottomLayer", "rect", 60, 200)]
    b.pads += [Pad("J1", "1", 150, 200, 0, "P", list(cu), hole=30),
               Pad("U1", "1", 1250, 200, 0, "P", list(cu), hole=30)]
    r_pin, _ = _r(b, cell_mils=10)
    r_eq, _ = _r(b, cell_mils=10, sink_model="equipotential")
    assert r_pin == pytest.approx(r_eq, rel=1e-6)
    # two layers in parallel between the pad edges, no barrel in the way
    assert r_pin == pytest.approx(RS * (1100 - 60) / 200 / 2, rel=2e-3)


# --- connectivity follows the copper ------------------------------------------

def _strip_with(regions):
    b = _board(2400, 400)
    b.regions += regions
    b.pads += [smd("J1", "1", 120, 200, "P", 40, 100), smd("U1", "1", 2280, 200, "P", 40, 100)]
    return b


def test_a_polygon_outline_is_not_copper():
    outline = [(100, 100), (2300, 100), (2300, 300), (100, 300)]
    poured = [(100, 150), (2300, 150), (2300, 250), (100, 250)]
    # outline alone (an unpoured polygon): nothing joins the pads
    b = _strip_with([Region("TopLayer", outline, [], "P", "pour_boundary", source="polygon:A")])
    with pytest.raises(ir.IRDropError, match="not connected"):
        _r(b, cell_mils=10)
    # outline and its poured copper: the poured copper alone carries it
    b = _strip_with([Region("TopLayer", outline, [], "P", "pour_boundary", source="polygon:A"),
                     Region("TopLayer", poured, [], "P", "pour", source="polygon:A"),
                     Region("TopLayer", outline, [], "P", "pour_boundary", source="polygon:B")])
    r, res = _r(b, cell_mils=10, sink_model="equipotential")
    assert r == pytest.approx(RS * 2120 / 100, rel=1e-3)
    note = [n for n in res["notes"] if "no poured copper" in n]
    assert len(note) == 1 and "polygon:B" in note[0] and "polygon:A" not in note[0]
    assert not any("plane clearance" in n for n in res["notes"])   # no plane on this board


@pytest.mark.parametrize("cell", [5, 10, 25])
def test_a_gap_narrower_than_a_cell_is_still_a_gap(cell):
    b = _strip_with([Region("TopLayer", [(100, 100), (1196, 100), (1196, 300), (100, 300)], [], "P", "pour"),
                     Region("TopLayer", [(1204, 100), (2300, 100), (2300, 300), (1204, 300)], [], "P", "pour")])
    with pytest.raises(ir.IRDropError, match="not connected"):
        _r(b, cell_mils=cell)


@pytest.mark.parametrize("rad,w", [(50, 12), (30, 10), (20, 12)])
def test_tight_arc_is_a_bent_strip_between_the_pad_edges(rad, w):
    from eda_agent.layout.model import Arc
    b = _board(600, 600)
    b.pads += [smd("J1", "1", 300 + rad, 300, "P", 8, 8), smd("U1", "1", 300, 300 + rad, "P", 8, 8)]
    b.arcs.append(Arc("TopLayer", 300, 300, rad, 0, 90, w, "P"))
    r, res = _r(b, cell_mils=2)
    theta = math.pi / 2 - 2 * math.asin(4 / rad)
    exact = RHO * theta / (1.4 * ph.MIL * math.log((rad + w / 2) / (rad - w / 2)))
    assert r == pytest.approx(exact, rel=0.004)
    assert res["tracks"][0]["length_mils"] == pytest.approx(rad * theta, abs=0.15)
    assert res["network"]["open_track_ends"] == 0


def test_tracks_that_cross_are_joined():
    b = _board(1400, 800)
    b.pads += [smd("J1", "1", 200, 400, "P", 40, 40), smd("U1", "1", 700, 700, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 400, 1200, 400, 10, "P"),
                 Track("TopLayer", 700, 100, 700, 700, 10, "P")]
    r, res = _r(b, cell_mils=5)
    per_mil = RHO / (10 * 1.4 * ph.MIL)
    assert r == pytest.approx(per_mil * (480 + 280), rel=1e-3)
    assert res["network"]["open_track_ends"] == 2          # the two far ends
    assert res["network"]["track_parts_inside_pours"] == 0


def test_via_on_an_unbroken_track_taps_it():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40),
               Pad("U1", "1", 700, 200, 0, "P", [PadCopper("BottomLayer", "rect", 40, 40)])]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    b.vias.append(Via(700, 200, 24, 12, "TopLayer", "BottomLayer", "P"))
    r, _ = _r(b, cell_mils=3)
    barrel = ph.via_barrel_resistance(62.99, 12, 0.79, RHO)
    assert r == pytest.approx(RHO / (10 * 1.4 * ph.MIL) * (500 - 20 - 12) + barrel, rel=2e-3)


@pytest.mark.parametrize("gap", [0.0, 3.0, 8.0])
def test_track_ends_whose_copper_overlaps_are_joined(gap):
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 200, 700, 200, 10, "P"),
                 Track("TopLayer", 700 + gap, 200, 1200, 200, 10, "P")]
    r, _ = _r(b, cell_mils=5)
    assert r == pytest.approx(RHO / (10 * 1.4 * ph.MIL) * (960 - gap), rel=1e-3)


def test_track_ends_apart_by_more_than_their_width_are_not_joined():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 200, 700, 200, 10, "P"),
                 Track("TopLayer", 712, 200, 1200, 200, 10, "P")]
    with pytest.raises(ir.IRDropError, match="not connected"):
        _r(b, cell_mils=5)


def test_stub_ending_inside_a_wide_trunk_is_joined_at_the_trunk():
    b = _board(1400, 800)
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 100), smd("U1", "1", 700, 600, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 200, 1200, 200, 100, "P"),
                 Track("TopLayer", 700, 240, 700, 600, 10, "P")]      # ends 40 mil off the centreline
    r, _ = _r(b, cell_mils=5)
    exact = RHO / (1.4 * ph.MIL) * (340 / 10 + 480 / 100)
    assert r == pytest.approx(exact, rel=1e-3)


def test_track_into_a_pour_reads_the_same_on_any_grid():
    got = []
    for cell in (10, 5, 4, 2.5):
        b = _board(2400, 400)
        b.regions.append(Region("TopLayer", [(100, 100), (1000, 100), (1000, 300), (100, 300)],
                                [], "P", "pour"))
        b.pads += [smd("J1", "1", 120, 200, "P", 40, 200), smd("U1", "1", 2000, 200, "P", 40, 40)]
        b.tracks.append(Track("TopLayer", 900, 200, 2000, 200, 10, "P"))
        got.append(_r(b, cell_mils=cell)[0])
    assert max(got) / min(got) - 1 < 0.005
    assert min(got) > RHO / (10 * 1.4 * ph.MIL) * 980      # the track outside the pour, and more


def _to_pour_edge(cell, delta, back):
    """A pour whose right edge is ``delta`` past a grid line, and a track
    that starts ``back`` mils inside that edge (negative: outside it)."""
    b = _board(2400, 400)
    e = 1000 + delta
    b.regions.append(Region("TopLayer", [(100, 100), (e, 100), (e, 300), (100, 300)], [], "P", "pour"))
    b.pads += [smd("J1", "1", 120, 200, "P", 40, 200), smd("U1", "1", 2000, 200, "P", 40, 40)]
    b.tracks.append(Track("TopLayer", e - back, 200, 2000, 200, 10, "P"))
    return _r(b, cell_mils=cell)


@pytest.mark.parametrize("delta", [0.5, 2.4, 9.5])
@pytest.mark.parametrize("back", [0.0, -3.0])
def test_a_track_end_that_touches_a_pour_is_joined_wherever_the_grid_falls(delta, back):
    # The end sits on the pour's edge, or 3 mil outside it with its round
    # cap (5 mil) still on the copper. The last sliver of the pour is
    # thinner than the grid resolves; the end is joined all the same, and
    # to the pour, not to a sliver that hangs on nothing.
    got = []
    for cell in (10, 5):
        r, res = _to_pour_edge(cell, delta, back)
        assert res["network"]["open_track_ends"] == 0
        got.append(r)
    assert got[0] == pytest.approx(got[1], rel=0.006)
    # the track from its end to the pad's edge, and the pour on top of it
    track = RHO / (10 * 1.4 * ph.MIL) * (1980 - (1000 + delta - back))
    assert track < got[1] < track + 4e-3


@pytest.mark.parametrize("cell", [10, 5])
def test_a_track_end_a_mil_short_of_a_pour_is_not_joined(cell):
    # 6 mil outside the edge with a 5 mil cap: a 1 mil gap is a gap
    with pytest.raises(ir.IRDropError, match="not connected"):
        _to_pour_edge(cell, 2.4, -6.0)


# --- cell size, inputs, reply ---------------------------------------------------

def test_auto_cell_follows_the_rail_not_the_board():
    b = _board(20000, 15000)
    b.pads += [smd("J1", "1", 200, 200, "P", 40, 40), smd("U1", "1", 1200, 200, "P", 40, 40)]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    r, res = _r(b)
    assert res["cell_mils"] == 10.0                       # a quarter of the pad
    assert r == pytest.approx(RHO / (10 * 1.4 * ph.MIL) * 960, rel=1e-3)
    assert not any("coarser" in n for n in res["notes"])


def test_auto_cell_says_when_the_board_forces_a_coarser_grid():
    b = LayoutBoard(outline=[(0, 0), (20000, 0), (20000, 15000), (0, 15000)],
                    layers=[Layer("TopLayer", "signal", 0),
                            Layer("InternalPlane1", "plane", 1, plane_net="P"),
                            Layer("BottomLayer", "signal", 2)])
    for ref, x in (("J1", 9000), ("U1", 11000)):
        b.pads.append(Pad(ref, "1", x, 7500, 0, "P", [PadCopper("TopLayer", "round", 30, 30),
                                                      PadCopper("BottomLayer", "round", 30, 30)], hole=12))
    r, res = _r(b)
    assert res["cell_mils"] > 30 / 4
    assert any("coarser than a quarter of the smallest pad" in n for n in res["notes"])
    # and the answer holds on that coarse grid: wall to wall on the plane
    a = 6 + 0.79
    assert r == pytest.approx(RS / math.pi * math.acosh(2000 / (2 * a)), rel=0.03)


def test_auto_cell_on_a_rail_routed_right_across_a_large_board():
    # Small pads ask for a 4 mil cell, but the rail's own extent, 19600 by
    # 14600 mil, would then be 180 million cells: the grid's own limit sets
    # the cell (1.02 sqrt(area / 4e6) = 8.63 before the margin round the
    # copper; 9 with it), a note says so, and the run goes through with the
    # right answer.
    b = _board(20000, 15000)
    b.pads += [smd("J1", "1", 200, 200, "P", 16, 16), smd("U1", "1", 19800, 14800, "P", 16, 16)]
    b.tracks += [Track("TopLayer", 200, 200, 19800, 200, 10, "P"),
                 Track("TopLayer", 19800, 200, 19800, 14800, 10, "P")]
    r, res = _r(b)
    assert res["cell_mils"] == 9.0
    assert any("coarser than a quarter of the smallest pad (4 mil) because of the board's size" in n
               for n in res["notes"])
    assert r == pytest.approx(RHO / (10 * 1.4 * ph.MIL) * (19600 + 14600 - 16), rel=2e-3)


def test_sources_and_sinks_must_say_volts_and_amps():
    b = _board()
    b.pads += [smd("J1", "1", 200, 200, "P"), smd("U1", "1", 1200, 200, "P")]
    b.tracks.append(Track("TopLayer", 200, 200, 1200, 200, 10, "P"))
    bad = [
        ([{"ref": "J1"}], SNK, "needs volts"),
        (SRC, [{"ref": "U1"}], "needs amps"),
        ([{"ref": "J1", "voltage": 1.0}], SNK, "unknown key"),
        (SRC, [{"ref": "U1", "amps": 1.0, "current": 2}], "unknown key"),
        (SRC, [{"ref": "U1", "amps": 0}], "above 0"),
        ([{"ref": "J1", "volts": -1}], SNK, "above 0"),
        ([{"ref": "J1", "volts": "x"}], SNK, "needs volts"),
        ([{"volts": 1.0}], SNK, "needs ref or pads"),
        ([], SNK, "at least one source"),
        (SRC, [], "at least one sink"),
        (SRC, ["U1"], "is a dict"),
    ]
    for sources, sinks, msg in bad:
        with pytest.raises(ir.IRDropError, match=msg):
            ir.analyse_net(b, "P", None, sources, sinks)
    with pytest.raises(ir.IRDropError, match="not on the board"):
        ir.analyse_net(b, "Q", None, SRC, SNK)
    with pytest.raises(ir.IRDropError, match="plating"):
        ir.analyse_net(b, "P", None, SRC, SNK, plating_mils=0)


def _two_rail_board():
    b = _board()
    b.pads += [smd("", "TP1", 200, 150, "P", 40, 40), smd("", "TP2", 200, 250, "G", 40, 40),
               smd("U1", "1", 1200, 150, "P", 40, 40), smd("U1", "2", 1200, 250, "G", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 150, 1200, 150, 10, "P"),
                 Track("TopLayer", 200, 250, 1200, 250, 20, "G")]
    return b


def test_return_pads_name_the_return_of_a_source_given_by_pads():
    b = _two_rail_board()
    src = [{"pads": [".TP1"], "volts": 1.0, "return_pads": [".TP2"]}]
    out = ir.analyse(b, "P", None, src, SNK, return_net="G", cell_mils=5, check_convergence=False)
    per_mil = RHO / (1.4 * ph.MIL)
    assert out["supply"]["sinks"][0]["drop_mv"] / 1e3 == pytest.approx(per_mil * 960 / 10, rel=1e-3)
    assert out["return"]["sinks"][0]["drop_mv"] / 1e3 == pytest.approx(per_mil * 960 / 20, rel=1e-3)
    assert out["rails"][0]["total_drop_mv"] / 1e3 == pytest.approx(per_mil * 960 * 0.15, rel=1e-3)
    # without them a free pad has no component to find its return by
    with pytest.raises(ir.IRDropError, match="return_pads"):
        ir.analyse(b, "P", None, [{"pads": [".TP1"], "volts": 1.0}], SNK, return_net="G", cell_mils=5)
    with pytest.raises(ir.IRDropError, match="supply net itself"):
        ir.analyse(b, "P", None, src, SNK, return_net="P")
    with pytest.raises(ir.IRDropError, match="not on the board"):
        ir.analyse(b, "P", None, src, SNK, return_net="AGND")


def test_reply_is_plain_json_with_no_nan_and_lists_the_worst_pins_only():
    import json
    b = plane_board()
    # an island of the net nothing reaches, on the top layer
    b.regions.append(Region("TopLayer", [(100, 100), (300, 100), (300, 300), (100, 300)],
                            [], "+1V0", "pour"))
    res = ir.analyse_net(b, "+1V0", stack_from_stackup(STACKUP),
                         [{"ref": "U1", "volts": 1.0}], [{"ref": "U2", "amps": 2.0}],
                         cell_mils=10, check_convergence=False, limit=2)
    text = json.dumps(ir.public(res), allow_nan=False)
    assert "_pins" not in text and "NaN" not in text
    s = res["sinks"][0]
    assert s["pin_count"] == 4 and len(s["pins"]) == 2 and len(s["_pins"]) == 4
    assert s["pins"][0]["pad"] == s["worst_pin"]
    assert s["worst_pin_volts"] <= s["best_pin_volts"]
    assert any("not connected to any source" in n for n in res["notes"])
    assert any(n.startswith("0.040 in2 of +1V0 copper") for n in res["notes"])
    # a speck that would print as 0.000 in2 is not worth a note
    b.regions[-1] = Region("TopLayer", [(100, 100), (115, 100), (115, 115), (100, 115)],
                           [], "+1V0", "pour")
    res = ir.analyse_net(b, "+1V0", stack_from_stackup(STACKUP),
                         [{"ref": "U1", "volts": 1.0}], [{"ref": "U2", "amps": 2.0}],
                         cell_mils=10, check_convergence=False, limit=2)
    assert not any("not connected to any source" in n for n in res["notes"])


def test_public_result_has_no_working_arrays_and_no_nan():
    raw = {"a": float("nan"), "b": [np.float64(1.5), float("inf"), np.int64(3)],
           "_maps": {"V": np.zeros(3)}, "c": {"_pins": [1], "d": np.float32(0.25), "e": "x"}}
    out = ir.public(raw)
    assert out == {"a": None, "b": [1.5, None, 3], "c": {"d": 0.25, "e": "x"}}
    assert type(out["b"][0]) is float and type(out["b"][2]) is int
    import json
    assert "NaN" not in json.dumps(out) and "Infinity" not in json.dumps(out)


def test_a_two_pin_sink_shares_equally_or_is_shorted_inside_the_part():
    # U1's two pins are fed by tracks of 460 and 1460 mil (pad edge to pad edge)
    b = _board(2000, 400)
    b.pads += [smd("J1", "1", 200, 100, "P", 40, 40), smd("J1", "2", 200, 300, "P", 40, 40),
               smd("U1", "1", 700, 100, "P", 40, 40), smd("U1", "2", 1700, 300, "P", 40, 40)]
    b.tracks += [Track("TopLayer", 200, 100, 700, 100, 10, "P"),
                 Track("TopLayer", 200, 300, 1700, 300, 10, "P")]
    per_mil = RHO / (10 * 1.4 * ph.MIL)
    r1, r2 = per_mil * 460, per_mil * 1460
    # per pin: half an amp down each, so the far pin is the worst
    res = ir.analyse_net(b, "P", None, SRC, SNK, cell_mils=5, check_convergence=False)
    snk = res["sinks"][0]
    assert snk["worst_pin"] == "U1.2" and snk["pin_count"] == 2
    assert snk["drop_mv"] == pytest.approx(0.5 * r2 * 1e3, rel=3e-3)
    volts = {p["pad"]: p["volts"] for p in snk["pins"]}
    assert 1.0 - volts["U1.1"] == pytest.approx(0.5 * r1, rel=3e-3)
    # shorted inside the part: one voltage, the current divides as 1 / R
    res = ir.analyse_net(b, "P", None, SRC, SNK, cell_mils=5, check_convergence=False,
                         sink_model="equipotential")
    snk = res["sinks"][0]
    volts = [p["volts"] for p in snk["pins"]]
    assert volts[0] == pytest.approx(volts[1], abs=2e-6)
    assert snk["drop_mv"] == pytest.approx(r1 * r2 / (r1 + r2) * 1e3, rel=3e-3)
    amps = sorted(t["amps"] for t in res["tracks"])
    assert amps == pytest.approx(sorted([r2 / (r1 + r2), r1 / (r1 + r2)]), rel=3e-3)
