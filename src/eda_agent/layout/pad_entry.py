# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""How traces enter pads (offline audit on a LayoutBoard).

Surface-mount pads are where solder and current meet, and the way a trace
arrives matters for both:

* ``off_axis``: a trace entering at an angle to the pad's sides rather than
  straight along one of its axes (an entry at a corner).
* ``off_center``: a trace landing well to one side of the pad's centreline,
  which pulls the part sideways as the solder melts.
* ``wider_than_pad``: a trace wider than the pad it enters.
* ``crowded``: more entries than a small pad can take cleanly.
* ``crosses_pad``: a same-net trace running over the pad and on, rather than
  ending on it.
* ``unbalanced``: the two pads of a two-pin part fed very unequally (one by a
  fill or a wide trace, the other by a thin one); the heavier side holds
  heat longer and the part can tombstone.

Pads are judged in their own frame (``Pad.rotation``). Round pads have no
axis and skip the angle check. Every finding carries the pad, the measured
value and the threshold, so it can be read without the code.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Optional

from eda_agent.layout import geom
from eda_agent.layout.copper_path import pad_layers
from eda_agent.layout.model import LayoutBoard, Pad

LIMIT = 50
ANGLE_TOL_DEG = 5.0          # off_axis beyond this from the nearest axis
CENTER_FRACTION = 0.5        # off_center beyond this share of the half-width
CENTER_PAD_MILS = 60.0       # off_center applies to pads this small: a trace
                             # at the edge of a large power pad is harmless
SMALL_PAD_MILS = 40.0        # crowded applies to pads this small or smaller
MAX_ENTRIES_SMALL = 2
UNBALANCE_RATIO = 3.0        # unbalanced: heavier side at least this many
UNBALANCE_MIN_MILS = 10.0    # times the lighter, and this much wider
TOMBSTONE_PAD_MILS = 80.0    # unbalanced applies to parts with pads this small
WIDE_RATIO = 1.1             # wider_than_pad: trace this much wider than the
WIDE_MIN_MILS = 1.0          # pad and by at least this much (rounding: a 10 mil
                             # trace into a 0.25 mm pad is 10 vs 9.84)


def _pad_size(pad: Pad) -> tuple[float, float, str]:
    """(width along the pad's x, height along its y, shape) on its first
    copper layer."""
    for c in pad.copper:
        if c.w or c.h:
            return c.w, c.h, c.shape
    return 0.0, 0.0, "round"


def _to_pad_frame(pad: Pad, dx: float, dy: float) -> tuple[float, float]:
    a = math.radians(-pad.rotation)
    return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)


def _inside(pad: Pad, x: float, y: float, margin: float = 0.0) -> bool:
    w, h, shape = _pad_size(pad)
    if not w and not h:
        return math.hypot(x - pad.x, y - pad.y) <= 10.0 + margin
    lx, ly = _to_pad_frame(pad, x - pad.x, y - pad.y)
    if shape == "round" and abs(w - h) < 1e-6:
        return math.hypot(lx, ly) <= w / 2 + margin
    return abs(lx) <= w / 2 + margin and abs(ly) <= h / 2 + margin


def _segment_hits_pad(pad: Pad, t) -> bool:
    """True when the segment's interior passes over the pad."""
    for i in range(1, 20):
        f = i / 20.0
        if _inside(pad, t.x1 + (t.x2 - t.x1) * f, t.y1 + (t.y2 - t.y1) * f):
            return True
    return False


