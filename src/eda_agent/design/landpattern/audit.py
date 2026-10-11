# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Audit a footprint that already exists against the package it is for.

The package description (:class:`PackageSpec`) gives the IPC-7351B land
pattern the engine would draw, and the terminals the lands have to
solder. The footprint is what an editor reads back: pads in mm (the
``library.get_pad_geometry`` rows) and, where the backend can read them,
its other primitives (silkscreen, courtyard, assembly, paste windows) and
height.

What is compared, and against what:

* pad count, numbering and pin 1, by matching pad centres after centring
  both patterns and trying the four quarter turns (a turned footprint is
  a library convention; a MIRRORED one is a defect and is named as such);
* pitch exactly, and the row span, land sizes and land centres against
  the IPC pattern within ``CROSS_CHECK_TOL``, the deviations measured
  between real vendor patterns and IPC: a footprint that matches the
  vendor's own pattern passes, one built from a misread number does not;
* every land covers the nominal centre of the terminal it solders;
* courtyard present and enclosing the lands and the body, silkscreen off
  the copper, paste on an exposed pad, manual mask settings, the origin
  at the package centroid, and the height against the package maximum.

A check that could not run says so under ``not_examined``; a clean result
lists what it covered under ``examined``.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from eda_agent.design.footprint_audit import pad_position_mm
from eda_agent.design.landpattern.compare import CROSS_CHECK_TOL
from eda_agent.design.landpattern.drafting import DEFAULT_RULES
from eda_agent.design.landpattern.model import FootprintModel
from eda_agent.design.landpattern.spec import PackageSpec

__all__ = [
    "audit_footprint_vs_package",
    "classify_layer",
    "terminal_outlines",
]

#: Pitch must match the package within this, mm: a footprint drawn in
#: whole mils puts 0.65 mm at 25.6 mil = 0.65024 mm, well inside it.
PITCH_TOL = 0.01
#: The footprint origin may sit this far from the package centroid.
ORIGIN_TOL = 0.05
#: Custom lands are the package's own numbers, held as the land-pattern
#: audit holds a transcribed pattern (footprint_audit.py, 0.05 mm).
CUSTOM_TOL = 0.05
#: Assembly outline sides may sit this far from the body.
ASSEMBLY_TOL = 0.25
#: Exposed pad paste coverage window (TB389 rev. 7 p. 3: 50 to 80 %).
EP_PASTE_RANGE = (0.50, 0.80)

_TERMINAL_FAMILIES = ("chip", "melf", "molded", "sod", "crystal2", "soic",
                      "son", "sot23", "crystal4", "sot223", "qfn", "qfp",
                      "plcc", "bga", "lga")


# --- terminals -------------------------------------------------------------------


def _span_value(spec: PackageSpec, axis: str) -> Optional[float]:
    if axis == "x":
        d = spec.lead_span_x or spec.body_x
    elif spec.lead_span_y is not None:
        d = spec.lead_span_y
    elif spec.family in ("qfn", "son"):
        d = spec.body_y
    else:
        d = spec.lead_span_x
    return d.nom if d is not None else None


def terminal_outlines(spec: PackageSpec, model: FootprintModel) -> list[dict]:
    """The nominal contact area of each terminal, in the model's frame.

    ``[{number, shape, x0, y0, x1, y1}]`` for the lead and grid families;
    empty for the rest (asymmetric tab parts, through-hole, custom), whose
    terminals this does not model. A pad that is not a terminal (an
    exposed pad) gets none.
    """
    if spec.family not in _TERMINAL_FAMILIES:
        return []
    out: list[dict] = []
    t = spec.lead_length.nom if spec.lead_length is not None else None
    w = spec.lead_width.nom if spec.lead_width is not None else None
    lx, ly = _span_value(spec, "x"), _span_value(spec, "y")
    for p in model.pads:
        if p.group == "grid":
            if spec.family == "bga" and spec.ball_diameter is not None:
                r = spec.ball_diameter.nom / 2.0
                out.append({"number": p.number, "shape": "circle",
                            "x0": p.x - r, "y0": p.y - r,
                            "x1": p.x + r, "y1": p.y + r})
            elif w is not None:
                h = spec.lead_length.nom if spec.lead_length is not None else w
                out.append({"number": p.number, "shape": "rect",
                            "x0": p.x - w / 2, "y0": p.y - h / 2,
                            "x1": p.x + w / 2, "y1": p.y + h / 2})
            continue
        if t is None or w is None or p.role == "thermal":
            continue
        width = w
        if p.role == "tab":
            if spec.tab_width is None or spec.family != "sot223":
                continue
            width = spec.tab_width.nom
        if p.group in ("left", "right") and lx:
            sgn = -1.0 if p.group == "left" else 1.0
            a, b = sgn * lx / 2.0, sgn * (lx / 2.0 - t)
            out.append({"number": p.number, "shape": "rect",
                        "x0": min(a, b), "x1": max(a, b),
                        "y0": p.y - width / 2, "y1": p.y + width / 2})
        elif p.group in ("top", "bottom") and ly:
            sgn = 1.0 if p.group == "top" else -1.0
            a, b = sgn * ly / 2.0, sgn * (ly / 2.0 - t)
            out.append({"number": p.number, "shape": "rect",
                        "y0": min(a, b), "y1": max(a, b),
                        "x0": p.x - width / 2, "x1": p.x + width / 2})
    return out


