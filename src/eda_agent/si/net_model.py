# SPDX-License-Identifier: Apache-2.0
"""A routed net or pair as a channel: S-parameters, TDR and figures.

The path from :mod:`.extract` is turned into blocks: each section a line
with the RLGC(f) of its own cross-section (layer, width, and for a pair
the gap), each via the two-port of :mod:`.via`, each stub an open line
in shunt. Cross-sections are solved once each and shared.

The TDR is the step response of S11 (Sdd11 for a pair) with a Gaussian
edge of the rise time asked for, turned into impedance,
Z = Z0 (1 + rho) / (1 - rho); the TDT gives the delay and, for a pair,
the skew between its sides.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from ..emc.stack import Stack
from ..layout import copper_raster
from ..layout.model import LayoutBoard
from . import channel as ch
from . import planner
from . import rlgc as _rlgc
from . import via as _via
from .extract import NetPath, PairPath
from .mtl import mixed_mode
from .section import MIL

#: A return via further than this is as good as none.
RETURN_REACH_MILS = 250.0
GROUND_PREFIXES = ("GND", "AGND", "DGND", "PGND", "VSS", "GROUND", "0V")


@dataclass
class Model:
    f: np.ndarray
    s: np.ndarray                     # (nf, 2n, 2n) single-ended
    n: int
    z_ref: float
    blocks: list[dict[str, Any]] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def sdd(self) -> np.ndarray:
        return mixed_mode(self.s)


class _Lines:
    """RLGC of each cross-section, solved once."""

    def __init__(self, stack: Stack, opt: planner.Options, f: np.ndarray, resolution: float):
        self.info = {i["layer"]: i for i in planner.layer_sections(stack, opt)}
        self.opt, self.f, self.res = opt, f, resolution
        self.cache: dict[tuple, _rlgc.RLGC] = {}
        self.summary: dict[str, Any] = {}

    def get(self, layer: str, width_mils: float, gap_mils: Optional[float] = None) -> _rlgc.RLGC:
        key = (layer, round(width_mils, 2), None if gap_mils is None else round(gap_mils, 2))
        if key not in self.cache:
            if layer not in self.info:
                raise ValueError(f"layer {layer} has no reference copper in the stack "
                                 "(a plane layer, or a layer the stackup does not list)")
            info = self.info[layer]
            n, s = (1, 0.0) if gap_mils is None else (2, gap_mils * MIL)
            sec = planner.build(info, self.opt, width_mils * MIL, n, s)
            r = _rlgc.compute(sec, self.f, self.res)
            self.cache[key] = r
            k = len(self.f) // 2
            figs = _rlgc.figures(r, k)
            name = f"{layer} {key[1]:g} mil" + ("" if gap_mils is None else f" gap {key[2]:g} mil")
            self.summary[name] = {
                "structure": info["structure"],
                **{k2: round(v, 3) for k2, v in figs.items()
                   if isinstance(v, float) and (k2.startswith("z") or "loss" in k2
                                                or "delay" in k2)}}
        return self.cache[key]


def _ground_nets(board: LayoutBoard) -> set[str]:
    nets = {l.plane_net for l in board.layers if l.kind == "plane" and l.plane_net}
    for p in board.pads:
        if p.net.upper().startswith(GROUND_PREFIXES):
            nets.add(p.net)
    for v in board.vias:
        if v.net.upper().startswith(GROUND_PREFIXES):
            nets.add(v.net)
    return nets


def _refs(info: dict[str, Any]) -> list[str]:
    r = info.get("reference")
    return list(r) if isinstance(r, list) else [r] if r else []


def via_geometry(board: LayoutBoard, stack: Stack, lines: _Lines, idx: int,
                 from_layer: str, to_layer: str, opt: planner.Options) -> _via.ViaGeometry:
    v = board.vias[idx]
    z = {l.name: l.z_mils for l in stack.layers}
    names = [l.name for l in stack.layers]
    low = v.low_layer if v.low_layer in z else names[-1]
    high = v.high_layer if v.high_layer in z else names[0]
    # Altium's "low" is the top of the span (lower layer number); heights
    # grow upwards, so sort them.
    z_lo, z_hi = sorted((z[low], z[high]))
    clearance = copper_raster.plane_clearance_from_rules(board)
    if clearance is None:
        clearance = _via.DEFAULT_CLEARANCE_MILS
    planes = [l.z_mils for l in stack.layers if l.kind == "plane"]
    if not planes:
        planes = [l.z_mils for l in stack.layers if l.name not in (from_layer, to_layer)]
    notes = []
    # return path
    ref_a = _refs(lines.info.get(from_layer, {}))
    ref_b = _refs(lines.info.get(to_layer, {}))
    ret_h = ret_s = ret_d = 0.0
    if ref_a and ref_b and not set(ref_a) & set(ref_b):
        pairs = [(abs(z[a] - z[b]), a, b) for a in ref_a for b in ref_b if a in z and b in z]
        ret_h = min(pairs)[0] if pairs else 0.0
        gnd = _ground_nets(board)
        best, drill = math.inf, 0.0
        for o in board.vias:
            if o.net in gnd and o is not v:
                d = math.hypot(o.x - v.x, o.y - v.y)
                if d < best:
                    best, drill = d, o.hole
        for p in board.pads:
            if p.net in gnd and p.hole > 0:
                d = math.hypot(p.x - v.x, p.y - v.y)
                if d < best:
                    best, drill = d, p.hole
        if best > RETURN_REACH_MILS:
            notes.append(f"no return via within {RETURN_REACH_MILS:g} mil of the via at "
                         f"({v.x:.1f}, {v.y:.1f}) as it changes reference; taken at "
                         f"{RETURN_REACH_MILS:g} mil")
            best, drill = RETURN_REACH_MILS, v.hole
        ret_s, ret_d = best, drill
    return _via.ViaGeometry(
        drill_mils=v.hole, pad_mils=v.diameter, antipad_mils=v.diameter + 2 * clearance,
        z_entry=z[from_layer], z_exit=z[to_layer], z_low=z_lo, z_high=z_hi,
        er=stack.er_between(names[0], names[-1]), tand=opt.tand, planes=planes,
        return_height_mils=ret_h, return_spacing_mils=ret_s, return_drill_mils=ret_d,
        notes=notes)


def _via_block(board, stack, lines, opt, f, idx, a, b, label) -> tuple[np.ndarray, dict]:
    g = via_geometry(board, stack, lines, idx, a, b, opt)
    mats, info = _via.two_port(g, f)
    return mats, info


def single(board: LayoutBoard, stack: Stack, path: NetPath, f: Sequence[float],
           opt: Optional[planner.Options] = None, resolution: float = 30.0,
           z_ref: float = 50.0) -> Model:
    opt = opt or planner.Options()
    f = np.asarray(f, dtype=float)
    lines = _Lines(stack, opt, f, resolution)
    chan = ch.Channel(f, 1)
    blocks: list[dict[str, Any]] = []
    notes = list(path.notes)

    def stubs_after(i: int) -> None:
        for s in path.stubs:
            if s.at == i:
                r = lines.get(s.layer, s.width)
                chan.add(ch.stub_block(r, s.length * MIL, f"stub {s.length:.0f} mil"))
                blocks.append({"kind": "stub", **s.to_dict()})

    stubs_after(-1)
    for i, sec in enumerate(path.sections):
        if sec.kind == "line":
            r = lines.get(sec.layer, sec.width)
            chan.add(ch.line_block(r, sec.length * MIL, sec.layer))
            blocks.append(sec.to_dict())
        else:
            mats, info = _via_block(board, stack, lines, opt, f, sec.via, sec.from_layer,
                                    sec.to_layer, "via")
            chan.add(ch.Block("via", "via", mats, info))
            blocks.append({**sec.to_dict(), **_via_summary(info)})
            notes.extend(info["notes"])
        stubs_after(i)
    return Model(f, chan.s(z_ref), 1, z_ref, blocks, lines.summary, notes)


def _via_summary(info: dict[str, Any]) -> dict[str, Any]:
    return {"z_barrel_ohms": round(info["z_barrel_ohms"], 2),
            "stub_mils": round(info["stub_mils"], 2),
            "stub_resonance_ghz": (round(info["stub_resonance_hz"] / 1e9, 2)
                                   if info["stub_resonance_hz"] else None),
            "return_loop_nh": round(info["return_l_h"] * 1e9, 3)}


def _chain(lines: _Lines, pieces: Sequence[Sequence[Any]], scale: float, f) -> np.ndarray:
    """Per-frequency 2x2 chain of single lines (layer, width, length)."""
    out = np.broadcast_to(np.eye(2, dtype=complex), (len(f), 2, 2)).copy()
    for layer, w, L in pieces:
        if L * scale <= 0:
            continue
        r = lines.get(layer, w)
        out = out @ ch.line_block(r, L * scale * MIL).mats
    return out


def pair(board: LayoutBoard, stack: Stack, pp: PairPath, f: Sequence[float],
         opt: Optional[planner.Options] = None, resolution: float = 30.0,
         z_ref: float = 50.0) -> Model:
    """Single-ended 4-port of a pair (ports P near, N near, P far, N far)."""
    opt = opt or planner.Options()
    f = np.asarray(f, dtype=float)
    lines = _Lines(stack, opt, f, resolution)
    chan = ch.Channel(f, 2)
    blocks: list[dict[str, Any]] = []
    notes = list(pp.notes) + list(pp.p.notes)
    vias_p = [s for s in pp.p.sections if s.kind == "via"]
    vias_n = [s for s in pp.n.sections if s.kind == "via"]

    def uncoupled(zone, half: float, label: str) -> None:
        a = _chain(lines, zone["uncoupled_p"], half, f)
        b = _chain(lines, zone["uncoupled_n"], half, f)
        mats = np.stack([ch.per_conductor([a[k], b[k]]) for k in range(len(f))])
        chan.add(ch.Block("line", label, mats))

    for j, zone in enumerate(pp.zones):
        uncoupled(zone, 0.5, "breakout")
        for layer, w, gap, L in zone["coupled"]:
            r = lines.get(layer, w, gap)
            chan.add(ch.line_block(r, L * MIL, layer))
            blocks.append({"kind": "coupled", "layer": layer, "width_mils": w, "gap_mils": gap,
                           "length_mils": round(L, 3)})
        uncoupled(zone, 0.5, "breakout")
        up = sum(x[2] for x in zone["uncoupled_p"])
        un = sum(x[2] for x in zone["uncoupled_n"])
        if up or un:
            blocks.append({"kind": "uncoupled", "p_mils": round(up, 3), "n_mils": round(un, 3)})
        if j < len(pp.zones) - 1 and j < len(vias_p) and j < len(vias_n):
            vp, vn = vias_p[j], vias_n[j]
            mp, ip = _via_block(board, stack, lines, opt, f, vp.via, vp.from_layer, vp.to_layer, "")
            mn, inn = _via_block(board, stack, lines, opt, f, vn.via, vn.from_layer, vn.to_layer, "")
            chan.add(ch.Block("via", "via pair",
                              np.stack([ch.per_conductor([mp[k], mn[k]]) for k in range(len(f))])))
            blocks.append({"kind": "via pair", "from_layer": vp.from_layer, "to_layer": vp.to_layer,
                           **_via_summary(ip)})
            notes.extend(ip["notes"] + inn["notes"])
    if any(s for s in pp.p.stubs) or any(s for s in pp.n.stubs):
        notes.append("stubs on a pair's nets are listed but not modelled")
    notes.append("the vias of a pair are modelled uncoupled; their coupling lowers the "
                 "differential via impedance a little")
    return Model(f, chan.s(z_ref), 2, z_ref, blocks, lines.summary, sorted(set(notes)))


# -- time domain ----------------------------------------------------------------------

def step_response(f: np.ndarray, h: np.ndarray, rise: float, pad: int = 8
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Step response of a transfer function sampled at f = df .. N df.

    DC is taken from the lowest point (its real part). A Gaussian edge of
    10-90 % rise time ``rise`` limits the band."""
    N = len(f)
    df = f[0]
    if not np.allclose(np.diff(f), df, rtol=1e-6, atol=df * 1e-6):
        raise ValueError("a step response needs the uniform band df, 2 df, ... N df")
    sigma = rise / 2.5631
    # The Gaussian edge is centred t0 late so all of it lies after t = 0
    # (a centred one would wrap half its rise to the end of the period).
    t0 = 4 * sigma
    full = np.concatenate([[h[0].real], h])
    ff = np.concatenate([[0.0], f])
    full = full * np.exp(-0.5 * (2 * math.pi * ff * sigma) ** 2 - 2j * math.pi * ff * t0)
    n = 2 * N * pad
    step = np.cumsum(np.fft.irfft(full, n))
    # sample i of the running sum holds the step up to the END of its
    # interval, half a sample after the sample's own time
    t = (np.arange(n) + 0.5) / (n * df) - t0
    keep = (t >= -t0) & (t < 0.95 / df - t0)
    return t[keep], step[keep]


