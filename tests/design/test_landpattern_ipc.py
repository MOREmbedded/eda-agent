# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""IPC-7351B arithmetic: equations, goal tables, rounding, input checks.

The strongest checks here are external: a vendor table built per
IPC-7351 (KEMET C1002 Table 3, chips; KEMET T491 Table 2, tantalum) and
the PCB Libraries SMD reference calculator cells quoted in the thread
that ties them to IPC-7351B section 3.1.1. The engine reproduces each
exactly, which pins the RMS form of S, the chip goals, the inward L
goals, the rounding and the courtyard rule at once.
"""

from __future__ import annotations

import math

import pytest

from eda_agent.design.landpattern import (
    Dim,
    PackageSpec,
    SpecError,
    build_footprint,
    dim,
)
from eda_agent.design.landpattern import ipc7351 as ipc
from tests.design import _landpattern_cases as cases


def hand(L, T, W, toe, heel, side, F=0.10, P=0.05):
    """The IPC-7351 equations written out independently of the engine.

    Z = Lmin + 2 Jt + sqrt(CL^2 + F^2 + P^2)
    G = Smax(RMS) - 2 Jh - sqrt(CS^2 + F^2 + P^2)
    X = Wmin + 2 Js + sqrt(CW^2 + F^2 + P^2)
    Smin = Lmin - 2 Tmax, Smax = Lmax - 2 Tmin,
    CS = Stol(RMS) = sqrt(CL^2 + 2 CT^2),
    Smax(RMS) = Smax - (Smax - Smin - Stol(RMS)) / 2.
    """
    cl, ct, cw = L[1] - L[0], T[1] - T[0], W[1] - W[0]
    s_min = L[0] - 2 * T[1]
    s_max = L[1] - 2 * T[0]
    cs = math.sqrt(cl ** 2 + 2 * ct ** 2)
    s_max_rms = s_max - (s_max - s_min - cs) / 2
    z = L[0] + 2 * toe + math.sqrt(cl ** 2 + F ** 2 + P ** 2)
    g = s_max_rms - 2 * heel - math.sqrt(cs ** 2 + F ** 2 + P ** 2)
    x = W[0] + 2 * side + math.sqrt(cw ** 2 + F ** 2 + P ** 2)
    return z, g, x


# --- the equations against independent numbers ------------------------------------


def test_pcb_libraries_calculator_cells_reproduced():
    """PCB Libraries forum topic 785 (Feb 2021) screenshot of the SMD
    Reference Calculator, which the thread says matches the IPC-7351B
    section 3.1.1 example: Lmin 5.85, Lmax 6.20, T 0.40-1.27, W 0.31-0.51,
    fab +/-0.05 and placement +/-0.025 (F 0.10, P 0.05), goals 0.35 /
    0.35 / 0.03. Its cells: Stol(RMS) 1.28, New Smax 4.99, Zmax 6.91742,
    Gmin 3.01053, Yref 0.59913; rounded on its 0.02 place and 0.01 size
    grid: C 4.96, X 1.95, Y 0.60."""
    goals = ipc.Goals(0.35, 0.35, 0.03, 0.25)
    c = ipc.land_calc((5.85, 6.20), (0.40, 1.27), (0.31, 0.51), goals,
                      F=0.10, P=0.05, size_round=0.01, span_round=0.02)
    assert c.s_tol_rms == pytest.approx(1.279179, abs=1e-6)
    assert c.s_max_rms == pytest.approx(4.99459, abs=1e-5)
    assert c.Z == pytest.approx(6.91742, abs=1e-5)
    assert c.G == pytest.approx(3.01053, abs=1e-5)
    assert c.X == pytest.approx(0.59913, abs=1e-5)
    assert (c.span, c.pad_length, c.pad_width) == (4.96, 1.95, 0.60)


def test_ipc7351_2005_worked_example_stol_rms():
    """IPC-7351:2005 p. 9: L 5.8-6.2, T 0.4-1.27 give Stol 2.14 worst
    case and Stol(RMS) 1.29."""
    c = ipc.land_calc((5.8, 6.2), (0.4, 1.27), (0.3, 0.5),
                      ipc.Goals(0.35, 0.35, 0.03, 0.25))
    assert c.s_min == pytest.approx(3.26)
    assert c.s_max == pytest.approx(5.40)
    assert c.s_tol == pytest.approx(2.14)
    assert round(c.s_tol_rms, 2) == 1.29


def test_soic8_hand_calculation_exact():
    """TI D0008A (LM358B Rev. AB p. 54) at density N with the default F
    0.10 and P 0.05: Z 6.906, G 2.972, X 0.599 before rounding; lands
    1.95 x 0.60 on a 4.9 mm span after."""
    m = build_footprint(cases.soic8())
    c = m.ipc["calcs"]["rows"]
    z, g, x = hand((5.80, 6.19), (0.41, 1.27), (0.31, 0.51), 0.35, 0.35, 0.03)
    assert (round(z, 3), round(g, 3), round(x, 3)) == (6.906, 2.972, 0.599)
    assert c["Z"] == pytest.approx(z, abs=1e-3)
    assert c["G"] == pytest.approx(g, abs=1e-3)
    assert c["X"] == pytest.approx(x, abs=1e-3)
    assert (c["pad_length"], c["span"], c["pad_width"]) == (1.95, 4.9, 0.6)
    assert c["lead_form"] == "gullwing"
    p1 = m.pad("1")
    assert (p1.x, p1.y, p1.width, p1.height) == (-2.45, 1.905, 1.95, 0.6)


def test_rms_form_is_the_symmetric_b_form_not_the_2005_one():
    """IPC-7351:2005 p. 9 took Smax(RMS) = Smin + Stol(RMS); IPC-7351B
    (and the PCBL calculator) shrink the S range about its middle. The two
    differ by half the difference, which this pins."""
    c = ipc.land_calc((5.80, 6.19), (0.41, 1.27), (0.31, 0.51),
                      ipc.Goals(0.35, 0.35, 0.03, 0.25))
    old = c.s_min + c.s_tol_rms
    assert c.s_max_rms == pytest.approx(c.s_max - (c.s_tol - c.s_tol_rms) / 2)
    assert c.s_max_rms - old == pytest.approx((c.s_tol - c.s_tol_rms) / 2)
    assert c.s_max_rms != pytest.approx(old, abs=0.1)


def test_outward_l_bend_heel_for_sot23_5():
    """TI DBV0005A: Smin = 2.6 - 2 x 0.6 = 1.4 <= body 1.75 and Ttol 0.3 <
    0.5, so the outward L heel goal 0.15 applies (Hausherr CADlib p. 23).
    Z 3.715, G 1.298, X 0.589; lands 1.20 x 0.60 on 2.5."""
    m = build_footprint(cases.sot23_5())
    c = m.ipc["calcs"]["rows"]
    assert c["lead_form"] == "outward_l"
    assert c["goals"]["inner"] == 0.15
    z, g, x = hand((2.6, 3.0), (0.3, 0.6), (0.3, 0.5), 0.35, 0.15, 0.03)
    assert (round(z, 3), round(g, 3), round(x, 3)) == (3.715, 1.298, 0.589)
    assert (c["Z"], c["G"], c["X"]) == pytest.approx((z, g, x), abs=1e-3)
    assert (c["pad_length"], c["span"], c["pad_width"]) == (1.2, 2.5, 0.6)


def test_fine_pitch_table_for_lqfp():
    """TI PN0080A, 0.5 mm pitch: the fine pitch table (side -0.02). Z
    14.915, G 11.798, X 0.280; lands 1.55 x 0.30 on 13.4."""
    m = build_footprint(cases.lqfp80())
    c = m.ipc["calcs"]["rows_x"]
    assert c["lead_form"] == "gullwing_fine"
    assert c["goals"]["side"] == -0.02
    z, g, x = hand((13.8, 14.2), (0.45, 0.75), (0.17, 0.27), 0.35, 0.35, -0.02)
    assert (round(z, 3), round(g, 3), round(x, 3)) == (14.915, 11.798, 0.28)
    assert (c["Z"], c["G"], c["X"]) == pytest.approx((z, g, x), abs=1e-3)
    assert (c["pad_length"], c["span"], c["pad_width"]) == (1.55, 13.4, 0.3)


def test_qfn_no_lead_goals_and_heel_trim():
    """TI RGT0016C: no-lead goals (toe 0.30, heel 0, side -0.04): Z 3.729,
    G 2.009, X 0.264; lands 0.85 x 0.25 on 2.9. The 1.75 mm thermal land
    (the 1.68 +/-0.07 pad at its maximum) leaves 0.15 mm, so the heels are
    trimmed to keep 0.20 mm (IPC-7351:2005 p. 14 note)."""
    m = build_footprint(cases.qfn16())
    c = m.ipc["calcs"]["rows_x"]
    z, g, x = hand((2.9, 3.1), (0.3, 0.5), (0.18, 0.30), 0.30, 0.0, -0.04)
    assert (round(z, 3), round(g, 3), round(x, 3)) == (3.729, 2.009, 0.264)
    assert (c["Z"], c["G"], c["X"]) == pytest.approx((z, g, x), abs=1e-3)
    assert (c["pad_length"], c["span"], c["pad_width"]) == (0.85, 2.9, 0.25)
    p1 = m.pad("1")
    ep = m.pad("17")
    assert ep.width == 1.75
    assert p1.width == pytest.approx(0.80)
    gap = (p1.x + p1.width / 2) - (-ep.width / 2)
    assert -gap == pytest.approx(0.20, abs=1e-6) or abs(gap) >= 0.2 - 1e-6
    assert any("trimmed" in n for n in m.ipc["notes"])


# --- vendor tables built per IPC-7351 ------------------------------------------------


def _kemet_values(m):
    p2 = m.pad("2")
    x0, y0, x1, y1 = m.courtyard_bbox()
    return (round(p2.x, 4), p2.width, p2.height, round(x1 - x0, 4), round(y1 - y0, 4))


@pytest.mark.parametrize("size", ["0402", "0603", "0805", "1206"])
@pytest.mark.parametrize("density", ["M", "N"])
def test_kemet_c1002_table3_reproduced_exactly(size, density):
    """KEMET C1002 Table 3 p. 12 (C, Y, X, V1, V2) from the p. 3
    dimensions, levels A and B, with P = 0.10: that table was built with
    the larger placement tolerance (see ipc7351.PLACE_TOL)."""
    m = build_footprint(cases.kemet_chip(size), density, place_tol=0.10)
    assert _kemet_values(m) == cases.KEMET_TABLE3[size][density]


@pytest.mark.parametrize("size", ["0402", "1206"])
def test_kemet_c1002_level_c_reproduced(size):
    m = build_footprint(cases.kemet_chip(size), "L", place_tol=0.10)
    assert _kemet_values(m) == cases.KEMET_TABLE3[size]["L"]


@pytest.mark.parametrize("size,ours", [("0603", 0.70), ("0805", 0.80)])
def test_kemet_c1002_level_c_centre_disagreement(size, ours):
    """For 0603 and 0805 at level C, KEMET prints C = 0.60 and 0.75 but its
    own V1 in the same row (2.40, 2.80) only fits C = 0.70 and 0.80, which
    is what the equations give. Y, X, V1 and V2 still match exactly."""
    m = build_footprint(cases.kemet_chip(size), "L", place_tol=0.10)
    got = _kemet_values(m)
    want = cases.KEMET_TABLE3[size]["L"]
    assert got[0] == ours != want[0]
    assert got[1:] == want[1:]


def test_kemet_t491_inward_l_table2():
    """KEMET T491 case A, Table 2 p. 15 (an IPC-7351 table for inward L
    leads). With the default F 0.10 / P 0.05 its Z (2L + S: 3.82 / 4.42 /
    5.02) and widths (1.13 / 1.23 / 1.35) come out to 0.01 mm at levels C /
    B / A, and its gaps (0.98 / 0.82 / 0.62) step by twice the inner goal
    differences (0.15 - 0.07, 0.25 - 0.15). The absolute gap only matches
    with T up to 1.10, against the 1.00 the dimension table prints (S 0.80
    +0.2/-0.3): KEMET's land table evidently used +/-0.3."""
    rows = {"L": (0.98, 1.42, 1.13), "N": (0.82, 1.80, 1.23), "M": (0.62, 2.20, 1.35)}
    gs = {}
    for d, (s_gap, l_len, w) in rows.items():
        m = build_footprint(cases.t491_case_a(), d)
        c = m.ipc["calcs"]["molded"]
        assert c["Z"] == pytest.approx(2 * l_len + s_gap, abs=0.005)
        assert c["X"] == pytest.approx(w, abs=0.005)
        gs[d] = c["G"]
        wide = cases.t491_case_a()
        wide.lead_length = Dim(0.5, 0.8, 1.1)
        cw = build_footprint(wide, d).ipc["calcs"]["molded"]
        assert cw["G"] == pytest.approx(s_gap, abs=0.005)
    assert gs["M"] - gs["N"] == pytest.approx(0.62 - 0.82, abs=1e-6)
    assert gs["N"] - gs["L"] == pytest.approx(0.82 - 0.98, abs=1e-6)


