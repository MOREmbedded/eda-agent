# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Every family, built from its datasheet case, checked for what a person
would look for on the drawing: lands numbered the right way round, pin 1
marked at pin 1, silkscreen off the copper, the courtyard around
everything, the origin at the package centre, and a model that survives
JSON and renders to valid SVG."""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET

import pytest

from eda_agent.design.landpattern import (
    Circle,
    CustomPad,
    FootprintModel,
    Line,
    Polygon,
    SpecError,
    build_footprint,
    render_svg,
)
from eda_agent.design.landpattern.drafting import DEFAULT_RULES
from eda_agent.design.landpattern.smd import bga_row_label
from tests.design import _landpattern_cases as cases

ALL = sorted(cases.ALL_CASES)


def _model(name: str) -> FootprintModel:
    return build_footprint(cases.ALL_CASES[name]())


def _point_rect(px, py, b):
    dx = max(b[0] - px, 0.0, px - b[2])
    dy = max(b[1] - py, 0.0, py - b[3])
    return math.hypot(dx, dy)


def _seg_rect(x1, y1, x2, y2, b):
    """Distance from a segment to an axis-aligned box (0 if they meet)."""
    n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.005))
    return min(_point_rect(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n, b)
               for i in range(n + 1))


def _inside(inner, outer, eps=1e-6):
    return (inner[0] >= outer[0] - eps and inner[1] >= outer[1] - eps
            and inner[2] <= outer[2] + eps and inner[3] <= outer[3] + eps)


# --- invariants on every family -----------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_lands_do_not_collide(name):
    m = _model(name)
    for i, a in enumerate(m.pads):
        for b in m.pads[i + 1:]:
            if a.number == b.number:
                continue
            ab, bb = a.bbox(), b.bbox()
            dx = max(bb[0] - ab[2], ab[0] - bb[2])
            dy = max(bb[1] - ab[3], ab[1] - bb[3])
            assert dx > 0 or dy > 0, (name, a.number, b.number)


@pytest.mark.parametrize("name", ALL)
def test_silk_keeps_clear_of_copper(name):
    m = _model(name)
    clr = DEFAULT_RULES.silk_clearance
    for g in m.layer("silk"):
        for p in m.pads:
            b = p.bbox()
            if isinstance(g, Line):
                d = _seg_rect(g.x1, g.y1, g.x2, g.y2, b)
                assert d >= clr + g.width / 2 - 1e-3, (name, p.number, g, d)
            elif isinstance(g, Circle):
                d = _point_rect(g.x, g.y, b)
                assert d >= clr + g.r - 1e-3, (name, p.number, g, d)


@pytest.mark.parametrize("name", ALL)
def test_courtyard_encloses_everything(name):
    m = _model(name)
    cy = m.courtyard_bbox()
    for p in m.pads:
        assert _inside(p.bbox(), cy), (name, p.number)
    body = (m.body.x0, m.body.y0, m.body.x1, m.body.y1)
    assert _inside(body, cy), name
    for g in m.layer("silk"):
        if isinstance(g, Line):
            hw = g.width / 2
            box = (min(g.x1, g.x2) - hw, min(g.y1, g.y2) - hw,
                   max(g.x1, g.x2) + hw, max(g.y1, g.y2) + hw)
        elif isinstance(g, Circle):
            box = (g.x - g.r, g.y - g.r, g.x + g.r, g.y + g.r)
        else:
            continue
        assert _inside(box, cy), (name, g)
    # Edges sit on the 0.05 mm courtyard grid.
    for v in cy:
        assert abs(v / 0.05 - round(v / 0.05)) < 1e-6, (name, cy)


@pytest.mark.parametrize("name", ALL)
def test_pin1_mark_is_next_to_pin1(name):
    m = _model(name)
    dots = [g for g in m.layer("silk") if isinstance(g, Circle) and g.filled]
    pin1 = m.pin1
    p1 = min(m.pads, key=lambda p: math.hypot(p.x - pin1[0], p.y - pin1[1]))
    if not dots:
        # Only parts with no polarity go without a mark.
        assert m.family in ("chip", "crystal2"), name
        return
    assert len(dots) == 1, name
    d = dots[0]
    nearest = min(m.pads, key=lambda p: _point_rect(d.x, d.y, p.bbox()))
    assert nearest.number == p1.number, (name, nearest.number, p1.number)


@pytest.mark.parametrize("name", ALL)
def test_model_round_trips_and_names_its_source(name):
    spec = cases.ALL_CASES[name]()
    m = build_footprint(spec)
    text = json.dumps(m.to_dict())
    back = FootprintModel.from_json(text)
    assert back.to_dict() == m.to_dict()
    assert spec.source.document in m.description
    assert m.units == "mm" and m.origin == "package centroid"
    assert m.body.z1 == spec.height.max
    assert m.courtyard.layer == "courtyard"
    assert m.ref.layer == "assembly"
    assert any(g.layer == "assembly" for g in m.graphics)


@pytest.mark.parametrize("name", ALL)
def test_svg_is_well_formed_and_shows_every_land(name):
    m = _model(name)
    svg = render_svg(m)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    texts = {t.text for t in root.iter() if t.tag.endswith("text")}
    for p in m.pads:
        if p.number:
            assert p.number in texts, (name, p.number)
    assert svg.count("<g") == svg.count("</g>")


@pytest.mark.parametrize("name", ["soic8", "sot23_5", "qfn16", "lqfp80", "bga80",
                                  "sod123", "chip_0603", "son8", "dip8"])
def test_origin_is_the_package_centre(name):
    m = _model(name)
    assert (m.body.x0 + m.body.x1) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (m.body.y0 + m.body.y1) / 2 == pytest.approx(0.0, abs=1e-6)


# --- numbering ---------------------------------------------------------------------------


def test_sot23_5_pins_4_and_5_line_up_with_3_and_1():
    """TI DBV0005A p. 41 and Nexperia SOT753: pin 4 opposite pin 3, pin 5
    opposite pin 1; the old generator centred them (FOOTPRINT_PLAN)."""
    m = _model("sot23_5")
    p = {q.number: q for q in m.pads}
    assert p["4"].y == p["3"].y and p["5"].y == p["1"].y
    assert p["4"].x == -p["3"].x > 0 and p["5"].x == -p["1"].x > 0
    assert p["2"].y == 0.0 and p["2"].x < 0


def test_sot23_3_and_6_numbering():
    p = {q.number: q for q in _model("sot23_3").pads}
    assert p["1"].x < 0 and p["2"].x < 0 and p["3"].x > 0
    assert p["1"].y > 0 > p["2"].y and p["3"].y == 0
    p = {q.number: q for q in _model("sot23_6").pads}
    assert [p[n].y for n in "123"] == [0.95, 0.0, -0.95]
    assert [p[n].y for n in "456"] == [-0.95, 0.0, 0.95]
    assert all(p[n].x > 0 for n in "456")


def test_dual_rows_count_counter_clockwise():
    p = {q.number: q for q in _model("soic8").pads}
    assert p["1"].x < 0 and p["1"].y > 0           # top left
    assert p["4"].x < 0 and p["4"].y < 0           # bottom left
    assert p["5"].x > 0 and p["5"].y < 0           # bottom right
    assert p["8"].x > 0 and p["8"].y > 0           # top right


def test_quad_numbering():
    p = {q.number: q for q in _model("lqfp80").pads}
    assert p["1"].x < 0 and p["1"].y == max(q.y for q in p.values() if q.x < -5)
    assert p["21"].y < 0 and p["21"].x == min(q.x for q in p.values() if q.y < -5)
    assert p["41"].x > 0 and p["41"].y == min(q.y for q in p.values() if q.x > 5)
    assert p["61"].y > 0 and p["61"].x == max(q.x for q in p.values() if q.y > 5)
    # TI BQA0014A: pin 1 is the left pin of the top side, then CCW.
    p = {q.number: q for q in _model("qfn14_rect").pads}
    assert p["1"].group == "top" and p["14"].group == "top" and p["1"].x < p["14"].x
    assert [p[str(n)].group for n in range(2, 7)] == ["left"] * 5
    assert p["2"].y > p["6"].y
    assert p["7"].group == p["8"].group == "bottom" and p["7"].x < p["8"].x
    assert p["15"].role == "thermal"
    # TI FN0044A: pin 1 top centre, pin 2 to its left.
    p = {q.number: q for q in _model("plcc44").pads}
    assert p["1"].x == 0.0 and p["1"].group == "top"
    assert p["2"].x < 0 and p["44"].x > 0
    assert p["7"].group == "left" and p["18"].group == "bottom"


def test_bga_row_letters_skip_ambiguous_ones():
    labels = [bga_row_label(i) for i in range(22)]
    assert labels[:9] == list("ABCDEFGHJ")
    assert not set("IOQSXZ") & set("".join(labels[:20]))
    assert labels[19] == "Y" and labels[20] == "AA" and labels[21] == "AB"
    m = _model("bga80")
    names = {p.number for p in m.pads}
    assert len(names) == 80 and "C3" not in names and "J9" in names
    assert m.pin1 == (m.pad("A1").x, m.pad("A1").y)
    assert m.pad("A1").x < 0 < m.pad("A1").y
    spec = cases.bga80()
    spec.depopulated = ("I1",)
    with pytest.raises(SpecError, match="not in the grid"):
        build_footprint(spec)


def test_lga_renamed_grid_and_pin1_corner():
    """Bosch BME280 Fig. 21 (top view): pins 1-4 down the right column,
    5-8 up the left. The pin 1 mark and the assembly chamfer follow pin 1
    to the top right."""
    m = _model("lga8")
    p = {q.number: q for q in m.pads}
    assert [p[n].x > 0 for n in "1234"] == [True] * 4
    assert p["1"].y > p["2"].y > p["3"].y > p["4"].y
    assert p["5"].x < 0 and p["5"].y < p["8"].y
    outline = [g for g in m.layer("assembly") if isinstance(g, Polygon)][0]
    x1 = max(x for x, _ in outline.points)
    y1 = max(y for _, y in outline.points)
    assert (x1, y1) not in outline.points            # top right chamfered
    assert len(outline.points) == 5


# --- exposed pads, tabs, origin ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["soic8_ep", "msop8_ep", "son8", "qfn16",
                                  "qfn14_rect"])
def test_exposed_pad_paste_and_clearance(name):
    m = _model(name)
    ep = next(p for p in m.pads if p.role == "thermal")
    assert ep.paste_apertures
    area = sum(a.width * a.height for a in ep.paste_apertures)
    cov = area / (ep.width * ep.height)
    assert 0.5 <= cov <= 0.8, (name, cov)
    for a in ep.paste_apertures:
        assert ep.x - ep.width / 2 <= a.x - a.width / 2 + 1e-9
        assert a.x + a.width / 2 <= ep.x + ep.width / 2 + 1e-9
        assert ep.y - ep.height / 2 <= a.y - a.height / 2 + 1e-9
        assert a.y + a.height / 2 <= ep.y + ep.height / 2 + 1e-9
    eb = ep.bbox()
    for p in m.pads:
        if p is ep:
            continue
        b = p.bbox()
        dx = max(b[0] - eb[2], eb[0] - b[2])
        dy = max(b[1] - eb[3], eb[1] - b[3])
        gap = math.hypot(dx, dy) if dx > 0 and dy > 0 else max(dx, dy)
        assert gap >= DEFAULT_RULES.min_pad_gap - 1e-6, (name, p.number, gap)


def test_dpak_origin_tab_and_cropped_lead():
    m = _model("dpak")
    assert (m.body.x0 + m.body.x1) / 2 == pytest.approx(0.0, abs=1e-6)
    numbers = sorted(p.number for p in m.pads)
    assert numbers == ["1", "2", "3"]
    tab = m.pad("2")
    assert tab.role == "tab" and tab.x > 0
    assert m.pad("1").x < 0 and m.pad("3").x < 0
    assert m.pad("1").y == -m.pad("3").y == pytest.approx(2.286)


def test_sot89_tab_is_pin_2():
    m = _model("sot89")
    twos = m.pads_named("2")
    assert len(twos) == 2 and {p.role for p in twos} == {"signal", "tab"}
    assert (m.body.x0 + m.body.x1) / 2 == pytest.approx(0.0, abs=1e-6)


# --- through-hole ---------------------------------------------------------------------------


def test_dip_padstack_and_pin1():
    """SOT97-1 lead 0.53 x 0.36 max, diagonal 0.641: hole 0.85, pad 1.45 at
    level B (IPC-2222 Table 9-3, IPC-2221 Tables 9-1 and 9-2)."""
    m = _model("dip8")
    p1 = m.pad("1")
    assert (p1.hole, p1.width, p1.shape, p1.kind) == (0.85, 1.45, "rect", "tht")
    assert all(p.shape == "circle" for p in m.pads if p.number != "1")
    assert m.pad("8").x > 0 and m.pad("8").y > 0
    assert m.pad("1").x == pytest.approx(-3.81)


def test_to92_tight_pitch_gives_ovals():
    m = _model("to92")
    assert {p.width for p in m.pads} == {1.05}
    assert {p.height for p in m.pads} == {1.5}
    assert m.pad("2").shape == "oval" and m.pad("1").shape == "rect"
    assert any("ovals" in n for n in m.ipc["notes"])


def test_connector_contacts_and_mounting_holes():
    m = _model("connector_usb_b")
    legs = m.pads_named("MH")
    assert len(legs) == 2
    assert {(p.hole, p.kind, p.role) for p in legs} == {(2.3, "tht", "mounting")}
    assert m.pad("1").shape == "rect" and m.pad("1").x > 0 and m.pad("1").y > 0
    spec = cases.usb_b()
    spec.mounting_pads = (CustomPad("", -6.02, -3.71, 2.3, 2.3, shape="circle",
                                    hole=2.3, plated=False),)
    m2 = build_footprint(spec)
    npth = [p for p in m2.pads if p.kind == "npth"]
    assert len(npth) == 1 and npth[0].plated is False


def test_custom_lands_are_kept():
    spec = cases.custom_sot23_vendor()
    m = build_footprint(spec)
    got = {(p.number, p.x, p.y, p.width, p.height) for p in m.pads}
    want = {(c.number, c.x, c.y, c.width, c.height) for c in spec.custom_pads}
    assert got == want
