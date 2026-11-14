# SPDX-License-Identifier: Apache-2.0
"""Crosstalk onto a routed net from the copper that runs beside it.

For each stretch of the victim's route the board is searched for tracks
and arcs of other nets on the same layer that run parallel to it within
reach. The stretches are added up per aggressor and gap, and each gap's
coupling is taken from the field solver's two-trace cross-section:

    Kc = |C12| / C11        capacitive coupling
    Kl = L12 / L11          inductive coupling (L from the air-filled C)

A step of rise time tr on the aggressor then induces, as fractions of the
aggressor's swing on a matched line (the classic weak-coupling results):

    near end   NEXT = (Kc + Kl) / 4, reached once the coupled stretch is
               longer than half the edge (2 TD >= tr), and 2 TD / tr of
               that below it
    far end    FEXT = (Kc - Kl) / 2 * TD / tr, which is zero in a
               homogeneous dielectric (stripline) and negative on a
               microstrip

with TD the delay of the coupled stretch. A differential victim hears the
difference between what reaches its two sides, and a differential
aggressor the difference between its two sides: each contribution carries
the sign of the victim's side and the aggressor's side it belongs to.

One aggressor's near end grows with the length it shares with the victim
until it reaches (Kc + Kl) / 4 of its tightest gap, however the stretch is
cut into segments. Aggressors add up as their magnitudes (all switching
together, the worst case). The aggressor's edge is taken as fast as the
victim's. Grounds, plane nets and supply rails do not switch and are left
out.
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ..layout import geom
from ..layout.model import LayoutBoard
from . import planner
from . import rlgc as _rlgc
from .section import MIL

MU0 = 4e-7 * math.pi
EPS0 = 8.8541878128e-12

GROUND_PREFIXES = ("GND", "AGND", "DGND", "PGND", "VSS", "GROUND", "0V")
#: Supply rails by their usual names: +3V3, 1V8, VCC_IO, VDD, VBAT, AVDD ...
SUPPLY = re.compile(r"^(?:[+-]?\d+(?:\.\d+)?V\d*|[+-]\d+V?|[AD]?V(?:CC|DD|EE|BAT|BUS|IN|REF|SYS|PP)|PWR|VPP)(?![A-Z])")

#: Two tracks within this angle of each other run side by side.
PARALLEL_DEG = 10.0
#: A net that runs beside the victim for less than this in all is a
#: crossing or a corner, not an aggressor. It is the net's whole shared
#: length that counts: a curve is many short chords.
MIN_RUN_MILS = 10.0
#: Two runs beside the same net whose end and start lie this close along the
#: victim are one stretch.
JOIN_MILS = 2.0
#: An aggressor's arc is followed by chords this close to it.
ARC_TOL_MILS = 0.05
#: Reach of the search, in heights above the reference plane (edge to edge),
#: when the spec does not give ``xtalk_reach_mils``.
REACH_HEIGHTS = 4.0
#: Gaps are solved on a geometric grid of this ratio and read between its
#: points: coupling falls smoothly with the gap.
GAP_STEP = 1.35
#: The far-end fraction stops growing here: the weak-coupling result does
#: not hold beyond it.
FEXT_CAP = 0.5


@dataclass
class Run:
    """One stretch of the victim beside one aggressor track."""
    aggressor: str
    layer: str
    width: float              # victim's, mils
    gap: float                # edge to edge, mils
    length: float             # mils
    at: tuple[float, float]   # where the stretch starts on the victim
    side: int = 1             # +1 the victim's positive side (or a single net), -1 negative
    end: Optional[tuple[float, float]] = None   # where it ends on the victim


@dataclass
class Aggressor:
    name: str                             # a net, or "pair NAME"
    nets: list[str]
    layer: str = ""
    gap_mils: float = 0.0                 # the closest
    length_mils: float = 0.0              # every coupled stretch added up
    next: float = 0.0                     # signed fractions of the aggressor's swing
    fext: float = 0.0
    at: tuple[float, float] = (0.0, 0.0)  # start of the longest stretch
    longest: float = 0.0
    runs: list[Run] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"aggressor": self.name, "nets": self.nets, "layer": self.layer,
                "gap_mils": round(self.gap_mils, 2), "length_mils": round(self.length_mils, 1),
                "next_pct": round(100 * abs(self.next), 3),
                "fext_pct": round(100 * abs(self.fext), 3),
                "at_mils": [round(self.at[0], 2), round(self.at[1], 2)]}


def _tracks_by_layer(board: LayoutBoard, own: set[str]) -> dict[str, dict[str, np.ndarray]]:
    # (x1, y1, x2, y2, width, net) per layer; an arc as its chords
    by: dict[str, list] = {}
    for t in board.tracks:
        if not t.net or t.net in own or t.keepout:
            continue
        by.setdefault(t.layer, []).append((t.x1, t.y1, t.x2, t.y2, t.width, t.net))
    for a in board.arcs:
        if not a.net or a.net in own or a.keepout:
            continue
        pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, ARC_TOL_MILS)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            by.setdefault(a.layer, []).append((x1, y1, x2, y2, a.width, a.net))
    out = {}
    for layer, ts in by.items():
        out[layer] = {"a": np.array([t[:2] for t in ts], dtype=float),
                      "b": np.array([t[2:4] for t in ts], dtype=float),
                      "w": np.array([t[4] for t in ts], dtype=float),
                      "net": np.array([t[5] for t in ts], dtype=object)}
    return out


def find_runs(board: LayoutBoard, own: set[str], sections, reach, side: int = 1) -> list[Run]:
    """The stretches of ``sections`` (a path's line sections) that run
    beside tracks of nets outside ``own``. ``reach(layer, width)`` is how
    far edge to edge the search looks."""
    tracks = _tracks_by_layer(board, own)
    sin_max = math.sin(math.radians(PARALLEL_DEG))
    runs: list[Run] = []
    for s in sections:
        if s.kind != "line" or len(s.pts) < 2 or s.layer not in tracks:
            continue
        tr = tracks[s.layer]
        far = reach(s.layer, s.width)
        for p, q in zip(s.pts, s.pts[1:]):
            p, q = np.asarray(p, dtype=float), np.asarray(q, dtype=float)
            L = float(np.hypot(*(q - p)))
            if L < 1e-6:
                continue
            u = (q - p) / L
            d = tr["b"] - tr["a"]
            dl = np.hypot(d[:, 0], d[:, 1])
            ok = dl > 1e-6
            cross = np.zeros(len(dl))
            cross[ok] = np.abs(d[ok, 0] * u[1] - d[ok, 1] * u[0]) / dl[ok]
            sa = (tr["a"] - p) @ u
            sb = (tr["b"] - p) @ u
            lo = np.maximum(np.minimum(sa, sb), 0.0)
            hi = np.minimum(np.maximum(sa, sb), L)
            # the aggressor's distance from the victim's axis at the middle
            # of the overlap
            mid = 0.5 * (lo + hi)
            span = sb - sa
            with np.errstate(divide="ignore", invalid="ignore"):
                frac = np.where(np.abs(span) > 1e-9, (mid - sa) / span, 0.5)
            pt = tr["a"] + d * frac[:, None]
            off = np.abs((pt[:, 0] - p[0]) * u[1] - (pt[:, 1] - p[1]) * u[0])
            gap = off - 0.5 * (s.width + tr["w"])
            keep = ok & (cross <= sin_max) & (hi - lo > 1e-6) & (gap > 0) & (gap <= far)
            # in the order the victim meets them, whichever way they were drawn
            for k in sorted(np.nonzero(keep)[0], key=lambda i: lo[i]):
                start, stop = p + u * lo[k], p + u * hi[k]
                runs.append(Run(str(tr["net"][k]), s.layer, float(s.width), float(gap[k]),
                                float(hi[k] - lo[k]), (float(start[0]), float(start[1])), side,
                                (float(stop[0]), float(stop[1]))))
    return runs


class Coupling:
    """Kc, Kl and the delay per metre of two traces ``gap`` apart on a
    layer, from the field solver, on a grid of gaps and read between."""

    def __init__(self, info: dict[str, dict[str, Any]], opt: planner.Options,
                 f_ref: float, resolution: float = 15.0):
        self.info, self.opt, self.f, self.res = info, opt, f_ref, resolution
        self.solved: dict[tuple[str, float, int], tuple[float, float, float]] = {}

    def height(self, layer: str) -> float:
        i = self.info.get(layer) or {}
        hs = [i[k] for k in ("h_mils", "h_below_mils", "h_above_mils") if k in i]
        return min(hs) if hs else 0.0

    def _at(self, layer: str, width: float, k: int) -> tuple[float, float, float]:
        key = (layer, round(width, 2), k)
        if key not in self.solved:
            if layer not in self.info:
                raise ValueError(f"layer {layer} has no reference copper in the stack")
            gap = GAP_STEP ** k
            sec = planner.build(self.info[layer], self.opt, width * MIL, 2, gap * MIL)
            cs, c0, _ = _rlgc.capacitance(sec, [self.f], self.res)
            c = cs[0].real
            ind = MU0 * EPS0 * np.linalg.inv(c0)
            self.solved[key] = (abs(c[0, 1]) / c[0, 0], ind[0, 1] / ind[0, 0],
                                math.sqrt(ind[0, 0] * c[0, 0]))
        return self.solved[key]

    def get(self, layer: str, width: float, gap: float) -> tuple[float, float, float]:
        """(Kc, Kl, seconds per metre) at ``gap`` mils."""
        x = math.log(max(gap, 0.5)) / math.log(GAP_STEP)
        k0 = math.floor(x)
        a, b = self._at(layer, width, k0), self._at(layer, width, k0 + 1)
        t = x - k0
        # coupling falls close to a power of the gap: read it on log scales
        out = []
        for va, vb in zip(a[:2], b[:2]):
            out.append(math.exp((1 - t) * math.log(max(va, 1e-12))
                                + t * math.log(max(vb, 1e-12))))
        return out[0], out[1], (1 - t) * a[2] + t * b[2]


def _of_run(run: Run, coupling: Coupling, rise: float) -> tuple[float, float, float]:
    """(near end while it still grows with length, its ceiling, far end)."""
    kc, kl, tpd = coupling.get(run.layer, run.width, run.gap)
    td = run.length * MIL * tpd
    kb = 0.25 * (kc + kl)
    return kb * 2 * td / rise, kb, 0.5 * (kc - kl) * td / rise


def quiet_nets(board: LayoutBoard, patterns=()) -> set[str]:
    """Nets that do not switch: plane nets, grounds, supply rails by name,
    and whatever matches ``patterns`` (globs)."""
    planes = {x.plane_net for x in board.layers if x.plane_net}
    pats = [str(x) for x in patterns or ()]
    out = set()
    for net in {t.net for t in board.tracks if t.net}:
        up = net.upper()
        if (net in planes or up.startswith(GROUND_PREFIXES) or SUPPLY.match(up)
                or any(fnmatch.fnmatchcase(up, x.upper()) for x in pats)):
            out.add(net)
    return out


def assess(board: LayoutBoard, runs: list[Run], coupling: Coupling, rise: float
           ) -> dict[str, Any]:
    """Per aggressor and in total: the near-end and far-end crosstalk of
    ``runs`` for an aggressor edge of ``rise`` seconds."""
    pair_of: dict[str, tuple[str, int]] = {}
    for d in board.diff_pairs:
        pair_of[d.positive] = (d.name, 1)
        pair_of[d.negative] = (d.name, -1)
    groups: dict[str, Aggressor] = {}
    # the near end of one aggressor net beside one side of the victim grows
    # with the length they share until it reaches the tightest gap's ceiling
    near: dict[tuple[str, str, int], list[float]] = {}
    shared: dict[str, float] = {}
    for r in runs:
        shared[r.aggressor] = shared.get(r.aggressor, 0.0) + r.length
    # runs that follow one another along the victim are one stretch: a curve
    # is many chords, and the stretch starts where its first chord does
    stretch: dict[tuple[str, int], list] = {}    # (net, side) -> [start, end, length]
    for r in runs:
        if shared[r.aggressor] < MIN_RUN_MILS:
            continue
        pname, sign = pair_of.get(r.aggressor, ("", 1))
        key = f"pair {pname}" if pname else r.aggressor
        g = groups.setdefault(key, Aggressor(key, []))
        if r.aggressor not in g.nets:
            g.nets.append(r.aggressor)
        grow, top, far = _of_run(r, coupling, rise)
        acc = near.setdefault((key, r.aggressor, r.side), [0.0, 0.0, r.side * sign])
        acc[0] += grow
        acc[1] = max(acc[1], top)
        g.fext += r.side * sign * far
        g.length_mils += r.length
        g.runs.append(r)
        if not g.gap_mils or r.gap < g.gap_mils:
            g.gap_mils, g.layer = r.gap, r.layer
        cur = stretch.get((r.aggressor, r.side))
        if cur is not None and r.end is not None and math.dist(cur[1], r.at) <= JOIN_MILS:
            cur[1], cur[2] = r.end, cur[2] + r.length
        else:
            cur = stretch[(r.aggressor, r.side)] = [r.at, r.end or r.at, r.length]
        if cur[2] > g.longest:
            g.longest, g.at = cur[2], cur[0]
    for (key, _net, _side), (grow, top, sign) in near.items():
        groups[key].next += sign * min(grow, top)
    for g in groups.values():
        g.fext = max(-FEXT_CAP, min(FEXT_CAP, g.fext))
    rows = sorted(groups.values(), key=lambda g: -max(abs(g.next), abs(g.fext)))
    total_next = sum(abs(g.next) for g in rows)
    total_fext = min(sum(abs(g.fext) for g in rows), FEXT_CAP)
    return {"aggressors": [g.to_dict() for g in rows],
            "next_pct": round(100 * total_next, 3), "fext_pct": round(100 * total_fext, 3),
            "worst_pct": round(100 * max(total_next, total_fext), 3),
            "rise_ps": round(rise * 1e12, 2)}


def of_path(board: LayoutBoard, path, coupling: Coupling, rise: float,
            reach_mils: Optional[float] = None, quiet=()) -> dict[str, Any]:
    """Crosstalk onto a routed net (a ``NetPath``) or pair (a ``PairPath``).
    Nets of ``quiet`` are not aggressors."""
    def reach(layer: str, width: float) -> float:
        if reach_mils is not None:
            return float(reach_mils)
        return max(REACH_HEIGHTS * coupling.height(layer), 3.0 * width)

    if hasattr(path, "p") and hasattr(path, "n"):
        own = {path.p.net, path.n.net} | set(quiet)
        runs = find_runs(board, own, path.p.sections, reach, 1) \
            + find_runs(board, own, path.n.sections, reach, -1)
    else:
        runs = find_runs(board, {path.net} | set(quiet), path.sections, reach, 1)
    return assess(board, runs, coupling, rise)
