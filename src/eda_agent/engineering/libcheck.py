"""Library expert checks: footprint, symbol and component audits, plus IPC cross-check.

Input geometry is the footprint dictionary (micrometres) produced by footprint.py/ipc7351.py, or the
read-back of an existing PcbLib footprint (pcblib_query_footprint_detail), so libraries made by other
tools (PCB Library Expert, vendor downloads, colleagues) can be audited against the same rules."""
from __future__ import annotations

import math
import re

from .ipc7351 import build as ipc_build

SEV = ("error", "warning", "info")


def _f(sev, check, msg, obj=""):
    return {"severity": sev, "check": check, "object": obj, "message": msg}


def _gap(a, b):
    dx = abs(a["x"] - b["x"]) - (a["w"] + b["w"]) / 2
    dy = abs(a["y"] - b["y"]) - (a["h"] + b["h"]) / 2
    if dx < 0 and dy < 0:
        return min(dx, dy)
    return max(dx, dy) if (dx < 0 or dy < 0) else math.hypot(dx, dy)


def audit_footprint(fp: dict, *, min_pad_gap_um: int = 150, silk_clear_um: int = 100,
                    naming=None, origin_tol_um: int = 50) -> list[dict]:
    out = []
    pads = fp.get("pads", [])
    if not pads:
        return [_f("error", "pads", "footprint has no pads", fp.get("name", ""))]
    names = [p["name"] for p in pads]
    if len(set(names)) != len(names):
        out.append(_f("error", "pad_names", "duplicate pad names"))
    numeric = sorted(int(n) for n in names if n.isdigit())
    if numeric and numeric != list(range(1, len(numeric) + 1)):
        out.append(_f("warning", "pad_numbering", f"numeric pad names not continuous from 1: {numeric[:10]}"))
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            g = _gap(pads[i], pads[j])
            if g < min_pad_gap_um:
                out.append(_f("error", "pad_gap", f"gap {g:.0f} um < {min_pad_gap_um} um", f"{pads[i]['name']}-{pads[j]['name']}"))
    c = fp.get("courtyard")
    if not c:
        out.append(_f("error", "courtyard", "no courtyard"))
    else:
        for p in pads:
            if (p["x"] - p["w"] / 2 < c["x1"] or p["x"] + p["w"] / 2 > c["x2"] or
                    p["y"] - p["h"] / 2 < c["y1"] or p["y"] + p["h"] / 2 > c["y2"]):
                out.append(_f("error", "courtyard", "pad outside courtyard", p["name"]))
    for s in fp.get("silkscreen", []):
        for p in pads:
            # distance from silk segment to pad rectangle (axis-aligned segments only)
            if s["y1"] == s["y2"]:
                y = s["y1"]
                if min(s["x1"], s["x2"]) - silk_clear_um < p["x"] + p["w"] / 2 and \
                        max(s["x1"], s["x2"]) + silk_clear_um > p["x"] - p["w"] / 2 and \
                        abs(y - p["y"]) < p["h"] / 2 + silk_clear_um:
                    out.append(_f("warning", "silk_clearance", "silkscreen closer than clearance to pad", p["name"]))
    if not fp.get("pin1_marker") and len(pads) > 2:
        out.append(_f("warning", "pin1_marker", "no pin 1 marker"))
    sig = [p for p in pads if not p.get("thermal")]
    cx = (max(p["x"] for p in sig) + min(p["x"] for p in sig)) / 2
    cy = (max(p["y"] for p in sig) + min(p["y"] for p in sig)) / 2
    if abs(cx) > origin_tol_um or abs(cy) > origin_tol_um:
        out.append(_f("warning", "origin", f"origin not at pad-pattern centre (offset {cx:.0f},{cy:.0f} um)"))
    if fp.get("has_3d") is False:
        out.append(_f("warning", "3d_model", "no 3D body"))
    if naming is not None and fp.get("name"):
        try:
            naming.validate("footprint", fp["name"])
        except Exception as e:  # ValidationFailed
            out.append(_f("error", "naming", str(e), fp["name"]))
    return out


def audit_symbol(sym: dict, *, grid: int = 100, naming=None) -> list[dict]:
    out = []
    pins = sym.get("pins", [])
    nums = [p["number"] for p in pins]
    if len(set(nums)) != len(nums):
        out.append(_f("error", "pin_numbers", "duplicate pin numbers"))
    for p in pins:
        if p.get("x", 0) % grid or p.get("y", 0) % grid:
            out.append(_f("error", "grid", f"pin off {grid} mil grid", p["number"]))
        if p.get("length", 200) % grid and p.get("length", 200) not in (150, 250):
            out.append(_f("warning", "pin_length", f"unusual pin length {p.get('length')}", p["number"]))
        if p.get("hidden"):
            out.append(_f("warning", "hidden_pin", "hidden pin (company practice usually forbids)", p["number"]))
    if naming is not None and sym.get("lib_ref"):
        try:
            naming.validate("symbol", sym["lib_ref"])
        except Exception as e:
            out.append(_f("error", "naming", str(e), sym["lib_ref"]))
    return out


def cross_check_ipc(fp: dict, ipc_spec: dict, tol_um: int = 20, goals: dict | None = None) -> dict:
    """Compare an existing footprint (e.g. produced by PCB Library Expert) with this system's IPC-7351B
    calculation for the same package data. Agreement within tol_um is independent evidence that both are right;
    disagreement is reported pad by pad. Different solder-joint goal tables legitimately cause differences."""
    ref = ipc_build(ipc_spec, goals)
    ref_pads = {p["name"]: p for p in ref["pads"]}
    got = {p["name"]: p for p in fp.get("pads", [])}
    diffs = []
    for name, r in ref_pads.items():
        if name not in got:
            diffs.append({"pad": name, "issue": "missing"})
            continue
        g = got[name]
        d = {k: round(g[k] - r[k] * 1000) for k in ("x", "y", "w", "h")}
        if any(abs(v) > tol_um for v in d.values()):
            diffs.append({"pad": name, "delta_um": d})
    extra = sorted(set(got) - set(ref_pads))
    return {"ipc_name": ref["ipc_name"], "agree": not diffs and not extra, "tolerance_um": tol_um,
            "differences": diffs[:50], "extra_pads": extra, "reference": {"Z": ref["rounded"]["Z"],
            "G": ref["rounded"]["G"], "X": ref["rounded"]["X"], "goal_status": ref["goal_status"]}}


def summarize(findings: list[dict]) -> dict:
    return {"passed": not any(f["severity"] == "error" for f in findings),
            "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in SEV}, "findings": findings}


# ---------------------------------------------------------------- PCB Library Expert hand-off
RISKY_DELPHI = re.compile(r"\b(RunApplication|ShellExecute|WinExec|CreateOleObject|DeleteFile|RemoveDir|"
                          r"RenameFile|CopyFile|CreateObject|WScript|Shell\.|RegWrite|SaveToFile)\b", re.I)


def scan_script(text: str) -> list[dict]:
    """Static scan of a generated DelphiScript (e.g. PCB Library Expert Altium output) before the owner runs it.
    It flags calls that touch processes, the registry or files. Footprint-building scripts normally need none
    of these except possibly SaveToFile; anything flagged must be reviewed by a human."""
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        for m in RISKY_DELPHI.finditer(line):
            hits.append({"line": n, "call": m.group(1), "text": line.strip()[:160]})
    return hits