# --- tables and constants ----------------------------------------------------------


def test_goal_table_values():
    """Spot values with their sources (see ipc7351 comments)."""
    gw = ipc.goal_table("gullwing")
    assert (gw.least, gw.nominal, gw.most) == (
        ipc.Goals(0.15, 0.25, 0.01, 0.10), ipc.Goals(0.35, 0.35, 0.03, 0.25),
        ipc.Goals(0.55, 0.45, 0.05, 0.50))
    assert ipc.goal_table("gullwing_fine").nominal.side == -0.02
    assert ipc.goal_table("outward_l").nominal.inner == 0.15
    assert ipc.goal_table("chip").nominal == ipc.Goals(0.35, 0.0, 0.0, 0.25)
    assert ipc.goal_table("chip_small").nominal == ipc.Goals(0.20, 0.0, 0.0, 0.15)
    assert ipc.goal_table("nolead").nominal == ipc.Goals(0.30, 0.0, -0.04, 0.25)
    assert ipc.goal_table("jlead").nominal == ipc.Goals(0.35, -0.20, 0.03, 0.25)
    assert ipc.goal_table("inward_l").nominal == ipc.Goals(0.50, 0.15, -0.05, 0.25)
    assert ipc.goal_table("melf").nominal == ipc.Goals(0.40, 0.10, 0.05, 0.25)
    assert ipc.BGA_COURTYARD == {"L": 0.5, "N": 1.0, "M": 2.0}
    assert (ipc.FAB_TOL, ipc.PLACE_TOL) == (0.10, 0.05)


