"""Validation of existing libraries (SchLib/PcbLib dumps, Altium 365 library, vendor downloads).

Checks per symbol: pin grid/duplicates/hidden pins, owner naming, required parameters (owner ruleset types
component_parameter_required / parameter_pattern / parameter_forbidden_values), footprint link present and
resolvable, symbol pin count vs footprint pad count.
Checks per footprint: library_audit (gaps, courtyard on the configured layer, silkscreen clearance, origin,
3D body), owner naming, and consistency with the IPC-7351B name (pin count, pitch, row span) when the name
follows the convention.

Phase 1 (this module): logic only, working on already-assembled symbol/footprint dicts or on raw dump
rows shaped like a bulk library-dump bridge command would return them. eda-agent has no such bulk dump
command yet (see docs/ENGINEERING_TOOLS.md); live reads through the existing granular bridge tools
(get_components, get_footprint_pads, get_pin_list, ...) are Phase 2 work.
"""
from __future__ import annotations

import csv
import html
import io
import re
import time
from pathlib import Path

from .libcheck import audit_footprint, audit_symbol

_IPC = [
    (re.compile(r"^SOIC127P(\d+)X(\d+)-(\d+)([MNL])$"), ("gullwing2", 127)),
    (re.compile(r"^(SOP|SSOP|TSOP|SOT)(\d+)P(\d+)X(\d+)-(\d+)([MNL])$"), ("gullwing2", None)),
    (re.compile(r"^QFP(\d+)P(\d+)X(\d+)X(\d+)-(\d+)([MNL])$"), ("gullwing4", None)),
    (re.compile(r"^QFN(\d+)P(\d+)X(\d+)X(\d+)-(\d+)([MNL])$"), ("nolead4", None)),
    (re.compile(r"^(RESC|CAPC|INDC|DIOC|LEDC|FUSC)(\d{2})(\d{2})X(\d+)([MNL])$"), ("chip", None)),
]


def parse_ipc_name(name: str) -> dict | None:
    for rx, (kind, fixed_pitch) in _IPC:
        m = rx.match(name)
        if not m:
            continue
        g = m.groups()
        if kind == "chip":
            return {"kind": kind, "prefix": g[0], "body_l": int(g[1]) / 10, "body_w": int(g[2]) / 10,
                    "height": int(g[3]) / 100, "pins": 2, "density": g[4]}
        if fixed_pitch:
            return {"kind": kind, "prefix": "SOIC", "pitch": 1.27, "span": int(g[0]) / 100, "height": int(g[1]) / 100,
                    "pins": int(g[2]), "density": g[3]}
        if kind == "gullwing2":
            return {"kind": kind, "prefix": g[0], "pitch": int(g[1]) / 100, "span": int(g[2]) / 100,
                    "height": int(g[3]) / 100, "pins": int(g[4]), "density": g[5]}
        return {"kind": kind, "prefix": "QFP" if kind == "gullwing4" else "QFN", "pitch": int(g[0]) / 100,
                "span": int(g[1]) / 100, "span2": int(g[2]) / 100, "height": int(g[3]) / 100, "pins": int(g[4]),
                "density": g[5]}
    return None


def ipc_consistency(fp: dict, info: dict) -> list[dict]:
    out = []
    pads = [p for p in fp["pads"] if not p.get("thermal")]
    if len(pads) != info["pins"] and len(fp["pads"]) != info["pins"]:
        out.append({"severity": "error", "check": "ipc_pins", "object": fp["name"],
                    "message": f"name says {info['pins']} pins, footprint has {len(fp['pads'])} pads"})
    if info.get("pitch") and len(pads) >= 2:
        ys = sorted({p["y"] for p in pads if p["x"] < 0})
        if len(ys) >= 2:
            step = min(b - a for a, b in zip(ys, ys[1:])) / 1000
            if abs(step - info["pitch"]) > 0.011:
                out.append({"severity": "error", "check": "ipc_pitch", "object": fp["name"],
                            "message": f"name says pitch {info['pitch']} mm, pads are {step:.3f} mm apart"})
    return out


def footprints_from_dump(rb_fp: list, rb_pad: list, rb_trk: list, overlay: str, court_layer: str) -> dict:
    fps = {}
    for r in rb_fp:
        fps[r[0]] = {"name": r[0], "description": r[1], "height_um": int(r[2] or 0), "has_3d": int(r[3] or 0) > 0,
                     "pads": [], "silkscreen": [], "court_tracks": []}
    for r in rb_pad:
        if r[0] in fps:
            fps[r[0]]["pads"].append({"name": r[1], "x": int(r[2]), "y": int(r[3]), "w": int(r[4]), "h": int(r[5]),
                                      "shape_code": r[6], "layer": r[7], "hole_um": int(r[8])})
    for r in rb_trk:
        if r[0] not in fps:
            continue
        seg = {"layer": r[1], "x1": int(r[2]), "y1": int(r[3]), "x2": int(r[4]), "y2": int(r[5])}
        if r[1] == overlay:
            fps[r[0]]["silkscreen"].append(seg)
        elif r[1] == court_layer:
            fps[r[0]]["court_tracks"].append(seg)
    for fp in fps.values():
        ct = fp.pop("court_tracks")
        if ct:
            xs = [v for t in ct for v in (t["x1"], t["x2"])]
            ys = [v for t in ct for v in (t["y1"], t["y2"])]
            fp["courtyard"] = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
        if fp["pads"]:
            fp["pin1_marker"] = {"present_if_silk": bool(fp["silkscreen"])} if fp["silkscreen"] else None
    return fps


