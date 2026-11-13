# SPDX-License-Identifier: Apache-2.0
"""How a part's pins reach the plane pair, and what that costs.

Each pin gets a *leg*: the way from its pad to the plane of its net.

* **in the plane copper**: the pad sits in the rail's own area copper on a
  layer of the plane pair. No via, no inductance.
* **through-hole pin**: its barrel is the via.
* **via in the pad**, or a via its copper reaches: the vias are found by
  following the net's copper on the pad's layer, pad to track to track to
  via land, the way the current goes. A neighbour's via 30 mil away that no
  copper of this pin leads to is not this pin's via. The fan-out is the
  path's own length and widths, mu0 h l / (w + 2 h) per stretch over the
  reference plane ``h`` below (a microstrip with the fringing of Wheeler's
  wide-strip limit), and Rs l / w.
* **through a pour**: the pad is in a pour of its net on an outer layer and
  the pour's vias take it down. Two round contacts a distance l apart on a
  sheet over a plane: (mu0 h / pi) acosh(l / 2a).
* **nearest via**, when no copper path is found (a board read without its
  tracks): the nearest via that is nearer to this part's pad than to any
  other part's, as a track of the pad's width. Said in the notes.

A via carries half of a via-pair loop from the part's layer down to the
nearer plane, (mu0 h / 2 pi) acosh(s / 2r), s the distance to the part's
nearest via of the other net; the vias of the farther plane also cross
the cavity, mu0 d / (2 pi) ln(r_eq / r) beyond what their grid node holds
(Peaceman). The legs of one net are in parallel, the two nets in series:
N pins on N dog-bones are a 1/N of one, which a per-part count of vias
with one trace in series is not.

Mutual coupling between neighbouring via pairs is left out (it raises the
inductance of a dense array a little; the partial-inductance matrix of the
whole field would be the next step).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Optional

from ..layout import geom
from ..layout.model import LayoutBoard, Pad
from . import physics as ph

#: How far along the copper a capacitor's via may be (mils).
VIA_SEARCH_MILS = 200.0
#: The same for a pin that found nothing nearer (a load fed by a track).
FAR_SEARCH_MILS = 5000.0
_BUCKET = 100.0


@dataclass
class Leg:
    """One way from a part's pin (or pins) down to its plane."""
    x: float
    y: float
    radius: float = 0.0        # barrel radius with plating, mils; 0: no barrel
    hole: float = 0.0
    fan_l: float = 0.0         # H
    fan_r: float = 0.0         # ohm
    fan_mils: float = 0.0
    how: str = "via"
    pads: int = 1


@dataclass
class _Barrel:
    x: float
    y: float
    hole: float
    land: float
    layers: frozenset
    key: tuple