# --- geometry ------------------------------------------------------------------


def _rot_q(x: float, y: float, q: int) -> tuple[float, float]:
    q %= 4
    if q == 1:
        return (-y, x)
    if q == 2:
        return (-x, -y)
    if q == 3:
        return (y, -x)
    return (x, y)


def _f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pad_shape(row: dict) -> str:
    shape = str(row.get("shape") or "").lower()
    if shape in ("round", "circle", "ellipse", "oval", "oblong"):
        return "round"
    return "rect"


def _pad_box(row: dict, grow: float = 0.0) -> tuple[float, float, float, float]:
    x, y = pad_position_mm(row)
    hw, hh = _num(row, "w_mm") / 2.0 + grow, _num(row, "h_mm") / 2.0 + grow
    a = math.radians(_num(row, "rotation") % 360.0)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    ex, ey = hw * c + hh * s, hw * s + hh * c
    return (x - ex, y - ey, x + ex, y + ey)


def _pt_seg(px: float, py: float, ax: float, ay: float, bx: float,
            by: float) -> float:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segs_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def _seg_seg(a, b, c, d) -> float:
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_pt_seg(*a, *c, *d), _pt_seg(*b, *c, *d),
               _pt_seg(*c, *a, *b), _pt_seg(*d, *a, *b))


def _seg_box(a, b, hw: float, hh: float) -> float:
    """Distance from segment ab to the box [-hw, hw] x [-hh, hh]; 0 if they
    touch or cross."""
    def inside(p):
        return abs(p[0]) <= hw + 1e-12 and abs(p[1]) <= hh + 1e-12
    if inside(a) or inside(b):
        return 0.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    edges = list(zip(corners, corners[1:] + corners[:1]))
    if any(_segs_cross(a, b, c, d) for c, d in edges):
        return 0.0
    return min(min(_seg_seg(a, b, c, d) for c, d in edges), 1e9)


def _seg_to_pad(a, b, row: dict, grow: float) -> float:
    """Clearance from segment ab to a pad's copper (grown by ``grow``)."""
    x, y = pad_position_mm(row)
    w, h = _num(row, "w_mm") + 2 * grow, _num(row, "h_mm") + 2 * grow
    ang = -math.radians(_num(row, "rotation"))
    c, s = math.cos(ang), math.sin(ang)

    def tf(p):
        dx, dy = p[0] - x, p[1] - y
        return (dx * c - dy * s, dx * s + dy * c)

    A, B = tf(a), tf(b)
    if _pad_shape(row) == "round":
        r = min(w, h) / 2.0
        half = max(w, h) / 2.0 - r
        core = ((-half, 0.0), (half, 0.0)) if w >= h else ((0.0, -half), (0.0, half))
        return _seg_seg(A, B, core[0], core[1]) - r
    return _seg_box(A, B, w / 2.0, h / 2.0)


def _arc_segments(arc: dict) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    xc, yc, r = _num(arc, "xc"), _num(arc, "yc"), _num(arc, "r")
    a0, a1 = _num(arc, "start"), _num(arc, "end")
    sweep = (a1 - a0) % 360.0
    if sweep <= 1e-9:
        sweep = 360.0
    n = max(4, int(math.ceil(sweep / 10.0)))
    pts = [(xc + r * math.cos(math.radians(a0 + sweep * k / n)),
            yc + r * math.sin(math.radians(a0 + sweep * k / n)))
           for k in range(n + 1)]
    return list(zip(pts, pts[1:]))


def _box_union(boxes: Iterable[tuple[float, float, float, float]]):
    bl = list(boxes)
    if not bl:
        return None
    return (min(b[0] for b in bl), min(b[1] for b in bl),
            max(b[2] for b in bl), max(b[3] for b in bl))


def _item_boxes(prims: dict, layers: set[str]) -> list[tuple[float, float, float, float]]:
    out = []
    for t in prims.get("tracks") or []:
        if str(t.get("layer")) in layers:
            out.append((min(_num(t, "x1"), _num(t, "x2")),
                        min(_num(t, "y1"), _num(t, "y2")),
                        max(_num(t, "x1"), _num(t, "x2")),
                        max(_num(t, "y1"), _num(t, "y2"))))
    for a in prims.get("arcs") or []:
        if str(a.get("layer")) in layers:
            pts = [p for seg in _arc_segments(a) for p in seg]
            out.append((min(p[0] for p in pts), min(p[1] for p in pts),
                        max(p[0] for p in pts), max(p[1] for p in pts)))
    for key in ("regions", "fills"):
        for r in prims.get(key) or []:
            if str(r.get("layer")) in layers:
                out.append((_num(r, "x1"), _num(r, "y1"), _num(r, "x2"),
                            _num(r, "y2")))
    return out


# --- layers by role ------------------------------------------------------------


