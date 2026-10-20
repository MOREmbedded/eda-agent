# SPDX-License-Identifier: Apache-2.0
"""Compare two library footprints, pad by pad and layer by layer.

Inputs are the live reads (``lib_get_pad_geometry`` and
``lib_get_footprint_primitives``) of each footprint, so the two may come
from one library or two. Pads are matched by name; a pad present on one
side only is listed, and a matched pair lists every field that differs
beyond ``tol_mm`` (lengths) or exactly (everything else). Primitives are
compared as counts per layer and kind, which is what tells a vendor
revision that moved the silkscreen from one that changed the copper.
"""

from __future__ import annotations

from typing import Any

#: Pad fields compared as lengths, within the tolerance.
LENGTH_FIELDS = ("x_mm", "y_mm", "w_mm", "h_mm", "hole_mm", "hole_w_mm",
                 "paste_expansion_mm", "mask_expansion_mm")

#: Pad fields compared exactly.
EXACT_FIELDS = ("shape", "corner_pct", "rotation", "hole_type", "plated", "layer")

_PRIMITIVE_KINDS = ("tracks", "arcs", "regions", "fills", "texts", "vias")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pads_by_name(pads: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in pads.get("pads") or []:
        out.setdefault(str(p.get("name", "")), []).append(p)
    return out


def _counts(prims: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for kind in _PRIMITIVE_KINDS:
        for item in prims.get(kind) or []:
            layer = str(item.get("layer", "")) if kind != "vias" else "Multi-Layer"
            key = f"{layer}/{kind}"
            out[key] = out.get(key, 0) + 1
    return out


def _pad_diff(a: dict, b: dict, tol_mm: float) -> dict[str, list]:
    diff: dict[str, list] = {}
    for f in LENGTH_FIELDS:
        va, vb = _num(a.get(f)), _num(b.get(f))
        if va is None and vb is None:
            continue
        if va is None or vb is None or abs(va - vb) > tol_mm:
            diff[f] = [a.get(f), b.get(f)]
    for f in EXACT_FIELDS:
        va, vb = a.get(f), b.get(f)
        na, nb = _num(va), _num(vb)
        same = (abs(na - nb) < 1e-9) if na is not None and nb is not None else va == vb
        if not same:
            diff[f] = [va, vb]
    return diff


def compare_footprints(pads_a: dict, prims_a: dict, pads_b: dict, prims_b: dict,
                       tol_mm: float = 0.001) -> dict[str, Any]:
    """``{identical, pads: {only_a, only_b, changed, duplicate_names},
    primitives: {layer/kind: [count_a, count_b]}, height_mm}``."""
    by_a, by_b = _pads_by_name(pads_a), _pads_by_name(pads_b)
    only_a = sorted(set(by_a) - set(by_b))
    only_b = sorted(set(by_b) - set(by_a))
    duplicates = sorted(n for n in set(by_a) | set(by_b)
                        if len(by_a.get(n, [])) > 1 or len(by_b.get(n, [])) > 1)
    changed: dict[str, dict] = {}
    for name in sorted(set(by_a) & set(by_b)):
        if name in duplicates:
            # Several pads share the name (a thermal pad split in four, a
            # shield's tabs): pair them in reading order, and say so.
            pairs = zip(sorted(by_a[name], key=lambda p: (_num(p.get("x_mm")) or 0,
                                                          _num(p.get("y_mm")) or 0)),
                        sorted(by_b[name], key=lambda p: (_num(p.get("x_mm")) or 0,
                                                          _num(p.get("y_mm")) or 0)))
        else:
            pairs = zip(by_a[name], by_b[name])
        for i, (pa, pb) in enumerate(pairs):
            d = _pad_diff(pa, pb, tol_mm)
            if d:
                changed[name if i == 0 else f"{name}#{i + 1}"] = d
        if len(by_a[name]) != len(by_b[name]):
            changed.setdefault(name, {})["count"] = [len(by_a[name]), len(by_b[name])]
    ca, cb = _counts(prims_a), _counts(prims_b)
    prim_diff = {k: [ca.get(k, 0), cb.get(k, 0)] for k in sorted(set(ca) | set(cb))
                 if ca.get(k, 0) != cb.get(k, 0)}
    ha, hb = _num(prims_a.get("height_mm")), _num(prims_b.get("height_mm"))
    height = None
    if ha != hb and not (ha is not None and hb is not None and abs(ha - hb) <= tol_mm):
        height = [prims_a.get("height_mm"), prims_b.get("height_mm")]
    return {
        "identical": not (only_a or only_b or changed or prim_diff or height),
        "pad_counts": [len(pads_a.get("pads") or []), len(pads_b.get("pads") or [])],
        "pads": {"only_a": only_a, "only_b": only_b, "changed": changed,
                 "duplicate_names": duplicates},
        "primitives": prim_diff,
        "height_mm": height,
        "tol_mm": tol_mm,
    }


__all__ = ["compare_footprints"]
