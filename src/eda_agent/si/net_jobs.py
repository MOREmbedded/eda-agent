# SPDX-License-Identifier: Apache-2.0
"""Net-channel jobs: a routed net or pair of the board, end to end.

The job takes a saved board (LayoutBoard JSON) and its stackup, finds the
net's path, models it (:mod:`.net_model`) and writes the channel's
Touchstone file, the path it found, the TDR and a plot. Lengths in the
spec are mils, frequencies GHz, times ps.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..emc.stack import stack_from_stackup
from ..emc.touchstone import write_touchstone
from ..layout.model import LayoutBoard
from . import channel as ch
from . import extract, net_model, planner
from .jobs import _metric, _num, plan_options

_PLOT_LOCK = threading.Lock()

NET_SPEC: dict[str, tuple[str, bool, str]] = {
    "net": ("text", False, "the net to model (give net or pair)"),
    "pair": ("text", False, "a differential pair of the board, or one of its nets"),
    "from_pin": ("text", False, "start pin, DESIGNATOR.PIN (default: the two pins farthest apart)"),
    "to_pin": ("text", False, "end pin"),
    "data_rate_gbps": ("number", False, "data rate; the Nyquist frequency is half of it "
                       "(default: a quarter of f_max)"),
    "f_max_ghz": ("number", False, "top of the band (default 20)"),
    "points": ("number", False, "frequency points, evenly spaced (default 400)"),
    "rise_ps": ("number", False, "10-90 % edge of the TDR and step responses (default 35)"),
    "z_ref": ("number", False, "port impedance per line (default 50; a pair's "
                               "differential reference is twice this)"),
    "z_target": ("number", False, "impedance the TDR is judged against (default z_ref, "
                                  "or 2 z_ref for a pair)"),
    "z_tol_pct": ("number", False, "allowed TDR deviation from z_target (default 10)"),
    "il_max_db": ("number", False, "allowed insertion loss at Nyquist (no check if absent)"),
    "rl_min_db": ("number", False, "required return loss up to Nyquist (no check if absent)"),
    "skew_max_ps": ("number", False, "allowed intra-pair skew (default 5 % of a unit interval "
                                     "when data_rate_gbps is given)"),
    "tand": ("number", False, "loss tangent of every dielectric (default 0.02)"),
    "foil": ("text", False, "copper roughness preset"),
    "mask_mils": ("number", False, "solder mask on outer layers (default 0.6)"),
    "etch_mils": ("number", False, "etch narrowing of the top face (default 0)"),
    "rules": ("bool", False, "also work out Altium design rules for the net (default true)"),
}


def _plot(path: Path, f, curves, t, tdr, z_target, title: str) -> bool:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception:  # noqa: BLE001 - optional extra
        return False
    with _PLOT_LOCK:
        fig = Figure(figsize=(9, 7), dpi=110)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(211)
        for label, y in curves:
            ax.plot(np.asarray(f) / 1e9, y, label=label, lw=1.5)
        ax.set_xlabel("Frequency (GHz)")
        ax.set_ylabel("dB")
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend()
        bx = fig.add_subplot(212)
        bx.plot(np.asarray(t) * 1e12, tdr, lw=1.5, label="TDR")
        bx.axhline(z_target, ls="--", lw=1, color="0.5", label=f"{z_target:g} ohm")
        bx.set_xlabel("Time (ps)")
        bx.set_ylabel("Impedance (ohm)")
        bx.grid(True, alpha=0.3)
        bx.legend()
        fig.tight_layout()
        fig.savefig(path)
    return True


def run_net_channel(spec: dict[str, Any], board_data: dict, stackup: Optional[dict],
                    folder: Path, progress=None) -> dict[str, Any]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *_: None)
    board = LayoutBoard.from_dict(board_data)
    if not stackup:
        raise ValueError("a net channel needs the board's stackup")
    stack = stack_from_stackup(stackup, board.layers)
    opt = plan_options(spec)
    f_max = _num(spec, "f_max_ghz", 20) * 1e9
    points = int(_num(spec, "points", 400))
    if not 16 <= points <= 4000:
        raise ValueError("points is 16 to 4000")
    f = ch.uniform_band(f_max, points)
    rate = spec.get("data_rate_gbps")
    f_nyq = float(rate) * 1e9 / 2 if rate else f_max / 4
    rise = _num(spec, "rise_ps", 35) * 1e-12
    z_ref = _num(spec, "z_ref", 50)
    say("finding the routed path", 0.05)
    if spec.get("pair"):
        pos, neg = extract.find_pair(board, str(spec["pair"]))
        name = next((d.name for d in board.diff_pairs if d.positive == pos), str(spec["pair"]))
        path = extract.pair_path(board, name, pos, neg)
        say("solving the cross-sections and the channel", 0.15)
        model = net_model.pair(board, stack, path, f, opt, z_ref=z_ref)
        label = f"pair {name}"
    elif spec.get("net"):
        path = extract.net_path(board, str(spec["net"]), str(spec.get("from_pin") or ""),
                                str(spec.get("to_pin") or ""))
        say("solving the cross-sections and the channel", 0.15)
        model = net_model.single(board, stack, path, f, opt, z_ref=z_ref)
        label = f"net {path.net}"
    else:
        raise ValueError("give a net or a pair")
    say("figures, TDR and files", 0.85)
    figs = net_model.figures(model, f_nyq, rise)
    pair = model.n == 2
    z_target = _num(spec, "z_target", 2 * z_ref if pair else z_ref)
    ts = write_touchstone(folder / "channel", list(f), model.s.tolist(), z0=z_ref)
    (folder / "path.json").write_text(json.dumps(path.to_dict(), indent=1), encoding="utf-8")
    (folder / "sections.json").write_text(json.dumps(
        {"blocks": model.blocks, "cross_sections": model.sections}, indent=1), encoding="utf-8")
    if pair:
        mm = model.sdd
        s11, s21 = mm[:, 0, 0], mm[:, 1, 0]
        curves = [("SDD21", ch.db(s21)), ("SDD11", ch.db(s11)), ("SCD21", ch.db(mm[:, 3, 0]))]
    else:
        s11, s21 = model.s[:, 0, 0], model.s[:, 1, 0]
        curves = [("S21", ch.db(s21)), ("S11", ch.db(s11))]
    t, z = net_model.tdr(f, s11, 2 * z_ref if pair else z_ref, rise)
    np.savetxt(folder / "tdr.csv", np.column_stack([t * 1e12, z]), delimiter=",",
               header="time_ps,impedance_ohm", comments="")
    files = {"touchstone": Path(ts).name, "path": "path.json", "sections": "sections.json",
             "tdr": "tdr.csv"}
    if _plot(folder / "channel.png", f, curves, t, z, z_target, label):
        files["plot"] = "channel.png"

    nyq = f"at {figs['f_nyquist_hz'] / 1e9:g} GHz"
    m: dict[str, Any] = {
        "insertion_loss_db": _metric(round(figs["insertion_loss_db_at_nyquist"], 3), "dB",
                                     f"Insertion loss {nyq} (Nyquist)"),
        "return_loss_db": _metric(round(figs["return_loss_db_worst"], 3), "dB",
                                  "Worst return loss up to Nyquist"),
        "length_mils": _metric(round((path.p.length if pair else path.length), 2), "mil",
                               "Routed length" + (" (positive side)" if pair else "")),
        "vias": _metric(path.p.vias if pair else path.vias, "", "Vias on the path"),
    }
    if figs.get("delay_ps") is not None:
        m["delay_ps"] = _metric(round(figs["delay_ps"], 2), "ps", "Delay (50 % of the step)")
    if "tdr_min_ohms" in figs:
        m["tdr_min_ohms"] = _metric(round(figs["tdr_min_ohms"], 2), "ohm", "Lowest TDR impedance")
        m["tdr_max_ohms"] = _metric(round(figs["tdr_max_ohms"], 2), "ohm", "Highest TDR impedance")
    if pair:
        m["mode_conversion_db"] = _metric(round(figs["scd21_db_at_nyquist"], 3), "dB",
                                          f"SCD21 {nyq}")
        if "skew_ps" in figs:
            m["skew_ps"] = _metric(round(figs["skew_ps"], 3), "ps", "Intra-pair skew (P - N)")
        m["length_n_mils"] = _metric(round(path.n.length, 2), "mil", "Routed length (negative side)")
    else:
        m["stubs"] = _metric(len(path.stubs), "", "Stubs off the path")

    verdicts = [
        {"check": "passivity", "pass": figs["passivity_max_sv"] <= 1 + 1e-6,
         "detail": f"largest singular value of S {figs['passivity_max_sv']:.6f}"},
        {"check": "reciprocity", "pass": figs["reciprocity_err"] < 1e-9, "detail": "S = S^T"},
    ]
    tol = _num(spec, "z_tol_pct", 10) / 100
    if "tdr_min_ohms" in figs:
        lo, hi = figs["tdr_min_ohms"], figs["tdr_max_ohms"]
        verdicts.append({"check": "impedance", "pass": bool(lo >= z_target * (1 - tol)
                                                             and hi <= z_target * (1 + tol)),
                         "detail": f"TDR {lo:.1f} to {hi:.1f} ohm against {z_target:g} ohm "
                                   f"+/- {tol * 100:g} % at {rise * 1e12:g} ps"})
    if spec.get("il_max_db") is not None:
        lim = float(spec["il_max_db"])
        verdicts.append({"check": "insertion loss", "pass": figs["insertion_loss_db_at_nyquist"] <= lim,
                         "detail": f"{figs['insertion_loss_db_at_nyquist']:.2f} dB {nyq}, "
                                   f"{lim:g} dB allowed"})
    if spec.get("rl_min_db") is not None:
        lim = float(spec["rl_min_db"])
        verdicts.append({"check": "return loss", "pass": figs["return_loss_db_worst"] >= lim,
                         "detail": f"{figs['return_loss_db_worst']:.2f} dB worst, {lim:g} dB needed"})
    if pair and "skew_ps" in figs:
        lim = spec.get("skew_max_ps")
        if lim is None and rate:
            lim = 0.05 * 1e3 / float(rate)
        if lim is not None:
            verdicts.append({"check": "skew", "pass": abs(figs["skew_ps"]) <= float(lim),
                             "detail": f"{figs['skew_ps']:.2f} ps, {float(lim):.2f} ps allowed"})
    notes = [
        f"path {path.p.start if pair else path.start} to {path.p.end if pair else path.end}; "
        "lines from the 2D field and eddy-current solvers of each cross-section, vias from "
        "the coaxial-barrel model with stubs, pad capacitance and the return-via loop",
        f"TDR and delay from a Gaussian step of {rise * 1e12:g} ps (10-90 %)",
    ] + list(model.notes)
    for name, figs_ in model.sections.items():
        notes.append(f"cross-section {name}: " + ", ".join(
            f"{k} {v:g}" for k, v in figs_.items() if isinstance(v, (int, float))))
    out = {"metrics": m, "verdicts": verdicts, "notes": notes, "files": files,
           "figures": {"path": path.to_dict(), "blocks": model.blocks}}
    if spec.get("rules", True):
        say("design rules for Altium", 0.95)
        try:
            out["altium_steps"] = rule_steps(spec, figs, path, stack, opt, z_target)
        except (ValueError, KeyError) as exc:
            out["notes"].append(f"no design rules worked out: {exc}")
    return out


def _main_layer(path) -> tuple[str, float]:
    """The layer most of the path runs on, and its total length there."""
    by: dict[str, float] = {}
    if isinstance(path, extract.PairPath):
        for z in path.zones:
            for layer, _w, _g, L in z["coupled"]:
                by[layer] = by.get(layer, 0.0) + L
        sections = path.p.sections
    else:
        sections = path.sections
    if not by:
        for s in sections:
            if s.kind == "line":
                by[s.layer] = by.get(s.layer, 0.0) + s.length
    layer = max(by, key=by.get)
    return layer, by[layer]


def rule_steps(spec: dict[str, Any], figs: dict[str, Any], path, stack, opt,
               z_target: float) -> list[dict[str, Any]]:
    """pcb_create_design_rule calls that hold the net to its targets.

    The width (and for a pair the gap) come from the field solver for
    ``z_target`` on the layer the net mostly runs on, with +/- 10 % on the
    width; the length limit from the loss budget ``il_max_db`` at the
    measured loss per mil; the uncoupled-length limit from the skew
    budget. Nothing is written here; the tool applies them when asked."""
    pair = isinstance(path, extract.PairPath)
    name = path.name if pair else path.net
    scope = f"InDifferentialPair('{name}')" if pair else f"InNet('{name}')"
    layer, _ = _main_layer(path)
    info = {i["layer"]: i for i in planner.layer_sections(stack, opt)}.get(layer)
    steps: list[dict[str, Any]] = []
    if info is not None:
        sol = planner.solve_width(info, opt, float(z_target), pair)
        if sol.get("ok"):
            w = sol["width_mils"]
            others = sorted({x.layer for x in (path.p.sections if pair else path.sections)
                             if x.kind == "line" and x.layer != layer})
            why = f"{z_target:g} ohm on {layer}: {w:g} mil from the field solver"
            if others:
                why += (f"; a net-scoped width rule covers every layer, so {', '.join(others)} "
                        "may need their own width (si_stackup_plan gives it per layer)")
            steps.append({"tool": "pcb_create_design_rule", "why": why,
                          "args": {"name": f"SI width {name}", "rule_type": "width",
                                   "value": round(w * 0.9, 2), "max_value": round(w * 1.1, 2),
                                   "favored_value": round(w, 2), "scope": scope}})
            if pair:
                gap = sol["gap_mils"]
                args = {"name": f"SI pair {name}", "rule_type": "differential_pairs",
                        "value": round(gap * 0.9, 2), "max_value": round(gap * 1.1, 2),
                        "favored_value": round(gap, 2), "scope": scope}
                lim = spec.get("skew_max_ps")
                rate = spec.get("data_rate_gbps")
                if lim is None and rate:
                    lim = 0.05 * 1e3 / float(rate)
                if lim is not None and figs.get("delay_ps") and path.p.length > 0:
                    ps_per_mil = figs["delay_ps"] / path.p.length
                    args["max_uncoupled_length"] = max(int(float(lim) / ps_per_mil), 10)
                steps.append({"tool": "pcb_create_design_rule", "why":
                              f"{z_target:g} ohm differential on {layer}: gap {gap:g} mil",
                              "args": args})
    if spec.get("il_max_db") is not None:
        length = path.p.length if pair else path.length
        il = figs["insertion_loss_db_at_nyquist"]
        if il > 0 and length > 0:
            budget = float(spec["il_max_db"]) / il * length
            steps.append({"tool": "pcb_create_design_rule", "why":
                          f"{float(spec['il_max_db']):g} dB at Nyquist over {il / length * 1000:.3g} "
                          f"dB per 1000 mil as routed (vias included)",
                          "args": {"name": f"SI length {name}", "rule_type": "length",
                                   "value": 0, "max_value": round(budget, 1), "scope": scope}})
    return steps
