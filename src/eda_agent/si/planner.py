# SPDX-License-Identifier: Apache-2.0
"""Stackup planner: trace widths and pair geometry for target impedances.

For every signal layer of a stack it builds the real cross-section (the
copper thickness, the dielectric below and above with their own Er, the
solder mask on outer layers, the nearest copper above and below as the
references) and solves the field for the width that gives the target
single-ended impedance, and the width (with a fixed gap, or a gap in
proportion to the width) that gives the target differential impedance.

The impedances are the field solver's at the design frequency, with the
laminates' Er there from the Djordjevic-Sarkar model. With ``loss`` the
final geometries are also run through the full RLGC model for the loss
per inch at the frequencies asked for.

The result can be checked against Altium's own impedance profiles (the
reply of ``pcb_get_impedance_profiles``): for each profile and layer it
gives our impedance at Altium's width and our width for the profile's
target, a second opinion on the Layer Stack Manager's solver.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from scipy.optimize import brentq

from ..emc.stack import Stack
from ..engineering.toolkit import fdsolver as fd
from . import rlgc as _rlgc
from .materials import Copper, Laminate
from .section import MIL, Section, microstrip, stripline

DEFAULT_TAND = 0.02
RESOLUTION = 30.0
#: Gap-to-width ratios tried, tightest first, when neither a gap nor a
#: ratio is given: the first that reaches the target is used.
RATIOS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


@dataclass
class Options:
    f_design: float = 1e9
    tand: float = DEFAULT_TAND
    f_spec: float = 1e9
    mask_mils: float = 0.6
    mask_er: float = 3.5
    mask_tand: float = 0.025
    etch_mils: float = 0.0
    copper: Copper = Copper()
    min_width_mils: float = 2.0
    max_width_mils: float = 60.0
    diff_gap_mils: Optional[float] = None    # fixed gap; else ratio * width
    diff_gap_ratio: Optional[float] = None   # fixed gap / width; None tries RATIOS
    plane_mils: float = 1.4


def _reference(stack: Stack, i: int, step: int):
    """The copper that layer ``i`` is referenced to on one side: the nearest
    plane, past any signal layers (returned second), or with no plane on
    that side the layer right next to it."""
    j, passed = i + step, []
    while 0 <= j < len(stack.layers):
        if stack.layers[j].kind == "plane":
            return stack.layers[j], passed
        passed.append(stack.layers[j])
        j += step
    return (passed[0] if passed else None), []


def layer_sections(stack: Stack, opt: Options) -> list[dict[str, Any]]:
    """For each signal layer: how its cross-section is built."""
    out = []
    n = len(stack.layers)
    for i, layer in enumerate(stack.layers):
        if layer.kind == "plane":
            continue
        above, over_above = _reference(stack, i, -1)
        below, over_below = _reference(stack, i, 1)
        if above is None and below is None:
            continue
        info: dict[str, Any] = {"layer": layer.name, "label": layer.label or layer.name,
                                "copper_mils": layer.copper_mils}
        label = lambda x: x.label or x.name  # noqa: E731
        lam = lambda a, b: Laminate(stack.er_between(a.name, b.name), opt.tand, opt.f_spec)  # noqa: E731
        if above is None or below is None:
            ref = below if below is not None else above
            info.update(structure="microstrip", reference=ref.name, reference_label=label(ref),
                        h_mils=abs(layer.z_mils - ref.z_mils), laminate=lam(layer, ref))
            if ref.kind != "plane":
                info["note"] = (f"the reference is signal layer {label(ref)}; keep it solid "
                                "copper under these traces or the figures do not hold")
        else:
            info.update(structure="stripline", reference=[above.name, below.name],
                        reference_label=[label(above), label(below)],
                        h_below_mils=layer.z_mils - below.z_mils,
                        h_above_mils=above.z_mils - layer.z_mils,
                        laminate=lam(layer, below), laminate_above=lam(layer, above))
            signal_refs = [label(x) for x in (above, below) if x.kind != "plane"]
            if signal_refs:
                info["note"] = (f"signal layer {' and '.join(signal_refs)} is taken as a "
                                "reference; keep it solid copper under these traces or the "
                                "figures do not hold")
        passed = [label(x) for x in over_above + over_below]
        if passed:
            info["note"] = " ".join(filter(None, [info.get("note"), (
                f"signal layer {' and '.join(passed)} lies between these traces and their "
                "reference plane; copper on it over or under them changes the figures")]))
        for key, other in (("h_mils", None), ("h_below_mils", below), ("h_above_mils", above)):
            if key in info and info[key] <= 0:
                other = other or (below if below is not None else above)
                raise ValueError(
                    f"the stackup has no dielectric thickness between {label(layer)} and "
                    f"{label(other)}; set it in the Layer Stack Manager")
        out.append(info)
    return out


def build(info: dict[str, Any], opt: Options, w: float, n: int = 1, s: float = 0.0) -> Section:
    """The section of ``info`` with traces of width ``w`` (m)."""
    t = info["copper_mils"] * MIL
    etch = opt.etch_mils * MIL
    if info["structure"] == "microstrip":
        mask = ((opt.mask_mils * MIL, Laminate(opt.mask_er, opt.mask_tand, opt.f_spec))
                if opt.mask_mils > 0 else None)
        return microstrip(w, info["h_mils"] * MIL, t, info["laminate"], n=n, s=s, etch=etch,
                          mask=mask, copper=opt.copper, plane_thickness=opt.plane_mils * MIL)
    return stripline(w, info["h_below_mils"] * MIL, t + info["h_above_mils"] * MIL, t,
                     info["laminate"], n=n, s=s, etch=etch, laminate_above=info["laminate_above"],
                     copper=opt.copper, plane_thickness=opt.plane_mils * MIL)


def impedances(sec: Section, f: float, resolution: float = RESOLUTION) -> dict[str, float]:
    """Quasi-static impedances at ``f``: z0 for one trace; z_diff, z_odd,
    z_even, z_common for two."""
    eps = [lam.eps(f).real for lam in sec.laminates()]
    sol = fd.solve(sec.to_fd(eps=eps, tand=[0.0] * len(eps)), resolution)
    C, C0 = sol.C, sol.C0
    c = fd.C0
    if sec.n == 1:
        return {"z0": 1 / (c * math.sqrt(C[0, 0] * C0[0, 0])), "er_eff": C[0, 0] / C0[0, 0]}
    codd, c0odd = C[0, 0] - C[0, 1], C0[0, 0] - C0[0, 1]
    cev, c0ev = C[0, 0] + C[0, 1], C0[0, 0] + C0[0, 1]
    zodd = 1 / (c * math.sqrt(codd * c0odd))
    zev = 1 / (c * math.sqrt(cev * c0ev))
    return {"z_odd": zodd, "z_even": zev, "z_diff": 2 * zodd, "z_common": zev / 2,
            "er_eff_odd": codd / c0odd, "er_eff_even": cev / c0ev}


def _gap(opt: Options, w: float) -> float:
    return opt.diff_gap_mils * MIL if opt.diff_gap_mils else (opt.diff_gap_ratio or 1.0) * w


def solve_width(info: dict[str, Any], opt: Options, target: float, diff: bool) -> dict[str, Any]:
    """Width (and gap) for ``target`` ohms, single-ended or differential.

    For a pair with neither a fixed gap nor a ratio, the gap/width ratios
    of ``RATIOS`` are tried tightest first."""
    if diff and not opt.diff_gap_mils and opt.diff_gap_ratio is None:
        last: dict[str, Any] = {}
        for ratio in RATIOS:
            last = solve_width(info, Options(**{**opt.__dict__, "diff_gap_ratio": ratio}),
                               target, True)
            if last.get("ok"):
                last["gap_rule"] = f"gap = {ratio:g} x width (the tightest ratio that reaches the target)"
                return last
        last["reason"] = (last.get("reason", "") + f"; also tried gap/width ratios "
                          f"{', '.join(f'{r:g}' for r in RATIOS)}")
        return last
    key = "z_diff" if diff else "z0"

    def z(w: float) -> float:
        sec = build(info, opt, w, 2 if diff else 1, _gap(opt, w) if diff else 0.0)
        return impedances(sec, opt.f_design)[key]

    lo, hi = opt.min_width_mils * MIL, opt.max_width_mils * MIL
    z_lo, z_hi = z(lo), z(hi)
    if not (z_hi <= target <= z_lo):
        reach = "too high" if target > z_lo else "too low"
        return {"ok": False, "target_ohms": target,
                "reason": f"{target} ohm is {reach} for this layer between "
                          f"{opt.min_width_mils} and {opt.max_width_mils} mil "
                          f"({z_hi:.1f} to {z_lo:.1f} ohm)"}
    w = brentq(lambda x: math.log(z(math.exp(x)) / target), math.log(lo), math.log(hi),
               xtol=1e-4)
    w = math.exp(w)
    sec = build(info, opt, w, 2 if diff else 1, _gap(opt, w) if diff else 0.0)
    figs = impedances(sec, opt.f_design, 2 * RESOLUTION)
    out = {"ok": True, "target_ohms": target, "width_mils": round(w / MIL, 3),
           **{k: round(v, 3) for k, v in figs.items()}}
    if diff:
        out["gap_mils"] = round(_gap(opt, w) / MIL, 3)
    return out


def _loss(info, opt, res: dict[str, Any], diff: bool, freqs: Sequence[float]) -> dict[str, Any]:
    w = res["width_mils"] * MIL
    sec = build(info, opt, w, 2 if diff else 1, res.get("gap_mils", 0.0) * MIL)
    r = _rlgc.compute(sec, freqs)
    key = "loss_diff_db_per_in" if diff else "loss_db_per_in"
    return {f"{f / 1e9:g} GHz": round(_rlgc.figures(r, k)[key], 4) for k, f in enumerate(r.f)}


def plan(stack: Stack, se_ohms: Optional[float] = 50.0, diff_ohms: Sequence[float] = (100.0,),
         opt: Optional[Options] = None, layers: Sequence[str] = (),
         loss_ghz: Sequence[float] = ()) -> dict[str, Any]:
    opt = opt or Options()
    rows = []
    for info in layer_sections(stack, opt):
        if layers and info["layer"] not in layers and info["label"] not in layers:
            continue
        row = {k: v for k, v in info.items() if k not in ("laminate", "laminate_above")}
        row["er"] = round(info["laminate"].er, 3)
        if "laminate_above" in info:
            row["er_above"] = round(info["laminate_above"].er, 3)
        if se_ohms:
            row["single"] = solve_width(info, opt, float(se_ohms), False)
            if loss_ghz and row["single"]["ok"]:
                row["single"]["loss_db_per_in"] = _loss(info, opt, row["single"], False,
                                                        [g * 1e9 for g in loss_ghz])
        row["pairs"] = []
        for target in diff_ohms or ():
            p = solve_width(info, opt, float(target), True)
            if loss_ghz and p["ok"]:
                p["loss_db_per_in"] = _loss(info, opt, p, True, [g * 1e9 for g in loss_ghz])
            row["pairs"].append(p)
        rows.append(row)
    return {"ok": True, "layers": rows, "design_frequency_hz": opt.f_design,
            "assumed_stack": stack.assumed, "stack_notes": list(stack.notes)}


def altium_steps(result: dict[str, Any], se_name: str = "SE {ohms}R",
                 diff_name: str = "DIFF {ohms}R") -> list[dict[str, Any]]:
    """The tool calls that write this plan as Altium impedance profiles,
    with each layer's width (and gap) locked to the planned value."""
    steps: list[dict[str, Any]] = []
    profiles: dict[str, list] = {}
    for row in result.get("layers", []):
        entries = []
        if row.get("single", {}).get("ok"):
            entries.append((False, row["single"]))
        entries += [(True, p) for p in row.get("pairs", []) if p.get("ok")]
        for diff, p in entries:
            name = (diff_name if diff else se_name).format(ohms=f"{p['target_ohms']:g}")
            profiles.setdefault(name, [(diff, p["target_ohms"])])
            args = {"profile": name, "layer": row["label"], "enabled": True,
                    "width_locked": True, "width_mils": p["width_mils"]}
            if diff:
                args.update(gap_locked=True, gap_mils=p["gap_mils"])
            profiles[name].append(args)
    for name, items in profiles.items():
        diff, target = items[0]
        steps.append({"tool": "pcb_impedance_profile",
                      "args": {"action": "create", "profile": name, "target_ohms": target,
                               "tolerance": 10.0, "differential": diff}})
        steps += [{"tool": "pcb_impedance_profile_layer", "args": a} for a in items[1:]]
    return steps