def test_density_levels_grow_the_pattern():
    sizes = {}
    for d in ("L", "N", "M"):
        m = build_footprint(cases.soic8(), d)
        x0, y0, x1, y1 = m.courtyard_bbox()
        sizes[d] = (m.ipc["calcs"]["rows"]["Z"], x1 - x0)
    assert sizes["L"][0] < sizes["N"][0] < sizes["M"][0]
    assert sizes["L"][1] < sizes["N"][1] < sizes["M"][1]
    with pytest.raises(ValueError):
        build_footprint(cases.soic8(), "X")


def test_rounding_is_half_away_from_zero():
    assert ipc.round_nearest(0.875, 0.05) == 0.9
    assert ipc.round_nearest(0.825, 0.05) == 0.85
    assert ipc.round_nearest(1.020928, 0.05) == 1.0
    assert ipc.round_nearest(-0.875, 0.05) == -0.9
    assert ipc.round_out(1.52, 0.05) == 1.55
    assert ipc.round_out(-1.52, 0.05) == -1.55
    assert ipc.round_up(0.8407, 0.05) == 0.85


def test_bga_land_table():
    """Hausherr CADlib p. 34: the IPC uses the maximum land variation."""
    assert ipc.bga_land(0.30)["land"] == 0.25
    assert ipc.bga_land(0.50)["land"] == 0.45
    assert ipc.bga_land(0.50, use_mmc=False)["land"] == 0.40
    assert ipc.bga_land(0.75)["land"] == 0.60
    assert ipc.bga_land(0.50, collapsing=False)["land"] == 0.60
    off = ipc.bga_land(0.32)
    assert not off["in_table"] and off["land"] == 0.25 and "note" in off


