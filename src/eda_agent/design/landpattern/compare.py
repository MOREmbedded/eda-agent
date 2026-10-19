# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Measure an IPC land pattern against the datasheet's recommended one.

Vendors deviate from IPC-7351 on purpose (TI's drawings say "Publication
IPC-7351 may have alternate designs"), so the two are not expected to
match. What must match exactly is the topology: pad count and pitch. The
sizes and the row span are compared within tolerances the caller states;
a difference far beyond them is more likely a transcription error in the
package dimensions than a design choice.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from eda_agent.design.landpattern.model import FootprintModel, Pad
from eda_agent.design.landpattern.spec import VendorLandPattern

__all__ = ["CROSS_CHECK_TOL", "compare_to_vendor", "cross_check",
           "pattern_metrics"]


def _signal(model: FootprintModel) -> list[Pad]:
    return [p for p in model.pads if p.role == "signal" and p.kind != "npth"]


def _pitch(pads: list[Pad], axis: str) -> Optional[float]:
    vals = sorted(round(p.y if axis == "y" else p.x, 6) for p in pads)
    gaps = [round(b - a, 6) for a, b in zip(vals, vals[1:]) if b - a > 1e-6]
    return min(gaps) if gaps else None


def pattern_metrics(model: FootprintModel) -> dict[str, Any]:
    """Row pitch, span (centre to centre), land length and width, exposed
    pad, tab and grid land size, read back from the model's lands."""
    sig = _signal(model)
    left = [p for p in sig if p.group == "left"]
    right = [p for p in model.pads if p.group == "right"]
    top = [p for p in sig if p.group == "top"]
    bottom = [p for p in sig if p.group == "bottom"]
    grid = [p for p in sig if p.group == "grid"]
    out: dict[str, Any] = {"pad_count": len([p for p in model.pads if p.number]),
                           "signal_count": len(sig)}
    if grid:
        out["pitch"] = _pitch(grid, "x")
        out["land_diameter"] = grid[0].width
        return out
    if left:
        p = left[0]
        out["pad_length"] = p.width
        out["pad_width"] = p.height
        out["pitch"] = _pitch(left, "y")
        right_sig = [q for q in right if q.role == "signal"] or right
        if right_sig:
            out["span"] = round(right_sig[0].x - p.x, 6)
    if top and bottom:
        out["span_y"] = round(top[0].y - bottom[0].y, 6)
        out["pad_length_y"] = top[0].height
        out["pad_width_y"] = top[0].width
        if out.get("pitch") is None:
            out["pitch"] = _pitch(top, "x")
    tab = [q for q in model.pads if q.role == "tab"]
    if tab:
        out["tab_length"] = tab[0].width
        out["tab_width"] = tab[0].height
        if left:
            out["span"] = round(tab[0].x - left[0].x, 6)
    ep = [q for q in model.pads if q.role == "thermal"]
    if ep:
        out["ep_x"] = ep[0].width
        out["ep_y"] = ep[0].height
        cov = None
        if ep[0].paste_apertures:
            area = sum(a.width * a.height for a in ep[0].paste_apertures)
            cov = area / (ep[0].width * ep[0].height)
        out["paste_coverage"] = round(cov, 4) if cov is not None else None
    return out


def _match(model_pads: list[Pad], vendor_pads: list) -> dict[str, Any]:
    """Centre both patterns, pair pads nearest-first (rotation 0 or 180,
    whichever fits better), and report the worst position and size error."""
    def centred(pts):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return [(x - cx, y - cy, w, h) for x, y, w, h in pts]

    m = centred([(p.x, p.y, p.width, p.height) for p in model_pads])
    best = None
    for flip in (1.0, -1.0):
        v = centred([(flip * q.x, flip * q.y, q.width, q.height) for q in vendor_pads])
        pairs = []
        cand = sorted((math.hypot(a[0] - b[0], a[1] - b[1]), i, j)
                      for i, a in enumerate(m) for j, b in enumerate(v))
        used_m, used_v = set(), set()
        for d, i, j in cand:
            if i in used_m or j in used_v:
                continue
            used_m.add(i)
            used_v.add(j)
            pairs.append((d, i, j))
        cost = sum(d for d, _, _ in pairs)
        if best is None or cost < best[0]:
            best = (cost, pairs, v)
    _, pairs, v = best
    pos = max((d for d, _, _ in pairs), default=0.0)
    size = max((max(abs(m[i][2] - v[j][2]), abs(m[i][3] - v[j][3]))
                for _, i, j in pairs), default=0.0)
    return {"max_position_error": round(pos, 4), "max_size_error": round(size, 4),
            "pairs": len(pairs)}


def compare_to_vendor(model: FootprintModel, vendor: VendorLandPattern, *,
                      span_tol: float = 0.5, length_tol: float = 0.5,
                      width_tol: float = 0.3, ep_tol: float = 0.2,
                      pitch_tol: float = 1e-6,
                      tab_tol: Optional[float] = None,
                      land_tol: Optional[float] = None) -> dict[str, Any]:
    """Compare a model with the vendor pattern; every check carries both
    numbers, the difference and the tolerance it was held to.

    ``tab_tol`` holds both tab sizes and ``land_tol`` the grid land
    diameter; left unset they take ``length_tol`` / ``width_tol`` as
    before."""
    met = pattern_metrics(model)
    checks: list[dict[str, Any]] = []

    def check(name: str, ours: Optional[float], theirs: Optional[float],
              tol: float) -> None:
        if ours is None or theirs is None:
            return
        diff = round(ours - theirs, 4)
        checks.append({"check": name, "ipc": ours, "vendor": theirs,
                       "diff": diff, "tol": tol, "ok": abs(diff) <= tol + 1e-9})

    if vendor.pads:
        named = [p for p in model.pads if p.number]
        checks.append({"check": "pad_count", "ipc": len(named),
                       "vendor": len(vendor.pads), "diff": len(named) - len(vendor.pads),
                       "tol": 0, "ok": len(named) == len(vendor.pads)})
        if len(named) == len(vendor.pads):
            mm = _match(named, list(vendor.pads))
            checks.append({"check": "position", "ipc": mm["max_position_error"],
                           "vendor": 0.0, "diff": mm["max_position_error"],
                           "tol": span_tol / 2, "ok": mm["max_position_error"] <= span_tol / 2})
            checks.append({"check": "size", "ipc": mm["max_size_error"],
                           "vendor": 0.0, "diff": mm["max_size_error"],
                           "tol": length_tol, "ok": mm["max_size_error"] <= length_tol})
    check("pitch", met.get("pitch"), vendor.pitch, pitch_tol)
    check("span", met.get("span"), vendor.span, span_tol)
    check("span_y", met.get("span_y"), vendor.span_y, span_tol)
    check("pad_length", met.get("pad_length"), vendor.pad_length, length_tol)
    check("pad_width", met.get("pad_width"), vendor.pad_width, width_tol)
    check("tab_length", met.get("tab_length"), vendor.tab_length,
          length_tol if tab_tol is None else tab_tol)
    check("tab_width", met.get("tab_width"), vendor.tab_width,
          width_tol if tab_tol is None else tab_tol)
    check("ep_x", met.get("ep_x"), vendor.ep_x, ep_tol)
    check("ep_y", met.get("ep_y"), vendor.ep_y, ep_tol)
    check("land_diameter", met.get("land_diameter"), vendor.land_diameter,
          width_tol if land_tol is None else land_tol)
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "metrics": met,
            "vendor_cite": vendor.cite.to_dict() if vendor.cite else {}}


