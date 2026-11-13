# SPDX-License-Identifier: Apache-2.0
"""Line-model and stackup-plan jobs: from a job spec to normalised results.

The simulation engines call these; so do the MCP tools. A spec is plain
JSON with lengths in mils (as Altium shows them) and frequencies in GHz.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..emc.touchstone import write_touchstone
from . import materials as mat
from . import mtl, planner
from . import rlgc as _rlgc
from .section import MIL, Section, microstrip, stripline

_PLOT_LOCK = threading.Lock()

#: Spec keys of a line model: (type, required, help). Shared by the job
#: kind and the MCP tool's documentation.
LINE_SPEC: dict[str, tuple[str, bool, str]] = {
    "structure": ("text", False, "microstrip (outer layer) or stripline (inner); "
                  "taken from the stackup when a layer is given"),
    "layer": ("text", False, "a copper layer of the stackup input (name or label)"),
    "width_mils": ("number", True, "trace width (bottom face)"),
    "traces": ("number", False, "number of coupled traces (default 1; 2 for a pair)"),
    "gap_mils": ("number", False, "edge-to-edge gap between coupled traces"),
    "copper_mils": ("number", False, "trace copper thickness (default 1.4 = 1 oz)"),
    "h_mils": ("number", False, "microstrip: dielectric height to the plane"),
    "h_below_mils": ("number", False, "stripline: trace bottom to the plane below"),
    "h_above_mils": ("number", False, "stripline: trace top to the plane above"),
    "laminate": ("text", False, "preset: " + ", ".join(mat.LAMINATES)),
    "er": ("number", False, "dielectric constant at f_spec_ghz (default 4.3)"),
    "tand": ("number", False, "loss tangent at f_spec_ghz (default 0.02)"),
    "f_spec_ghz": ("number", False, "frequency of the data-sheet Er and tan d (default 1)"),
    "er_above": ("number", False, "stripline: Er of the dielectric above the traces"),
    "tand_above": ("number", False, "stripline: tan d of the dielectric above"),
    "mask_mils": ("number", False, "microstrip: solder mask thickness (default 0)"),
    "mask_er": ("number", False, "solder mask Er (default 3.5)"),
    "mask_tand": ("number", False, "solder mask tan d (default 0.025)"),
    "etch_mils": ("number", False, "top face narrower than the bottom by this (default 0)"),
    "foil": ("text", False, "copper roughness preset: " + ", ".join(mat.FOILS)),
    "plane_mils": ("number", False, "reference plane copper thickness (default 1.4)"),
    "length_mm": ("number", False, "line length for the S-parameters (default 100)"),
    "f_min_ghz": ("number", False, "bottom of the band (default 0.01)"),
    "f_max_ghz": ("number", False, "top of the band (default 20)"),
    "points": ("number", False, "frequency points (default 201)"),
    "z_ref": ("number", False, "port reference impedance (default 50)"),
    "f_report_ghz": ("number", False, "frequency the headline figures are quoted at (default 1)"),
}

PLAN_SPEC: dict[str, tuple[str, bool, str]] = {
    "se_ohms": ("number", False, "single-ended target (default 50; 0 for none)"),
    "diff_ohms": ("list", False, "differential targets (default [100])"),
    "layers": ("list", False, "only these layers"),
    "tand": ("number", False, "loss tangent of every dielectric (default 0.02)"),
    "f_design_ghz": ("number", False, "frequency the impedances are solved at (default 1)"),
    "mask_mils": ("number", False, "solder mask on outer layers (default 0.6; 0 for none)"),
    "mask_er": ("number", False, "solder mask Er (default 3.5)"),
    "etch_mils": ("number", False, "etch narrowing of the top face (default 0)"),
    "foil": ("text", False, "copper roughness preset for the loss figures"),
    "min_width_mils": ("number", False, "narrowest trace considered (default 2)"),
    "max_width_mils": ("number", False, "widest trace considered (default 60)"),
    "diff_gap_mils": ("number", False, "fixed pair gap; default: tightest ratio that works"),
    "diff_gap_ratio": ("number", False, "fixed gap / width"),
    "loss_ghz": ("list", False, "frequencies for loss per inch (slower)"),
    "altium_profiles": ("object", False, "a pcb_get_impedance_profiles reply to compare with"),
}


def _num(spec: dict, key: str, default: float) -> float:
    v = spec.get(key)
    return float(default if v is None else v)


def section_from_spec(spec: dict[str, Any], stackup: Optional[dict] = None) -> Section:
    """The cross-section a line-model spec describes."""
    n = int(_num(spec, "traces", 1))
    if n < 1 or n > 8:
        raise ValueError("traces is 1 to 8")
    w = _num(spec, "width_mils", 0) * MIL
    if w <= 0:
        raise ValueError("width_mils must be more than 0")
    gap = _num(spec, "gap_mils", 0) * MIL
    if n > 1 and gap <= 0:
        raise ValueError("coupled traces need gap_mils")
    foil = mat.copper(spec.get("foil") or "smooth")
    etch = _num(spec, "etch_mils", 0) * MIL
    plane = _num(spec, "plane_mils", 1.4) * MIL
    if stackup is not None or spec.get("layer"):
        if stackup is None:
            raise ValueError("a layer needs the stackup input")
        if not spec.get("layer"):
            raise ValueError("with a stackup, name the layer")
        from ..emc.stack import stack_from_stackup
        stack = stack_from_stackup(stackup)
        opt = planner.Options(tand=_num(spec, "tand", planner.DEFAULT_TAND),
                              f_spec=_num(spec, "f_spec_ghz", 1) * 1e9,
                              mask_mils=_num(spec, "mask_mils", 0.6),
                              mask_er=_num(spec, "mask_er", 3.5),
                              mask_tand=_num(spec, "mask_tand", 0.025),
                              etch_mils=_num(spec, "etch_mils", 0), copper=foil,
                              plane_mils=_num(spec, "plane_mils", 1.4))
        name = str(spec["layer"])
        info = next((i for i in planner.layer_sections(stack, opt)
                     if name in (i["layer"], i["label"])), None)
        if info is None:
            raise ValueError(f"no signal layer {name!r} in the stackup")
        if spec.get("copper_mils") is not None:
            info["copper_mils"] = float(spec["copper_mils"])
        return planner.build(info, opt, w, n, gap)
    t = _num(spec, "copper_mils", 1.4) * MIL
    lam = mat.laminate(spec.get("laminate") or "", spec.get("er"), spec.get("tand"),
                       spec["f_spec_ghz"] * 1e9 if spec.get("f_spec_ghz") else None)
    structure = str(spec.get("structure") or "").lower()
    if structure == "microstrip":
        h = _num(spec, "h_mils", 0) * MIL
        if h <= 0:
            raise ValueError("a microstrip needs h_mils")
        mm = _num(spec, "mask_mils", 0)
        mask = (mm * MIL, mat.Laminate(_num(spec, "mask_er", 3.5), _num(spec, "mask_tand", 0.025),
                                       lam.f_spec)) if mm > 0 else None
        return microstrip(w, h, t, lam, n=n, s=gap, etch=etch, mask=mask, copper=foil,
                          plane_thickness=plane)
    if structure == "stripline":
        h1 = _num(spec, "h_below_mils", 0) * MIL
        h2 = _num(spec, "h_above_mils", 0) * MIL
        if h1 <= 0 or h2 <= 0:
            raise ValueError("a stripline needs h_below_mils and h_above_mils")
        above = None
        if spec.get("er_above") is not None or spec.get("tand_above") is not None:
            above = mat.Laminate(_num(spec, "er_above", lam.er), _num(spec, "tand_above", lam.tand),
                                 lam.f_spec)
        return stripline(w, h1, h2 + t, t, lam, n=n, s=gap, etch=etch, laminate_above=above,
                         copper=foil, plane_thickness=plane)
    raise ValueError("structure is microstrip or stripline (or give a stackup and a layer)")


def band(spec: dict[str, Any]) -> np.ndarray:
    f0 = _num(spec, "f_min_ghz", 0.01) * 1e9
    f1 = _num(spec, "f_max_ghz", 20) * 1e9
    n = int(_num(spec, "points", 201))
    if f0 <= 0 or f1 <= f0:
        raise ValueError("need 0 < f_min_ghz < f_max_ghz")
    if not 2 <= n <= 4001:
        raise ValueError("points is 2 to 4001")
    return np.linspace(f0, f1, n)


def _metric(value, unit="", label=""):
    out = {"value": value}
    if unit:
        out["unit"] = unit
    if label:
        out["label"] = label
    return out


def _plot(path: Path, f, curves: list[tuple[str, np.ndarray]], ylabel: str, title: str) -> bool:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception:  # noqa: BLE001 - optional extra
        return False
    with _PLOT_LOCK:
        fig = Figure(figsize=(8, 4.5), dpi=120)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        for label, y in curves:
            ax.plot(np.asarray(f) / 1e9, y, label=label, lw=1.6)
        ax.set_xlabel("Frequency (GHz)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(path)
    return True


def run_line_model(spec: dict[str, Any], folder: Path, stackup: Optional[dict] = None,
                   progress=None) -> dict[str, Any]:
    """Solve a line model; files go into ``folder``. Normalised results."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    sec = section_from_spec(spec, stackup)
    f = band(spec)
    f_rep = _num(spec, "f_report_ghz", 1) * 1e9
    grid = np.unique(np.concatenate([f, [f_rep]]))
    say("solving the cross-section (field and eddy current)", 0.2)
    r = _rlgc.compute(sec, grid)
    k_rep = int(np.argmin(np.abs(grid - f_rep)))
    rep = _rlgc.figures(r, k_rep)
    top = _rlgc.figures(r, len(grid) - 1)
    length = _num(spec, "length_mm", 100) / 1000.0
    if length <= 0:
        raise ValueError("length_mm must be more than 0")
    z_ref = _num(spec, "z_ref", 50)
    say("S-parameters", 0.8)
    keep = np.isin(grid, f)
    sub = _rlgc.RLGC(grid[keep], r.R[keep], r.L[keep], r.G[keep], r.C[keep], r.C0, r.info)
    s = mtl.s_params(sub, length, z_ref)
    nports = s.shape[1]
    ts = write_touchstone(folder / "line", list(sub.f), s.tolist(), z0=z_ref)
    (folder / "rlgc.json").write_text(json.dumps(_rlgc.to_dict(r), indent=1), encoding="utf-8")
    (folder / "section.json").write_text(json.dumps(sec.to_dict(), indent=1), encoding="utf-8")
    m: dict[str, Any] = {}
    files = {"touchstone": Path(ts).name, "rlgc": "rlgc.json", "section": "section.json"}
    at = f"at {f_rep / 1e9:g} GHz"
    if sec.n == 1:
        m["z0_ohms"] = _metric(round(rep["z0_qs"], 4), "ohm",
                               f"Characteristic impedance {at} (lossless, as fabs quote it)")
        m["z0_lossy_ohms"] = _metric(round(rep["z0"], 4), "ohm",
                                     f"Characteristic impedance {at} with the copper's "
                                     "internal inductance and loss")
        m["er_eff"] = _metric(round(rep["er_eff_qs"], 5), "", f"Effective Er {at} (C / C0)")
        m["delay_ps_per_in"] = _metric(round(rep["delay_ps_per_in"], 4), "ps/in", f"Delay {at}")
        m["loss_db_per_in"] = _metric(round(rep["loss_db_per_in"], 5), "dB/in", f"Loss {at}")
        m["loss_db_per_in_at_f_max"] = _metric(round(top["loss_db_per_in"], 5), "dB/in",
                                               f"Loss at {grid[-1] / 1e9:g} GHz")
        s21 = mtl.db(s[:, 1, 0])
        curves = [("S21", s21), ("S11", mtl.db(s[:, 0, 0]))]
    elif sec.n == 2:
        m["z_diff_ohms"] = _metric(round(rep["z_diff_qs"], 4), "ohm",
                                   f"Differential impedance {at} (lossless, as fabs quote it)")
        m["z_diff_lossy_ohms"] = _metric(round(rep["z_diff"], 4), "ohm",
                                         f"Differential impedance {at} with the copper's "
                                         "internal inductance and loss")
        m["z_common_ohms"] = _metric(round(rep["z_common_qs"], 4), "ohm", f"Common-mode impedance {at}")
        m["z_odd_ohms"] = _metric(round(rep["z_odd_qs"], 4), "ohm", f"Odd-mode impedance {at}")
        m["z_even_ohms"] = _metric(round(rep["z_even_qs"], 4), "ohm", f"Even-mode impedance {at}")
        m["delay_odd_ps_per_in"] = _metric(round(rep["delay_odd_ps_per_in"], 4), "ps/in",
                                           f"Odd-mode delay {at}")
        m["delay_even_ps_per_in"] = _metric(round(rep["delay_even_ps_per_in"], 4), "ps/in",
                                            f"Even-mode delay {at}")
        m["loss_diff_db_per_in"] = _metric(round(rep["loss_diff_db_per_in"], 5), "dB/in",
                                           f"Differential loss {at}")
        m["loss_diff_db_per_in_at_f_max"] = _metric(round(top["loss_diff_db_per_in"], 5), "dB/in",
                                                    f"Differential loss at {grid[-1] / 1e9:g} GHz")
        mm = mtl.mixed_mode(s)
        s21 = mtl.db(mm[:, 1, 0])
        next_ = mtl.db(s[:, 1, 0])
        fext = mtl.db(s[:, 3, 0])
        m["next_db_max"] = _metric(round(float(np.max(next_)), 3), "dB", "Worst near-end crosstalk")
        m["fext_db_max"] = _metric(round(float(np.max(fext)), 3), "dB", "Worst far-end crosstalk")
        curves = [("SDD21", s21), ("SDD11", mtl.db(mm[:, 0, 0])), ("NEXT", next_), ("FEXT", fext)]
    else:
        curves = [("S21 line 1", mtl.db(s[:, sec.n, 0]))]
        s21 = curves[0][1]
        m["mode_delay_ps_per_in"] = _metric(rep["mode_delay_ps_per_in"], "ps/in", f"Mode delays {at}")
    m["s21_db_at_f_max"] = _metric(round(float(s21[-1]), 4), "dB",
                                   f"{'SDD21' if sec.n == 2 else 'S21'} of {length * 1000:g} mm "
                                   f"at {sub.f[-1] / 1e9:g} GHz")
    m["r_dc_ohm_per_m"] = _metric(round(r.info["r_dc_ohm_per_m"][0], 5), "ohm/m",
                                  "DC resistance of trace 1 with its return")
    title = f"{sec.kind or 'line'}, {length * 1000:g} mm"
    if _plot(folder / "sparams.png", sub.f, curves, "dB", title):
        files["plot"] = "sparams.png"
    lerr = r.info["eddy_l_inf_vs_electrostatic"]
    verdicts = [
        {"check": "passivity", "pass": bool(np.max(np.linalg.svd(s, compute_uv=False)) <= 1 + 1e-6),
         "detail": "largest singular value of S over the band"},
        {"check": "reciprocity", "pass": bool(np.max(np.abs(s - np.transpose(s, (0, 2, 1)))) < 1e-9),
         "detail": "S = S^T"},
        {"check": "solvers agree", "pass": bool(lerr < 0.02),
         "detail": f"high-frequency inductance of the eddy-current and electrostatic solves "
                   f"differ by {100 * lerr:.2f} % (2 % allowed)"},
    ]
    notes = [
        "R(f), L(f): 2D eddy-current solve with skin and proximity effect, the planes included; "
        f"sqrt(f) asymptote above {r.info['eddy_f_top_hz'] / 1e9:.3g} GHz",
        "C(f), G(f): 2D field solve with Djordjevic-Sarkar dielectrics, extrapolated to zero cell size",
        f"S-parameters: exact modal solution of the coupled lines, {nports} ports, "
        f"near ends 1..{sec.n}, far ends {sec.n + 1}..{2 * sec.n}",
    ]
    if sec.copper.roughness != "none":
        notes.append(f"copper roughness: {sec.copper.roughness} model")
    return {"metrics": m, "verdicts": verdicts, "notes": notes, "files": files,
            "figures": {"report": {k: v for k, v in rep.items() if k != "zc_matrix"},
                        "f_max": {k: v for k, v in top.items() if k != "zc_matrix"}}}