def test_pth_padstack_matches_published_example():
    """Lead 0.55 mm: holes 0.80 / 0.75 / 0.70 and pads 1.50 / 1.35 / 1.20
    at levels A / B / C (IPC-2222 Table 9-3 and IPC-2221 Tables 9-1, 9-2
    as Hausherr CADlib pp. 55-56 reproduce them)."""
    assert ipc.pth_padstack(0.55, "M")["hole"] == 0.80
    assert ipc.pth_padstack(0.55, "M")["pad"] == 1.50
    assert ipc.pth_padstack(0.55, "N")["hole"] == 0.75
    assert ipc.pth_padstack(0.55, "N")["pad"] == 1.35
    assert ipc.pth_padstack(0.55, "L")["hole"] == 0.70
    assert ipc.pth_padstack(0.55, "L")["pad"] == 1.20


def test_tolerance_overrides():
    a = build_footprint(cases.soic8()).ipc
    b = build_footprint(cases.soic8(), fab_tol=0.05, place_tol=0.025).ipc
    assert (a["F"], a["P"]) == (0.10, 0.05)
    assert (b["F"], b["P"]) == (0.05, 0.025)
    assert b["calcs"]["rows"]["Z"] < a["calcs"]["rows"]["Z"]
    with pytest.raises(SpecError):
        build_footprint(cases.soic8(), place_tol=2.0)