def classify_layer(name: str, mech: dict[str, dict], *,
                   courtyard_layer: str = "",
                   assembly_layer: str = "") -> str:
    """Role of one layer name: silk, paste, copper, mask, courtyard,
    assembly or other, the way the package footprint tool picks its
    layers (named layer first, then the mechanical KIND, then the name)."""
    from eda_agent.design.footprint_policy import (
        _ASSEMBLY_HINTS,
        _COURTYARD_HINTS,
    )
    from eda_agent.design.landpattern.emit import _is_bottom

    n = str(name or "")
    low = "".join(n.split()).lower()
    if courtyard_layer and low == "".join(courtyard_layer.split()).lower():
        return "courtyard"
    if assembly_layer and low == "".join(assembly_layer.split()).lower():
        return "assembly"
    fixed = {"topoverlay": "silk", "toppaste": "paste", "toplayer": "copper",
             "topsolder": "mask", "multilayer": "copper"}
    if low in fixed:
        return fixed[low]
    info = mech.get(low) or {}
    kind = str(info.get("kind") or "").strip().lower()
    if kind == "courtyard top":
        return "courtyard"
    if kind == "assembly top":
        return "assembly"
    if kind not in ("", "not set", "unknown"):
        return "other"
    label = str(info.get("name") or n)
    if _is_bottom(label) or _is_bottom(n):
        return "other"
    lab = label.lower()
    if any(h in lab for h in _COURTYARD_HINTS):
        return "courtyard"
    if any(h in lab for h in _ASSEMBLY_HINTS):
        return "assembly"
    return "other"


# --- matching ------------------------------------------------------------------


def _match(model_pts: list[tuple[float, float]], fp_pts: list[tuple[float, float]],
           model_names: list[str], fp_names: list[str], *, mirror: bool = False):
    """Best quarter turn (and pairs) of the footprint onto the model, both
    centred on their pad centroids. Ties go to the turn whose numbering
    agrees, as the land-pattern audit does."""
    from eda_agent.design.footprint_audit import _greedy_match

    def centred(pts):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return [(p[0] - cx, p[1] - cy) for p in pts], (cx, cy)

    m, mc = centred(model_pts)
    f0, fc = centred(fp_pts)
    if mirror:
        f0 = [(-x, y) for x, y in f0]
    best = None
    for q in range(4):
        f = [_rot_q(x, y, q) for x, y in f0]
        pairs = _greedy_match(m, f)
        cost = sum(d for _, _, d in pairs)
        hits = sum(1 for i, j, _ in pairs if model_names[i] == fp_names[j])
        key = (round(cost, 6), -hits)
        if best is None or key < best[0]:
            best = (key, q, pairs, hits)
    return {"q": best[1], "pairs": best[2], "hits": best[3], "cost": best[0][0],
            "model_centroid": mc, "fp_centroid": fc, "mirror": mirror}


# --- the audit -----------------------------------------------------------------


class _Report:
    def __init__(self):
        self.findings: list[dict] = []
        self.examined: list[str] = []
        self.not_examined: list[dict] = []
        self.notes: list[str] = []

    def add(self, check: str, severity: str, message: str, **kw: Any) -> None:
        d = {"check": check, "severity": severity, "message": message}
        d.update({k: v for k, v in kw.items() if v is not None})
        self.findings.append(d)

    def skip(self, check: str, reason: str) -> None:
        self.not_examined.append({"check": check, "reason": reason})


def _group_axis(group: str) -> str:
    return "y" if group in ("left", "right") else "x"


def _row_pitch(points: list[tuple[float, float]], axis: str) -> Optional[float]:
    vals = sorted(round(p[1] if axis == "y" else p[0], 6) for p in points)
    gaps = [b - a for a, b in zip(vals, vals[1:]) if b - a > 1e-6]
    return min(gaps) if gaps else None


