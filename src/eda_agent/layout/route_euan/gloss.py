# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Gloss: re-lay the bends of finished tracks as short, clean 45/90 runs.

Owner review of v0 (2026-10-18): after the pad-entry fixes some lines still
turned back on themselves (a 45 degree spike past a pin, a hook into a pad
from the far side) or hugged a pad's long side to reach a via just past its
end. Each pass that bends a line's ends (entry repair, snapping, chamfers)
left the rest of the line as it was, so nothing re-simplified it.

This works on finished copper, any board, one net and layer at a time:

* chains: the runs of one net's tracks on one layer between fixed points
  (a pad centre, a via, a junction of three or more tracks, a width change,
  a point inside one of the net's pads);
* each end at a pad centre keeps an entry stub, straight out of the pad
  along one of its axes and centred, until the track is clear of the pad.
  An IC pin is left only along its long axis, away from its part; a
  two-pin part or a square pad may be left through any edge;
* between the stubs the chain is replaced by the shortest octilinear run
  (straight; straight then 45; 45 then straight; straight-45-straight) that
  turns no more than 90 degrees at any joint and clears every other net's
  copper by the clearance, exactly;
* a chain that cannot be replaced whole is shortened piecewise: any stretch
  between two of its points that one of those runs can replace shorter.

Nothing is changed unless it is shorter, or as long with fewer bends, and
never so that a joint turns back more sharply than before.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .. import geom
from ..model import LayoutBoard, Pad, Track

EPS = 1e-6
OFFSET_COST = 8.0       # mil a run must save to leave a pad off its centre
SLANT_COST = 25.0       # mil an end leaving its pad slantwise counts against a run


@dataclass
class GlossResult:
    removed: list[Track]
    added: list[Track]
    chains_changed: int
    chains_seen: int


class _Index:
    """Other-net copper per layer on a coarse grid, with removal."""

    def __init__(self, cell: float = 40.0):
        self.cell = cell
        self.items: list = []           # (layer or None, net, shape, alive)
        self.grid: dict = defaultdict(list)

    def _keys(self, layer, bb, pad):
        x0, y0, x1, y1 = bb
        c = self.cell
        for i in range(int(math.floor((x0 - pad) / c)), int(math.floor((x1 + pad) / c)) + 1):
            for j in range(int(math.floor((y0 - pad) / c)), int(math.floor((y1 + pad) / c)) + 1):
                yield (layer, i, j)

    def add(self, layer, net, shape, gap, kind: str = "") -> int:
        k = len(self.items)
        self.items.append([layer, net, shape, True, gap, kind])
        for key in self._keys(layer, shape.bbox, 0.0):
            self.grid[key].append(k)
        return k

    def kill(self, k: int) -> None:
        self.items[k][3] = False

    def clears(self, layer, net, shape, gap) -> bool:
        seen = set()
        for lay in (layer, None):
            for key in self._keys(lay, shape.bbox, gap + 30.0):
                for k in self.grid.get(key, ()):
                    if k in seen:
                        continue
                    seen.add(k)
                    _, n, sh, alive, g, _kind = self.items[k]
                    if not alive or n == net:
                        continue
                    need = max(gap, g)
                    if not geom.bboxes_near(shape, sh, need):
                        continue
                    if geom.clearance(shape, sh) < need - 1e-4:
                        return False
        return True


def _touching(idx, layer, net, shape, skip=()):
    """The net's own copper touching ``shape`` (pads, vias, other tracks),
    so a re-laid run can be held to every join it had: (id, None) for a pad
    or via, (id, end point) for a track whose end lies on the run (it has to
    stay on the new run, or its end would hang free: a net antenna), and
    (id, "x") for a track that crosses it."""
    out, seen = set(), set()
    for lay in (layer, None):
        for key in idx._keys(lay, shape.bbox, 1.0):
            for k in idx.grid.get(key, ()):
                if k in seen or k in skip:
                    continue
                seen.add(k)
                _, n, sh, alive, _g, kind = idx.items[k]
                if not (alive and n == net and geom.bboxes_near(shape, sh, 0.05)
                        and geom.clearance(shape, sh) <= 0.01):
                    continue
                if kind != "track":
                    out.add((k, None))
                    continue
                ends = [p for p in sh.pts if geom.clearance(geom.circle(p[0], p[1], 0.0), shape) <= 0.01]
                if ends:
                    out.update((k, (round(p[0], 3), round(p[1], 3))) for p in ends)
                else:
                    out.add((k, "x"))
    return out


def _only_on(idx, layer, net, t, own_id, host) -> bool:
    """Whether everything of the net touching track ``t`` also touches its
    host pad: then the pad joins it all and the track can go. A wide track
    on one pin that also covers the next pin (the eFuse's input pins on v0)
    is that pin's join, and stays."""
    pd, sh = host
    cap = geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width)
    for k, how in _touching(idx, layer, net, cap, {own_id}):
        kind = idx.items[k][5]
        if kind == f"pad:{id(pd)}":
            continue
        if isinstance(how, tuple):
            if geom.clearance(geom.circle(how[0], how[1], 0.0), sh) <= 0.0:
                continue
            return False
        if how == "x" or kind.startswith("pad:"):
            return False
        if geom.clearance(idx.items[k][2], sh) > 0.0:       # a via off the pad
            return False
    return True