def _project(px, py, ax, ay, bx, by) -> tuple[float, float]:
    """(clamped parameter, distance) of a point against a segment."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 <= 0 else min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return t, math.hypot(px - (ax + dx * t), py - (ay + dy * t))


class Copper:
    """A board's copper of the nets asked for, indexed for leg finding."""

    def __init__(self, board: LayoutBoard, stack, plating_mils: float,
                 rho: float = ph.RHO_CU_20C):
        self.board = board
        self.stack = stack
        self.plating = plating_mils
        self.rho = rho
        self.order = board.copper_layers()
        self._tracks: dict[tuple[str, str], tuple[list, dict]] = {}
        self._barrels: dict[str, tuple[list[_Barrel], dict]] = {}
        self._areas: dict[tuple[str, str], list] = {}
        self._pads: dict[str, object] = {}
        self._plane_net = {l.name: l.plane_net for l in board.layers if l.kind == "plane"}

    # -- indexes -------------------------------------------------------------

    def tracks(self, net: str, layer: str):
        key = (net, layer)
        if key not in self._tracks:
            segs = [(t.x1, t.y1, t.x2, t.y2, t.width) for t in self.board.tracks
                    if t.net == net and t.layer == layer and not t.keepout and t.width > 0
                    and (t.x1, t.y1) != (t.x2, t.y2)]
            for a in self.board.arcs:
                if a.net == net and a.layer == layer and not a.keepout and a.width > 0:
                    pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, tol=0.5)
                    segs += [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], a.width)
                             for i in range(len(pts) - 1)]
            grid: dict[tuple[int, int], list[int]] = {}
            for i, (x1, y1, x2, y2, w) in enumerate(segs):
                for gx in range(int(math.floor((min(x1, x2) - w) / _BUCKET)),
                                int(math.floor((max(x1, x2) + w) / _BUCKET)) + 1):
                    for gy in range(int(math.floor((min(y1, y2) - w) / _BUCKET)),
                                    int(math.floor((max(y1, y2) + w) / _BUCKET)) + 1):
                        grid.setdefault((gx, gy), []).append(i)
            self._tracks[key] = (segs, grid)
        return self._tracks[key]

    def barrels(self, net: str):
        if net not in self._barrels:
            seen: dict[tuple, _Barrel] = {}
            for v in self.board.vias:
                if v.net != net or v.hole <= 0:
                    continue
                key = (round(v.x, 1), round(v.y, 1))
                layers = frozenset(self.board.layers_between(v.low_layer, v.high_layer))
                if key in seen:           # stacked or duplicate: one barrel, both spans
                    b = seen[key]
                    seen[key] = _Barrel(b.x, b.y, max(b.hole, v.hole), max(b.land, v.diameter),
                                        b.layers | layers, key)
                else:
                    seen[key] = _Barrel(v.x, v.y, v.hole, max(v.diameter, v.hole), layers, key)
            for p in self.board.pads:
                if p.net == net and p.hole > 0 and p.plated:
                    key = (round(p.x, 1), round(p.y, 1))
                    land = max([max(c.w, c.h) for c in p.copper] or [p.hole])
                    seen.setdefault(key, _Barrel(p.x, p.y, p.hole, land,
                                                 frozenset(self.order), key))
            items = list(seen.values())
            grid: dict[tuple[int, int], list[int]] = {}
            for i, b in enumerate(items):
                grid.setdefault((int(math.floor(b.x / _BUCKET)),
                                 int(math.floor(b.y / _BUCKET))), []).append(i)
            self._barrels[net] = (items, grid)
        return self._barrels[net]

    def _near(self, grid: dict, x: float, y: float, reach: float) -> list[int]:
        out: set[int] = set()
        for gx in range(int(math.floor((x - reach) / _BUCKET)),
                        int(math.floor((x + reach) / _BUCKET)) + 1):
            for gy in range(int(math.floor((y - reach) / _BUCKET)),
                            int(math.floor((y + reach) / _BUCKET)) + 1):
                out.update(grid.get((gx, gy), ()))
        return sorted(out)

    def in_area(self, net: str, layer: str, x: float, y: float) -> bool:
        """Is the point in the net's area copper (pour, fill, plane) on the layer?"""
        if layer in self._plane_net:
            regs = [r for r in self.board.regions if r.layer == layer and not r.keepout
                    and r.kind in ("copper", "pour")]
            if not regs:
                return self._plane_net[layer] == net
            return any(r.net == net and geom.point_in_poly(x, y, r.shape()) for r in regs)
        key = (net, layer)
        if key not in self._areas:
            self._areas[key] = [r.shape() for r in self.board.regions
                                if r.net == net and r.layer == layer and not r.keepout
                                and r.kind in ("copper", "pour")]
        return any(sh.bbox[0] <= x <= sh.bbox[2] and sh.bbox[1] <= y <= sh.bbox[3]
                   and geom.point_in_poly(x, y, sh) for sh in self._areas[key])

    def owner(self, net: str, x: float, y: float) -> str:
        """Designator of the part whose pad on ``net`` is nearest the point."""
        from scipy.spatial import cKDTree
        if net not in self._pads:
            pads = [p for p in self.board.pads if p.net == net and p.comp]
            tree = cKDTree([(p.x, p.y) for p in pads]) if pads else None
            self._pads[net] = (tree, pads)
        tree, pads = self._pads[net]
        if tree is None:
            return ""
        return pads[int(tree.query((x, y))[1])].comp

    # -- stack ---------------------------------------------------------------

    def z(self, layer: str) -> Optional[float]:
        cl = self.stack.get(layer) if self.stack is not None else None
        return cl.z_mils if cl is not None else None

    def ref_height(self, layer: str) -> float:
        """Distance from a layer to the copper layer nearest it (mils)."""
        if self.stack is not None and self.stack.get(layer) is not None:
            nb = self.stack.neighbours(layer)
            if nb:
                return abs(self.stack.get(layer).z_mils - nb[0].z_mils) or 8.0
        return 8.0

    def sheet_r(self, layer: str) -> float:
        cl = self.stack.get(layer) if self.stack is not None else None
        return ph.sheet_resistance(cl.copper_mils if cl is not None and cl.copper_mils > 0
                                   else 1.4, self.rho)

    # -- legs ----------------------------------------------------------------

    def pad_layer(self, pad: Pad) -> str:
        if pad.hole > 0:
            return ""
        ls = [l for l in pad.layers() if l in self.order]
        return ls[0] if ls else (self.order[0] if self.order else "")

    def _strip(self, layer: str, mils: float, width: float) -> tuple[float, float]:
        """(H, ohm) of a stretch of track over its reference plane."""
        if mils <= 0:
            return 0.0, 0.0
        h = self.ref_height(layer)
        return (ph.MU0 * h * mils * ph.MIL / (width + 2 * h),
                self.sheet_r(layer) * mils / max(width, 1e-6))

    def follow(self, net: str, pad: Pad, layer: str, need: frozenset,
               reach: float) -> list[tuple[_Barrel, float, float, float]]:
        """Barrels the pad's copper leads to on ``layer``, each with the
        (H, ohm, mils) of the cheapest way there. ``need``: layers the
        barrel must join."""
        shape = pad.shape_on(layer)
        if shape is None:
            return []
        items, bgrid = self.barrels(net)
        cand = [items[i] for i in self._near(bgrid, pad.x, pad.y, reach + 50.0)
                if need <= items[i].layers]
        found: dict[tuple, tuple[float, float, float, _Barrel]] = {}

        def reach_barrel(b: _Barrel, l_h: float, r_ohm: float, mils: float) -> None:
            if b.key not in found or l_h < found[b.key][0]:
                found[b.key] = (l_h, r_ohm, mils, b)
        for b in cand:                                   # via in the pad, or touching it
            if geom.clearance(geom.circle(b.x, b.y, b.land), shape) <= 0.0:
                reach_barrel(b, 0.0, 0.0, 0.0)
        segs, tgrid = self.tracks(net, layer)
        local = self._near(tgrid, pad.x, pad.y, reach)
        if not local:
            return [(b, l, r, m) for (l, r, m, b) in found.values()]
        caps = {i: geom.capsule(*segs[i][:4], segs[i][4]) for i in local}
        length = {i: math.hypot(segs[i][2] - segs[i][0], segs[i][3] - segs[i][1]) for i in local}
        # state: (H so far, mils so far, track, entry parameter, still in the pad, ohm)
        heap: list = []
        best: dict[int, float] = {}
        for i in local:
            if geom.clearance(caps[i], shape) <= 0.0:
                t, _ = _project(pad.x, pad.y, *segs[i][:4])
                heapq.heappush(heap, (0.0, 0.0, i, t, True, 0.0))

        def in_pad(i: int, t: float) -> bool:
            x1, y1, x2, y2, _ = segs[i]
            return geom.clearance(geom.circle(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, 0.0),
                                  shape) <= 0.0
        near_of: dict[int, list[int]] = {}

        def neighbours(i: int) -> list[int]:
            if i not in near_of:
                x1, y1, x2, y2, w = segs[i]
                near_of[i] = [j for j in local if j != i
                              and geom.bboxes_near(caps[i], caps[j], 0.0)
                              and geom.clearance(caps[i], caps[j]) <= 0.0]
            return near_of[i]

        def joint(i: int, j: int) -> tuple[float, float]:
            """Parameters on i and j of the point where they meet."""
            xi1, yi1, xi2, yi2, _ = segs[i]
            xj1, yj1, xj2, yj2, _ = segs[j]
            den = (xi2 - xi1) * (yj2 - yj1) - (yi2 - yi1) * (xj2 - xj1)
            if abs(den) > 1e-12:
                qx, qy = xj1 - xi1, yj1 - yi1
                ti = (qx * (yj2 - yj1) - qy * (xj2 - xj1)) / den
                tj = (qx * (yi2 - yi1) - qy * (xi2 - xi1)) / den
                if 0.0 <= ti <= 1.0 and 0.0 <= tj <= 1.0:
                    return ti, tj
            opts = []
            for tj, (px, py) in ((0.0, (xj1, yj1)), (1.0, (xj2, yj2))):
                ti, d = _project(px, py, xi1, yi1, xi2, yi2)
                opts.append((d, ti, tj))
            for ti, (px, py) in ((0.0, (xi1, yi1)), (1.0, (xi2, yi2))):
                tj, d = _project(px, py, xj1, yj1, xj2, yj2)
                opts.append((d, ti, tj))
            _, ti, tj = min(opts)
            return ti, tj
        while heap:
            l_h, mils, i, t_in, skip, r_ohm = heapq.heappop(heap)
            if i in best and best[i] <= l_h:
                continue
            best[i] = l_h
            w = segs[i][4]

            def leg(t_out: float, trim: float = 0.0):
                """Cost of running from the entry to ``t_out`` on this track:
                the stretch inside the pad is the pad's, not a track, and
                ``trim`` (a land's radius) is left to the land."""
                run = abs(t_out - t_in) * length[i]
                inside, still = 0.0, False
                if skip:
                    if in_pad(i, t_out):
                        inside, still = run, True
                    else:                               # where it leaves the pad
                        lo_, hi_ = t_in, t_out
                        for _ in range(24):
                            mid = (lo_ + hi_) / 2
                            if in_pad(i, mid):
                                lo_ = mid
                            else:
                                hi_ = mid
                        inside = abs(lo_ - t_in) * length[i]
                eff = max(0.0, run - inside - trim)
                dl, dr = self._strip(layer, eff, w)
                return l_h + dl, r_ohm + dr, mils + run, still
            for b in cand:
                if geom.clearance(geom.circle(b.x, b.y, b.land), caps[i]) <= 0.0:
                    t_v, _ = _project(b.x, b.y, *segs[i][:4])
                    l2, r2, m2, _ = leg(t_v, trim=b.land / 2.0)
                    # across the land to the barrel wall, as wide as the land
                    dl, dr = self._strip(layer, max(0.0, b.land / 2.0 - b.hole / 2.0
                                                    - self.plating), b.land)
                    if m2 <= reach:
                        reach_barrel(b, l2 + dl, r2 + dr, m2)
            for j in neighbours(i):
                ti, tj = joint(i, j)
                l2, r2, m2, skip2 = leg(ti)
                if m2 <= reach and (j not in best or l2 < best[j]):
                    heapq.heappush(heap, (l2, m2, j, tj, skip2, r2))
        return [(b, l, r, m) for (l, r, m, b) in found.values()]

    def legs(self, net: str, pads: list[Pad], plane_layer: str,
             reach: float = VIA_SEARCH_MILS) -> tuple[list[Leg], list[str]]:
        """The legs of a part's pads on one net, and what had to be assumed."""
        notes: list[str] = []
        by_key: dict[tuple, Leg] = {}
        ref = next((p.comp for p in pads if p.comp), "")

        def add(key, x, y, hole, l_h, r_ohm, mils, how) -> None:
            radius = hole / 2.0 + self.plating if hole > 0 else 0.0
            if key in by_key:
                g = by_key[key]
                # another pad on the same barrel: its fan-out is in parallel
                if g.fan_l > 0 and l_h > 0:
                    g.fan_l = 1.0 / (1.0 / g.fan_l + 1.0 / l_h)
                else:
                    g.fan_l = 0.0
                if g.fan_r > 0 and r_ohm > 0:
                    g.fan_r = 1.0 / (1.0 / g.fan_r + 1.0 / r_ohm)
                else:
                    g.fan_r = 0.0
                g.fan_mils = min(g.fan_mils, mils)
                g.pads += 1
            else:
                by_key[key] = Leg(x, y, radius, hole, l_h, r_ohm, mils, how)
        for p in pads:
            if p.hole > 0 and p.plated:
                add(("b", round(p.x, 1), round(p.y, 1)), p.x, p.y, p.hole, 0.0, 0.0, 0.0,
                    "through-hole pin")
                continue
            layer = self.pad_layer(p)
            if plane_layer and layer == plane_layer and self.in_area(net, layer, p.x, p.y):
                add(("p", p.name, round(p.x, 1), round(p.y, 1)), p.x, p.y, 0.0, 0.0, 0.0, 0.0,
                    "in the plane copper")
                continue
            need = frozenset(l for l in (layer, plane_layer) if l)
            got = self.follow(net, p, layer, need, reach)
            how = "track"
            if not got and self.in_area(net, layer, p.x, p.y):
                got = self._through_pour(net, p, layer, need, reach)
                how = "through a pour"
            if not got and reach < FAR_SEARCH_MILS:
                got = self.follow(net, p, layer, need, FAR_SEARCH_MILS)
                if got:                      # the cheapest only: a long feed
                    got = [min(got, key=lambda g: g[1])]
            if not got:
                got = self._nearest(net, p, layer, need, reach, ref)
                how = "nearest via"
                if got:
                    notes.append(f"{p.comp}.{p.name}: no copper path to a via found; "
                                 f"the nearest {net} via ({got[0][3]:.0f} mil) is taken")
            if not got:
                notes.append(f"{p.comp}.{p.name}: no {net} via found")
                continue
            for b, l_h, r_ohm, mils in got:
                add(("b",) + b.key, b.x, b.y, b.hole, l_h, r_ohm, mils,
                    "via in pad" if mils <= 0 and how == "track" else how)
        return list(by_key.values()), notes

    def _through_pour(self, net, pad, layer, need, reach):
        items, bgrid = self.barrels(net)
        half = min([min(c.w, c.h) for c in pad.copper if c.layer == layer] or [10.0]) / 2.0
        out = []
        for i in self._near(bgrid, pad.x, pad.y, reach):
            b = items[i]
            if not (need <= b.layers) or not self.in_area(net, layer, b.x, b.y):
                continue
            d = math.hypot(b.x - pad.x, b.y - pad.y)
            if d <= reach:
                out.append((d, b))
        out.sort(key=lambda t: t[0])
        got = []
        h = self.ref_height(layer)
        for d, b in out:
            if d > out[0][0] + 30.0:
                break
            a = math.sqrt(max(half, 1.0) * (b.hole / 2.0 + self.plating))
            sq = math.acosh(max(1.0, d / (2 * a))) / math.pi          # squares between them
            got.append((b, ph.MU0 * h * ph.MIL * sq, self.sheet_r(layer) * sq, d))
        return got

    def _nearest(self, net, pad, layer, need, reach, ref):
        items, bgrid = self.barrels(net)
        out = []
        for i in self._near(bgrid, pad.x, pad.y, reach):
            b = items[i]
            d = math.hypot(b.x - pad.x, b.y - pad.y)
            if d <= reach and need <= b.layers and self.owner(net, b.x, b.y) in ("", ref):
                out.append((d, b))
        if not out:
            return []
        d, b = min(out, key=lambda t: t[0])
        sizes = [c for c in pad.copper if c.layer == layer]
        half = max([max(c.w, c.h) for c in sizes] or [0.0]) / 2.0
        width = min([min(c.w, c.h) for c in sizes] or [10.0])
        l_h, r_ohm = self._strip(layer, max(0.0, d - half - b.land / 2.0), width)
        return [(b, l_h, r_ohm, d)]


