# SPDX-License-Identifier: Apache-2.0
"""EMI pre-scan of a placed and routed board. Offline; changes nothing.

Four checks, each on the board's fast nets (see ``fast_nets``):

* ``reference_gaps``: lengths of track with no return-net copper on an
  adjacent layer beneath them. A track that is partly over its plane and
  partly not crosses a split or a slot, the textbook emission source: the
  return current detours round the gap and the loop it draws is large.
  A track with no adjacent return copper at all has no reference plane.
* ``edge_proximity``: fast tracks closer than ``edge_mils`` to the board
  edge or a cutout, where the plane's fringing field and the track's
  field leak off the board.
* ``return_vias``: fast-net vias with no return via near them, from
  ``layout.audit.return_via_audit`` (the same check, not a second one).
* ``emission``: a differential-mode radiated emission estimate per net
  for which the drive (frequency, rise time, swing) is known; see
  ``emission``. Advisory: it ranks nets, it does not predict a scan.

Plane copper is taken from split-plane, pour and region objects on return
nets (``layout.audit.return_nets``). A plane layer with a plane net and no
split-plane objects counts as solid across the board outline. Antipads
round vias are not modelled, so a gap check sees a perforated plane as
whole; a row of vias cutting a slot in the plane is the one case that
misses, and an openEMS run of that region settles it.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from ..layout import geom
from ..layout.audit import (
    _edge_distance, _is_high_speed_class, return_nets, return_via_audit,
)
from ..layout.model import LayoutBoard
from .emission import DEFAULT_LIMIT, LIMITS, Drive, spectrum
from ..units import MM_PER_MIL
from .stack import Stack, assumed_stack

MIL_M = MM_PER_MIL / 1000.0  # metres per mil

#: Sample spacing along a track for the reference and edge checks, mils.
STEP_MILS = 10.0
#: Default distance a fast track keeps from the board edge, mils (2.5 mm).
EDGE_MILS = 100.0
#: Default return-via distance, mils, as ``pcb_layout_audit`` uses.
RETURN_VIA_MILS = 40.0
#: A run of unreferenced track shorter than this is noise (a pad entry
#: over a plane's antipad edge, a rounding of the outline), mils.
MIN_GAP_RUN_MILS = 15.0

#: Net-name words that mark a net as fast: clocks, serial buses with
#: sharp edges, high-speed interfaces and switching nodes.
_FAST_WORDS = (
    "CLK", "CLOCK", "SCK", "SCLK", "MCLK", "BCLK", "LRCLK", "XTAL", "XIN",
    "XOUT", "OSC", "USB", "ETH", "RGMII", "RMII", "MII", "MDI", "HDMI",
    "TMDS", "LVDS", "MIPI", "CSI", "DSI", "PCIE", "SATA", "DDR", "DQ",
    "DQS", "SDIO", "QSPI", "SPI", "SW", "LX", "PHASE", "RF", "ANT",
)
_SPLIT = re.compile(r"[^A-Z0-9]+")


def _is_fast_name(net: str) -> bool:
    words = [w for w in _SPLIT.split(net.upper()) if w]
    for w in words:
        stem = w.rstrip("0123456789")
        for fw in _FAST_WORDS:
            if w == fw or stem == fw or (len(fw) >= 3 and w.startswith(fw)):
                return True
    return False


def fast_nets(board: LayoutBoard, extra: Iterable[str] = ()) -> tuple[list[str], str]:
    """The nets the pre-scan looks at, and why: differential pairs,
    high-speed net classes, nets named like clocks and fast buses, and
    any named in ``extra``. Return nets are never fast nets."""
    returns = return_nets(board)
    nets = set(board.nets())
    chosen = {n for d in board.diff_pairs for n in (d.positive, d.negative) if n}
    chosen |= {n for c, members in board.net_classes.items()
               if _is_high_speed_class(c) for n in members}
    chosen |= {n for n in nets if _is_fast_name(n)}
    chosen |= {n for n in extra if n}
    chosen -= returns
    why = ("differential pairs, high-speed classes, clock / fast-bus names"
           + (" and the nets given" if extra else ""))
    return sorted(chosen), why


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _segments(board: LayoutBoard, nets: set[str]):
    """(net, layer, x1, y1, x2, y2, width) for every track and arc piece."""
    for t in board.tracks:
        if t.net in nets and not t.keepout:
            yield t.net, t.layer, t.x1, t.y1, t.x2, t.y2, t.width
    for a in board.arcs:
        if a.net in nets and not a.keepout:
            pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2)
            for p, q in zip(pts, pts[1:]):
                yield a.net, a.layer, p[0], p[1], q[0], q[1], a.width


def _samples(x1, y1, x2, y2, step):
    length = math.hypot(x2 - x1, y2 - y1)
    n = max(1, int(math.ceil(length / step)))
    for k in range(n + 1):
        t = k / n
        yield x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
    return


class _ReferenceCopper:
    """Return-net copper per layer, indexed for inside tests."""

    def __init__(self, board: LayoutBoard, returns: set[str]):
        self.by_layer: dict[str, list[tuple[tuple, geom.PolyIndex]]] = {}
        for r in board.regions:
            if r.keepout or r.kind in ("cutout", "keepout") or r.net not in returns:
                continue
            if len(r.outline) < 3:
                continue
            shape = geom.polygon(r.outline, r.holes)
            self.by_layer.setdefault(r.layer, []).append((shape.bbox, geom.PolyIndex(shape)))
        if board.outline and len(board.outline) >= 3:
            for layer in board.layers:
                if (layer.kind == "plane" and layer.plane_net in returns
                        and layer.name not in self.by_layer):
                    shape = geom.polygon(board.outline, board.cutouts)
                    self.by_layer[layer.name] = [(shape.bbox, geom.PolyIndex(shape))]

    def has(self, layer: str) -> bool:
        return bool(self.by_layer.get(layer))

    def covers(self, layer: str, x: float, y: float) -> bool:
        for (x0, y0, x1, y1), idx in self.by_layer.get(layer, ()):
            if x0 <= x <= x1 and y0 <= y <= y1 and idx.contains(x, y):
                return True
        return False


def _neighbours(board: LayoutBoard, stack: Stack, layer: str) -> list[str]:
    near = [l.name for l in stack.neighbours(layer)]
    if near:
        return near
    order = board.copper_layers()
    if layer not in order:
        return []
    i = order.index(layer)
    return [order[j] for j in (i - 1, i + 1) if 0 <= j < len(order)]


def _pt(x, y) -> list[float]:
    return [round(x, 2), round(y, 2)]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def reference_gaps(board: LayoutBoard, stack: Stack, nets: list[str],
                   ref: Optional[_ReferenceCopper] = None,
                   limit: int = 50) -> dict:
    """Track lengths with no return copper on an adjacent layer."""
    returns = return_nets(board)
    ref = ref or _ReferenceCopper(board, returns)
    per_net: dict[str, dict] = {}
    for net, layer, x1, y1, x2, y2, _w in _segments(board, set(nets)):
        refs = [l for l in _neighbours(board, stack, layer) if ref.has(l)]
        row = per_net.setdefault(net, {"net": net, "length_mils": 0.0,
                                       "unreferenced_mils": 0.0, "runs": [],
                                       "no_plane_layers": set()})
        seg_len = math.hypot(x2 - x1, y2 - y1)
        row["length_mils"] += seg_len
        if not refs:
            row["unreferenced_mils"] += seg_len
            row["no_plane_layers"].add(layer)
            continue
        pts = list(_samples(x1, y1, x2, y2, STEP_MILS))
        piece = seg_len / max(1, len(pts) - 1)
        run_start, run_len = None, 0.0
        for x, y in pts:
            if any(ref.covers(l, x, y) for l in refs):
                if run_start is not None and run_len >= MIN_GAP_RUN_MILS:
                    row["runs"].append({"layer": layer, "from": _pt(*run_start),
                                        "to": _pt(x, y), "length_mils": round(run_len, 1),
                                        "reference_layers": refs})
                    row["unreferenced_mils"] += run_len
                run_start, run_len = None, 0.0
            else:
                if run_start is None:
                    run_start, run_len = (x, y), 0.0
                else:
                    run_len += piece
        if run_start is not None and run_len >= MIN_GAP_RUN_MILS:
            row["runs"].append({"layer": layer, "from": _pt(*run_start),
                                "to": _pt(x2, y2), "length_mils": round(run_len, 1),
                                "reference_layers": refs})
            row["unreferenced_mils"] += run_len
    crossing, no_plane = [], []
    for row in per_net.values():
        row["length_mils"] = round(row["length_mils"], 1)
        row["unreferenced_mils"] = round(row["unreferenced_mils"], 1)
        layers = sorted(row.pop("no_plane_layers"))
        if layers:
            no_plane.append({"net": row["net"], "layers": layers,
                             "length_mils": row["length_mils"]})
        if row["runs"]:
            row["runs"].sort(key=lambda r: -r["length_mils"])
            row["runs"] = row["runs"][:5]
            crossing.append(row)
    crossing.sort(key=lambda r: -r["unreferenced_mils"])
    return {
        "pass": not crossing and not no_plane,
        "counts": {"nets": len(per_net), "crossing_gaps": len(crossing),
                   "without_plane": len(no_plane)},
        "crossing_gaps": crossing[:limit],
        "without_plane": no_plane[:limit],
        "reference_layers": sorted(ref.by_layer),
    }


def edge_proximity(board: LayoutBoard, nets: list[str], edge_mils: float = EDGE_MILS,
                   limit: int = 50) -> dict:
    """Fast tracks nearer the board edge or a cutout than ``edge_mils``,
    measured from the copper edge (centre distance minus half width)."""
    if not board.outline or len(board.outline) < 3:
        return {"pass": True, "skipped": "the board has no outline", "findings": []}
    rings = [board.outline] + [c for c in board.cutouts if len(c) >= 3]
    worst: dict[str, dict] = {}
    for net, layer, x1, y1, x2, y2, w in _segments(board, set(nets)):
        for x, y in _samples(x1, y1, x2, y2, max(STEP_MILS, 25.0)):
            d = _edge_distance(x, y, rings) - w / 2
            if d < edge_mils and (net not in worst or d < worst[net]["edge_mils"]):
                worst[net] = {"net": net, "layer": layer, "at": _pt(x, y),
                              "edge_mils": round(d, 1)}
    found = sorted(worst.values(), key=lambda r: r["edge_mils"])
    return {"pass": not found, "min_edge_mils": edge_mils,
            "counts": {"nets_near_edge": len(found)}, "findings": found[:limit]}


def _reference_height(board: LayoutBoard, stack: Stack, ref: _ReferenceCopper,
                      layer: str) -> tuple[Optional[float], bool]:
    """Height to the nearest adjacent layer with return copper, and
    whether that is a real plane (False: the nearest layer of any kind,
    a lower bound on the real loop)."""
    near = _neighbours(board, stack, layer)
    for name in near:
        if ref.has(name):
            return stack.gap_mils(layer, name), True
    for name in near:
        return stack.gap_mils(layer, name), False
    return stack.thickness_mils or None, False


def emission(board: LayoutBoard, stack: Stack, nets: list[str], drives: dict,
             limit_name: str = DEFAULT_LIMIT, ref: Optional[_ReferenceCopper] = None,
             limit: int = 50) -> dict:
    """Differential-mode emission per net with a known drive."""
    returns = return_nets(board)
    ref = ref or _ReferenceCopper(board, returns)
    rows, needs = [], []
    lengths: dict[str, dict[str, float]] = {}
    for net, layer, x1, y1, x2, y2, _w in _segments(board, set(nets) | set(drives)):
        lengths.setdefault(net, {}).setdefault(layer, 0.0)
        lengths[net][layer] += math.hypot(x2 - x1, y2 - y1)
    outer = set(board.copper_layers()[:1] + board.copper_layers()[-1:])
    for net in sorted(set(nets) | set(drives)):
        if net not in drives:
            if net in lengths:
                needs.append(net)
            continue
        drive = drives[net] if isinstance(drives[net], Drive) else Drive.from_dict(drives[net])
        if drive.freq_hz <= 0:
            needs.append(net)
            continue
        area_sq_mils = 0.0
        lower_bound = False
        by_layer = lengths.get(net, {})
        for layer, length in by_layer.items():
            h, real = _reference_height(board, stack, ref, layer)
            if h is None:
                continue
            area_sq_mils += length * h
            lower_bound |= not real
        area_m2 = area_sq_mils * MIL_M * MIL_M
        spec = spectrum(area_m2, drive, limit_name)
        worst = spec["worst"]
        rows.append({
            "net": net,
            "length_mils": round(sum(by_layer.values()), 1),
            "outer_layer_mils": round(sum(v for k, v in by_layer.items() if k in outer), 1),
            "loop_area_mm2": round(area_m2 * 1e6, 3),
            "freq_mhz": round(drive.freq_hz / 1e6, 4),
            "rise_ns": round(drive.rise() * 1e9, 3),
            "worst": worst,
            "pass": worst is None or worst["margin_db"] >= 0,
            "lower_bound": lower_bound,
            "top_harmonics": spec["harmonics"][:5],
        })
    rows.sort(key=lambda r: r["worst"]["margin_db"] if r["worst"] else 1e9)
    return {
        "pass": all(r["pass"] for r in rows),
        "limit": limit_name, "distance_m": LIMITS[limit_name][0],
        "counts": {"estimated": len(rows), "over_limit": sum(not r["pass"] for r in rows),
                   "need_drive": len(needs)},
        "nets": rows[:limit],
        "need_drive": needs[:limit],
        "method": "Ott eq. 12-3 small-loop estimate, trace length x height to "
                  "the reference plane; differential mode only, advisory",
    }


CHECKS = ("reference_gaps", "edge_proximity", "return_vias", "emission")


def run_prescan(board: LayoutBoard, stack: Optional[Stack] = None, *,
                nets: Optional[list[str]] = None, drives: Optional[dict] = None,
                checks: Optional[list[str]] = None, edge_mils: float = EDGE_MILS,
                return_via_mils: float = RETURN_VIA_MILS,
                limit_name: str = DEFAULT_LIMIT, limit: int = 50) -> dict:
    """Run the pre-scan. ``nets`` overrides the fast-net choice; ``drives``
    maps a net to ``{freq_hz | freq_mhz, rise_ns, volts, duty, z_ohms}``."""
    stack = stack or assumed_stack(board.layers)
    drives = dict(drives or {})
    if limit_name not in LIMITS:
        raise ValueError(f"unknown limit {limit_name!r}; known: {sorted(LIMITS)}")
    if nets:
        scope, why = sorted(set(nets)), "the nets given"
    else:
        scope, why = fast_nets(board, extra=drives)
    names = list(checks) if checks else list(CHECKS)
    unknown = [c for c in names if c not in CHECKS]
    if unknown:
        raise ValueError(f"unknown check(s) {unknown}; known: {list(CHECKS)}")
    ref = _ReferenceCopper(board, return_nets(board))
    out: dict[str, dict] = {}
    if "reference_gaps" in names:
        out["reference_gaps"] = reference_gaps(board, stack, scope, ref, limit)
    if "edge_proximity" in names:
        out["edge_proximity"] = edge_proximity(board, scope, edge_mils, limit)
    if "return_vias" in names:
        out["return_vias"] = return_via_audit(board, nets=scope or None,
                                              max_distance_mils=return_via_mils,
                                              limit=limit)
    if "emission" in names:
        out["emission"] = emission(board, stack, scope, drives, limit_name, ref, limit)
    summary = {k: {"pass": v.get("pass", True), **(v.get("counts") or {})}
               for k, v in out.items()}
    return {
        "board": board.name,
        "pass": all(v["pass"] for v in summary.values()),
        "nets_checked": len(scope),
        "net_scope": why,
        "nets": scope[:limit],
        "summary": summary,
        "checks": out,
        "stack": stack.to_dict(),
        "notes": [
            "advisory pre-scan: it finds likely emitters, it does not "
            "replace a chamber scan",
            "antipads round vias are not modelled in the plane-gap check",
        ],
    }
