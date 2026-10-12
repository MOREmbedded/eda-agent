"""Footprint geometry from a datasheet land pattern.

This generator implements the datasheet's RECOMMENDED LAND PATTERN exactly as
entered (dimensions are inputs, not computed). It does NOT implement the
IPC-7351 land-pattern equations; when an IPC-derived pattern is required, use
Altium's IPC Compliant Footprint Wizard and record the result as a 'custom'
pattern so this pipeline can still verify pin-to-pad mapping and clearances.

Units: input in millimetres, output in integer micrometres (um), which is
what the bridge protocol carries to avoid locale-dependent decimal parsing
inside DelphiScript."""
from __future__ import annotations

from .errors import ValidationFailed


def um(v_mm: float) -> int:
    return int(round(v_mm * 1000))


def _dual_row(p: dict) -> list[dict]:
    n, pitch, span = int(p["pin_count"]), float(p["pitch"]), float(p["row_span"])
    if n % 2:
        raise ValidationFailed("dual_row pattern requires an even pin_count")
    half = n // 2
    top = (half - 1) * pitch / 2
    pads = []
    for i in range(half):  # left column, pin 1 at top, counting down
        pads.append({"name": str(i + 1), "x": -span / 2, "y": top - i * pitch})
    for i in range(half):  # right column, counting up (counter-clockwise numbering)
        pads.append({"name": str(half + i + 1), "x": span / 2, "y": -top + i * pitch})
    for pd in pads:
        pd.update(w=float(p["pad_w"]), h=float(p["pad_h"]), shape=p.get("shape", "rounded_rectangle"))
    return pads


def _custom(p: dict) -> list[dict]:
    pads = []
    for x in p["pads"]:
        pads.append({"name": str(x["name"]), "x": float(x["x"]), "y": float(x["y"]),
                     "w": float(x["w"]), "h": float(x["h"]), "shape": x.get("shape", "rounded_rectangle")})
    return pads


def _rects_gap(a: dict, b: dict) -> float:
    dx = abs(a["x"] - b["x"]) - (a["w"] + b["w"]) / 2
    dy = abs(a["y"] - b["y"]) - (a["h"] + b["h"]) / 2
    if dx < 0 and dy < 0:
        return min(dx, dy)  # overlap (negative)
    return max(dx, dy) if (dx < 0 or dy < 0) else (dx ** 2 + dy ** 2) ** 0.5


def generate_footprint(pkg: dict, min_pad_gap_mm: float = 0.15, silk_clearance_mm: float = 0.15) -> dict:
    pat = pkg["pattern"]
    if pat["type"] == "dual_row":
        pads = _dual_row(pat)
    elif pat["type"] == "custom":
        pads = _custom(pat)
    else:
        raise ValidationFailed(f"unsupported pattern type {pat['type']}")
    for x in pkg.get("extra_pads", []):
        pads += _custom({"pads": [x]})
    names = [p["name"] for p in pads]
    if len(set(names)) != len(names):
        raise ValidationFailed("duplicate pad names")
    errors = []
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            g = _rects_gap(pads[i], pads[j])
            if g < min_pad_gap_mm:
                errors.append(f"pads {pads[i]['name']} and {pads[j]['name']} gap {g:.3f} mm < {min_pad_gap_mm} mm")
    if errors:
        raise ValidationFailed("land pattern violates minimum pad gap", details={"errors": errors})
    body = pkg["body"]
    bl, bw = float(body["length"]), float(body["width"])
    exc = float(pkg.get("courtyard_excess", 0.25))
    xs = [p["x"] - p["w"] / 2 for p in pads] + [p["x"] + p["w"] / 2 for p in pads] + [-bw / 2, bw / 2]
    ys = [p["y"] - p["h"] / 2 for p in pads] + [p["y"] + p["h"] / 2 for p in pads] + [-bl / 2, bl / 2]
    court = {"x1": round(min(xs) - exc, 2), "y1": round(min(ys) - exc, 2),
             "x2": round(max(xs) + exc, 2), "y2": round(max(ys) + exc, 2)}
    # silkscreen: top and bottom body edges, shortened so they clear all pads
    sx = bw / 2
    for p in pads:
        if abs(p["y"]) + p["h"] / 2 + silk_clearance_mm > bl / 2:  # pad reaches body edge rows
            sx = min(sx, abs(p["x"]) - p["w"] / 2 - silk_clearance_mm)
    silk = []
    if sx > 0.1:
        silk = [{"x1": -sx, "y1": bl / 2, "x2": sx, "y2": bl / 2},
                {"x1": -sx, "y1": -bl / 2, "x2": sx, "y2": -bl / 2}]
    pin1 = next((p for p in pads if p["name"] == "1"), pads[0])
    marker = {"x": round(pin1["x"] - pin1["w"] / 2 - 0.4, 3), "y": pin1["y"], "radius": 0.15}
    return {
        "name": pkg["footprint_name"],
        "units": "um",
        "pads": [{"name": p["name"], "x": um(p["x"]), "y": um(p["y"]), "w": um(p["w"]), "h": um(p["h"]),
                  "shape": p["shape"]} for p in pads],
        "courtyard": {k: um(v) for k, v in court.items()},
        "silkscreen": [{k: um(v) for k, v in s.items()} for s in silk],
        "pin1_marker": {"x": um(marker["x"]), "y": um(marker["y"]), "radius": um(marker["radius"])},
        "height_um": um(float(body.get("height", 0))),
    }
