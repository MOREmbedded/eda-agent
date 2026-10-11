# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Golden land patterns from public datasheets.

Each case is a datasheet that prints both the package drawing and a
recommended land pattern (sources and pages in ``_landpattern_cases``).
Two kinds of check per case:

(a) the IPC-7351B calculation equals a hand calculation of the equations:
    Z, G and X to 0.001 mm before rounding, and the rounded land length,
    span and width exactly;
(b) the pattern lands close to the vendor's own: pad count and pitch
    exactly, and every vendor land centre inside the IPC land, with the
    span and land length within 0.5 mm and the width within 0.45 mm.

Why those bounds: vendors move lands on purpose (TI's drawings say
"Publication IPC-7351 may have alternate designs"). In these ten cases
the largest deviations are TI's SOIC-8 heel (span 0.5 mm wider, lands 0.4
mm shorter) and Nexperia's SOD123 land width (1.22 against IPC 0.80). A
transcription error of the kind the check exists to catch (mm read as
inches, span read as body, width read as pitch) moves a dimension by a
millimetre or more, or by a factor, so it fails these bounds.
"""

from __future__ import annotations

import pytest

from eda_agent.design.landpattern import build_footprint, compare_to_vendor
from eda_agent.design.landpattern.compare import pattern_metrics
from tests.design import _landpattern_cases as cases
from tests.design.test_landpattern_ipc import hand

SPAN_TOL = 0.5
LENGTH_TOL = 0.5
WIDTH_TOL = 0.45
EP_TOL = 0.1
LAND_TOL = 0.05

#: case: (spec factory, calc name, (L, T, W, toe, heel, side),
#:        hand Z/G/X to 3 places, rounded (length, span, width), pad count)
GOLDEN = {
    "chip_0603_kemet": (lambda: cases.kemet_chip("0603"), "chip",
                        ((1.45, 1.75), (0.20, 0.50), (0.65, 0.95), 0.35, 0.0, 0.0),
                        (2.47, 0.628, 0.970), (0.90, 1.5, 0.95), 2),
    "sod123_nexperia": (cases.sod123, "sod",
                        ((3.45, 3.75), (0.15, 0.45), (0.55, 0.70), 0.35, 0.15, 0.03),
                        (4.47, 2.428, 0.797), (1.0, 3.4, 0.8), 2),
    "sot23_3_nexperia": (cases.sot23_3, "rows",
                         ((2.1, 2.5), (0.15, 0.45), (0.38, 0.48), 0.35, 0.15, 0.03),
                         (3.215, 1.098, 0.59), (1.05, 2.2, 0.6), 3),
    "sot23_5_ti": (cases.sot23_5, "rows",
                   ((2.6, 3.0), (0.3, 0.6), (0.3, 0.5), 0.35, 0.15, 0.03),
                   (3.715, 1.298, 0.589), (1.2, 2.5, 0.6), 5),
    "soic8_ti": (cases.soic8, "rows",
                 ((5.80, 6.19), (0.41, 1.27), (0.31, 0.51), 0.35, 0.35, 0.03),
                 (6.906, 2.972, 0.599), (1.95, 4.9, 0.6), 8),
    "tssop8_ti": (cases.tssop8, "rows",
                  ((6.2, 6.6), (0.50, 0.75), (0.19, 0.30), 0.35, 0.35, 0.03),
                  (7.315, 4.171, 0.407), (1.55, 5.7, 0.4), 8),
    "qfn16_ti": (cases.qfn16, "rows_x",
                 ((2.9, 3.1), (0.3, 0.5), (0.18, 0.30), 0.30, 0.0, -0.04),
                 (3.729, 2.009, 0.264), (0.85, 2.9, 0.25), 17),
    "lqfp80_ti": (cases.lqfp80, "rows_x",
                  ((13.8, 14.2), (0.45, 0.75), (0.17, 0.27), 0.35, 0.35, -0.02),
                  (14.915, 11.798, 0.28), (1.55, 13.4, 0.3), 80),
    "sot223_diodes": (cases.sot223, "rows",
                      ((6.90, 7.10), (0.85, 1.05), (0.60, 0.80), 0.35, 0.35, 0.03),
                      (7.829, 4.209, 0.889), (1.8, 6.0, 0.9), 4),
}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_a_hand_calculation(case):
    make, calc_name, eq, zgx, rounded, _ = GOLDEN[case]
    m = build_footprint(make())
    c = m.ipc["calcs"][calc_name]
    z, g, x = hand(*eq)
    assert (round(z, 3), round(g, 3), round(x, 3)) == pytest.approx(zgx, abs=1e-9)
    assert c["Z"] == pytest.approx(z, abs=1e-3)
    assert c["G"] == pytest.approx(g, abs=1e-3)
    assert c["X"] == pytest.approx(x, abs=1e-3)
    assert (c["pad_length"], c["span"], c["pad_width"]) == rounded


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_b_close_to_vendor_pattern(case):
    make, _, _, _, _, count = GOLDEN[case]
    spec = make()
    m = build_footprint(spec)
    assert len(m.pads) == count
    r = compare_to_vendor(m, spec.vendor, span_tol=SPAN_TOL,
                          length_tol=LENGTH_TOL, width_tol=WIDTH_TOL,
                          ep_tol=EP_TOL, pitch_tol=1e-6)
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]
    v = spec.vendor
    if v.pitch is not None:
        assert r["metrics"]["pitch"] == pytest.approx(v.pitch, abs=1e-6)
    # The vendor's land centre (half its centre-to-centre span from the
    # origin) must sit inside the IPC land of the right-hand row.
    if v.span is not None:
        p = next(q for q in m.pads if q.group == "right")
        assert p.x - p.width / 2 < v.span / 2 < p.x + p.width / 2, (
            case, p.x, p.width, v.span)


def test_sot223_matches_diodes_z_note():
    """Diodes SOT223 p. 1: Y2 8.00 "is calculated by adding 0.2 mm to the
    'Z' dimension" (IPC-7351A). Our rounded Z is 7.80, so Y2 - 0.2 equals
    it exactly; the leads' land centre (C1 6.40 / 2 = 3.20) is inside our
    land (2.10 to 3.90)."""
    m = build_footprint(cases.sot223())
    c = m.ipc["calcs"]["rows"]
    assert c["z_rounded"] == pytest.approx(8.00 - 0.2, abs=1e-9)
    p1 = m.pad("1")
    assert p1.x - p1.width / 2 < -6.40 / 2 < p1.x + p1.width / 2
    tab = m.pad("4")
    assert tab.x - tab.width / 2 < 6.40 / 2 < tab.x + tab.width / 2
    assert tab.height == 3.2


def test_bga_land_matches_ti_exactly():
    """TI ZXH0080A: 0.30 mm nominal ball, 0.5 mm pitch. IPC-7351B gives a
    0.25 mm land (20 % reduction, maximum of the 0.25-0.20 variation); TI's
    example board layout prints 0.25 (0.235-0.265)."""
    spec = cases.bga80()
    m = build_footprint(spec)
    assert len(m.pads) == 80
    assert {p.width for p in m.pads} == {0.25}
    r = compare_to_vendor(m, spec.vendor, width_tol=LAND_TOL)
    assert r["ok"], r["checks"]
    assert m.ipc["calcs"]["ball_land"]["in_table"] is True


def test_qfn_thermal_land_and_paste_against_ti():
    """TI RGT0016C prints a 1.68 thermal land (the nominal die pad) and an
    85 % stencil. The IPC land is the maximum die pad, 1.75 (Hausherr CADlib
    p. 30), within 0.1 mm of TI's; the default paste coverage is 65 %, inside
    the 50 to 80 % TB389 range, which TI's 85 % is not."""
    spec = cases.qfn16()
    m = build_footprint(spec)
    met = pattern_metrics(m)
    assert abs(met["ep_x"] - spec.vendor.ep_x) <= EP_TOL
    assert 0.5 <= met["paste_coverage"] <= 0.8
    assert met["paste_coverage"] == pytest.approx(0.65, abs=0.03)