def compare_with_altium(stack: Stack, profiles: dict[str, Any], opt: Optional[Options] = None
                        ) -> list[dict[str, Any]]:
    """Our impedance at Altium's widths, and our width for its targets."""
    opt = opt or Options()
    secs = {i["label"]: i for i in layer_sections(stack, opt)}
    secs.update({i["layer"]: i for i in layer_sections(stack, opt)})
    out = []
    for prof in profiles.get("profiles") or []:
        diff = bool(prof.get("differential"))
        target = float(prof.get("target_ohms") or 0)
        for lay in prof.get("layers") or []:
            if not lay.get("enabled", True):
                continue
            info = secs.get(str(lay.get("layer")))
            row: dict[str, Any] = {"profile": prof.get("name"), "layer": lay.get("layer"),
                                   "differential": diff, "target_ohms": target,
                                   "altium_width_mils": lay.get("trace_width_mils"),
                                   "altium_ohms": lay.get("calculated_impedance_ohms")}
            if info is None:
                row["note"] = "layer not found in the stack, or a plane"
                out.append(row)
                continue
            w = float(lay.get("trace_width_mils") or 0) * MIL
            if w > 0:
                if diff:
                    gap = float(lay.get("diff_pair_gap_mils") or 0) * MIL
                    if gap > 0:
                        sec = build(info, opt, w, 2, gap)
                        row["our_ohms_at_altium_width"] = round(
                            impedances(sec, opt.f_design, 2 * RESOLUTION)["z_diff"], 3)
                else:
                    sec = build(info, opt, w)
                    row["our_ohms_at_altium_width"] = round(
                        impedances(sec, opt.f_design, 2 * RESOLUTION)["z0"], 3)
            if target > 0:
                o = Options(**{**opt.__dict__})
                if diff and lay.get("diff_pair_gap_mils"):
                    o.diff_gap_mils = float(lay["diff_pair_gap_mils"])
                sol = solve_width(info, o, target, diff)
                if sol.get("ok"):
                    row["our_width_mils"] = sol["width_mils"]
            if row.get("altium_ohms") and row.get("our_ohms_at_altium_width"):
                row["difference_pct"] = round(100 * (row["our_ohms_at_altium_width"]
                                                     / float(row["altium_ohms"]) - 1), 2)
            out.append(row)
    return out