def pad_entry_audit(board: LayoutBoard, limit: int = LIMIT,
                    refs: Optional[list[str]] = None) -> dict:
    """Audit every surface-mount pad (or those of ``refs``) for how traces
    enter it. Changes nothing."""
    wanted = set(refs) if refs else None
    by_layer_net: dict[tuple[str, str], list] = defaultdict(list)
    for t in board.tracks:
        if t.net and not t.keepout:
            by_layer_net[(t.layer, t.net)].append(t)
    fill = defaultdict(list)                # (layer, net) -> region shapes
    for r in board.regions:
        if r.net and not r.keepout and r.kind in ("copper", "pour"):
            fill[(r.layer, r.net)].append(r.shape())

    findings: list[dict] = []
    feed: dict[tuple[str, str], float] = {}        # pad -> copper width fed
    checked = 0
    for pad in board.pads:
        if not pad.comp or not pad.net or pad.hole > 0:
            continue
        if wanted is not None and pad.comp not in wanted:
            continue
        w, h, shape = _pad_size(pad)
        if not w and not h:
            continue
        checked += 1
        key = (pad.comp, str(pad.name))
        where = {"pad": f"{pad.comp}.{pad.name}", "net": pad.net,
                 "x": round(pad.x, 1), "y": round(pad.y, 1)}
        entries = []
        fed = 0.0
        for layer in pad_layers(board, pad):
            if any(geom.point_in_poly(pad.x, pad.y, s)
                   for s in fill.get((layer, pad.net), ())):
                fed = max(fed, max(w, h))            # a fill feeds the whole pad
            for t in by_layer_net.get((layer, pad.net), ()):
                in1, in2 = _inside(pad, t.x1, t.y1, 0.5), _inside(pad, t.x2, t.y2, 0.5)
                if in1 and in2:
                    continue                          # a stub inside the pad
                if in1 or in2:
                    ex, ey = (t.x1, t.y1) if in1 else (t.x2, t.y2)
                    ox, oy = (t.x2, t.y2) if in1 else (t.x1, t.y1)
                    entries.append((t, ex, ey, ox, oy))
                    fed += t.width
                elif _segment_hits_pad(pad, t):
                    findings.append({**where, "kind": "crosses_pad",
                                     "detail": "a trace of this net runs over "
                                     "the pad and on instead of ending on it"})
        feed[key] = fed
        small = min(w, h) <= SMALL_PAD_MILS
        if small and len(entries) > MAX_ENTRIES_SMALL:
            findings.append({**where, "kind": "crowded", "entries": len(entries),
                             "limit": MAX_ENTRIES_SMALL,
                             "detail": f"{len(entries)} traces enter a "
                                       f"{min(w, h):.0f} mil pad"})
        for t, ex, ey, ox, oy in entries:
            if (t.width > min(w, h) * WIDE_RATIO
                    and t.width - min(w, h) >= WIDE_MIN_MILS):
                findings.append({**where, "kind": "wider_than_pad",
                                 "trace_w": round(t.width, 1),
                                 "pad_min": round(min(w, h), 1)})
            lx, ly = _to_pad_frame(pad, ox - ex, oy - ey)
            ang = math.degrees(math.atan2(ly, lx)) % 90.0
            dev = min(ang, 90.0 - ang)
            if not (shape == "round" and abs(w - h) < 1e-6) and dev > ANGLE_TOL_DEG:
                # Exactly 45 degrees is a deliberate diagonal into a corner;
                # any other angle is more likely a mistake.
                diagonal = abs(dev - 45.0) <= ANGLE_TOL_DEG
                findings.append({**where, "kind": "off_axis",
                                 "angle_from_axis_deg": round(dev, 1),
                                 "limit_deg": ANGLE_TOL_DEG,
                                 "severity": "low" if diagonal else "high",
                                 "detail": "enters diagonally at a corner"
                                 if diagonal else "enters at an odd angle"})
            # Lateral offset of the landing point from the pad centreline
            # along the entry direction.
            px, py = _to_pad_frame(pad, ex - pad.x, ey - pad.y)
            along_x = abs(lx) >= abs(ly)
            lateral = abs(py) if along_x else abs(px)
            half = (h if along_x else w) / 2
            if (half > 0 and min(w, h) <= CENTER_PAD_MILS
                    and lateral > CENTER_FRACTION * half):
                findings.append({**where, "kind": "off_center",
                                 "offset_mils": round(lateral, 1),
                                 "limit_mils": round(CENTER_FRACTION * half, 1)})

    # Two-pin parts: compare what feeds each pad.
    by_comp = defaultdict(list)
    for p in board.pads:
        if p.comp and p.hole <= 0 and (wanted is None or p.comp in wanted):
            by_comp[p.comp].append(p)
    for comp, pads in by_comp.items():
        if len(pads) != 2:
            continue
        if max(max(_pad_size(p)[:2]) for p in pads) > TOMBSTONE_PAD_MILS:
            continue                  # too heavy a part to tombstone
        a, b = (feed.get((p.comp, str(p.name)), 0.0) for p in pads)
        heavy, light = max(a, b), min(a, b)
        # A pad fed by nothing is unrouted or fed by a via in the pad;
        # that is not an imbalance this check can judge.
        if light > 0 and heavy - light >= UNBALANCE_MIN_MILS and (
                heavy / light >= UNBALANCE_RATIO):
            findings.append({"pad": f"{comp}", "net": "", "kind": "unbalanced",
                             "feed_mils": [round(a, 1), round(b, 1)],
                             "detail": "the two pads are fed very unequally; "
                                       "the heavier side solders later and "
                                       "the part can tombstone"})

    # Severity decides the gate. A deliberate 45 degree corner entry is low
    # and an off-centre landing or a crowded pad is medium; only the entries
    # that cause real trouble (an odd angle, a trace much wider than its
    # pad, a trace running over a pad) fail the check.
    default = {"wider_than_pad": "high", "crosses_pad": "high",
               "off_center": "medium", "crowded": "medium",
               "unbalanced": "medium"}
    for f in findings:
        f.setdefault("severity", default.get(f["kind"], "medium"))
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f["kind"]] += 1
    rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: rank.get(f["severity"], 1))
    high = sum(1 for f in findings if f["severity"] == "high")
    return {"pass": high == 0,
            "counts": {"pads_checked": checked, "high": high,
                       **dict(counts)},
            "findings": findings[:limit],
            "thresholds": {"angle_deg": ANGLE_TOL_DEG,
                           "wide_ratio": WIDE_RATIO,
                           "tombstone_pad_mils": TOMBSTONE_PAD_MILS,
                           "center_fraction": CENTER_FRACTION,
                           "small_pad_mils": SMALL_PAD_MILS,
                           "unbalance_ratio": UNBALANCE_RATIO,
                           "unbalance_min_mils": UNBALANCE_MIN_MILS}}


__all__ = ["pad_entry_audit"]