def _key(p):
    return (round(p[0], 3), round(p[1], 3))


def _dir(a, b):
    d = math.dist(a, b)
    return None if d < EPS else ((b[0] - a[0]) / d, (b[1] - a[1]) / d)


def _octilinear(a, b) -> bool:
    if math.dist(a, b) < 0.05:
        return True
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
    return min(ang, 45.0 - ang) <= 0.05


def _length(pts) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _turns(pts):
    """Angle (degrees) the line turns at each inner point."""
    out = []
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        u, v = _dir(a, b), _dir(b, c)
        if u is None or v is None:
            continue
        out.append(math.degrees(math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1])))))
    return out


def _connectors(a, b):
    """Octilinear runs from a to b: straight; straight then 45; 45 then
    straight; straight-45-straight with the straight halves either side."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if _octilinear(a, b):
        return [[a, b]]
    ax, ay = abs(dx), abs(dy)
    sx, sy = math.copysign(1.0, dx), math.copysign(1.0, dy)
    m = min(ax, ay)
    out = []
    if ax >= ay:
        out.append([a, (b[0] - sx * m, a[1]), b])            # straight, then 45
        out.append([a, (a[0] + sx * m, b[1]), b])            # 45, then straight
        h = (ax - m) / 2
        out.append([a, (a[0] + sx * h, a[1]), (a[0] + sx * (h + m), b[1]), b])
    else:
        out.append([a, (a[0], b[1] - sy * m), b])
        out.append([a, (b[0], a[1] + sy * m), b])
        h = (ay - m) / 2
        out.append([a, (a[0], a[1] + sy * h), (b[0], a[1] + sy * (h + m)), b])
    return out


def _pad_axes(pad: Pad, layer: str):
    sh = pad.shape_on(layer)
    if sh is None:
        return None
    if sh.kind == "poly" and len(sh.pts) == 4:
        p0, p1, _, p3 = sh.pts
        la, lb = math.dist(p0, p1), math.dist(p0, p3)
        if la == 0 or lb == 0:
            return None
        ua = ((p1[0] - p0[0]) / la, (p1[1] - p0[1]) / la)
        ub = ((p3[0] - p0[0]) / lb, (p3[1] - p0[1]) / lb)
        a, b = la / 2 + sh.r, lb / 2 + sh.r
    elif sh.kind == "segment":
        (x0, y0), (x1, y1) = sh.pts[0], sh.pts[-1]
        ll = math.hypot(x1 - x0, y1 - y0)
        if ll == 0:
            return None
        ua = ((x1 - x0) / ll, (y1 - y0) / ll)
        ub = (-ua[1], ua[0])
        a, b = ll / 2 + sh.r, sh.r
    else:
        r_ = math.radians(pad.rotation or 0.0)
        ua, ub = (math.cos(r_), math.sin(r_)), (-math.sin(r_), math.cos(r_))
        a = b = sh.r
    return (ua, a, ub, b) if a >= b else (ub, b, ua, a)


def gloss_board(board: LayoutBoard, clearance: float = 5.0, region_clearance: float = 8.0,
                nets=None, layers=None, keep_regions=None, margin: float = 2.0, first: int = 0) -> GlossResult:
    """Gloss every chain of the board's tracks (``nets`` / ``layers`` to
    narrow it). ``keep_regions``: extra fixed copper as (layer, net,
    outline), e.g. pours that will be re-poured but keep their outline.
    ``first``: only tracks from this index on are re-laid (a router's own
    output); earlier tracks stay, and every join to them is kept.
    Returns the tracks removed and added; ``board.tracks`` is updated."""
    idx = _Index()
    comp_pads: dict = defaultdict(int)
    for p in board.pads:
        if p.comp:
            comp_pads[p.comp] += 1
    comp_xy = {c.ref: (c.x, c.y) for c in getattr(board, "components", [])}
    for p in board.pads:
        lays = [c.layer for c in p.copper] if p.is_smd else [None]
        for lay in lays:
            sh = p.shape_on(lay if lay else p.copper[0].layer)
            if sh is not None:
                idx.add(lay, p.net, sh, clearance, f"pad:{id(p)}")
    for v in board.vias:
        idx.add(None, v.net, geom.circle(v.x, v.y, v.diameter), clearance)
    for r in board.regions:
        if r.kind == "copper" and r.net and not r.keepout:
            idx.add(r.layer, r.net, geom.polygon(r.outline), region_clearance)
    for lay, net, outline in keep_regions or ():
        idx.add(lay, net, geom.polygon(outline), region_clearance)
    tid = {}
    for i, t in enumerate(board.tracks):
        if t.net and not t.comp and not t.keepout:
            tid[i] = idx.add(t.layer, t.net, geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width), clearance, "track")

    pads_by_net: dict = defaultdict(list)
    for p in board.pads:
        pads_by_net[p.net].append(p)
    centre = {}
    for p in board.pads:
        centre[(p.net, _key((p.x, p.y)))] = p
    viapts = {(v.net, _key((v.x, v.y))) for v in board.vias}

    removed, added = [], []
    changed = seen = 0
    groups: dict = defaultdict(list)
    for i, t in enumerate(board.tracks):
        if i < first or not t.net or t.comp or t.keepout:
            continue
        if nets is not None and t.net not in nets:
            continue
        if layers is not None and t.layer not in layers:
            continue
        groups[(t.net, t.layer)].append(i)

    dead = set()
    new_tracks: list[Track] = []
    # a track with both ends on one pad of its net is pad copper already: it
    # only thickens the pad's edge (a run along a pad's side, owner review 5)
    # and its ends stay joined through the pad, so it goes
    for (net, layer), ids in groups.items():
        own_sh = []
        for pd in pads_by_net.get(net, ()):
            sh = pd.shape_on(layer) if pd.is_smd else pd.shape_on(pd.copper[0].layer)
            if sh is not None:
                own_sh.append((pd, sh))
        keep_ids = []
        for i in ids:
            t = board.tracks[i]
            a, c = geom.circle(t.x1, t.y1, 0.0), geom.circle(t.x2, t.y2, 0.0)
            host = next(((pd, sh) for pd, sh in own_sh
                         if geom.clearance(a, sh) <= 0.0 and geom.clearance(c, sh) <= 0.0), None)
            if host is not None and _only_on(idx, layer, net, t, tid[i], host):
                dead.add(i)
                idx.kill(tid[i])
                removed.append(t)
            else:
                keep_ids.append(i)
        ids[:] = keep_ids
    for (net, layer), ids in groups.items():
        ends = defaultdict(list)
        for i in ids:
            t = board.tracks[i]
            ends[_key((t.x1, t.y1))].append(i)
            ends[_key((t.x2, t.y2))].append(i)
        own = [p for p in pads_by_net.get(net, ()) if p.shape_on(layer) is not None or not p.is_smd]

        def in_own_pad(pt, own=own, layer=layer):
            dot = geom.circle(pt[0], pt[1], 0.0)
            for p in own:
                sh = p.shape_on(layer) if p.is_smd else p.shape_on(p.copper[0].layer)
                if sh is not None and geom.clearance(dot, sh) <= 0.0:
                    return True
            return False

        def fixed(k, ends=ends, net=net):
            if len(ends[k]) != 2 or (net, k) in viapts or (net, k) in centre:
                return True
            a, b = (board.tracks[i] for i in ends[k])
            if abs(a.width - b.width) > 1e-6:
                return True
            return in_own_pad(k)

        def root_of(J, own, ends=ends, net=net, pads_=own, layer=layer):
            """A junction just outside a pad, joined to the pad's centre by one
            short stub that its branches share (one stub out of a pad's long
            side feeding two runs): the pad's centre, so a branch can be
            re-laid from the pad itself. None otherwise."""
            if (net, J) in viapts or (net, J) in centre:
                return None
            # an end on a pad's copper but off its centre (a track started on
            # the pad's edge or corner): the pad's centre, so the run can be
            # re-laid with a proper entry (owner review 5: tracks leaving along
            # a pad's edge, from a corner, tangent to a round pad)
            dot = geom.circle(J[0], J[1], 0.0)
            inside = [pd for pd in pads_ if (pd.shape_on(layer) if pd.is_smd else pd.shape_on(pd.copper[0].layer)) is not None
                      and geom.clearance(dot, pd.shape_on(layer) if pd.is_smd else pd.shape_on(pd.copper[0].layer)) <= 0.0]
            if len(inside) == 1:
                return _key((inside[0].x, inside[0].y))
            if len(ends[J]) < 3:
                return None
            found = []
            for i in ends[J]:
                if i == own:
                    continue
                t = board.tracks[i]
                o = _key((t.x2, t.y2)) if _key((t.x1, t.y1)) == J else _key((t.x1, t.y1))
                pad = centre.get((net, o))
                if pad is None:
                    continue
                reach = max(max(c.w, c.h) for c in pad.copper) / 2 + t.width + 8.0
                if math.dist(o, J) <= reach:
                    found.append(o)
            return found[0] if len(found) == 1 else None

        used = set()
        for start in list(ends):
            if not fixed(start):
                continue
            for i0 in list(ends[start]):
                if i0 in used:
                    continue
                pts, chain, cur, i = [start], [], start, i0
                while True:
                    t = board.tracks[i]
                    a, b = _key((t.x1, t.y1)), _key((t.x2, t.y2))
                    nxt = b if a == cur else a
                    pts.append(nxt); chain.append(i); used.add(i); cur = nxt
                    if fixed(nxt):
                        break
                    nb = [j for j in ends[nxt] if j not in used]
                    if len(nb) != 1:
                        break
                    i = nb[0]
                seen += 1
                if len(pts) < 3:
                    continue
                w = board.tracks[chain[0]].width
                roots = [root_of(pts[0], chain[0]), root_of(pts[-1], chain[-1])]
                new = _gloss_chain(pts, w, net, layer, idx, centre, comp_pads, comp_xy, clearance, margin, viapts,
                                   {tid[i] for i in chain}, roots)
                if new is None:
                    continue
                changed += 1
                for i in chain:
                    dead.add(i)
                    idx.kill(tid[i])
                    removed.append(board.tracks[i])
                for p_, q_ in zip(new, new[1:]):
                    if math.dist(p_, q_) < 0.01:
                        continue
                    nt = Track(layer, p_[0], p_[1], q_[0], q_[1], w, net)
                    new_tracks.append(nt)
                    added.append(nt)
                    idx.add(layer, net, geom.capsule(p_[0], p_[1], q_[0], q_[1], w), clearance, "track")
        # chains with no fixed end (closed loops) are left alone
    board.tracks[:] = [t for i, t in enumerate(board.tracks) if i not in dead] + new_tracks
    # a stub left joined to nothing (its branches all re-laid from the pad)
    pad_c = {(p.net, _key((p.x, p.y))) for p in board.pads}
    while True:
        drop = None
        for t in board.tracks:
            if (t.net, t.layer) not in groups:
                continue
            a, b = _key((t.x1, t.y1)), _key((t.x2, t.y2))
            for at_pad, free in ((a, b), (b, a)):
                if (t.net, at_pad) not in pad_c or (t.net, free) in viapts or (t.net, free) in pad_c:
                    continue
                dot = geom.circle(free[0], free[1], 0.0)
                hit = False
                for u in board.tracks:
                    if u is t or u.net != t.net or u.layer != t.layer:
                        continue
                    if geom.clearance(dot, geom.capsule(u.x1, u.y1, u.x2, u.y2, u.width)) <= 0.01:
                        hit = True
                        break
                if not hit:
                    for pd in pads_by_net.get(t.net, ()):
                        sh = pd.shape_on(t.layer) if pd.is_smd else pd.shape_on(pd.copper[0].layer)
                        if sh is not None and geom.clearance(dot, sh) <= 0.01:
                            hit = True
                            break
                if not hit:
                    drop = t
                    break
            if drop is not None:
                break
        if drop is None:
            break
        board.tracks.remove(drop)
        if drop in added:
            added.remove(drop)
        else:
            removed.append(drop)
    return GlossResult(removed, added, changed, seen)


def _stubs(end_pt, nxt_pt, w, net, layer, centre, comp_pads, comp_xy, margin, toward=None):
    """Entry stubs [c, e] at a pad-centre end; [] when the end is not at a
    pad centre (the end itself is fixed)."""
    pad = centre.get((net, _key(end_pt)))
    if pad is None:
        return None
    fr = _pad_axes(pad, layer if pad.is_smd else pad.copper[0].layer)
    if fr is None:
        return None
    ua, a, ub, b = fr
    c = (float(pad.x), float(pad.y))
    two_pin = comp_pads.get(pad.comp, 0) == 2
    dirs = []
    if a > 1.2 * b and not two_pin:
        # IC pin: along the long axis, away from its part (or the way the line went)
        cx = comp_xy.get(pad.comp)
        s = 1.0
        if cx is not None:
            s = 1.0 if (c[0] - cx[0]) * ua[0] + (c[1] - cx[1]) * ua[1] >= 0 else -1.0
        elif (nxt_pt[0] - c[0]) * ua[0] + (nxt_pt[1] - c[1]) * ua[1] < 0:
            s = -1.0
        dirs.append(((ua[0] * s, ua[1] * s), a))
    else:
        for (u, h) in ((ua, a), (ub, b)):
            dirs.append((u, h))
            dirs.append(((-u[0], -u[1]), h))
    out = []
    for u, h in dirs:
        e = (c[0] + u[0] * (h + w / 2 + margin), c[1] + u[1] * (h + w / 2 + margin))
        out.append(([c, e], u))
    if toward is not None and not (a > 1.2 * b and not two_pin):
        # a wide two-pin or square pad: also straight out through an edge in
        # line with where the run is going, off its centre but inside the pad
        # (another net's via over the middle of the edge kept the centred
        # exit out: R883 on v0). Centring matters at a fine pitch, not here.
        for (u, h), (v, hv) in (((ua, a), (ub, b)), ((ub, b), (ua, a))):
            room = hv - w / 2 - 0.5
            if room <= 0.5:
                continue
            off = (toward[0] - c[0]) * v[0] + (toward[1] - c[1]) * v[1]
            off = max(-room, min(room, off))
            if abs(off) <= 0.5:
                continue
            s0 = (c[0] + v[0] * off, c[1] + v[1] * off)
            for uu in (u, (-u[0], -u[1])):
                e = (s0[0] + uu[0] * (h + w / 2 + margin), s0[1] + uu[1] * (h + w / 2 + margin))
                out.append(([s0, e], uu))
    return out


def _gloss_chain(pts, w, net, layer, idx, centre, comp_pads, comp_xy, clr, margin, viapts=frozenset(),
                 own_ids=frozenset(), roots=(None, None)):
    pts = [tuple(p) for p in pts]
    old_turn = max(_turns(pts), default=0.0)
    old_bends = len(pts) - 2
    head = _stubs(pts[0], pts[1], w, net, layer, centre, comp_pads, comp_xy, margin, toward=pts[-1])
    tail = _stubs(pts[-1], pts[-2], w, net, layer, centre, comp_pads, comp_xy, margin, toward=pts[0])
    # an end at a via is free; at a junction or a width change the end
    # segment stays, so the run cannot fold into a sharp V with a branch
    heads = head if head is not None else (
        [([pts[0]], None)] if (net, _key(pts[0])) in viapts else [([pts[0], pts[1]], None)])
    tails = tail if tail is not None else (
        [([pts[-1]], None)] if (net, _key(pts[-1])) in viapts else [([pts[-1], pts[-2]], None)])
    # a junction just outside a pad: the branch may also be re-laid from the pad's centre
    if roots[0] is not None and head is None:
        heads = heads + (_stubs(roots[0], pts[1], w, net, layer, centre, comp_pads, comp_xy, margin) or [])
    if roots[1] is not None and tail is None:
        tails = tails + (_stubs(roots[1], pts[-2], w, net, layer, centre, comp_pads, comp_xy, margin) or [])

    def ok(seq):
        for p_, q_ in zip(seq, seq[1:]):
            if math.dist(p_, q_) < 0.01:
                continue
            if not idx.clears(layer, net, geom.capsule(p_[0], p_[1], q_[0], q_[1], w), clr):
                return False
        return True

    # every join the old run had (a pad it crosses, a track teed onto its
    # middle, a via it passes over) must still touch the new one
    caps = lambda seq: [geom.capsule(p_[0], p_[1], q_[0], q_[1], w) for p_, q_ in zip(seq, seq[1:])
                        if math.dist(p_, q_) >= 0.01]
    attach = set()
    for c_ in caps(pts):
        attach |= _touching(idx, layer, net, c_, own_ids)
    # joins at the run's own ends stay: an end kept is where it was, and a
    # junction a branch left by its pad stays joined to the pad by the stub
    ends_k = {_key(pts[0]), _key(pts[-1])}
    attach = {(k, how) for k, how in attach if not (isinstance(how, tuple) and _key(how) in ends_k)}

    def joins(seq):
        if not attach:
            return True
        cs = caps(seq)
        for k, how in attach:
            if isinstance(how, tuple):
                dot = geom.circle(how[0], how[1], 0.0)
                if not any(geom.clearance(dot, c_) <= 0.01 for c_ in cs):
                    return False
            else:
                sh = idx.items[k][2]
                if not any(geom.bboxes_near(c_, sh, 0.05) and geom.clearance(c_, sh) <= 0.01 for c_ in cs):
                    return False
        return True

    def credit(seq):
        # a branch re-laid from the pad no longer needs the shared stub
        c = 0.0
        if roots[0] is not None and _key(seq[0]) == _key(roots[0]):
            c += math.dist(roots[0], pts[0]) - buried(roots[0], pts[0])
        if roots[1] is not None and _key(seq[-1]) == _key(roots[1]):
            c += math.dist(roots[1], pts[-1]) - buried(roots[1], pts[-1])
        return c

    end_pads, end_pad_objs = [], []
    for q in (pts[0], pts[-1], roots[0], roots[1]):
        pd = centre.get((net, _key(q))) if q is not None else None
        if pd is not None:
            sh = pd.shape_on(layer) if pd.is_smd else pd.shape_on(pd.copper[0].layer)
            if sh is not None:
                end_pads.append(sh)
                end_pad_objs.append((pd, sh))

    def slant(seq):
        # an end on a pad whose first segment is not square to the pad's
        # edges (out of a corner, slantwise): the owner reads it as a fault
        n_ = 0
        for a_, b_ in ((seq[0], seq[1]), (seq[-1], seq[-2])):
            u_ = _dir(a_, b_)
            if u_ is None:
                continue
            dot_ = geom.circle(a_[0], a_[1], 0.0)
            for pd, sh in end_pad_objs:
                if geom.clearance(dot_, sh) > 0.0:
                    continue
                fr = _pad_axes(pd, layer if pd.is_smd else pd.copper[0].layer)
                if fr is None:
                    break
                ua_, _, ub_, _ = fr
                if max(abs(u_[0] * ua_[0] + u_[1] * ua_[1]), abs(u_[0] * ub_[0] + u_[1] * ub_[1])) < 0.999:
                    n_ += 1
                break
        return SLANT_COST * n_

    def buried(a, b):
        # how far a segment from a runs inside an end pad's copper (not seen, not routing)
        dot = lambda q: geom.circle(q[0], q[1], 0.0)
        for sh in end_pads:
            if geom.clearance(dot(a), sh) > 0.0:
                continue
            if geom.clearance(dot(b), sh) <= 0.0:
                return math.dist(a, b)
            lo, hi = 0.0, 1.0
            for _ in range(25):
                mid = (lo + hi) / 2
                q = (a[0] + (b[0] - a[0]) * mid, a[1] + (b[1] - a[1]) * mid)
                if geom.clearance(dot(q), sh) <= 0.0:
                    lo = mid
                else:
                    hi = mid
            return lo * math.dist(a, b)
        return 0.0

    def seen_len(seq):
        if len(seq) < 2:
            return 0.0
        return _length(seq) - buried(seq[0], seq[1]) - buried(seq[-1], seq[-2])

    old_len = seen_len(pts) + slant(pts)
    starts = {_key(pts[0])} | ({_key(roots[0])} if roots[0] is not None else set())
    finishes = {_key(pts[-1])} | ({_key(roots[1])} if roots[1] is not None else set())

    def offset(seq):
        # an end moved off the pad's centre (an in-line stub) pays its way
        return OFFSET_COST * ((_key(seq[0]) not in starts) + (_key(seq[-1]) not in finishes))

    def score_of(seq):
        sq = sum(1 for a in _turns(seq) if a > 89.0)
        return (round(seen_len(seq) - credit(seq) + offset(seq) + slant(seq) + 10.0 * sq, 2), len(seq))

    best = None
    for hs, hu in heads:
        if len(hs) > 1 and not ok(hs):
            continue
        for ts, tu in tails:
            if len(ts) > 1 and not ok(ts):
                continue
            H, T = hs[-1], ts[-1]
            for mid in _connectors(H, T):
                seq = hs[:-1] + mid + ts[::-1][1:]
                seq = [seq[0]] + [q for p_, q in zip(seq, seq[1:]) if math.dist(p_, q) > 0.01]
                if len(seq) < 2:
                    continue
                if any(not _octilinear(p_, q_) for p_, q_ in zip(seq[1:-1], seq[2:-1])):
                    continue
                turn = max(_turns(seq), default=0.0)
                if turn > 90.0 + 0.5:
                    continue
                score = score_of(seq)
                if best is not None and score >= best[0]:
                    continue
                if not ok(mid) or not joins(seq):
                    continue
                best = (score, seq, turn)
    if best is not None:
        _, seq, turn = best
        L, n = seen_len(seq) - credit(seq) + offset(seq) + slant(seq), len(seq)
        sq_new = sum(1 for a in _turns(seq) if a > 89.0)
        sq_old = sum(1 for a in _turns(pts) if a > 89.0)
        if ((L < old_len - 0.5 or (L <= old_len + 0.5 and n - 2 < old_bends)
             or (old_turn > 90.5 and L <= 1.2 * old_len)) and turn <= max(old_turn, 90.0) + 0.5
                and (sq_new <= sq_old or L < old_len - 10.0)):
            return seq
    # piecewise: shorten stretches between two of the chain's own points
    # (the end segments stay, so the entries stay as they are)
    cur = list(pts)
    improved = False
    i = 1
    while i < len(cur) - 2:
        done = False
        for j in range(len(cur) - 2, i, -1):
            a, b = cur[i], cur[j]
            for mid in _connectors(a, b):
                cand = cur[:i] + mid + cur[j + 1:]
                cand = [cand[0]] + [q for p_, q in zip(cand, cand[1:]) if math.dist(p_, q) > 0.01]
                if _length(cand) >= _length(cur) - 0.5 and len(cand) >= len(cur):
                    continue
                if max(_turns(cand), default=0.0) > max(90.0, max(_turns(cur), default=0.0)) + 0.5:
                    continue
                if max(_turns(cand), default=0.0) > 90.5 and max(_turns(cand), default=0.0) >= max(_turns(cur), default=0.0):
                    continue
                if (sum(1 for a in _turns(cand) if a > 89.0) > sum(1 for a in _turns(cur) if a > 89.0)
                        and _length(cand) >= _length(cur) - 10.0):
                    continue
                if ok(mid) and joins(cand):
                    cur = cand
                    improved = done = True
                    break
            if done:
                break
        if not done:
            i += 1
    return cur if improved else None