# --- cross-check: two sources from one datasheet ---------------------------------

#: How far a vendor's recommended land pattern may sit from the IPC pattern
#: of the same package before the pair reads as a transcription error, mm.
#:
#: Measured, not chosen. F1 (tests/design/test_landpattern_vendor.py) held
#: ten golden cases to span 0.5, land length 0.5, land width 0.45, thermal
#: land 0.1 and BGA land 0.05; the largest real deviations there are TI's
#: SOIC-8 span (0.5) and Nexperia's SOD123 land width (0.42). Across all 22
#: vendor patterns in tests/design/_landpattern_cases.py two go further:
#: Abracon ABS07's land width is 0.55 mm over IPC, and the Diodes TO252
#: tab land 0.68 mm wider and 0.55 mm shorter. Width and tab carry those
#: with a small margin. A transcription error of the kind this exists for
#: (a span read as the body, a width read as the pitch, mm read as mils)
#: moves a number by a millimetre or more, or by a factor, so it still
#: fails; the factor is checked on its own below (``RATIO_LIMIT``).
CROSS_CHECK_TOL: dict[str, float] = {
    "pitch": 0.005,
    "span": 0.5,
    "pad_length": 0.5,
    "pad_width": 0.6,
    "tab": 0.7,
    "ep": 0.1,
    "land_diameter": 0.05,
}

