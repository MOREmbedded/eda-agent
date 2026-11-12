# SPDX-License-Identifier: Apache-2.0
"""Calculator inputs read from a board's layer stack.

``pcb.get_layer_stackup`` is copper-centric: one entry per copper layer,
each carrying the dielectric beneath it. This turns that into the values
a calculator asks for, per copper layer: the copper thickness, the
dielectric to the layer below and above, its Er, and the board's total
thickness and plane count. Pure: no Altium here.
"""
from __future__ import annotations

from typing import Any

#: Altium layer ids of the internal plane layers.
_PLANE_IDS = range(39, 55)


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def from_stackup(live: dict[str, Any]) -> dict[str, Any]:
    """Per-layer prefill values from a ``pcb_get_layer_stackup`` reply.

    Returns ``{ok, board_name, layers: [{name, kind, structure, values}]}``
    where ``values`` maps a prefill key to ``{"value", "unit"}``.
    """
    rows = [r for r in (live or {}).get("layers") or [] if isinstance(r, dict)]
    if not rows:
        return {"ok": False, "reason": "the board's layer stack could not be read"}
    n = len(rows)
    thickness = sum(_num(r.get("copper_thickness_mils")) + _num(r.get("dielectric_height_mils"))
                    for r in rows)
    is_plane = [int(_num(r.get("layer_id"))) in _PLANE_IDS for r in rows]
    planes = sum(is_plane)
    gaps = [_num(rows[i].get("dielectric_height_mils")) for i in range(n - 1)
            if is_plane[i] and is_plane[i + 1]]
    inner = [_num(r.get("dielectric_height_mils")) for r in rows[:-1]
             if _num(r.get("dielectric_height_mils")) > 0]
    plane_gap = min(gaps) if gaps else (min(inner) if inner else 0.0)
    out = []
    for i, r in enumerate(rows):
        below = _num(r.get("dielectric_height_mils")) if i < n - 1 else 0.0
        above = _num(rows[i - 1].get("dielectric_height_mils")) if i > 0 else 0.0
        er_below = _num(r.get("dielectric_constant")) if i < n - 1 else 0.0
        er_above = _num(rows[i - 1].get("dielectric_constant")) if i > 0 else 0.0
        outer = i == 0 or i == n - 1
        h = below if i == 0 else above if i == n - 1 else min(x for x in (below, above) if x > 0) \
            if (below > 0 or above > 0) else 0.0
        er = (er_below if i == 0 else er_above if i == n - 1 else (er_below or er_above))
        vals: dict[str, dict[str, Any]] = {}

        def put(key: str, value: float, unit: str) -> None:
            if value > 0:
                vals[key] = {"value": round(value, 4), "unit": unit}
        put("stackup.copper", _num(r.get("copper_thickness_mils")), "mil")
        put("stackup.dielectric_height", h, "mil")
        put("stackup.h_below", below or above, "mil")
        put("stackup.h_above", above or below, "mil")
        put("stackup.er", er, "")
        put("stackup.plane_gap", plane_gap, "mil")
        put("board.thickness", thickness, "mil")
        vals["board.planes"] = {"value": "none" if planes == 0 else "one" if planes == 1 else "two",
                                "unit": ""}
        out.append({
            "name": str(r.get("name", f"Layer {i + 1}")),
            "kind": "plane" if is_plane[i] else "outer" if outer else "inner",
            "structure": "microstrip" if outer else "stripline",
            "values": vals,
        })
    return {"ok": True, "board_name": str(live.get("board_name", "")),
            "layer_count": n, "thickness_mils": round(thickness, 3), "layers": out}