def test_explicit_vendor_lands_matched_by_position():
    """Nexperia SOT23 Fig. 2 (p. 3) as explicit lands: 0.7 x 0.6 (span x
    pitch direction in zero orientation), rows 2.0 apart, 1.9 between the
    two lands of the left row. Matching centres both patterns and pairs
    nearest first, so a pattern turned half round still pairs up; a
    missing land fails the count."""
    from eda_agent.design.landpattern import CustomPad, VendorLandPattern

    lands = (CustomPad("1", -1.0, 0.95, 0.7, 0.6), CustomPad("2", -1.0, -0.95, 0.7, 0.6),
             CustomPad("3", 1.0, 0.0, 0.7, 0.6))
    m = build_footprint(cases.sot23_3())
    r = compare_to_vendor(m, VendorLandPattern(pads=lands))
    got = {c["check"]: c for c in r["checks"]}
    assert r["ok"] and got["pad_count"]["ipc"] == 3
    assert got["position"]["ipc"] == pytest.approx(0.1333, abs=1e-3)
    assert got["size"]["ipc"] == pytest.approx(1.05 - 0.7, abs=1e-6)
    turned = tuple(CustomPad(p.number, -p.x, -p.y, p.width, p.height) for p in lands)
    assert compare_to_vendor(m, VendorLandPattern(pads=turned))["ok"]
    short = compare_to_vendor(m, VendorLandPattern(pads=lands[:2]))
    assert not short["ok"]


def test_chip_0603_close_to_kemet_with_default_tolerances():
    """With the default P 0.05 the 0603 lands are 0.90 x 0.95 on 1.5,
    against KEMET's 0.95 x 1.00 on 1.6 (built with P 0.10): within 0.1."""
    spec = cases.kemet_chip("0603")
    r = compare_to_vendor(build_footprint(spec), spec.vendor, span_tol=0.1,
                          length_tol=0.1, width_tol=0.1)
    assert r["ok"], r["checks"]