#: Two sizes a factor of two or more apart disagree whatever the absolute
#: difference: the largest real ratio in the case file is 1.53 (Nexperia
#: SOD123 land width, 1.22 against 0.80).
RATIO_LIMIT = 2.0

#: Families whose recommended lands need not sit on the terminal pitch
#: (four-pad crystals put the lands at the corners: Abracon ABM8G prints
#: terminals 1.8 apart and lands 2.2 apart). Their pitch is held to the
#: span tolerance instead.
_LANDS_OFF_TERMINAL_PITCH = ("crystal4",)


def _span_dim(spec: Any, axis: str) -> str:
    if axis == "y":
        if spec.lead_span_y is not None:
            return "lead_span_y"
        if spec.family in ("qfn", "son") or spec.lead_span_x is None:
            return "body_y"
        return "lead_span_x"
    return "lead_span_x" if spec.lead_span_x is not None else "body_x"


def _ipc_sources(spec: Any, quantity: str) -> list[str]:
    """The package dimensions an IPC quantity is computed from."""
    if quantity == "pitch":
        return ["pitch"]
    if quantity in ("span", "pad_length", "position", "size"):
        return [_span_dim(spec, "x"), "lead_length", "lead_width"] \
            if quantity in ("position", "size") else \
            [_span_dim(spec, "x"), "lead_length"]
    if quantity == "span_y":
        return [_span_dim(spec, "y"), "lead_length"]
    if quantity == "pad_width":
        return ["lead_width"]
    if quantity == "tab_length":
        return ["tab_length" if spec.tab_length is not None else "lead_length",
                "lead_span_x"]
    if quantity == "tab_width":
        return ["tab_width"]
    if quantity in ("ep_x", "ep_y"):
        return [quantity]
    if quantity == "land_diameter":
        return ["ball_diameter"] if spec.family == "bga" else ["lead_width"]
    return []