def audit_footprint_vs_package(spec: PackageSpec, pads: list[dict], *,
                               model: Optional[FootprintModel] = None,
                               primitives: Optional[dict] = None,
                               mech_layers: Optional[list[dict]] = None,
                               density: str = "N",
                               courtyard_layer: str = "",
                               assembly_layer: str = "",
                               primitives_reason: str = "") -> dict:
    """Audit footprint ``pads`` (and ``primitives``) against ``spec``.

    ``pads`` are ``library.get_pad_geometry`` rows (``name``, ``x_mm``,
    ``y_mm``, ``w_mm``, ``h_mm``, ``rotation``, ``shape``, ``hole_mm``,
    paste and mask expansion and source). ``primitives`` is the
    ``library.get_footprint_primitives`` reply, or None when the backend
    cannot read them (``primitives_reason`` says why). ``mech_layers`` is
    ``library.get_mech_layers``' list, for the layer kinds.

    Returns ``{ok, errors, warnings, findings, examined, not_examined,
    alignment, metrics, notes}``.
    """
    from eda_agent.design.landpattern.builder import build_footprint
    from eda_agent.design.landpattern.compare import pattern_metrics

    r = _Report()
    if model is None:
        model = build_footprint(spec, density)
    tol = dict(CROSS_CHECK_TOL)
    fp = [p for p in pads if isinstance(p, dict)]
    mpads = list(model.pads)
    r.examined.append("pad_count")
    if not fp:
        r.add("pad_count", "error", "the footprint has no pads to audit",
              expected=str(len(mpads)), actual="0")
        return _finish(r, model, None, {})

    m_names = [p.number for p in mpads]
    f_names = [str(p.get("name") or "") for p in fp]
    if len(fp) != len(mpads):
        r.add("pad_count", "error",
              f"the footprint has {len(fp)} pads where the package needs "
              f"{len(mpads)} ({len([p for p in mpads if p.role == 'signal'])} "
              "terminals plus any exposed pad, tab or mounting pad)",
              expected=str(len(mpads)), actual=str(len(fp)))

    m_pts = [(p.x, p.y) for p in mpads]
    f_pts = [pad_position_mm(p) for p in fp]
    al = _match(m_pts, f_pts, m_names, f_names)
    q = al["q"]
    mcx, mcy = al["model_centroid"]
    fcx, fcy = al["fp_centroid"]

    def to_fp(x: float, y: float) -> tuple[float, float]:
        dx, dy = _rot_q(x - mcx, y - mcy, (4 - q) % 4)
        return (dx + fcx, dy + fcy)

    def aligned(i: int) -> tuple[float, float]:
        return _rot_q(f_pts[i][0] - fcx, f_pts[i][1] - fcy, q)

    pairs = al["pairs"]
    matched_m = {i: j for i, j, _ in pairs}
    matched_f = {j for _, j, _ in pairs}
    missing = [m_names[i] for i in range(len(mpads)) if i not in matched_m]
    extra = [f_names[j] for j in range(len(fp)) if j not in matched_f]
    if missing:
        r.add("pad_count", "error", "package pads with no footprint pad: "
              + ", ".join(missing), expected=", ".join(missing))
    if extra:
        r.add("pad_count", "error", "footprint pads with no package "
              "counterpart: " + ", ".join(extra), actual=", ".join(extra))

    # Numbering, pin 1 and mirroring.
    r.examined += ["numbering", "pin1"]
    wrong = [(m_names[i], f_names[j]) for i, j, _ in pairs
             if m_names[i] != f_names[j]]
    mirrored = False
    if wrong and len(fp) == len(mpads):
        mal = _match(m_pts, f_pts, m_names, f_names, mirror=True)
        if mal["hits"] == len(mpads) and mal["cost"] <= al["cost"] + 1e-6:
            mirrored = True
    if mirrored:
        r.add("numbering", "error",
              "the footprint is a MIRROR IMAGE of the package: its numbering "
              "runs the other way round (clockwise seen from the top), the "
              "way a bottom-view drawing reads. Every pad lands on the wrong "
              "terminal but its mirror twin", actual=f"{len(wrong)} pads "
              "numbered as their mirror position")
    elif wrong:
        shown = ", ".join(f"{a} carries {b!r}" for a, b in wrong[:8])
        more = f" and {len(wrong) - 8} more" if len(wrong) > 8 else ""
        r.add("numbering", "error",
              f"{len(wrong)} pad(s) at a package position carry another "
              f"number: package pad {shown}{more}")
    pin1 = next((p.number for p in mpads if (p.x, p.y) == tuple(model.pin1)),
                m_names[0])
    i1 = m_names.index(pin1)
    j_at_pin1 = matched_m.get(i1)
    if pin1 not in f_names:
        r.add("pin1", "error", f"the footprint has no pad {pin1!r}, so pin 1 "
              "cannot be found", expected=pin1)
    elif j_at_pin1 is None or f_names[j_at_pin1] != pin1:
        j1 = f_names.index(pin1)
        at = next((m_names[i] for i, j, _ in pairs if j == j1), None)
        px, py = to_fp(*model.pin1)
        r.add("pin1", "error",
              f"pad {pin1} is where the package has pin {at}; the package "
              f"puts pin 1 at ({_f(px)}, {_f(py)}) mm in the footprint's "
              "frame", pad=pin1, expected=f"({_f(px)}, {_f(py)}) mm",
              actual=f"({_f(f_pts[j1][0])}, {_f(f_pts[j1][1])}) mm")
    turned = ((4 - q) % 4) * 90
    if turned:
        r.notes.append(f"the footprint is turned {turned} degrees (counter-"
                       "clockwise) from IPC zero orientation, pin 1 upper "
                       "left; it was compared after turning it back")

    # Per pad: centre, size, terminal coverage, hole.
    r.examined += ["pad_position", "pad_size", "terminal_coverage", "holes"]
    terms = {t["number"]: t for t in terminal_outlines(spec, model)}
    if not terms:
        r.skip("terminal_coverage", f"terminals are not modelled for the "
               f"{spec.family} family")
    custom = spec.family in ("custom",)
    pos_tol = (CUSTOM_TOL if custom else tol["span"] / 2.0)
    for i, j, dist in pairs:
        mp, fr = mpads[i], fp[j]
        name = m_names[i]
        if dist > pos_tol + 1e-6:
            r.add("pad_position", "error",
                  f"pad {name} is {_f(dist)} mm from where the package's "
                  f"land centre is, beyond {_f(pos_tol)} mm", pad=name,
                  expected=f"within {_f(pos_tol)} mm", actual=f"{_f(dist)} mm")
        fw, fh = _num(fr, "w_mm"), _num(fr, "h_mm")
        rot = _num(fr, "rotation") % 180.0
        if abs(rot - 90.0) < 1e-6:
            fw, fh = fh, fw
        if q % 2:
            fw, fh = fh, fw
        kind_fp = "smd" if _num(fr, "hole_mm") <= 0 else "tht"
        if mp.kind == "smd" and kind_fp != "smd":
            r.add("holes", "error", f"pad {name} has a {_f(_num(fr, 'hole_mm'))} "
                  "mm hole where the package has a surface-mount land", pad=name)
        elif mp.kind != "smd" and kind_fp == "smd":
            r.add("holes", "error", f"pad {name} has no hole where the "
                  f"package needs {_f(mp.hole)} mm", pad=name)
        elif mp.kind != "smd":
            fh_ = _num(fr, "hole_mm")
            lead = _lead_max(model)
            if mp.role == "signal" and lead and fh_ < lead - 1e-6:
                r.add("holes", "error", f"pad {name}: the {_f(fh_)} mm hole is "
                      f"smaller than the lead ({_f(lead)} mm max), which will "
                      "not go in", pad=name, expected=f">= {_f(lead)} mm",
                      actual=f"{_f(fh_)} mm")
            elif mp.role != "signal" and fh_ < mp.hole - 0.1 - 1e-6:
                r.add("holes", "warning", f"pad {name}: the {_f(fh_)} mm hole "
                      f"is smaller than the drawing's {_f(mp.hole)} mm",
                      pad=name, expected=f"{_f(mp.hole)} mm",
                      actual=f"{_f(fh_)} mm")
            ring = (min(fw, fh) - fh_) / 2.0
            from eda_agent.design.landpattern.ipc7351 import PTH_ANNULAR_RING
            if fh_ > 0 and ring < PTH_ANNULAR_RING - 1e-6:
                r.add("holes", "warning", f"pad {name}: annular ring "
                      f"{_f(ring)} mm, below the IPC-2221 minimum of "
                      f"{PTH_ANNULAR_RING} mm", pad=name)
            continue
        if mp.role == "thermal":
            lim_l = lim_w = tol["ep"]
        elif mp.role == "tab":
            lim_l = lim_w = tol["tab"]
        elif mp.group == "grid":
            lim_l = lim_w = tol["land_diameter"]
        elif custom or mp.group == "custom":
            lim_l = lim_w = CUSTOM_TOL
        else:
            lim_l, lim_w = tol["pad_length"], tol["pad_width"]
        if mp.group in ("top", "bottom"):
            want_l, want_w, got_l, got_w = mp.height, mp.width, fh, fw
        else:
            want_l, want_w, got_l, got_w = mp.width, mp.height, fw, fh
        problems = []
        if abs(got_l - want_l) > lim_l + 1e-6:
            problems.append(f"length {_f(got_l)} against {_f(want_l)} "
                            f"(tolerance {_f(lim_l)})")
        if abs(got_w - want_w) > lim_w + 1e-6:
            problems.append(f"width {_f(got_w)} against {_f(want_w)} "
                            f"(tolerance {_f(lim_w)})")
        for got, want in ((got_l, want_l), (got_w, want_w)):
            if got > 0 and want > 0 and max(got, want) / min(got, want) >= 2.0 \
                    and not problems:
                problems.append(f"{_f(got)} against {_f(want)}, a factor of "
                                f"{_f(max(got, want) / min(got, want))}")
        if problems:
            r.add("pad_size", "error",
                  f"pad {name} differs from the IPC land: " + "; ".join(problems)
                  + " mm", pad=name,
                  expected=f"{_f(want_l)} x {_f(want_w)} mm (length x width)",
                  actual=f"{_f(got_l)} x {_f(got_w)} mm")
        t = terms.get(name)
        if t is not None:
            cx, cy = to_fp((t["x0"] + t["x1"]) / 2.0, (t["y0"] + t["y1"]) / 2.0)
            if _seg_to_pad((cx, cy), (cx, cy), fr, 0.0) > 1e-6:
                r.add("terminal_coverage", "error",
                      f"pad {name} does not cover the centre of the terminal "
                      f"it solders, at ({_f(cx)}, {_f(cy)}) mm", pad=name)

    # Pitch and span, from the matched pads in the aligned frame.
    r.examined += ["pitch", "span"]
    groups: dict[str, list[tuple[int, int]]] = {}
    for i, j, _ in pairs:
        if mpads[i].role == "signal":
            groups.setdefault(mpads[i].group, []).append((i, j))
    fp_metrics: dict[str, Any] = {}
    ipc_metrics = pattern_metrics(model)
    pitch_seen = set()
    for g, ij in groups.items():
        if len(ij) < 2 or g not in ("left", "right", "top", "bottom", "grid"):
            continue
        axes = ("x", "y") if g == "grid" else (_group_axis(g),)
        for ax in axes:
            mp_ = _row_pitch([(mpads[i].x - mcx, mpads[i].y - mcy) for i, _ in ij], ax)
            fp_ = _row_pitch([aligned(j) for _, j in ij], ax)
            if mp_ is None or fp_ is None:
                continue
            fp_metrics.setdefault("pitch", {})[f"{g}_{ax}"] = round(fp_, 4)
            if abs(mp_ - fp_) > PITCH_TOL and (g, ax) not in pitch_seen:
                pitch_seen.add((g, ax))
                r.add("pitch", "error",
                      f"the {g} pads are {_f(fp_)} mm apart where the package "
                      f"pitch is {_f(mp_)} mm ({_pitch_cite(spec)})",
                      expected=f"{_f(mp_)} mm", actual=f"{_f(fp_)} mm")

    def mean_x(ij, axis):
        vals = [aligned(j)[0 if axis == "x" else 1] for _, j in ij]
        return sum(vals) / len(vals) if vals else None

    def model_mean(ij, axis):
        vals = [(mpads[i].x - mcx) if axis == "x" else (mpads[i].y - mcy)
                for i, _ in ij]
        return sum(vals) / len(vals) if vals else None

    for a, b, axis, label in (("left", "right", "x", "span"),
                              ("bottom", "top", "y", "span_y")):
        if a in groups and b in groups:
            fspan = mean_x(groups[b], axis) - mean_x(groups[a], axis)
            mspan = model_mean(groups[b], axis) - model_mean(groups[a], axis)
            fp_metrics[label] = round(fspan, 4)
            if abs(fspan - mspan) > tol["span"] + 1e-6:
                r.add("span", "error",
                      f"the rows are {_f(fspan)} mm apart centre to centre "
                      f"where the IPC pattern for this package puts them "
                      f"{_f(mspan)} mm apart, beyond the {_f(tol['span'])} mm "
                      "real vendor patterns stay within",
                      expected=f"{_f(mspan)} mm", actual=f"{_f(fspan)} mm")

    # Origin at the package centroid.
    r.examined.append("origin")
    ox, oy = to_fp(0.0, 0.0)
    if math.hypot(ox, oy) > ORIGIN_TOL:
        r.add("origin", "warning",
              f"the footprint origin is {_f(math.hypot(ox, oy))} mm from the "
              f"package centroid, which sits at ({_f(ox)}, {_f(oy)}) mm; "
              "IPC-7351 puts the origin at the centroid, where pick and place "
              "aims", expected="(0, 0) mm", actual=f"({_f(ox)}, {_f(oy)}) mm")

    # Paste and mask, from the pad fields.
    r.examined += ["paste", "mask"]
    _check_paste_and_mask(r, mpads, fp, pairs, primitives)

    # The other primitives.
    if primitives is None:
        why = primitives_reason or "the footprint's other primitives were not read"
        for chk in ("courtyard", "silk", "assembly", "paste_windows", "height"):
            r.skip(chk, why)
    else:
        mech = {"".join(str(m.get("layer") or "").split()).lower(): m
                for m in (mech_layers or []) if isinstance(m, dict)}
        for m in mech_layers or []:
            if isinstance(m, dict) and m.get("name"):
                mech.setdefault("".join(str(m["name"]).split()).lower(), m)
        roles: dict[str, set[str]] = {}
        for key in ("tracks", "arcs", "regions", "fills", "texts"):
            for item in primitives.get(key) or []:
                lay = str(item.get("layer") or "")
                role = classify_layer(lay, mech, courtyard_layer=courtyard_layer,
                                      assembly_layer=assembly_layer)
                roles.setdefault(role, set()).add(lay)
        body_fp = _body_in_fp(model, to_fp)
        _check_courtyard(r, model, fp, roles.get("courtyard", set()),
                         primitives, body_fp)
        _check_silk(r, fp, roles.get("silk", set()), primitives)
        _check_assembly(r, roles.get("assembly", set()), primitives, body_fp)
        _check_height(r, spec, primitives)
    met = {"footprint": fp_metrics,
           "ipc": {k: ipc_metrics.get(k) for k in ("pitch", "span", "span_y",
                                                  "pad_length", "pad_width")
                   if ipc_metrics.get(k) is not None}}
    return _finish(r, model, {"rotation_deg": turned, "mirrored": mirrored,
                              "origin_offset_mm": [round(ox, 4), round(oy, 4)]},
                   met)


