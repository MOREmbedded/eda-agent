# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Through-hole families: holes and pads from IPC-2222 / IPC-2221.

Hole = maximum lead + hole-over-lead allowance (IPC-2222 Table 9-3); pad =
hole + 2 x minimum annular ring (IPC-2221 Table 9-2) + fabrication
allowance (IPC-2221 Table 9-1); see :func:`ipc7351.pth_padstack`. A
rectangular lead is sized by its diagonal (Hausherr CADlib p. 55). Pin 1
gets a square pad, the universal through-hole orientation mark.

Where the pitch leaves less than the land-to-land clearance between round
pads, the pads become ovals narrowed along the row, as long as the
annular ring across the narrow side stays at or above the IPC-2221
minimum.
"""

from __future__ import annotations

import math
from typing import Callable

from eda_agent.design.landpattern import ipc7351 as ipc
from eda_agent.design.landpattern.builder import Builder
from eda_agent.design.landpattern.drafting import silk_arc_points
from eda_agent.design.landpattern.model import Circle, Line, Polygon
from eda_agent.design.landpattern.spec import SpecError, dim

__all__ = ["FAMILY_BUILDERS"]

#: Courtyard excess for through-hole parts, the IPC-7351B spacing from
#: the maximum component boundary: least 0.1, nominal 0.25, most 0.5 mm
#: (Hausherr CADlib p. 18, illustrated there with plated through-hole
#: courtyards).
_THT_EXCESS = {"L": 0.10, "N": 0.25, "M": 0.50}


def _lead_max(b: Builder) -> float:
    s = b.spec
    if s.lead_diameter is not None:
        return s.lead_diameter.max
    if s.lead_width is not None and s.lead_thickness is not None:
        return math.hypot(s.lead_width.max, s.lead_thickness.max)
    if s.lead_width is not None:
        return s.lead_width.max * math.sqrt(2.0)
    raise SpecError(
        f"{s.family} needs lead_diameter, or lead_width (square post), or "
        "lead_width and lead_thickness (rectangular lead)")


def _padstack(b: Builder) -> tuple[float, float]:
    lead = _lead_max(b)
    ps = ipc.pth_padstack(lead, b.d)
    ps["source"] = ("hole: IPC-2222 Table 9-3; ring: IPC-2221 Table 9-2; "
                    "allowance: IPC-2221 Table 9-1 (Hausherr CADlib pp. 55-56)")
    b.calcs["padstack"] = ps
    b.tables["pth"] = {"title": "Plated through-hole padstack",
                       "source": ps["source"]}
    b.excess = _THT_EXCESS[b.d]
    return ps["hole"], ps["pad"]


def _pad_shape(b: Builder, pad: float, hole: float, pitch: float
               ) -> tuple[float, float, str]:
    """(width along the row, height across it, shape) for a round pad of
    diameter ``pad``, narrowed to an oval when the pitch is too tight."""
    limit = pitch - b.rules.min_pad_gap
    if pad <= limit + 1e-9:
        return pad, pad, "circle"
    w = math.floor(limit / 0.05 + 1e-9) * 0.05
    ring = (w - hole) / 2.0
    if ring < ipc.PTH_ANNULAR_RING - 1e-9:
        raise SpecError(
            f"pitch {pitch} mm leaves no room for a {hole} mm hole with the "
            f"{ipc.PTH_ANNULAR_RING} mm minimum annular ring and "
            f"{b.rules.min_pad_gap} mm between pads")
    b.notes.append(
        f"pads narrowed to {w:.2f} x {pad:.2f} mm ovals to keep "
        f"{b.rules.min_pad_gap} mm between them at {pitch} mm pitch "
        f"(annular ring {ring:.3f} mm across the narrow side)")
    return round(w, 4), pad, "oval"


def _tht(b: Builder, num: str, x: float, y: float, w: float, h: float,
         hole: float, shape: str, first: bool, group: str = "") -> None:
    if first:
        shape = "rect"
    b.pad(num, x, y, w, h, shape=shape, kind="tht", hole=hole, group=group)


# --- families -------------------------------------------------------------------


def _dip(b: Builder) -> None:
    """DIP: rows ``row_pitch`` apart (JEDEC e1/eA), ``pitch`` along them.
    Pin 1 top left, counter-clockwise. body_x = plastic width (E), body_y =
    plastic length (D)."""
    s = b.spec
    hole, pad = _padstack(b)
    p = s.pitch.nom
    e = s.row_pitch.nom
    w, h, shape = _pad_shape(b, pad, hole, p)
    per = s.pins // 2
    top = (per - 1) * p / 2.0
    for i in range(per):
        _tht(b, str(i + 1), -e / 2, top - i * p, h, w, hole, shape, i == 0, "left")
    for j in range(per):
        _tht(b, str(per + j + 1), e / 2, -top + j * p, h, w, hole, shape, False,
             "right")
    b.set_body(s.body_x.max, s.body_y.max)
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")


def _header(b: Builder) -> None:
    """Pin header, one or two rows (``rows``), running down Y. One row: pin
    1 at the top. Two rows: pin 1 top left, pin 2 top right, odd pins left,
    even pins right. Square posts give ``lead_width``; the body defaults to
    one pitch per position."""
    s = b.spec
    rows = s.rows or 1
    if rows not in (1, 2):
        raise SpecError("header rows must be 1 or 2")
    if s.pins % rows:
        raise SpecError(f"{s.pins} pins do not split into {rows} rows")
    hole, pad = _padstack(b)
    p = s.pitch.nom
    rp = (s.row_pitch or s.pitch).nom
    w, h, shape = _pad_shape(b, pad, hole, min(p, rp) if rows == 2 else p)
    per = s.pins // rows
    top = (per - 1) * p / 2.0
    for k in range(s.pins):
        if rows == 1:
            x, y = 0.0, top - k * p
        else:
            x = -rp / 2 if k % 2 == 0 else rp / 2
            y = top - (k // 2) * p
        _tht(b, str(k + 1), x, y, h, w, hole, shape, k == 0, f"r{k % rows}")
    bx = s.body_x.max if s.body_x else (rows - 1) * rp + p
    by = s.body_y.max if s.body_y else per * p
    if s.body_x is None or s.body_y is None:
        b.notes.append("header body not given; one pitch per position assumed")
    b.set_body(bx, by)
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")


def _radial(b: Builder) -> None:
    """Radial can (electrolytic): two leads ``pitch`` apart (F), body
    diameter ``body_x`` (D). Pin 1 (+) on the left with the square pad."""
    s = b.spec
    hole, pad = _padstack(b)
    f = s.pitch.nom
    w, h, shape = _pad_shape(b, pad, hole, f)
    _tht(b, "1", -f / 2, 0.0, w, h, hole, shape, True, "a")
    _tht(b, "2", f / 2, 0.0, w, h, hole, shape, False, "b")
    d = s.body_x.max
    b.set_body(d, d)
    b.silk_path = silk_arc_points(0.0, 0.0, d / 2.0, 0.0, 360.0, 72)
    b.silk_closed = False
    b.assembly_shape = Circle("assembly", 0.0, 0.0, round(d / 2.0, 4),
                              b.rules.assembly_width)
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")


def _axial(b: Builder) -> None:
    """Axial part laid flat: leads bent to ``pitch`` (a board choice, not a
    package dimension), body ``body_x`` long and ``body_y`` in diameter.
    Pin 1 on the left (the cathode band end for a diode)."""
    s = b.spec
    hole, pad = _padstack(b)
    lp = s.pitch.nom
    if lp <= s.body_x.max + pad:
        raise SpecError(
            f"lead pitch {lp} mm does not clear the {s.body_x.max} mm body and "
            f"the {pad} mm pads; bend the leads wider")
    _tht(b, "1", -lp / 2, 0.0, pad, pad, hole, "circle", True, "a")
    _tht(b, "2", lp / 2, 0.0, pad, pad, hole, "circle", False, "b")
    b.set_body(s.body_x.max, s.body_y.max)
    hx = s.body_x.max / 2.0
    b.extra_silk_paths = [([(-lp / 2, 0.0), (-hx, 0.0)], False),
                          ([(hx, 0.0), (lp / 2, 0.0)], False)]
    b.marker = True if s.polarized is None else bool(s.polarized)
    b.chamfer = b.marker
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")


def _inline_row(b: Builder, n: int, pitch: float, y: float, hole: float,
                pad: float) -> None:
    w, h, shape = _pad_shape(b, pad, hole, pitch)
    x0 = -(n - 1) * pitch / 2.0
    for i in range(n):
        _tht(b, str(i + 1), x0 + i * pitch, y, w, h, hole, shape, i == 0, "row")


def _row_y(b: Builder) -> float:
    """Lead row position: ``options['row_offset']`` is its distance from the
    body face at +Y (TO-220: the tab's back face; TO-92: the flat face);
    without it the row runs through the body centre."""
    s = b.spec
    off = dim((s.options or {}).get("row_offset"))
    if off is None:
        return 0.0
    return round(s.body_y.nom / 2.0 - off.nom, 6)


def _to220(b: Builder) -> None:
    """TO-220 standing up: leads in a row along X, body ``body_x`` wide (E)
    and ``body_y`` deep (A, tab included) with the tab at +Y. Rectangular
    leads give ``lead_width`` and ``lead_thickness``."""
    s = b.spec
    hole, pad = _padstack(b)
    _inline_row(b, s.pins, s.pitch.nom, _row_y(b), hole, pad)
    b.set_body(s.body_x.max, s.body_y.max)
    tab = dim((s.options or {}).get("tab_thickness"))
    if tab is not None:
        x0, y0, x1, y1 = b.body
        b.extra_assembly.append(Line("assembly", x0, round(y1 - tab.nom, 4), x1,
                                     round(y1 - tab.nom, 4),
                                     b.rules.assembly_width))
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")[::-1]


def _to92(b: Builder) -> None:
    """TO-92: leads in a row along X, the flat face at +Y. body_x is the
    body diameter (D), body_y its depth from the flat face to the round
    back (E)."""
    s = b.spec
    hole, pad = _padstack(b)
    _inline_row(b, s.pins, s.pitch.nom, _row_y(b), hole, pad)
    d_ = s.body_x.max
    e_ = s.body_y.max
    r = d_ / 2.0
    y_flat = e_ / 2.0
    yc = y_flat - e_ + r        # circle centre; the round back is at -E/2
    dy = y_flat - yc
    if abs(dy) >= r:
        raise SpecError("TO-92 depth must be less than its diameter")
    xc = math.sqrt(r * r - dy * dy)
    a0 = math.degrees(math.atan2(dy, xc))
    # The flat face left to right, then clockwise round the back.
    pts = [(-xc, y_flat), (xc, y_flat)]
    pts += silk_arc_points(0.0, yc, r, a0, -180.0 - a0, 60)[1:-1]
    b.set_body(d_, e_)
    b.silk_path = pts
    b.silk_closed = True
    b.assembly_shape = Polygon("assembly", [(round(x, 4), round(y, 4)) for x, y in pts],
                               b.rules.assembly_width)
    b.pin1_candidates = b.cands_row_end_then_toe(b.pads[0], "left")[::-1]


def _connector(b: Builder) -> None:
    """Board connector: ``pins`` contacts along X at ``pitch`` in one or two
    rows (``rows``, ``row_pitch``), plus ``mounting_pads`` (pegs, NPTH
    holes, solder tabs) placed as given. The body is ``body_x`` by
    ``body_y``, offset by ``options['body_offset']`` (x, y) from the contact
    row centre, which is the origin unless ``options['origin']`` is
    'body'."""
    s = b.spec
    rows = s.rows or 1
    hole, pad = _padstack(b)
    p = s.pitch.nom
    if s.custom_pads:
        # Contact positions as the datasheet's hole pattern gives them; the
        # padstack still comes from the lead size. Pads are narrowed only
        # if two contacts sit closer than a round pad allows.
        pts = [(cp.number, cp.x, cp.y) for cp in s.custom_pads]
        closest = min((((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
                       for i, (_, ax, ay) in enumerate(pts)
                       for (_, bx, by) in pts[i + 1:]), default=p)
        w, h, shape = _pad_shape(b, pad, hole, closest)
        for k, (num, x, y) in enumerate(pts):
            _tht(b, num, x, y, w, h, hole, shape, num == "1", f"c{num}")
    else:
        rp = (s.row_pitch or s.pitch).nom if rows == 2 else 0.0
        w, h, shape = _pad_shape(b, pad, hole, p)
        per = s.pins // rows
        x0 = -(per - 1) * p / 2.0
        for k in range(s.pins):
            if rows == 1:
                x, y = x0 + k * p, 0.0
            else:
                x, y = x0 + (k // 2) * p, (rp / 2 if k % 2 == 0 else -rp / 2)
            _tht(b, str(k + 1), x, y, w, h, hole, shape, k == 0, f"r{k % rows}")
    for mp in s.mounting_pads:
        kind = "npth" if (mp.hole > 0 and not mp.plated) else (
            "tht" if mp.hole > 0 else "smd")
        b.pad(mp.number, mp.x, mp.y, mp.width, mp.height, shape=mp.shape,
              kind=kind, hole=mp.hole, hole_length=mp.hole_length,
              plated=mp.plated, role="mounting", group=f"m{mp.number}{mp.x}")
    off = (s.options or {}).get("body_offset", (0.0, 0.0))
    if s.body_x is not None and s.body_y is not None:
        b.set_body(s.body_x.max, s.body_y.max, float(off[0]), float(off[1]))
    if (s.options or {}).get("origin") == "body" and b.body is not None:
        bx0, by0, bx1, by1 = b.body
        b.offset = (-(bx0 + bx1) / 2.0, -(by0 + by1) / 2.0)
    # The mark goes outside the contact field next to pin 1: above it, or
    # on its outer side, never between two contacts.
    p1 = next((q for q in b.pads if q.number == "1"), b.pads[0])
    r = b.dot_radius()
    c = b.rules.silk_clearance
    x0, y0, x1, y1 = p1.bbox()
    xs = [q.x for q in b.pads if q.role == "signal"]
    ys = [q.y for q in b.pads if q.role == "signal"]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    out_x = x1 + c + r if p1.x >= mx else x0 - c - r
    out_y = y1 + c + r if p1.y >= my else y0 - c - r
    b.pin1_candidates = [((x0 + x1) / 2, out_y), (out_x, (y0 + y1) / 2),
                         (out_x, out_y)]


FAMILY_BUILDERS: dict[str, Callable[[Builder], None]] = {
    "dip": _dip,
    "header": _header,
    "radial": _radial,
    "axial": _axial,
    "to220": _to220,
    "to92": _to92,
    "connector": _connector,
}
