# SPDX-License-Identifier: Apache-2.0
"""One library footprint as an SVG, from the live library reads.

Input is what ``lib_get_pad_geometry`` and ``lib_get_footprint_primitives``
return: millimetres from the footprint origin, each primitive tagged with
the name the library's layer stack gives its layer. The point is a quick
look at a part just drawn (pin 1 where it should be, silkscreen clear of
the pads, a courtyard that encloses everything), not a measurement: the
numbers stay in the reads, and the audits (``lib_audit_footprint_vs_
datasheet``, ``lib_audit_footprint_policies``) are what judge them.

Fills are drawn exactly. A region's read carries only its bounding box, so
a region is drawn as that box, dashed, and the legend says so.
"""

from __future__ import annotations

import html
import math
from typing import Any

#: Layer kind -> (colour, draw order). Lower orders paint first.
_KINDS = {
    "mechanical": ("#8b5cf6", 0),
    "courtyard": ("#f59e0b", 1),
    "assembly": ("#a3a3a3", 2),
    "bottom_copper": ("#3b82f6", 3),
    "copper": ("#dc2626", 4),
    "paste": ("#9ca3af", 5),
    "solder": ("#7c3aed", 6),
    "overlay": ("#facc15", 7),
    "other": ("#22d3ee", 8),
}


def layer_kind(name: str) -> str:
    """Classify a layer by its name (Altium's or a stack's own)."""
    n = name.replace(" ", "").replace("_", "").lower()
    if "courtyard" in n:
        return "courtyard"
    if "assembly" in n:
        return "assembly"
    if "overlay" in n or "silk" in n:
        return "overlay"
    if "paste" in n:
        return "paste"
    if "solder" in n or "mask" in n:
        return "solder"
    if n.startswith("mech"):
        return "mechanical"
    if n.startswith("bottom"):
        return "bottom_copper"
    if n.startswith(("top", "multi", "mid")):
        return "copper"
    return "other"