def _pitch_cite(spec: PackageSpec) -> str:
    from eda_agent.design.landpattern.check import describe_dim
    return describe_dim("pitch", spec.pitch, spec)


def _lead_max(model: FootprintModel) -> float:
    """The largest lead the IPC padstack was sized for (0 when unknown)."""
    ps = (model.ipc.get("calcs") or {}).get("padstack") or {}
    try:
        return float(ps.get("lead_max") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _body_in_fp(model: FootprintModel, to_fp) -> Optional[tuple]:
    b = model.body
    if b.x1 <= b.x0 or b.y1 <= b.y0:
        return None
    pts = [to_fp(b.x0, b.y0), to_fp(b.x1, b.y0), to_fp(b.x1, b.y1),
           to_fp(b.x0, b.y1)]
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def _check_paste_and_mask(r: _Report, mpads, fp, pairs, primitives) -> None:
    paste_layers: set[str] = set()
    if primitives is not None:
        paste_layers = {str(it.get("layer")) for k in ("regions", "fills")
                        for it in primitives.get(k) or []
                        if classify_layer(str(it.get("layer")), {}) == "paste"}
    for i, j, _ in pairs:
        mp, fr = mpads[i], fp[j]
        if mp.kind != "smd":
            continue
        src = str(fr.get("paste_expansion_source") or "rule")
        exp = _num(fr, "paste_expansion_mm")
        w, h = _num(fr, "w_mm"), _num(fr, "h_mm")
        closed = src == "manual" and exp <= -min(w, h) / 2.0 + 1e-9
        if mp.role == "thermal":
            area = w * h
            windows = []
            if primitives is not None:
                box = _pad_box(fr)
                for k in ("regions", "fills"):
                    for it in primitives.get(k) or []:
                        if str(it.get("layer")) not in paste_layers:
                            continue
                        cx = (_num(it, "x1") + _num(it, "x2")) / 2.0
                        cy = (_num(it, "y1") + _num(it, "y2")) / 2.0
                        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                            a = _num(it, "area") or (
                                (_num(it, "x2") - _num(it, "x1"))
                                * (_num(it, "y2") - _num(it, "y1")))
                            windows.append(a)
            if closed:
                if primitives is None:
                    r.skip("paste_windows", "the pad's own paste is closed and "
                           "the paste windows could not be read")
                elif not windows:
                    r.add("paste", "error",
                          f"exposed pad {mp.number}: its own paste opening is "
                          "closed and no paste window sits on it, so it gets "
                          "no solder", pad=mp.number)
                else:
                    cov = sum(windows) / area if area > 0 else 0.0
                    r.notes.append(f"exposed pad {mp.number}: {len(windows)} "
                                   f"paste window(s) cover {_f(cov * 100)} % "
                                   "of it")
                    lo, hi = EP_PASTE_RANGE
                    if not lo <= cov <= hi:
                        r.add("paste", "warning",
                              f"exposed pad {mp.number}: {len(windows)} paste "
                              f"window(s) cover {_f(cov * 100)} % of it, "
                              f"outside the {int(lo * 100)} to {int(hi * 100)} "
                              "% of TB389 rev. 7 p. 3", pad=mp.number,
                              actual=f"{_f(cov * 100)} %")
            elif windows:
                r.add("paste", "warning",
                      f"exposed pad {mp.number} keeps its own full paste "
                      f"opening and also has {len(windows)} window(s) on it: "
                      "the windows sit inside a full opening", pad=mp.number)
            else:
                r.add("paste", "warning",
                      f"exposed pad {mp.number} is printed with full paste "
                      f"(its own opening, {'rule-driven' if src == 'rule' else f'expansion {_f(exp)} mm'}); "
                      "TB389 rev. 7 p. 3 asks for 50 to 80 % coverage in "
                      "windows, to keep the part from floating", pad=mp.number)
        elif closed:
            r.add("paste", "error", f"pad {mp.number} has its paste opening "
                  f"closed (manual expansion {_f(exp)} mm), so the terminal "
                  "gets no solder", pad=mp.number)
    # Mask: only manual values say anything about the footprint itself.
    manual = []
    for i, j, _ in pairs:
        fr = fp[j]
        if str(fr.get("mask_expansion_source") or "rule") != "manual":
            continue
        e = _num(fr, "mask_expansion_mm")
        manual.append((mpads[i].number, fr, e))
        if e < -1e-9:
            r.add("mask", "warning", f"pad {mpads[i].number}: solder mask "
                  f"expansion {_f(e)} mm puts mask over the land edge",
                  pad=mpads[i].number)
    if not manual:
        r.notes.append("solder mask: every pad follows the design rule, which "
                       "lives on the board, not in the footprint")
    for a in range(len(manual)):
        for b in range(a + 1, len(manual)):
            na, fa, ea = manual[a]
            nb, fb, eb = manual[b]
            if ea <= 0 and eb <= 0:
                continue
            ba, bb = _pad_box(fa, max(ea, 0.0)), _pad_box(fb, max(eb, 0.0))
            gap = max(bb[0] - ba[2], ba[0] - bb[2], bb[1] - ba[3], ba[1] - bb[3])
            raw = max(_pad_box(fb)[0] - _pad_box(fa)[2],
                      _pad_box(fa)[0] - _pad_box(fb)[2],
                      _pad_box(fb)[1] - _pad_box(fa)[3],
                      _pad_box(fa)[1] - _pad_box(fb)[3])
            if gap <= 0 < raw:
                r.add("mask", "warning",
                      f"the mask openings of pads {na} and {nb} merge (manual "
                      f"expansions {_f(ea)} and {_f(eb)} mm across a "
                      f"{_f(raw)} mm gap): no mask web between them")


def _check_courtyard(r: _Report, model, fp, layers: set[str], prims: dict,
                     body_fp) -> None:
    r.examined.append("courtyard")
    boxes = _item_boxes(prims, layers)
    if not boxes:
        r.add("courtyard", "error",
              "no courtyard: nothing is drawn on a courtyard layer (by "
              "mechanical kind \"Courtyard Top\", or a layer named for it), so "
              "placement clearance cannot be checked against this part")
        return
    court = _box_union(boxes)
    copper = _box_union(_pad_box(p) for p in fp)
    need = _box_union([b for b in (copper, body_fp) if b is not None])
    short = []
    for k, side in ((0, "left"), (1, "bottom"), (2, "right"), (3, "top")):
        over = (court[k] - need[k]) if k < 2 else (need[k] - court[k])
        if over > 1e-3:
            short.append(f"{side} by {_f(over)} mm")
    if short:
        r.add("courtyard", "error",
              "the courtyard does not enclose the lands and the body: it "
              "falls short on the " + ", ".join(short),
              expected=f"around ({_f(need[0])}, {_f(need[1])}) to "
                       f"({_f(need[2])}, {_f(need[3])}) mm",
              actual=f"({_f(court[0])}, {_f(court[1])}) to "
                     f"({_f(court[2])}, {_f(court[3])}) mm")
        return
    excess = float((model.ipc.get("courtyard") or {}).get("excess") or 0.0)
    margin = min(need[0] - court[0], need[1] - court[1], court[2] - need[2],
                 court[3] - need[3])
    if excess and margin < excess - 0.05 - 1e-9:
        r.add("courtyard", "warning",
              f"the courtyard clears the lands and body by {_f(margin)} mm "
              f"at its tightest; IPC-7351B asks {_f(excess)} mm for this "
              f"family at density {model.density}",
              expected=f">= {_f(excess)} mm", actual=f"{_f(margin)} mm")


def _check_silk(r: _Report, fp, layers: set[str], prims: dict) -> None:
    r.examined.append("silk")
    clear = DEFAULT_RULES.silk_clearance
    over: dict[str, float] = {}
    near: dict[str, float] = {}

    def note(name: str, c: float) -> None:
        if c < -1e-6:
            over[name] = min(over.get(name, 0.0), c)
        elif c < clear - 1e-6:
            near[name] = min(near.get(name, clear), c)

    segs: list[tuple[tuple, tuple, float]] = []
    for t in prims.get("tracks") or []:
        if str(t.get("layer")) in layers:
            segs.append(((_num(t, "x1"), _num(t, "y1")),
                         (_num(t, "x2"), _num(t, "y2")), _num(t, "width") / 2))
    for a in prims.get("arcs") or []:
        if str(a.get("layer")) in layers:
            for s0, s1 in _arc_segments(a):
                segs.append((s0, s1, _num(a, "width") / 2))
    for p in fp:
        name = str(p.get("name") or "")
        grow = 0.0
        if str(p.get("mask_expansion_source") or "") == "manual":
            grow = max(0.0, _num(p, "mask_expansion_mm"))
        for a, b, hw in segs:
            note(name, _seg_to_pad(a, b, p, grow) - hw)
        pb = _pad_box(p, grow)
        for key in ("regions", "fills"):
            for it in prims.get(key) or []:
                if str(it.get("layer")) not in layers:
                    continue
                gx = max(_num(it, "x1") - pb[2], pb[0] - _num(it, "x2"))
                gy = max(_num(it, "y1") - pb[3], pb[1] - _num(it, "y2"))
                note(name, max(gx, gy) if (gx < 0 or gy < 0) else math.hypot(gx, gy))
    if over:
        r.add("silk", "error",
              "silkscreen lies on the copper of pad(s) "
              + ", ".join(f"{n} (by {_f(-v)} mm)" for n, v in sorted(over.items()))
              + "; the fab clips it or prints ink on the land")
    if near:
        r.add("silk", "warning",
              "silkscreen closer to the copper than the "
              f"{_f(clear)} mm drafting default (Hausherr): pad(s) "
              + ", ".join(f"{n} ({_f(v)} mm)" for n, v in sorted(near.items())))
    texts_over = []
    for t in prims.get("texts") or []:
        if str(t.get("layer")) not in layers or "x1" not in t:
            continue
        tb = (_num(t, "x1"), _num(t, "y1"), _num(t, "x2"), _num(t, "y2"))
        for p in fp:
            pb = _pad_box(p)
            if tb[0] < pb[2] and pb[0] < tb[2] and tb[1] < pb[3] and pb[1] < tb[3]:
                texts_over.append(f"{t.get('text')!r} on pad {p.get('name')}")
    if texts_over:
        r.add("silk", "warning", "silkscreen text over copper (from the text "
              "boxes, which can lag an edit): " + ", ".join(texts_over[:6]))


def _check_assembly(r: _Report, layers: set[str], prims: dict, body_fp) -> None:
    r.examined.append("assembly")
    boxes = _item_boxes(prims, layers)
    if not boxes:
        r.add("assembly", "warning", "no assembly outline: nothing is drawn on "
              "an assembly layer (kind \"Assembly Top\" or a name for it)")
        return
    if body_fp is None:
        return
    a = _box_union(boxes)
    off = max(abs(a[k] - body_fp[k]) for k in range(4))
    if off > ASSEMBLY_TOL + 1e-9:
        r.add("assembly", "warning",
              f"the assembly outline does not follow the package body: a side "
              f"is {_f(off)} mm from it",
              expected=f"({_f(body_fp[0])}, {_f(body_fp[1])}) to "
                       f"({_f(body_fp[2])}, {_f(body_fp[3])}) mm",
              actual=f"({_f(a[0])}, {_f(a[1])}) to ({_f(a[2])}, {_f(a[3])}) mm")


def _check_height(r: _Report, spec: PackageSpec, prims: dict) -> None:
    r.examined.append("height")
    if spec.height is None or spec.height.max <= 0:
        r.skip("height", "the package gives no height")
        return
    if "height_mm" not in prims:
        r.skip("height", "the footprint height was not read")
        return
    got = _num(prims, "height_mm")
    want = spec.height.max
    if got <= 0:
        r.add("height", "warning", f"the footprint has no height; the "
              f"package is {_f(want)} mm high at most, and 3D clearance "
              "checks read the height", expected=f"{_f(want)} mm", actual="0")
    elif got < want - 0.01:
        r.add("height", "warning", f"the footprint height {_f(got)} mm is "
              f"below the package maximum {_f(want)} mm, so clearance checks "
              "under-read it", expected=f"{_f(want)} mm", actual=f"{_f(got)} mm")
    elif got > 1.5 * want + 0.1:
        r.add("height", "warning", f"the footprint height {_f(got)} mm is far "
              f"above the package maximum {_f(want)} mm",
              expected=f"{_f(want)} mm", actual=f"{_f(got)} mm")


def _finish(r: _Report, model: FootprintModel, alignment, metrics) -> dict:
    errors = sum(1 for f in r.findings if f["severity"] == "error")
    warnings = sum(1 for f in r.findings if f["severity"] == "warning")
    out = {"ok": errors == 0, "errors": errors, "warnings": warnings,
           "findings": r.findings, "examined": r.examined,
           "not_examined": r.not_examined, "notes": r.notes,
           "ipc_model": {"name": model.name, "density": model.density,
                         "pads": len(model.pads)}}
    if alignment is not None:
        out["alignment"] = alignment
    if metrics:
        out["metrics"] = metrics
    return out