# --- input checks -------------------------------------------------------------------


def test_dim_forms():
    assert dim(0.95) == Dim(0.95, 0.95, 0.95)
    assert dim((1.0, 2.0)).nom == 1.5
    assert dim((1.0, 1.2, 2.0)).nom == 1.2
    assert dim({"nom": 1.6, "tol": 0.15}).pair == (1.45, 1.75)
    assert dim({"nom": 0.8, "plus": 0.2, "minus": 0.3}).pair == (0.5, 1.0)
    d = dim({"min": 1, "max": 2, "cite": {"document": "X", "page": "3"}})
    assert d.cite.page == "3"
    with pytest.raises(SpecError):
        dim({"max": 2})
    with pytest.raises(SpecError):
        dim([1, 2, 3, 4])


def _soic(**kw):
    base = dict(family="soic", pins=8, pitch=1.27, lead_span_x=(5.80, 6.19),
                lead_length=(0.41, 1.27), lead_width=(0.31, 0.51),
                body_x=(3.81, 3.98), body_y=(4.81, 5.00))
    base.update(kw)
    return PackageSpec(**base)


@pytest.mark.parametrize("kw,needle", [
    ({"lead_span_x": (6.19, 5.80)}, "min <= nom <= max"),
    ({"lead_span_x": None}, "needs lead_span_x"),
    ({"lead_width": (0.3, 1.3)}, "not below the pitch"),
    ({"body_x": (6.5, 6.6)}, "wider than lead_span_x"),
    ({"pins": 7}, "even pin count"),
    ({"ep_x": (2.0, 2.2)}, "both ep_x and ep_y"),
    ({"ep_x": (2.0, 2.2), "ep_y": (6.0, 6.2)}, "larger than the body"),
    ({"lead_length": (3.0, 3.2)}, "do not fit"),
    ({"family": "nonsense"}, "unknown family"),
])
def test_bad_specs_are_refused_with_a_reason(kw, needle):
    with pytest.raises(SpecError) as e:
        build_footprint(_soic(**kw))
    assert needle in str(e.value)


def test_sot23_and_quad_pin_counts_checked():
    with pytest.raises(SpecError, match="3, 5 or 6"):
        s = cases.sot23_5()
        s.pins = 4
        build_footprint(s)
    with pytest.raises(SpecError, match="divisible by 4"):
        s = cases.qfn16()
        s.pins = 14
        build_footprint(s)
    with pytest.raises(SpecError, match="adding up"):
        s = cases.qfn14_rect()
        s.pins_per_side = (5, 2, 5, 3)
        build_footprint(s)


def test_spec_round_trips_through_dict():
    s = cases.soic8()
    t = PackageSpec.from_dict(s.to_dict())
    assert t.to_dict() == s.to_dict()
    assert build_footprint(t).to_dict() == build_footprint(s).to_dict()
    with pytest.raises(SpecError, match="unknown PackageSpec fields"):
        PackageSpec.from_dict({"family": "soic", "lead_spam": 1})