def _f(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _pad_svg(p: dict, colour: str) -> str:
    x = _f(p, "copper_x_mm", _f(p, "x_mm"))
    y = _f(p, "copper_y_mm", _f(p, "y_mm"))
    w, h = _f(p, "w_mm"), _f(p, "h_mm")
    rot = _f(p, "rotation")
    shape = str(p.get("shape", "")).replace(" ", "").lower()
    tf = f' transform="rotate({-rot:g} {x:g} {-y:g})"' if rot else ""
    if shape.startswith("round") and "rect" not in shape:
        body = f'<ellipse cx="{x:g}" cy="{-y:g}" rx="{w / 2:g}" ry="{h / 2:g}"/>'
    elif shape.startswith("oct"):
        c = min(w, h) / 4
        pts = [(-w / 2 + c, -h / 2), (w / 2 - c, -h / 2), (w / 2, -h / 2 + c),
               (w / 2, h / 2 - c), (w / 2 - c, h / 2), (-w / 2 + c, h / 2),
               (-w / 2, h / 2 - c), (-w / 2, -h / 2 + c)]
        body = '<polygon points="' + " ".join(
            f"{x + a:g},{-y + b:g}" for a, b in pts) + '"/>'
    else:
        r = 0.0
        if "rounded" in shape or "roundrect" in shape:
            # corner_pct is a percentage of half the shorter side.
            r = min(w, h) / 2 * max(0.0, min(_f(p, "corner_pct"), 100.0)) / 100.0
        body = (f'<rect x="{x - w / 2:g}" y="{-y - h / 2:g}" width="{w:g}" '
                f'height="{h:g}" rx="{r:g}"/>')
    hole = ""
    d = _f(p, "hole_mm")
    if d > 0:
        hw = _f(p, "hole_w_mm")
        if str(p.get("hole_type", "")).lower() == "slot" and hw > 0:
            length = max(d, hw)
            width = min(d, hw)
            hrot = rot + _f(p, "hole_rotation")
            hole = (f'<rect class="hole" x="{x - length / 2:g}" y="{-y - width / 2:g}" '
                    f'width="{length:g}" height="{width:g}" rx="{width / 2:g}" '
                    f'transform="rotate({-hrot:g} {x:g} {-y:g})"/>')
        else:
            hole = f'<circle class="hole" cx="{x:g}" cy="{-y:g}" r="{d / 2:g}"/>'
    label = ""
    if min(w, h) >= 0.3:
        size = min(w, h) * 0.5
        label = (f'<text class="pad-name" x="{x:g}" y="{-y:g}" font-size="{size:g}">'
                 f'{_esc(p.get("name", ""))}</text>')
    return (f'<g class="pad" data-name="{_esc(p.get("name", ""))}" fill="{colour}"{tf}>'
            f'{body}</g>{hole}{label}')


def _arc_svg(a: dict) -> str:
    cx, cy, r = _f(a, "xc"), _f(a, "yc"), _f(a, "r")
    if r <= 0:
        return ""
    sa, ea = _f(a, "start"), _f(a, "end")
    sweep = (ea - sa) % 360.0 or 360.0
    w = max(_f(a, "width"), 0.01)
    if sweep >= 359.999:
        return f'<circle cx="{cx:g}" cy="{-cy:g}" r="{r:g}" fill="none" stroke-width="{w:g}"/>'
    sx, sy = cx + r * math.cos(math.radians(sa)), cy + r * math.sin(math.radians(sa))
    ex, ey = cx + r * math.cos(math.radians(sa + sweep)), cy + r * math.sin(math.radians(sa + sweep))
    large = 1 if sweep > 180 else 0
    # Anticlockwise in Altium's Y-up frame is clockwise once Y is flipped.
    return (f'<path d="M{sx:g},{-sy:g} A{r:g},{r:g} 0 {large} 0 {ex:g},{-ey:g}" '
            f'fill="none" stroke-width="{w:g}"/>')


def render_footprint_svg(pads: dict, prims: dict,
                         margin_mm: float = 0.5) -> tuple[str, dict]:
    """The SVG text and a summary ``{bbox_mm, pads, by_layer}``."""
    items: list[tuple[int, str]] = []
    by_layer: dict[str, int] = {}
    xs: list[float] = [0.0]
    ys: list[float] = [0.0]

    def _put(layer: str, svg: str, *pts: tuple[float, float]) -> None:
        kind = layer_kind(layer)
        by_layer[layer] = by_layer.get(layer, 0) + 1
        items.append((_KINDS[kind][1], f'<g data-layer="{_esc(layer)}" '
                      f'stroke="{_KINDS[kind][0]}" fill="{_KINDS[kind][0]}">{svg}</g>'))
        for x, y in pts:
            xs.append(x)
            ys.append(y)

    for t in prims.get("tracks") or []:
        w = max(_f(t, "width"), 0.01)
        _put(str(t.get("layer", "")),
             f'<line x1="{_f(t, "x1"):g}" y1="{-_f(t, "y1"):g}" x2="{_f(t, "x2"):g}" '
             f'y2="{-_f(t, "y2"):g}" stroke-width="{w:g}" stroke-linecap="round"/>',
             (_f(t, "x1"), _f(t, "y1")), (_f(t, "x2"), _f(t, "y2")))
    for a in prims.get("arcs") or []:
        r = _f(a, "r")
        _put(str(a.get("layer", "")), _arc_svg(a),
             (_f(a, "xc") - r, _f(a, "yc") - r), (_f(a, "xc") + r, _f(a, "yc") + r))
    for kind, dashed in (("fills", False), ("regions", True)):
        for f in prims.get(kind) or []:
            x1, y1, x2, y2 = (_f(f, k) for k in ("x1", "y1", "x2", "y2"))
            style = ('fill="none" stroke-width="0.05" stroke-dasharray="0.2 0.1"'
                     if dashed else 'fill-opacity="0.5" stroke="none"')
            _put(str(f.get("layer", "")),
                 f'<rect x="{min(x1, x2):g}" y="{-max(y1, y2):g}" width="{abs(x2 - x1):g}" '
                 f'height="{abs(y2 - y1):g}" {style}/>', (x1, y1), (x2, y2))
    for p in pads.get("pads") or []:
        kind = layer_kind(str(p.get("layer", "Top Layer")))
        colour = _KINDS[kind][0]
        x = _f(p, "copper_x_mm", _f(p, "x_mm"))
        y = _f(p, "copper_y_mm", _f(p, "y_mm"))
        half = math.hypot(_f(p, "w_mm"), _f(p, "h_mm")) / 2
        by_layer[str(p.get("layer", ""))] = by_layer.get(str(p.get("layer", "")), 0) + 1
        items.append((_KINDS["copper"][1], _pad_svg(p, colour)))
        xs.extend((x - half, x + half))
        ys.extend((y - half, y + half))
    for v in prims.get("vias") or []:
        r = _f(v, "diameter") / 2
        x, y = _f(v, "x"), _f(v, "y")
        items.append((_KINDS["copper"][1],
                      f'<circle cx="{x:g}" cy="{-y:g}" r="{r:g}" fill="{_KINDS["copper"][0]}"/>'
                      f'<circle class="hole" cx="{x:g}" cy="{-y:g}" r="{_f(v, "hole") / 2:g}"/>'))
        xs.extend((x - r, x + r))
        ys.extend((y - r, y + r))

    x1, x2 = min(xs) - margin_mm, max(xs) + margin_mm
    y1, y2 = min(ys) - margin_mm, max(ys) + margin_mm
    w, h = x2 - x1, y2 - y1
    cross = max(w, h) / 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x1:g} {-y2:g} {w:g} {h:g}">',
        f'<title>{_esc(pads.get("name") or prims.get("name") or "footprint")}</title>',
        '<style>.hole{fill:#111827}.pad-name{fill:#fff;font-family:sans-serif;'
        'text-anchor:middle;dominant-baseline:central}</style>',
        f'<rect x="{x1:g}" y="{-y2:g}" width="{w:g}" height="{h:g}" fill="#1f2937"/>',
    ]
    parts += [svg for _, svg in sorted(items, key=lambda i: i[0])]
    parts.append(f'<g stroke="#e5e7eb" stroke-width="{cross / 8:g}">'
                 f'<line x1="{-cross:g}" y1="0" x2="{cross:g}" y2="0"/>'
                 f'<line x1="0" y1="{-cross:g}" x2="0" y2="{cross:g}"/></g>')
    parts.append("</svg>")
    summary = {
        "bbox_mm": {"x1": round(x1 + margin_mm, 4), "y1": round(y1 + margin_mm, 4),
                    "x2": round(x2 - margin_mm, 4), "y2": round(y2 - margin_mm, 4)},
        "pads": len(pads.get("pads") or []),
        "by_layer": by_layer,
        "regions_as_boxes": len(prims.get("regions") or []),
    }
    return "".join(parts), summary


__all__ = ["layer_kind", "render_footprint_svg"]