def symbols_from_dump(rb_sym: list, rb_pin: list, rb_prm: list) -> dict:
    syms = {}
    for r in rb_sym:
        syms[r[0]] = {"lib_ref": r[0], "designator": r[1], "description": r[2], "pin_count": int(r[3] or 0),
                      "footprints": [x for x in (r[4] if len(r) > 4 else "").split(";") if x],
                      "pins": [], "parameters": {}}
    for r in rb_pin:
        if r[0] in syms:
            syms[r[0]]["pins"].append({"number": r[1], "name": r[2], "x": int(r[4]), "y": int(r[5]),
                                       "length": int(r[7]), "hidden": r[8] not in ("0", "", "False")})
    for r in rb_prm:
        if r[0] in syms:
            syms[r[0]]["parameters"][r[1]] = r[2]
    return syms


def validate(symbols: dict, footprints: dict, naming=None, ruleset: dict | None = None) -> dict:
    from .rules import evaluate
    rows = []

    def add(kind, name, findings):
        for f in findings:
            rows.append({"item_type": kind, "item": name, **f})

    for name, fp in footprints.items():
        add("footprint", name, audit_footprint(fp, naming=naming))
        info = parse_ipc_name(name)
        if info:
            add("footprint", name, ipc_consistency(fp, info))
    for ref, sym in symbols.items():
        add("symbol", ref, audit_symbol(sym, naming=naming))
        if not sym["footprints"]:
            rows.append({"item_type": "symbol", "item": ref, "severity": "error", "check": "footprint_link",
                         "object": ref, "message": "no PCB footprint model linked"})
        for fpn in sym["footprints"]:
            fp = footprints.get(fpn)
            if fp is None:
                if footprints:
                    rows.append({"item_type": "symbol", "item": ref, "severity": "warning", "check": "footprint_resolve",
                                 "object": fpn, "message": "linked footprint not found in the validated PcbLib"})
                continue
            npads = len(fp["pads"])
            if sym["pin_count"] and npads < sym["pin_count"]:
                rows.append({"item_type": "symbol", "item": ref, "severity": "error", "check": "pin_pad_count",
                             "object": fpn, "message": f"{sym['pin_count']} pins but {npads} pads"})
    if ruleset:
        # library symbols have no designator; use "<prefix>_<libref>" so prefix-scoped rules (U, Q, J ...) apply
        key = {ref: f"{(s.get('designator') or '').rstrip('?') or 'X'}_{ref}" for ref, s in symbols.items()}
        back = {v: k for k, v in key.items()}
        comps = [{"designator": key[ref], "lib_ref": ref} for ref in symbols]
        params = {key[ref]: s["parameters"] for ref, s in symbols.items()}
        res = evaluate({**ruleset, "rules": [r for r in ruleset["rules"] if r["type"] in (
            "component_parameter_required", "parameter_pattern", "parameter_forbidden_values")]},
            {"components": comps, "parameters": params})
        for f in res["findings"]:
            ref = back.get(f["object"], f["object"])
            rows.append({"item_type": "symbol", "item": ref, "severity": f["severity"], "check": f"rule:{f['rule']}",
                         "object": ref, "message": f["message"]})
    counts = {s: sum(1 for r in rows if r["severity"] == s) for s in ("error", "warning", "info")}
    bad_items = sorted({(r["item_type"], r["item"]) for r in rows if r["severity"] == "error"})
    return {"symbols": len(symbols), "footprints": len(footprints), "counts": counts,
            "items_with_errors": len(bad_items), "passed": counts["error"] == 0, "findings": rows}


def write_reports(result: dict, out_dir: Path, title: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"library_validation_{stamp}"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["severity", "item_type", "item", "check", "object", "message"], extrasaction="ignore")
    w.writeheader()
    for r in result["findings"]:
        w.writerow(r)
    (base.with_suffix(".csv")).write_text(buf.getvalue(), encoding="utf-8")
    colors = {"error": "#fde2e1", "warning": "#fff4d6", "info": "#eef4fb"}
    trs = "".join(f"<tr style='background:{colors.get(r['severity'], '#fff')}'><td>{html.escape(r['severity'])}</td>"
                  f"<td>{html.escape(r['item_type'])}</td><td>{html.escape(r['item'])}</td><td>{html.escape(r['check'])}</td>"
                  f"<td>{html.escape(str(r.get('object', '')))}</td><td>{html.escape(r['message'])}</td></tr>"
                  for r in sorted(result["findings"], key=lambda r: ("error", "warning", "info").index(r["severity"])))
    c = result["counts"]
    doc = (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
           f"<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px}}td,th{{padding:4px 8px;border-bottom:1px solid #ddd;"
           f"font-size:13px;text-align:left}}</style><h1>{html.escape(title)}</h1>"
           f"<p>{result['symbols']} symbols, {result['footprints']} footprints - <b>{c['error']} errors</b>, "
           f"{c['warning']} warnings, {c['info']} info. Generated {time.strftime('%Y-%m-%d %H:%M')}.</p>"
           f"<table><tr><th>Severity</th><th>Type</th><th>Item</th><th>Check</th><th>Object</th><th>Message</th></tr>{trs}</table>")
    (base.with_suffix(".html")).write_text(doc, encoding="utf-8")
    return {"csv": str(base.with_suffix(".csv")), "html": str(base.with_suffix(".html"))}