def cross_check(spec: Any, *, model: Optional[FootprintModel] = None,
                density: str = "N",
                tolerances: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """The IPC pattern from the package drawing against the vendor's own
    recommended pattern from the same datasheet.

    Per quantity (pitch, span, land length and width, tab, exposed pad,
    grid land, and pad count, position and size for an explicit vendor
    pad list): both numbers, where each came from, the difference, the
    tolerance and a verdict. A disagreement beyond ``CROSS_CHECK_TOL`` or
    by a factor of ``RATIO_LIMIT`` is reported as a likely transcription
    error in one of the two sources, naming both. Density N by default:
    the tolerances were measured at N.
    """
    from eda_agent.design.landpattern.builder import build_footprint
    from eda_agent.design.landpattern.check import describe_dim

    v = spec.vendor
    if v is None:
        return {"ran": False, "ok": True, "checks": [],
                "reason": "the package carries no vendor land pattern "
                          "(``vendor``); nothing to cross-check"}
    tol = dict(CROSS_CHECK_TOL)
    tol.update(tolerances or {})
    if model is None:
        model = build_footprint(spec, density)
    pitch_tol = tol["span"] if spec.family in _LANDS_OFF_TERMINAL_PITCH \
        else tol["pitch"]
    base = compare_to_vendor(model, v, span_tol=tol["span"],
                             length_tol=tol["pad_length"],
                             width_tol=tol["pad_width"], ep_tol=tol["ep"],
                             pitch_tol=pitch_tol, tab_tol=tol["tab"],
                             land_tol=tol["land_diameter"])
    vcite = v.cite.short() if v.cite is not None and v.cite.short() else \
        "no citation"
    out: list[dict[str, Any]] = []
    for c in base["checks"]:
        q = c["check"]
        names = _ipc_sources(spec, q)
        sources = [describe_dim(n, getattr(spec, n, None), spec)
                   for n in names if getattr(spec, n, None) is not None]
        ipc_v, ven_v, diff, t = c["ipc"], c["vendor"], c["diff"], c["tol"]
        ok = bool(c["ok"])
        ratio = None
        if q not in ("pad_count", "position", "size") and \
                isinstance(ipc_v, (int, float)) and isinstance(ven_v, (int, float)) \
                and ipc_v > 0 and ven_v > 0:
            ratio = round(max(ipc_v, ven_v) / min(ipc_v, ven_v), 4)
            if ratio >= RATIO_LIMIT:
                ok = False
        rec = dict(c)
        rec.update({"quantity": q, "ok": ok, "ratio": ratio,
                    "ipc_from": sources, "vendor_cite": vcite})
        if q in ("position", "size"):
            what = ("the worst land centre" if q == "position"
                    else "the worst land size")
            if ok:
                rec["verdict"] = "agree"
                rec["message"] = (f"{what} differs by {ipc_v} mm, within "
                                  f"{t} mm")
            else:
                rec["verdict"] = "likely transcription error"
                rec["message"] = (
                    f"{what} differs by {ipc_v} mm between the IPC pattern "
                    f"(from {'; '.join(sources) or 'the package'}) and the "
                    f"vendor's lands ({vcite}), beyond {t} mm; one of the two "
                    "sources is probably mistyped")
        elif q == "pad_count":
            rec["verdict"] = "agree" if ok else "likely transcription error"
            rec["message"] = (
                f"{ipc_v} lands from the package against {ven_v} vendor lands "
                f"({vcite})" + ("" if ok else "; a land is missing from one "
                                "of the two, or the pin count is wrong"))
        elif ok:
            rec["verdict"] = "agree"
            rec["message"] = (f"{q}: IPC {ipc_v} mm and the vendor's {ven_v} "
                              f"mm agree within {t} mm")
        else:
            why = (f"a factor of {ratio} apart" if ratio is not None and
                   ratio >= RATIO_LIMIT else
                   f"{abs(diff)} mm apart, beyond the {t} mm real vendor "
                   "patterns stay within")
            rec["verdict"] = "likely transcription error"
            rec["message"] = (
                f"{q}: the IPC pattern gives {ipc_v} mm (from "
                f"{'; '.join(sources) or 'the package'}) and the vendor's "
                f"land pattern gives {ven_v} mm ({vcite}), {why}. One of the "
                f"two numbers is probably mistyped: check "
                f"{', '.join(names) or 'the package'} against the package "
                f"drawing and the vendor {q} against {vcite}")
        out.append(rec)
    return {"ran": True, "ok": all(r["ok"] for r in out), "checks": out,
            "density": model.density, "metrics": base["metrics"],
            "vendor_cite": vcite,
            "tolerances": {"pitch": pitch_tol, **{k: v_ for k, v_ in tol.items()
                                                  if k != "pitch"}},
            "tolerance_source": "CROSS_CHECK_TOL in design/landpattern/"
                                "compare.py: the largest deviations measured "
                                "across the case file's vendor patterns"}
