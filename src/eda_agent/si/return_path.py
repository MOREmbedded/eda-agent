# SPDX-License-Identifier: Apache-2.0
"""The copper under a routed net: where its return current loses its path.

A trace's return current runs in the reference copper right beneath it.
Where that copper stops (a split between two plane nets, a slot, a row of
antipads grown into one, the edge of a pour) the current has to go round:
the loop it opens is an impedance step, radiates, and couples every other
trace that crosses the same gap.

For each trace section of the route the reference layers come from the
stackup (the plane under a microstrip, the two planes of a stripline). The
route is sampled along its centre line, and each sample is looked up in
the copper the board carries for that layer: split planes, pours and solid
regions, with their holes. A stretch with no copper beneath is a gap; a
gap is reported with its length, its place and the nets either side.

A reference layer with no copper shapes at all is taken as solid when it
is a plane layer (Altium draws planes in the negative, and a board read
without its split planes says nothing about them), and as missing when it
is a signal layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from ..layout.model import LayoutBoard

#: Kinds of region that are copper a return current can use.
COPPER_KINDS = ("plane", "pour", "copper")
#: Distance between samples along the route, mils.
STEP_MILS = 2.0
#: A gap shorter than this is no finding unless the reference net changes
#: across it (``return_gap_min_mils``).
MIN_GAP_MILS = 8.0
#: A gap this close to an end of a trace section is the antipad of the
#: section's own via or the clearance round its pad.
END_CLEAR_MILS = 40.0


@dataclass
class Gap:
    layer: str                 # the trace's
    reference: str             # the layer with no copper
    length: float              # along the route, mils
    at: tuple[float, float]    # where it starts
    distance: float            # from the start of the route, mils
    net_before: str = ""
    net_after: str = ""
    other_solid: bool = False  # a stripline whose other reference is whole here

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "reference": self.reference,
                "length_mils": round(self.length, 1),
                "at_mils": [round(self.at[0], 2), round(self.at[1], 2)],
                "distance_mils": round(self.distance, 1),
                "net_before": self.net_before, "net_after": self.net_after,
                "other_reference_solid": self.other_solid}

    def text(self) -> str:
        if self.net_before and self.net_after and self.net_before != self.net_after:
            what = f"the split between {self.net_before} and {self.net_after}"
        elif not self.net_before and not self.net_after:
            what = "no copper"
        else:
            what = f"a gap of {self.length:.0f} mil"
        if what.startswith("the split"):
            what += f" ({self.length:.0f} mil)"
        return f"crosses {what} in {self.reference}"


def _in_ring(x: np.ndarray, y: np.ndarray, ring) -> np.ndarray:
    r = np.asarray(ring, dtype=float)
    xi, yi = r[:, 0], r[:, 1]
    xj, yj = np.roll(xi, 1), np.roll(yi, 1)
    inside = np.zeros(len(x), dtype=bool)
    for a, b, c, d in zip(xi, yi, xj, yj):
        if b == d:
            continue
        hit = ((b > y) != (d > y)) & (x < a + (y - b) * (c - a) / (d - b))
        inside ^= hit
    return inside


class Copper:
    """The copper shapes of one layer, asked point by point."""

    def __init__(self, board: LayoutBoard, layer: str):
        self.shapes = [r for r in board.regions
                       if r.layer == layer and r.kind in COPPER_KINDS and not r.keepout
                       and len(r.outline) >= 3]
        self.cutouts = [r for r in board.regions
                        if r.layer == layer and r.kind == "cutout" and len(r.outline) >= 3]
        self.nets = sorted({r.net for r in self.shapes})

    def under(self, pts: np.ndarray) -> np.ndarray:
        """For each point the index into ``nets`` of the copper there, -1 for none."""
        x, y = pts[:, 0], pts[:, 1]
        out = np.full(len(pts), -1, dtype=int)
        for r in self.shapes:
            o = np.asarray(r.outline, dtype=float)
            box = ((x >= o[:, 0].min()) & (x <= o[:, 0].max())
                   & (y >= o[:, 1].min()) & (y <= o[:, 1].max()))
            if not box.any():
                continue
            idx = np.nonzero(box)[0]
            ins = _in_ring(x[idx], y[idx], r.outline)
            for h in r.holes:
                if len(h) >= 3 and ins.any():
                    ins &= ~_in_ring(x[idx], y[idx], h)
            out[idx[ins]] = self.nets.index(r.net)
        for c in self.cutouts:
            out[_in_ring(x, y, c.outline)] = -1
        return out


def _sample(pts, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Points every ``step`` along a polyline, and their distance from its start."""
    p = np.asarray(pts, dtype=float)
    seg = np.hypot(*(p[1:] - p[:-1]).T)
    total = float(seg.sum())
    n = max(int(round(total / step)), 1)
    s = (np.arange(n) + 0.5) * total / n
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    k = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
    t = np.where(seg[k] > 0, (s - cum[k]) / np.where(seg[k] > 0, seg[k], 1.0), 0.0)
    return p[k] + (p[k + 1] - p[k]) * t[:, None], s