def plan_options(spec: dict[str, Any]) -> planner.Options:
    return planner.Options(
        f_design=_num(spec, "f_design_ghz", 1) * 1e9,
        tand=_num(spec, "tand", planner.DEFAULT_TAND),
        mask_mils=_num(spec, "mask_mils", 0.6), mask_er=_num(spec, "mask_er", 3.5),
        etch_mils=_num(spec, "etch_mils", 0), copper=mat.copper(spec.get("foil") or "smooth"),
        min_width_mils=_num(spec, "min_width_mils", 2), max_width_mils=_num(spec, "max_width_mils", 60),
        diff_gap_mils=spec.get("diff_gap_mils"), diff_gap_ratio=spec.get("diff_gap_ratio"))


def run_stackup_plan(spec: dict[str, Any], stackup: dict, folder: Path, progress=None) -> dict[str, Any]:
    from ..emc.stack import stack_from_stackup
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    stack = stack_from_stackup(stackup)
    opt = plan_options(spec)
    se = spec.get("se_ohms", 50)
    diff = spec.get("diff_ohms", [100])
    say("solving widths per layer", 0.2)
    res = planner.plan(stack, float(se) if se else None, [float(x) for x in diff or []], opt,
                       [str(x) for x in spec.get("layers") or []],
                       [float(x) for x in spec.get("loss_ghz") or []])
    res["altium_steps"] = planner.altium_steps(res)
    if spec.get("altium_profiles"):
        say("comparing with Altium's profiles", 0.8)
        res["altium_comparison"] = planner.compare_with_altium(stack, spec["altium_profiles"], opt)
    (folder / "plan.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    metrics = {}
    for row in res["layers"]:
        lay = row["layer"]
        if row.get("single", {}).get("ok"):
            metrics[f"{lay}.se_width_mils"] = _metric(row["single"]["width_mils"], "mil",
                                                      f"{row['label']}: width for {se:g} ohm")
        for p in row.get("pairs", []):
            if p.get("ok"):
                metrics[f"{lay}.diff{p['target_ohms']:g}_width_mils"] = _metric(
                    p["width_mils"], "mil", f"{row['label']}: pair width for {p['target_ohms']:g} ohm")
                metrics[f"{lay}.diff{p['target_ohms']:g}_gap_mils"] = _metric(
                    p["gap_mils"], "mil", f"{row['label']}: pair gap for {p['target_ohms']:g} ohm")
    verdicts = []
    for row in res["layers"]:
        for p in ([row["single"]] if "single" in row else []) + row.get("pairs", []):
            verdicts.append({"check": f"{row['label']} {p['target_ohms']:g} ohm "
                                      f"{'pair' if p in row.get('pairs', []) else 'single'}",
                             "pass": bool(p.get("ok")), "detail": p.get("reason", "")})
    for c in res.get("altium_comparison", []):
        if "difference_pct" in c:
            verdicts.append({"check": f"Altium profile {c['profile']} on {c['layer']}",
                             "pass": abs(c["difference_pct"]) <= 3.0,
                             "detail": f"our {c['our_ohms_at_altium_width']} ohm vs Altium's "
                                       f"{c['altium_ohms']} at {c['altium_width_mils']} mil"})
    return {"metrics": metrics, "verdicts": verdicts, "files": {"plan": "plan.json"},
            "notes": list(res.get("stack_notes", []))
            + [f"{row['label']}: {row['note']}" for row in res.get("layers", []) if row.get("note")],
            "plan": res}
