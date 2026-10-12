"""IPC-7351B land-pattern calculator and naming.

Equations (IPC-7351, section 3; confirmed in the standard's text and independent sources):
    Zmax = Lmin + 2*Jt + sqrt(CL^2 + F^2 + P^2)
    Gmin = Smax - 2*Jh - sqrt(CS^2 + F^2 + P^2)
    Xmax = Wmin + 2*Js + sqrt(CW^2 + F^2 + P^2)
with the RMS method for the inner dimension:
    Smin = Lmin - 2*Tmax,   Stol(RMS) = sqrt(Ltol^2 + 2*Ttol^2),   Smax(RMS) = Smin + Stol(RMS),   CS = Stol(RMS)
L = toe-to-toe span (or body length for chips), T = terminal/lead length, W = terminal/lead width.

Solder-joint goals (Jt, Jh, Js) and courtyard excess come from config/ipc/ipc7351b_goals.json, overridable by
config/ipc/company_goals.json. Unconfigured (null) goals are refused, never guessed.

Supported families: chip (2 terminals), gullwing2 (SOIC/SOP/SSOP/TSOP/SOT), gullwing4 (QFP), nolead2 (DFN/SON),
nolead4 (QFN), optional exposed thermal pad. Output is the footprint dictionary used by footprint.py and
the native create_pcblib_footprint command (integer micrometres).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .errors import ValidationFailed

# Owner-editable copy (set by install.ps1) survives updates; the release copy is only the default.
ROOT_CFG = Path(__file__).resolve().parent / "data"
DENSITIES = ("M", "N", "L")
PREFIXES = {
    "chip": ("RESC", "CAPC", "INDC", "DIOC", "LEDC", "FUSC"),
    "gullwing2": ("SOIC", "SOP", "SSOP", "TSOP", "SOT"),
    "gullwing4": ("QFP",),
    "nolead2": ("DFN", "SON"),
    "nolead4": ("QFN",),
}


# ---------------------------------------------------------------- goals
def load_goals(base: Path | None = None, company: Path | None = None) -> dict:
    root = Path(os.environ.get("EDA_AGENT_IPC_DIR") or os.environ.get("ALTIUM_AI_IPC_DIR") or ROOT_CFG)
    base = base or root / "ipc7351b_goals.json"
    g = json.loads(Path(base).read_text(encoding="utf-8"))
    company = company or root / "company_goals.json"
    if Path(company).exists():
        over = json.loads(Path(company).read_text(encoding="utf-8"))
        for k in ("tolerances", "rounding"):
            g[k].update(over.get(k, {}))
        if "min_pad_gap" in over:
            g["min_pad_gap"] = over["min_pad_gap"]
        for fam, vals in over.get("families", {}).items():
            tgt = g["families"].setdefault(fam, {})
            for key, v in vals.items():
                if isinstance(v, dict) and isinstance(tgt.get(key), dict):
                    tgt[key].update(v)
                else:
                    tgt[key] = v
            tgt["status"] = vals.get("status", "company override")
    return g


def goal_family(kind: str, pitch: float | None, body_l: float) -> str:
    if kind == "chip":
        return "chip_ge_1608" if body_l >= 1.6 - 1e-9 else "chip_lt_1608"
    if kind in ("gullwing2", "gullwing4"):
        return "gullwing_pitch_gt_0625" if pitch and pitch > 0.625 else "gullwing_pitch_le_0625"
    return "nolead"


def goals_for(g: dict, family: str, density: str) -> dict:
    f = g["families"][family]
    out = {}
    for key in ("toe", "heel", "side", "courtyard"):
        v = f[key].get(density)
        if v is None:
            raise ValidationFailed(
                f"IPC goal '{key}' for family {family}, density {density} is not configured",
                details={"family": family, "status": f.get("status"),
                         "fix": "add the value from your licensed IPC-7351B copy to config/ipc/company_goals.json"})
        out[key] = float(v)
    out["status"] = f.get("status", "")
    return out


# ---------------------------------------------------------------- rounding helpers
def _up(v: float, step: float) -> float:
    return round(math.ceil(v / step - 1e-9) * step, 4)


def _down(v: float, step: float) -> float:
    return round(math.floor(v / step + 1e-9) * step, 4)


def _rng(d: dict, key: str) -> tuple[float, float]:
    v = d[key]
    if isinstance(v, (int, float)):
        return float(v), float(v)
    lo, hi = float(v[0]), float(v[1])
    if lo > hi:
        raise ValidationFailed(f"{key}: min {lo} greater than max {hi}")
    return lo, hi


# ---------------------------------------------------------------- core equations
def land_equations(L: tuple, T: tuple, W: tuple, jt: float, jh: float, js: float, F: float, P: float) -> dict:
    Lmin, Lmax = L
    Tmin, Tmax = T
    Wmin, Wmax = W
    Ltol, Ttol, Wtol = Lmax - Lmin, Tmax - Tmin, Wmax - Wmin
    Smin = Lmin - 2 * Tmax
    Stol_rms = math.sqrt(Ltol ** 2 + 2 * Ttol ** 2)
    Smax_rms = Smin + Stol_rms
    Z = Lmin + 2 * jt + math.sqrt(Ltol ** 2 + F ** 2 + P ** 2)
    G = Smax_rms - 2 * jh - math.sqrt(Stol_rms ** 2 + F ** 2 + P ** 2)
    X = Wmin + 2 * js + math.sqrt(Wtol ** 2 + F ** 2 + P ** 2)
    return {"Zmax": Z, "Gmin": G, "Xmax": X, "Smin": Smin, "Smax_rms": Smax_rms, "Stol_rms": Stol_rms}


# ---------------------------------------------------------------- naming (IPC-7351B convention)
def _h(v: float) -> str:      # two places past the decimal, no leading zeros: 1.75 -> 175, 0.55 -> 55
    return str(int(round(v * 100)))


def _d1(v: float) -> str:     # chip body digits: 1.6 -> 16, 0.8 -> 08
    return f"{int(v * 10 + 1e-6):02d}"


def ipc_name(kind: str, prefix: str, density: str, dims: dict) -> str:
    if prefix not in PREFIXES[kind]:
        raise ValidationFailed(f"prefix {prefix} not valid for {kind}; allowed {PREFIXES[kind]}")
    if kind == "chip":
        return f"{prefix}{_d1(dims['body_l_nom'])}{_d1(dims['body_w_nom'])}X{_h(dims['height_max'])}{density}"
    pins = dims["pins_total"]
    if kind == "gullwing2":
        if prefix == "SOIC":
            if abs(dims["pitch"] - 1.27) > 1e-6:
                raise ValidationFailed("SOIC prefix requires 1.27 mm pitch; use SOP")
            return f"SOIC127P{_h(dims['span_nom'])}X{_h(dims['height_max'])}-{pins}{density}"
        return f"{prefix}{_h(dims['pitch'])}P{_h(dims['span_nom'])}X{_h(dims['height_max'])}-{pins}{density}"
    if kind == "gullwing4":
        return (f"QFP{_h(dims['pitch'])}P{_h(dims['span_nom'])}X{_h(dims['span2_nom'])}X{_h(dims['height_max'])}"
                f"-{pins}{density}")
    if kind == "nolead4":
        return (f"QFN{_h(dims['pitch'])}P{_h(dims['body_l_nom'])}X{_h(dims['body_w_nom'])}X{_h(dims['height_max'])}"
                f"-{pins}{density}")
    if prefix == "SON":
        return (f"SON{_h(dims['pitch'])}P{_h(dims['body_l_nom'])}X{_h(dims['body_w_nom'])}X{_h(dims['height_max'])}"
                f"-{pins}{density}")
    return f"DFN{_h(dims['body_l_nom'])}X{_h(dims['body_w_nom'])}X{_h(dims['height_max'])}-{pins}{density}"


# ---------------------------------------------------------------- footprint builder
def build(spec: dict, goals: dict | None = None) -> dict:
    """spec keys:
      kind: chip | gullwing2 | gullwing4 | nolead2 | nolead4
      prefix: IPC prefix (RESC, SOIC, QFP ...); density: M | N | L
      pins: total terminal count (excluding thermal pad); pitch (mm, not for chip)
      L: [min, max] toe-to-toe span (chip: body length); L2 for the other axis of quad packages
      T: [min, max] terminal length; W: [min, max] terminal width
      body_l, body_w: [min, max] body size; height: [min, max] or max
      thermal_pad: optional {"w": mm, "h": mm} (nominal exposed pad)
    """
    g = goals or load_goals()
    kind, density = spec["kind"], spec.get("density", "N")
    if kind not in PREFIXES or density not in DENSITIES:
        raise ValidationFailed(f"kind must be one of {sorted(PREFIXES)} and density one of {DENSITIES}")
    L, T, W = _rng(spec, "L"), _rng(spec, "T"), _rng(spec, "W")
    bl, bw = _rng(spec, "body_l"), _rng(spec, "body_w")
    H = _rng(spec, "height")
    pitch = float(spec["pitch"]) if kind != "chip" else None
    pins = 2 if kind == "chip" else int(spec["pins"])
    fam = goal_family(kind, pitch, (bl[0] + bl[1]) / 2)
    gl = goals_for(g, fam, density)
    F, P = float(g["tolerances"]["F"]), float(g["tolerances"]["P"])
    rnd = float(g["rounding"]["pad_small_chip"] if fam == "chip_lt_1608" else g["rounding"]["pad"])
    eq = land_equations(L, T, W, gl["toe"], gl["heel"], gl["side"], F, P)
    Z, G, X = _up(eq["Zmax"], rnd), _down(eq["Gmin"], rnd), _up(eq["Xmax"], rnd)
    warnings = []
    min_gap = float(g["min_pad_gap"])
    if pitch and X > pitch - min_gap + 1e-9:
        X = _down(pitch - min_gap, 0.01)
        warnings.append(f"pad width limited to pitch - min_pad_gap = {X:.2f} mm; side fillet goal not met")
    if G <= 0:
        raise ValidationFailed("computed inner gap Gmin <= 0: check L/T dimensions")
    pad_len = round((Z - G) / 2, 4)
    C = round((Z + G) / 2, 4)
    if kind in ("gullwing2", "nolead2", "gullwing4", "nolead4") and pins % (2 if kind.endswith("2") else 4):
        raise ValidationFailed("pin count not divisible by number of sides")
    pads = []
    if kind == "chip":
        pads = [{"name": "1", "x": -C / 2, "y": 0.0, "w": pad_len, "h": X},
                {"name": "2", "x": C / 2, "y": 0.0, "w": pad_len, "h": X}]
    else:
        sides = 2 if kind.endswith("2") else 4
        per = pins // sides
        top = (per - 1) * pitch / 2
        n = 1
        C2 = C
        if sides == 4:
            L2 = _rng(spec, "L2") if "L2" in spec else L
            eq2 = land_equations(L2, T, W, gl["toe"], gl["heel"], gl["side"], F, P)
            Z2, G2 = _up(eq2["Zmax"], rnd), _down(eq2["Gmin"], rnd)
            C2 = round((Z2 + G2) / 2, 4)
        for i in range(per):  # left side, top to bottom
            pads.append({"name": str(n), "x": -C / 2, "y": top - i * pitch, "w": pad_len, "h": X}); n += 1
        if sides == 4:
            for i in range(per):  # bottom side, left to right
                pads.append({"name": str(n), "x": -top + i * pitch, "y": -C2 / 2, "w": X, "h": pad_len}); n += 1
        for i in range(per):  # right side, bottom to top
            pads.append({"name": str(n), "x": C / 2, "y": -top + i * pitch, "w": pad_len, "h": X}); n += 1
        if sides == 4:
            for i in range(per):  # top side, right to left
                pads.append({"name": str(n), "x": top - i * pitch, "y": C2 / 2, "w": X, "h": pad_len}); n += 1
    tp = spec.get("thermal_pad")
    if tp:
        pads.append({"name": str(pins + 1), "x": 0.0, "y": 0.0, "w": float(tp["w"]), "h": float(tp["h"]), "thermal": True})
    # courtyard: max of body and lands, plus excess, rounded up
    xs = [abs(p["x"]) + p["w"] / 2 for p in pads] + [bl[1] / 2 if kind == "chip" else bw[1] / 2]
    ys = [abs(p["y"]) + p["h"] / 2 for p in pads] + [bw[1] / 2 if kind == "chip" else bl[1] / 2]
    cr = float(g["rounding"]["courtyard"])
    cx, cy = _up(max(xs) + gl["courtyard"], cr), _up(max(ys) + gl["courtyard"], cr)
    # achieved solder joints (worst case) for review
    achieved = {"toe_min": round((Z - L[1]) / 2, 3), "heel_min": round((eq["Smin"] - G) / 2, 3),
                "side_min": round((X - W[1]) / 2, 3)}
    dims = {"body_l_nom": sum(bl) / 2, "body_w_nom": sum(bw) / 2, "height_max": H[1], "pins_total": len(pads),
            "pitch": pitch or 0, "span_nom": sum(L) / 2,
            "span2_nom": sum(_rng(spec, "L2")) / 2 if "L2" in spec else sum(L) / 2}
    name = ipc_name(kind, spec["prefix"], density, dims)
    return {"ipc_name": name, "family": fam, "goal_status": gl["status"], "density": density,
            "equations": {k: round(v, 4) for k, v in eq.items()}, "rounded": {"Z": Z, "G": G, "X": X},
            "pad_length": pad_len, "row_pitch": C, "pads": pads, "courtyard": {"x1": -cx, "y1": -cy, "x2": cx, "y2": cy},
            "body": {"length": bl[1], "width": bw[1], "height": H[1]}, "achieved_joints": achieved,
            "goals_used": {k: gl[k] for k in ("toe", "heel", "side", "courtyard")}, "F": F, "P": P,
            "warnings": warnings}
