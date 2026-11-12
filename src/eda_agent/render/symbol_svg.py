# SPDX-License-Identifier: Apache-2.0
"""One library symbol as an SVG, from ``library.get_symbol_geometry``.

The read has the shape of one component in the schematic geometry
(primitives in mils, Altium colours, pins as their body-side root with a
length and a rotation), so the drawing is the schematic renderer's: a
symbol previewed here looks as it will on the sheet. Only the frame is
this module's: the bounding box, worked out from the graphics and both
ends of every pin because a symbol that is not placed has no box of its
own, and the free text (labels and visible parameters), drawn as the
symbol's own text.
"""
from __future__ import annotations

import math
from typing import Any

from .sch_svg import SchRenderOptions, render_sch_svg

#: Direction a pin points, body side to electrical end, by its rotation.
_PIN_DIR = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def overbar(text: Any) -> str:
    """Altium's negation mark as drawn: a letter followed by a backslash
    carries a bar (``R\\S\\T\\`` reads RST with a bar over it). The bar is a
    combining overline, so the text stays text."""
    out: list[str] = []
    for ch in str(text or ""):
        if ch == "\\" and out and out[-1] not in ("\u0305", " "):
            out.append("\u0305")
        else:
            out.append(ch)
    return "".join(out)


def symbol_bbox(read: dict[str, Any], *, hidden_pins: bool = False
                ) -> tuple[float, float, float, float] | None:
    """(x1, y1, x2, y2) in mils around the graphics, the text anchors and
    both ends of each pin drawn; None for a symbol with nothing in it."""
    xs: list[float] = []
    ys: list[float] = []
    for p in read.get("primitives") or []:
        kind = p.get("kind")
        if kind in ("rect", "roundrect", "line"):
            xs += [_num(p.get("x1")), _num(p.get("x2"))]
            ys += [_num(p.get("y1")), _num(p.get("y2"))]
        elif kind == "arc":
            r = _num(p.get("r"))
            r2 = _num(p.get("r2")) or r
            xs += [_num(p.get("cx")) - r, _num(p.get("cx")) + r]
            ys += [_num(p.get("cy")) - r2, _num(p.get("cy")) + r2]
        elif kind == "ellipse":
            xs += [_num(p.get("cx")) - _num(p.get("rx")), _num(p.get("cx")) + _num(p.get("rx"))]
            ys += [_num(p.get("cy")) - _num(p.get("ry")), _num(p.get("cy")) + _num(p.get("ry"))]
        elif kind in ("polyline", "polygon", "bezier"):
            for v in p.get("pts") or []:
                xs.append(_num(v[0]))
                ys.append(_num(v[1]))
        elif kind == "text":
            xs.append(_num(p.get("x")))
            ys.append(_num(p.get("y")))
    for pin in read.get("pins") or []:
        if pin.get("hidden") and not hidden_pins:
            continue
        x, y = _num(pin.get("x")), _num(pin.get("y"))
        dx, dy = _PIN_DIR.get(int(_num(pin.get("rot"))) % 360, (1, 0))
        length = _num(pin.get("len"))
        xs += [x, x + dx * length]
        ys += [y, y + dy * length]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def symbol_geometry(read: dict[str, Any], *, hidden_pins: bool = False) -> dict[str, Any]:
    """The read as a one-component schematic geometry."""
    des = str(read.get("designator") or "") or "?"
    box = symbol_bbox(read, hidden_pins=hidden_pins) or (-100.0, -100.0, 100.0, 100.0)
    graphics = [p for p in read.get("primitives") or [] if p.get("kind") != "text"]
    texts = [{"name": p.get("name", ""), "text": overbar(p.get("text", "")), "x": p.get("x", 0),
              "y": p.get("y", 0), "rot": p.get("rot", 0), "color": p.get("color")}
             for p in read.get("primitives") or [] if p.get("kind") == "text"]
    pins = [dict(p, comp=des, name=overbar(p.get("name", ""))) for p in read.get("pins") or []
            if hidden_pins or not p.get("hidden")]
    return {
        "doc": read.get("lib_ref") or "symbol",
        "components": [{
            "des": des, "lib_ref": read.get("lib_ref", ""), "x": 0, "y": 0,
            "bbox": {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]},
            "primitives": graphics, "params": texts}],
        "pins": pins,
    }


def render_symbol_svg(read: dict[str, Any], *, hidden_pins: bool = False,
                      title: str = "") -> str:
    """The symbol as a self-contained SVG string (viewBox in mils)."""
    geom = symbol_geometry(read, hidden_pins=hidden_pins)
    box = geom["components"][0]["bbox"]
    span = max(box["x2"] - box["x1"], box["y2"] - box["y1"], 100.0)
    # Room for the designator above and the library reference below.
    margin = int(max(120, math.ceil(span * 0.08)))
    opt = SchRenderOptions(margin=margin, title=title or read.get("lib_ref") or None)
    return render_sch_svg(geom, opt)


__all__ = ["overbar", "render_symbol_svg", "symbol_bbox", "symbol_geometry"]