def _parallel(values: list[float]) -> float:
    if not values or any(v <= 0 for v in values):
        return 0.0
    return 1.0 / sum(1.0 / v for v in values)


def mount(copper: Copper, cav, net: str, ground: str, pwr_pads: list[Pad],
          gnd_pads: list[Pad], reach: float = VIA_SEARCH_MILS) -> dict:
    """Inductance and resistance between a part's pads and the plane pair
    (``cav``; None: down to the nearest copper layer, for the lumped model).

    Returns a dict with ``_l`` (H), ``_r`` (ohm), ``_at`` (where the power
    legs meet the plane, [(x, y, pads)]) and the breakdown that is shown."""
    if not pwr_pads or not gnd_pads:
        return {"_l": 1e-9, "_r": 0.0, "_at": [], "total_nh": 1.0, "assumed": True,
                "note": "pads on both nets not found; 1 nH assumed"}
    part_layer = next((copper.pad_layer(p) for p in pwr_pads + gnd_pads if p.hole <= 0), "")
    if not part_layer:
        part_layer = copper.order[0] if copper.order else ""
    zc = copper.z(part_layer)
    if cav is not None and zc is not None:
        zp, zg = cav.z_power, cav.z_ground
        near_is_power = abs(zc - zp) <= abs(zc - zg)
        h = abs(zc - (zp if near_is_power else zg))
        d = cav.d_mils
        cell = cav.cell_mils
        planes = {net: cav.power_layer, ground: cav.ground_layer}
    else:
        near_is_power = True
        h = copper.ref_height(part_layer)
        d, cell = 0.0, 50.0
        planes = {net: "", ground: ""}
    legs_p, notes_p = copper.legs(net, pwr_pads, planes[net], reach)
    legs_g, notes_g = copper.legs(ground, gnd_pads, planes[ground], reach)
    notes = notes_p + notes_g
    if not legs_p or not legs_g:
        side = "power" if not legs_p else "ground"
        return {"_l": 1e-9, "_r": 0.0, "_at": [], "total_nh": 1.0, "assumed": True,
                "note": f"no {side} via reached within {reach:g} mil of the pads; "
                        f"1 nH assumed", "notes": notes}

    def side(legs: list[Leg], others: list[Leg], crosses: bool, far_mils: float):
        ls, rs, parts = [], [], []
        for g in legs:
            if g.radius <= 0:                       # in the plane copper
                ls.append(0.0); rs.append(0.0); parts.append((0.0, 0.0, 0.0))
                continue
            s = min(math.hypot(g.x - o.x, g.y - o.y) for o in others)
            lv = 0.5 * ph.via_pair_loop_inductance(h, max(s, 2.0001 * g.radius), g.radius)
            lc = ph.cavity_port_inductance(d, g.radius, cell) if crosses and d > 0 else 0.0
            run = h + (far_mils if crosses else 0.0)
            rb = ph.via_barrel_resistance(run, g.hole, copper.plating, copper.rho) if run > 0 else 0.0
            ls.append(g.fan_l + lv + lc)
            rs.append(g.fan_r + rb)
            parts.append((lv, g.fan_l, lc))
        l_side = _parallel(ls)
        # the share of each part of the legs in the parallel sum: with the
        # current split as 1 / L, L = sum (L / L_i)^2 L_i
        split = [0.0, 0.0, 0.0]
        if l_side > 0:
            for li, pt in zip(ls, parts):
                wgt = (l_side / li) ** 2
                for k in range(3):
                    split[k] += wgt * pt[k]
        return l_side, _parallel(rs), split
    lp, rp, sp_ = side(legs_p, legs_g, crosses=not near_is_power, far_mils=d)
    lg, rg, sg_ = side(legs_g, legs_p, crosses=near_is_power, far_mils=d)
    total, r_tot = lp + lg, rp + rg
    out = {"_l": total, "_r": r_tot,
           "_at": [(g.x, g.y, g.pads) for g in legs_p],
           "_at_ground": [(g.x, g.y, g.pads) for g in legs_g],
           "total_nh": round(total * 1e9, 4),
           "vias_nh": round((sp_[0] + sg_[0]) * 1e9, 4),
           "traces_nh": round((sp_[1] + sg_[1]) * 1e9, 4),
           "cavity_nh": round((sp_[2] + sg_[2]) * 1e9, 4),
           "resistance_mohm": round(r_tot * 1e3, 4),
           "power_legs": len(legs_p), "ground_legs": len(legs_g),
           "via_pairs": min(sum(1 for g in legs_p if g.radius > 0) or len(legs_p),
                            sum(1 for g in legs_g if g.radius > 0) or len(legs_g)),
           "height_mils": round(h, 2),
           "how": sorted({g.how for g in legs_p + legs_g}),
           "fanout_mils": [round(min(g.fan_mils for g in legs_p), 1),
                           round(min(g.fan_mils for g in legs_g), 1)]}
    if len(legs_p) == 1 and len(legs_g) == 1 and legs_p[0].radius > 0 and legs_g[0].radius > 0:
        out["via_spacing_mils"] = round(math.hypot(legs_p[0].x - legs_g[0].x,
                                                   legs_p[0].y - legs_g[0].y), 2)
    if notes:
        out["notes"] = notes
    return out