def tdr(f: np.ndarray, s11: np.ndarray, z0: float, rise: float) -> tuple[np.ndarray, np.ndarray]:
    t, rho = step_response(f, s11, rise)
    rho = np.clip(rho, -0.999, 0.999)
    return t, z0 * (1 + rho) / (1 - rho)


def delay_50(t: np.ndarray, step: np.ndarray) -> Optional[float]:
    """When the step first reaches half its final value (interpolated)."""
    final = step[-1]
    if abs(final) < 1e-6:
        return None
    half = 0.5 * final
    above = np.nonzero(step >= half)[0]
    if not len(above):
        return None
    i = int(above[0])
    if i == 0:
        return float(t[0])
    t0, t1, y0, y1 = t[i - 1], t[i], step[i - 1], step[i]
    return float(t0 + (half - y0) * (t1 - t0) / (y1 - y0))


def figures(m: Model, f_nyquist: float, rise: float) -> dict[str, Any]:
    """The numbers a designer reads off a channel."""
    k = ch.nyquist_index(m.f, f_nyquist)
    out: dict[str, Any] = {"f_nyquist_hz": float(m.f[k])}
    band = m.f <= m.f[k] * 1.0000001
    if m.n == 1:
        s11, s21 = m.s[:, 0, 0], m.s[:, 1, 0]
        z0 = m.z_ref
    else:
        mm = m.sdd
        s11, s21 = mm[:, 0, 0], mm[:, 1, 0]
        z0 = 2 * m.z_ref
        out["scd21_db_at_nyquist"] = float(ch.db(mm[k, 3, 0]))
        t, sp = step_response(m.f, m.s[:, 2, 0], rise)
        _, sn = step_response(m.f, m.s[:, 3, 1], rise)
        dp, dn = delay_50(t, sp), delay_50(t, sn)
        if dp is not None and dn is not None:
            out["skew_ps"] = (dp - dn) * 1e12
    out["insertion_loss_db_at_nyquist"] = float(-ch.db(s21[k]))
    out["return_loss_db_worst"] = float(-np.max(ch.db(s11[band])))
    t, step = step_response(m.f, s21, rise)
    d = delay_50(t, step)
    out["delay_ps"] = d * 1e12 if d is not None else None
    tt, z = tdr(m.f, s11, z0, rise)
    # the TDR inside the channel: before the far end's reflection returns
    end = 2 * (d or tt[-1]) + 2 * rise
    inside = (tt > rise) & (tt < end)
    if inside.any():
        out["tdr_min_ohms"] = float(np.min(z[inside]))
        out["tdr_max_ohms"] = float(np.max(z[inside]))
    out["passivity_max_sv"] = ch.passivity(m.s)
    out["reciprocity_err"] = ch.reciprocity(m.s)
    return out