def _references(info: dict[str, Any]) -> list[str]:
    r = info.get("reference")
    return [r] if isinstance(r, str) else list(r or [])


def find_gaps(board: LayoutBoard, sections, info: dict[str, dict[str, Any]],
              min_gap: float = MIN_GAP_MILS) -> tuple[list[Gap], list[str]]:
    """The gaps under a path's line sections, and notes on what could not
    be looked at. ``info`` is the planner's layer table keyed by layer."""
    kinds = {x.name: x.kind for x in board.layers}
    copper: dict[str, Copper] = {}
    gaps: list[Gap] = []
    notes: list[str] = []
    assumed: set[str] = set()
    done = 0.0
    for s in sections:
        length = float(getattr(s, "length", 0.0) or 0.0)
        if s.kind != "line" or len(s.pts) < 2 or s.layer not in info:
            done += length if s.kind == "line" else 0.0
            continue
        pts, dist = _sample(s.pts, STEP_MILS)
        total = float(dist[-1] + dist[0]) if len(dist) else 0.0
        step = total / max(len(dist), 1)
        refs = _references(info[s.layer])
        covers = {}
        for ref in refs:
            c = copper.setdefault(ref, Copper(board, ref))
            if not c.shapes and kinds.get(ref) == "plane":
                assumed.add(ref)
                continue
            covers[ref] = (c, c.under(pts))
        for ref, (c, net) in covers.items():
            bare = net < 0
            edges = np.flatnonzero(np.diff(np.concatenate([[False], bare, [False]]).astype(int)))
            for a, b in zip(edges[::2], edges[1::2]):
                glen = (b - a) * step
                before = c.nets[net[a - 1]] if a > 0 else ""
                after = c.nets[net[b]] if b < len(net) else ""
                at_end = a == 0 or b == len(net)
                whole = a == 0 and b == len(net)
                if at_end and not whole and glen <= END_CLEAR_MILS:
                    continue
                changes = bool(before and after and before != after)
                if glen < min_gap and not changes:
                    continue
                other = [o for o in covers if o != ref]
                solid = bool(other) and all((covers[o][1][a:b] >= 0).all() for o in other) \
                    or bool(set(refs) - set(covers) - {ref})
                gaps.append(Gap(s.layer, ref, float(glen), (float(pts[a, 0]), float(pts[a, 1])),
                                done + float(dist[a] - 0.5 * step), before, after, solid))
        done += total
    for ref in sorted(assumed):
        notes.append(f"{ref} has no copper shapes in the board data and is taken as a solid plane")
    gaps.sort(key=lambda g: (g.other_solid, -g.length))
    return gaps, notes


def of_path(board: LayoutBoard, path, info: dict[str, dict[str, Any]],
            min_gap: Optional[float] = None) -> dict[str, Any]:
    """The return path of a routed net (``NetPath``) or pair (``PairPath``)."""
    mg = MIN_GAP_MILS if min_gap is None else float(min_gap)
    if hasattr(path, "p") and hasattr(path, "n"):
        gp, notes = find_gaps(board, path.p.sections, info, mg)
        gn, more = find_gaps(board, path.n.sections, info, mg)
        gaps = sorted(gp + gn, key=lambda g: (g.other_solid, -g.length))
        notes = sorted(set(notes) | set(more))
    else:
        gaps, notes = find_gaps(board, path.sections, info, mg)
    return {"gaps": [dict(g.to_dict(), text=g.text()) for g in gaps], "notes": notes,
            "uncovered_mils": round(sum(g.length for g in gaps), 1),
            "worst_mils": round(max((g.length for g in gaps), default=0.0), 1)}
