# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A negotiated-congestion maze router over the routing grid.

Every net is routed as a tree: from its first pad, each further pad is
joined to the nearest part of the tree already laid, by a shortest path
through a window of the grid (``search``). Fixed copper is a hard wall,
cut to each net's own clearance and width by the grid's distance fields.
Other nets' routes are soft: stepping near one costs more, and more each
round (PathFinder's present-congestion factor), while every cell that was
fought over keeps a history cost. Nets still in conflict are ripped up
and rerouted until none are, so the order nets are routed in stops
deciding who gets the good channels.

What a route occupies is stamped as two exclusion fields per layer: where
another net's track centre may not go, and where another net's via centre
may not go. A stamp is the union of discs round the route's cells, made
with a Euclidean distance transform.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy.ndimage import distance_transform_edt

from .. import geom
from ..drc import pad_barrels
from ..model import LayoutBoard, Pad, Region, Track, Via
from ..rules import RuleSet
from .grid import (EDGE, KEEPOUT, NETLESS, RouteGrid, _seg_dist, fill_polygon, inner_depth,
                   shape_distance)
from .params import RouteRules, ViaStyle
from . import pour as pour_mod
from .fanout import FanoutPlan, find_bgas, find_exposed_pads, plan_fanout
from .pour import find_pours
from .global_route import GlobalRouter, prim_pairs
from .search import MOVES, Window, build_graph, shortest_path

EPS = 1e-3


@dataclass
class Terminal:
    pad: int                      # index into board.pads
    cells: np.ndarray             # (k, 3): layer, y, x
    center: tuple[float, float]
    # Cells deep enough in the pad that a track centred there is wholly
    # pad copper: the pad already keeps other nets off, so these are
    # neither stamped nor counted in a conflict.
    deep: np.ndarray = None
    # Cells outside the pad reached by a stub out along one of its axes,
    # where the grid has no way out of it: cell -> the stub's points.
    stubs: dict = field(default_factory=dict)
    # Set when the pad is narrower than the net's track (Router neck_pads):
    # its stubs are laid at this width and the track starts at full width
    # where the stub ends, the way a person necks down at a small pad.
    neck: float | None = None
    # A pad neither the full track nor a neck stub reaches: kept so the net
    # counts it as failed, but never entered at full width (its cells are
    # not opened); the branch pass reaches it narrower.
    blocked: bool = False


@dataclass
class NetJob:
    id: int
    name: str
    width: float
    vias: list[ViaStyle]                      # preferred first
    terminals: list[Terminal]
    plane_mask: np.ndarray | None = None      # (ny, nx) cells in its plane
    paths: list[list[tuple[int, int, int]]] = field(default_factory=list)
    # Per path, the width it is laid at when not the net's (a branch the
    # trunk could not reach at full width, Router._branch_down); empty or
    # None means job.width.
    path_w: list = field(default_factory=list)
    via_at: dict = field(default_factory=dict)   # (y, x) -> index into vias
    stamp: list = field(default_factory=list)
    failed: int = 0
    # A via on a cell of a surface pad goes in at the pad's centre, where
    # a via in a BGA ball clears the neighbouring balls; a cell of the
    # grid can be half a cell off it. (y, x) -> (px, py, room at centre).
    pad_via: dict = field(default_factory=dict)
    pv_arr: np.ndarray | None = None
    # Follow the global route's corridors. Dropped once the net is in
    # conflict: a corridor from a global route that has not settled
    # holds a net in the crowd it was meant to avoid.
    corridors: bool = True
    # A poured net, once poured: the pieces it joins (its pads and pour
    # islands), each {"terminals": [...], "cells": (k, 3)}. Routed by
    # joining these, not its pads one by one.
    groups: list | None = None
    # Per via style, the cells (k, 2) where a via of that style would land
    # in one of the net's own pads that may not take one (see
    # Router._no_via_cells).
    no_via: list | None = None
    # join_existing: the net's pads grouped by the copper already joining
    # them, each (terminal indices, extra cells (k, 3) of that copper). The
    # first group seeds the tree and each other is joined to it whole.
    pre_groups: list | None = None
    # reuse_vias: (y, x) -> (vx, vy) of a via of the net already on the
    # board; a route changing layer there uses it and adds none.
    reuse: dict = field(default_factory=dict)
    # pad_entry="axis": (k, 3) cells beside the long edges of the net's own IC
    # pins that its tracks and vias keep out of, so a route leaves a pin
    # through its short end.
    side_bar: np.ndarray | None = None
    # Where the net stands in the routing order: 0 high current, 1 rails,
    # 2 the rest (see Router._rank).
    rank: int = 2
    _deep: set | None = None

    @property
    def escapes(self) -> dict:
        """Every escape cell of this net's pads, with its stub."""
        out: dict = {}
        for t in self.terminals:
            out.update(t.stubs)
        return out

    @property
    def deep_cells(self) -> set:
        if self._deep is None:
            self._deep = {tuple(c) for t in self.terminals
                          for c in t.cells[t.deep].tolist()}
        return self._deep

    @property
    def span(self) -> float:
        xs = [t.center[0] for t in self.terminals]
        ys = [t.center[1] for t in self.terminals]
        return (max(xs) - min(xs)) + (max(ys) - min(ys)) if xs else 0.0


@dataclass
class PourPlane:
    """An inner layer a pour covers: a plane for its net, in practice.

    On every public board read, an inner layer poured edge to edge for
    one net carried no tracks at all, or very few: the person used it as
    a plane. Routing signals through it cuts the plane up, and routing
    its net as tracks instead fought every signal for room, so a dense
    board never settled."""

    layer: str
    net: str
    outline: list
    holes: list = field(default_factory=list)


@dataclass
class RouteReport:
    nets: int = 0
    connections: int = 0
    failed: int = 0
    iterations: int = 0
    conflicts: list[int] = field(default_factory=list)
    seconds: float = 0.0
    pitch: float = 0.0
    grid: tuple = ()
    global_stats: dict = field(default_factory=dict)
    stalled: bool = False
    # Stalled with a plane to give back and left as it stood: its routes
    # still conflict, and its count of failures is no score.
    unsettled: bool = False


class Router:
    def __init__(self, board: LayoutBoard, rules: RuleSet | None = None,
                 pitch: float | None = None, max_iterations: int = 25,
                 via_cost: float = 30.0, greed: float = 1.0,
                 planes: bool = True, released: frozenset = frozenset(),
                 global_routing: bool = True, nets: frozenset | None = None,
                 pres_growth: float = 1.8, history_step: float = 0.4,
                 repair_rounds: int = 2, fanout: bool = True, pours: bool = True,
                 stages=None, log: Callable[[str], None] | None = None,
                 exclude_layers: frozenset = frozenset(), neck_pads: bool = False,
                 layer_dirs: dict | None = None, dir_strength: float | None = None,
                 outer_cost: float = 1.0, join_existing: bool = False,
                 join_regions: list | None = None, via_pad_gap: float = 0.5,
                 same_net_via_gap: float | None = None, reuse_vias: bool = False,
                 power_vias: float = 0.0, pad_entry: str = "", gloss: bool = False):
        from .stages import StageOptions, StageReport

        self.board = board
        # The planned stages run before the negotiation, each fixing its
        # copper for the ones after it (see stages.py).
        self.options = StageOptions.coerce(stages)
        self.stage_report = StageReport()
        # Route only these nets; every other net's copper stays as it is.
        self.only = frozenset(nets) if nets else None
        self.rules = rules or RuleSet.from_board(board)
        self.log = log or (lambda s: None)
        self.max_iterations = max_iterations
        self.via_base = via_cost
        # PathFinder: how fast sharing gets dearer each round, and how much
        # a fought-over cell keeps costing afterwards.
        self.pres_growth = pres_growth
        self.history_step = history_step
        self.repair_rounds = repair_rounds
        # Weighted A*: the straight-line estimate scaled by this. At 1 the
        # search is exact; above 1 the route found may cost up to that
        # factor more, for a narrower search.
        self.greed = greed
        nets = board.nets()
        net_id = {n: i + 1 for i, n in enumerate(nets)}
        self.rr = RouteRules(board, self.rules, net_id)
        pads_of = board.pads_by_net()
        # neck_pads: a net is routed at its rule's preferred width and necks
        # down only at the pads narrower than that (Terminal.neck). Without
        # it the narrowest pad sets the whole net's width, and a 20 V rail
        # that touches one 0402 was routed 8 mil from end to end.
        self.neck_pads = bool(neck_pads)
        # Per layer name: 'h' or 'v', the direction its long runs keep to;
        # dir_strength the extra cost of a move across it (0.6 when unset).
        # outer_cost: the cost of a cell on the board's outer layers, so
        # long runs go inside and the outer layers keep the pad hops.
        self.layer_dirs = dict(layer_dirs or {})
        self.dir_strength = dir_strength
        self.outer_cost = float(outer_cost)
        # join_existing: route only what the board's copper leaves apart. Pads
        # already joined by a net's tracks, vias or a join region (layer, net,
        # outline) are one group; a net already whole is not routed.
        self.join_existing = bool(join_existing)
        self.join_regions = list(join_regions or [])
        # Fab rules for vias (JLCPCB multilayer, 2026-10-18):
        #  via_pad_gap       copper gap a via keeps from a pad of its own net
        #                    it is not centred in (pad_vias="centre" puts a
        #                    via that fits a pad at the pad's centre, POFV).
        #                    At 0.5 mil vias sat glued to the pads' sides.
        #  same_net_via_gap  copper gap between two vias of one net; None
        #                    leaves it to the clearance rules, which judge
        #                    other nets only, and vias of one net touched.
        #  reuse_vias        a via of the net already on the board is a
        #                    free layer change, and a plane pad it already
        #                    joins is not routed again (the plane fanout's
        #                    vias were doubled by the router's own).
        #  power_vias        nets this wide or wider (mil) get extra vias at
        #                    every layer change, one per ~15 mil, up to 4.
        #  pad_entry="axis"  a track leaves a pad along the pad's axis and
        #                    turns at 45 or 90 degrees, not slantwise.
        self.via_pad_gap = float(via_pad_gap)
        self.same_net_via_gap = same_net_via_gap
        self.reuse_vias = bool(reuse_vias)
        self.power_vias = float(power_vias)
        # Re-lay the routed lines' bends as short 45/90 runs once every
        # other pass is done (gloss.py; owner review 5, 2026-10-18).
        self.gloss = bool(gloss)
        self.pad_entry = str(pad_entry or "")
        self._net_vias: dict = {}
        for v in board.vias:
            if v.net:
                self._net_vias.setdefault(v.net, []).append((float(v.x), float(v.y), float(v.diameter)))
        widths = [self._net_width(n, pads_of.get(n, [])) for n in nets] or [6.0]
        self.w_def = float(np.median(widths))
        self.c = self.rr.default_clearance
        self.pitch = pitch or self._pitch_for(min(widths), self.c)
        vmax = max((v.diameter for n in nets for v in self.rr.vias(n)), default=20.0)
        self._clearance_groups(nets, net_id)
        # The distance fields reach as far as the largest clearance any
        # rule asks: short of it, a 3 mm rule between two net classes was
        # never seen, and its tracks passed pads 54 mil away.
        reach = (max(self.max_clearance, self.rules.max_clearance(), self.rr.edge_clearance)
                 + max(widths) / 2 + vmax / 2 + 2 * self.pitch)
        # Inner layers a pour covers become planes, and are not routed on.
        self.released = frozenset(released)
        self.pour_planes = [p for p in self._pour_planes() if p.layer not in self.released] \
            if planes else []
        on_planes = {p.layer for p in self.pour_planes}
        # Layers the caller keeps free of routing (a solid outer heat-spreader,
        # say): never a plane, never routed.
        self.exclude_layers = frozenset(exclude_layers)
        routing = [l for l in board.signal_layers() if l not in on_planes and l not in self.exclude_layers]
        self.grid = RouteGrid(board, self.pitch, reach, layers=routing, grow=self.object_grow,
                              field_of=self.field_of, n_fields=self.n_fields).build()
        self._fixed: dict | None = None       # fixed shapes per layer, for exact checks
        self._routed: dict | None = None      # routed copper per layer, likewise
        assert self.grid.net_id == net_id
        L, ny, nx = self.grid.d1.shape
        self.L, self.ny, self.nx = L, ny, nx
        # Where another net's via centre may not go depends on that via's
        # size, so there is one field per via radius in use. Judged
        # against one size for all, a small via beside a track was a
        # conflict to one net and not to the other, and never settled.
        self.via_radii = sorted({v.diameter / 2 for n in nets for v in self.rr.vias(n)}) or [10.0]
        self._occupancy_classes(nets, net_id, widths)
        self.occ_t = np.zeros((len(self.t_classes), L, ny, nx), dtype=np.int16)
        self.occ_v = np.zeros((len(self.v_classes), L, ny, nx), dtype=np.int16)
        self.hist_t = np.zeros((L, ny, nx), dtype=np.float32)
        self.hist_v = np.zeros((ny, nx), dtype=np.float32)
        # While a repair negotiates among a few nets, the occupancy of every
        # other net, as walls (_negotiate_locally).
        self._walls: tuple | None = None
        self.move_factor = self._move_factors()
        copper = board.copper_layers()
        outer_names = {copper[0], copper[-1]} if copper else set()
        self.layer_cost = np.array([self.outer_cost if n in outer_names else 1.0
                                    for n in self.grid.layers])
        # Nets a plane or a pour joins: the rails, routed after the high
        # current nets and before the rest when the order is by class.
        self._rails = set(self._plane_masks()) | {r.net for r in board.regions
                                                 if r.kind == "pour_boundary" and r.net}
        # Pads a via may go into: the balls of a BGA, and an IC's exposed
        # pad. Never a passive's pad, and not an IC's pins either (see
        # _no_via_cells).
        bgas = find_bgas(board)
        self.via_pads = {i for b in bgas for i in b.pads} | set(find_exposed_pads(board))
        if self.options.pad_vias == "centre":
            # A via anywhere in a pad is for a real exposed pad (an IC's, with
            # pins round it) or a BGA ball; a test point's big pad is a pad.
            per = {}
            for p_ in board.pads:
                if p_.comp:
                    per[p_.comp] = per.get(p_.comp, 0) + 1
            bga = {i for b in bgas for i in b.pads}
            self.via_pads = {i for i in self.via_pads
                             if i in bga or per.get(board.pads[i].comp, 0) >= 3}
        # Surface pads the board's own copper already takes to a via or a
        # plated hole of their net, and where: the plane fan-out and the
        # pour joining count those pads as on every layer already.
        self.pad_barrels = pad_barrels(board)
        # What copper laid before the routing joins: pad -> the via it was
        # given, pads joined to another pad of their net, nets joined
        # whole.
        self.laid_via: dict[int, Via] = {}
        self.laid_joined: set[int] = set()
        self.laid_nets: set[str] = set()
        # Pad -> cells (layer, y, x) of copper laid from it that a route of
        # its net may end on: a pair's track, which the leg's other pads
        # join.
        self.laid_cells: dict[int, list] = {}
        self._own_board = False
        # Per layer and cell, the net whose pin access lane it lies in (0:
        # none); every other net pays to cross it (stages.reserve_pin_lanes).
        self.lane_owner: np.ndarray | None = None
        self.jobs = self._jobs()
        self.fanout = FanoutPlan()
        if fanout and bgas:
            self._apply_fanout(plan_fanout(self, bgas))
        if self.options.any():
            from .stages import run_stages
            if run_stages(self):
                # The terminals were cut against the copper as it was; the
                # stages' copper narrows the room round them.
                self.jobs = self._jobs()
                self._adjust_jobs()
        # A net with a pour outline on a routing layer is routed like any
        # other, then poured, and its routes the pour makes redundant are
        # taken up (see pour.py).
        self.pours = find_pours(self) if pours else []
        self.pour_nets = {pr.net for pr in self.pours}
        self.use_global = global_routing
        self.gr: GlobalRouter | None = None

    # -- set-up --------------------------------------------------------------

    def lay(self, vias=(), tracks=()) -> list:
        """Lay copper down as fixed, its net's own: on a copy of the board
        (the caller's is left alone) and in the grid, where it is foreign
        copper to every other net. Returns what went into the grid,
        (layer index, owner, shape)."""
        from ..bench import copy_board

        if not self._own_board:
            self.board = copy_board(self.board)
            self._own_board = True
        self.board.vias.extend(vias)
        self.board.tracks.extend(tracks)
        g = self.grid
        added = []
        for v in vias:
            span = set(self.board.layers_between(v.low_layer, v.high_layer))
            for li, layer in enumerate(g.layers):
                if layer in span:
                    s = g._grown(v.shape_on(layer), "via", v.net, v.comp, layer)
                    g.add_shape(s, g.owner(v.net), li)
                    added.append((li, g.owner(v.net), s))
        for t in tracks:
            li = g.layer_index.get(t.layer)
            if li is not None:
                s = g._grown(t.shape(), "track", t.net, t.comp, t.layer)
                g.add_shape(s, g.owner(t.net), li)
                added.append((li, g.owner(t.net), s))
        # The exact checks read the grid's shapes; the terminals built
        # before the fanout filled them without its vias, and a stub
        # passed one at 3.9 mil where 4 was required.
        self._fixed = None
        return added

    def _apply_fanout(self, plan: FanoutPlan) -> None:
        """Lay the fanout down as the net's own fixed copper. Each fanned
        out ball can then be reached on every layer at its via; a ball of
        a plane net whose via lands in its plane is connected already."""
        if not plan.vias:
            return
        self.fanout = plan
        self.lay(plan.vias, plan.tracks)
        self.laid_via.update(plan.by_pad)
        self._adjust_jobs()

    def _extend_terminal(self, job: NetJob, t: Terminal, cells) -> None:
        """Add cells of copper laid from a terminal's pad to the terminal,
        where a track of the net centred there clears the copper round it."""
        row = self.rr.clearance_row(job.id)
        need = job.width / 2 + self._bow(job) - EPS
        have = {tuple(c) for c in t.cells.tolist()}
        extra = []
        for li, y, x in cells:
            if (li, y, x) in have:
                continue
            at = (slice(y, y + 1), slice(x, x + 1))
            if float(self._slack(job.id, li, at, row)[0, 0]) >= need:
                extra.append((li, y, x))
        if extra:
            t.cells = np.concatenate([t.cells, np.array(extra, dtype=np.int64)])
            t.deep = np.concatenate([t.deep, np.zeros(len(extra), dtype=bool)])

    def _adjust_jobs(self) -> None:
        """Bring the jobs into line with the copper laid before routing: a
        net joined whole is not routed; a pad joined to another of its net
        is no terminal; a pad given a via is reached on every layer at the
        via, and a pad of a plane net whose via lands in its plane is
        connected already."""
        spec = self.grid.spec
        keep = []
        for job in self.jobs:
            if job.name in self.laid_nets:
                continue
            terms = []
            for t in job.terminals:
                if t.pad in self.laid_joined:
                    continue
                if t.pad in self.laid_cells:
                    self._extend_terminal(job, t, self.laid_cells[t.pad])
                v = self.laid_via.get(t.pad)
                if v is None:
                    terms.append(t)
                    continue
                ci, cj = spec.cell(v.x, v.y)
                inside = 0 <= ci < spec.nx and 0 <= cj < spec.ny
                if job.plane_mask is not None and inside and job.plane_mask[cj, ci]:
                    continue        # joined to its plane by its via
                if inside and self.options.legacy():
                    extra = np.array([(li, cj, ci) for li in range(self.L)], dtype=np.int64)
                    t.cells = np.concatenate([t.cells, extra])
                    t.deep = np.concatenate([t.deep, np.ones(len(extra), dtype=bool)])
                elif inside:
                    # The cell is up to 0.71 of a cell off the via's centre:
                    # a track centred there lies wholly in the via's copper
                    # only when the track is narrow enough. Where it is not,
                    # the cell is an ordinary one: open only where the track
                    # fits, and stamped and checked like any other.
                    off = math.hypot(float(spec.x(ci)) - v.x, float(spec.y(cj)) - v.y)
                    deep = job.width / 2 + off <= v.diameter / 2 + EPS
                    row = self.rr.clearance_row(job.id)
                    at = (slice(cj, cj + 1), slice(ci, ci + 1))
                    need = job.width / 2 + self._bow(job) - EPS
                    layers = [li for li in range(self.L)
                              if deep or float(self._slack(job.id, li, at, row)[0, 0]) >= need]
                    if layers:
                        extra = np.array([(li, cj, ci) for li in layers], dtype=np.int64)
                        t.cells = np.concatenate([t.cells, extra])
                        t.deep = np.concatenate([t.deep, np.full(len(extra), deep)])
                terms.append(t)
            job.terminals = terms
            job._deep = None
            # A pad given its via needs no second one at its centre.
            for t in terms:
                if t.pad in self.laid_via:
                    p = self.board.pads[t.pad]
                    ci0, cj0 = spec.cell(p.x, p.y)
                    job.pad_via.pop((cj0, ci0), None)
            if job.pad_via:
                job.pv_arr = np.array([(y, x, r) for (y, x), (_, _, r) in job.pad_via.items()])
            else:
                job.pv_arr = None
            need = 1 if job.plane_mask is not None else 2
            if len(job.terminals) >= need:
                keep.append(job)
        self.jobs = keep

    @staticmethod
    def _pitch_for(width: float, clearance: float) -> float:
        """A pitch at which the closest two tracks may lie, width plus
        clearance plus the corner margin, is exactly three cells.

        At a third of width plus clearance with the margin on top, two
        tracks three cells apart fell short by the margin, so the closest
        legal packing the grid allowed was four cells: a third of every
        layer's room thrown away.
        """
        D = width / 2 + clearance
        p = (width + clearance) / 3.0
        for _ in range(4):
            p = (width + clearance + p * p / (4.0 * D)) / 3.0
        return max(1.5, min(5.0, p * 1.001))

    def _net_width(self, net: str, pads) -> float:
        """The width a net is routed at: the rule's preferred width, capped
        by its narrowest pad unless the pads neck down (neck_pads)."""
        return self.rr.width(net, () if self.neck_pads else pads)

    def _move_factors(self) -> np.ndarray:
        """Per layer, the cost multiplier of each of the eight moves.

        Inner layers alternate a preferred direction; the outer layers,
        where parts fan out, prefer none on a board with inner layers.
        """
        L = self.L
        f = np.ones((L, 8))
        copper = self.board.copper_layers()
        board_outer = {copper[0], copper[-1]} if copper else set()
        names = list(self.grid.layers)
        inner_seen = 0
        for l in range(L):
            name = names[l] if l < len(names) else ""
            # OUTER means the board's outer copper. Judged by the ends of
            # the routing list, an inner layer became "outer" whenever the
            # layer outside it was left out of routing (a plane, or an
            # excluded heat-spreader), lost its direction, and the inner
            # layer before it took the other one.
            outer = name in board_outer if name else l in (0, L - 1)
            if L > 2 and outer:
                strength = 0.0
            else:
                strength = self.dir_strength if self.dir_strength is not None else \
                    (0.25 if L <= 2 else 0.6)
            if name in self.layer_dirs:
                horizontal = str(self.layer_dirs[name]).lower().startswith('h')
                if outer:
                    strength = self.dir_strength if self.dir_strength is not None else 0.6
            elif outer:
                horizontal = (l % 2 == 0)
            else:
                horizontal = (inner_seen % 2 == 0)
            if not outer:
                inner_seen += 1
            for k, (dx, dy, _) in enumerate(MOVES):
                if dx and dy:
                    f[l, k] = 1.0 + strength / 2 + 0.02
                elif (dy == 0) == horizontal:
                    f[l, k] = 1.0
                else:
                    f[l, k] = 1.0 + strength
        return f

    #: Share of the board a pour must cover for its layer to be a plane.
    PLANE_COVER = 0.8

    def _pour_planes(self) -> list[PourPlane]:
        """Inner layers whose largest pour covers most of the board.

        A pour nested inside that one, of another net, is a split: its
        net's plane where it lies, and a hole in the larger one.
        """
        from .grid import GridSpec

        b = self.board
        if len(b.outline) < 3:
            return []
        xs = [p[0] for p in b.outline]
        ys = [p[1] for p in b.outline]
        step = max(max(xs) - min(xs), max(ys) - min(ys)) / 200.0 or 1.0
        spec = GridSpec(min(xs), min(ys), step, int((max(xs) - min(xs)) / step) + 1,
                        int((max(ys) - min(ys)) / step) + 1)
        board_cells = fill_polygon(spec, [b.outline] + list(b.cutouts))
        area = max(int(board_cells.sum()), 1)
        signal = b.signal_layers()
        pads = b.pads_by_net()
        out = []
        for layer in signal[1:-1]:
            pours = [r for r in b.regions if r.layer == layer and r.kind == "pour_boundary"
                     and r.net in pads]
            if not pours:
                continue
            cover = [(int((fill_polygon(spec, [r.outline]) & board_cells).sum()) / area, r)
                     for r in pours]
            cover.sort(key=lambda cr: -cr[0])
            share, main = cover[0]
            if share < self.PLANE_COVER:
                continue
            nested = [r for _, r in cover[1:] if r.net != main.net]
            out.append(PourPlane(layer, main.net, main.outline, [r.outline for r in nested]))
            for r in nested:
                out.append(PourPlane(layer, r.net, r.outline, []))
        return out

    def _plane_masks(self) -> dict[str, np.ndarray]:
        b = self.board
        out: dict[str, np.ndarray] = {}
        full = np.ones((self.ny, self.nx), dtype=bool)
        for layer in b.layers:
            if layer.kind == "plane" and layer.plane_net:
                out[layer.plane_net] = full
        rings = [(r.net, r.outline, r.holes) for r in b.regions if r.kind == "plane" and r.net]
        rings += [(p.net, p.outline, p.holes) for p in self.pour_planes]
        for net, outline, holes in rings:
            # Inside the outline and in none of its holes, as the copper
            # is drawn (and the DRC reads it). Filled even-odd with its
            # holes, another net's pour taken as a hole that reached past
            # the outline came back as plane where it did, and plane
            # vias stood in no copper of their net.
            m = fill_polygon(self.grid.spec, [outline])
            for h in holes:
                m &= ~fill_polygon(self.grid.spec, [h])
            out[net] = out.get(net, np.zeros_like(m)) | m
        return out

    def _jobs(self) -> list[NetJob]:
        planes = self._plane_masks()
        jobs = []
        index = {id(p): i for i, p in enumerate(self.board.pads)}
        for net, pads in self.board.pads_by_net().items():
            if self.only is not None and net not in self.only:
                continue
            plane = planes.get(net)
            nid = self.grid.net_id[net]
            width = self._net_width(net, pads)
            vias = self.rr.vias(net)
            terms = [self._terminal(index[id(p)], p, nid, width) for p in pads]
            terms = [t for t in terms if len(t.cells)]
            if plane is not None:
                # Through-hole pads in the plane already meet it; each
                # surface pad needs a via into it, and so does a
                # through-hole pad the plane does not reach (outside a
                # split, or in another net's island of a pour plane): left
                # out as joined, such a pad stayed apart.
                terms = [t for t in terms if self.board.pads[t.pad].is_smd
                         or not self._in_plane(plane, self.board.pads[t.pad])]
                if self.reuse_vias and terms:
                    # A pad its net's copper already joins to a via into
                    # the plane is in it: routed again it got a second via.
                    served = set()
                    for tk, _cells, has_via in self._existing_groups(net, terms, with_vias=True):
                        if has_via:
                            served.update(tk)
                    terms = [t for k, t in enumerate(terms) if k not in served]
                if not terms:
                    continue
            elif len(terms) < 2:
                continue
            job = NetJob(nid, net, width, vias, terms, plane)
            if self.join_existing and plane is None:
                groups = self._existing_groups(net, terms)
                if len(groups) < 2:
                    continue
                job.pre_groups = groups
            job.rank = self._rank(job)
            job.no_via = self._no_via_cells(job, [index[id(p)] for p in pads])
            self._pad_vias(job)
            if self.options.pad_vias == "centre" and job.no_via is not None and job.pad_via:
                # The centre cell of a pad a via fits is where it goes.
                keep = set(job.pad_via)
                job.no_via = [nv[[(int(a), int(b)) not in keep for a, b in nv]] if len(nv) else nv
                              for nv in job.no_via]
            if self.pad_entry == "axis":
                job.side_bar = self._side_bar(job)
            if self.reuse_vias:
                spec = self.grid.spec
                for vx, vy, _d in self._net_vias.get(net, ()):
                    ci, cj = spec.cell(vx, vy)
                    if 0 <= ci < spec.nx and 0 <= cj < spec.ny:
                        job.reuse[(cj, ci)] = (vx, vy)
            jobs.append(job)
        jobs.sort(key=self._order)
        return jobs

    def _side_bar(self, job: NetJob):
        """Cells beside the long edges of the net's own elongated IC pins, on
        each pin's layers, out to the track's half-width, the clearance and a
        cell: a route there left the pin sideways, between it and the next
        pin, and turned into its centre (owner, 2026-10-18)."""
        spec = self.grid.spec
        cells = []
        for t in job.terminals:
            pad = self.board.pads[t.pad]
            if not pad.is_smd or self._is_passive(pad) or t.pad in self.via_pads:
                continue
            for c in pad.copper:
                li = self.grid.layer_index.get(c.layer)
                if li is None:
                    continue
                fr = self._pad_frame(pad, c.layer)
                if fr is None:
                    continue
                ua, a, ub, b = fr
                if a <= 1.2 * b:
                    continue
                band = b + job.width / 2 + self.c + self.pitch
                r_ = math.hypot(a, band)
                i0, j0, i1, j1 = spec.window(pad.x - r_, pad.y - r_, pad.x + r_, pad.y + r_)
                if i0 >= i1 or j0 >= j1:
                    continue
                X = spec.x(np.arange(i0, i1))[None, :] - pad.x
                Y = spec.y(np.arange(j0, j1))[:, None] - pad.y
                du = X * ua[0] + Y * ua[1]
                dv = X * ub[0] + Y * ub[1]
                m = (np.abs(du) <= a) & (np.abs(dv) > b) & (np.abs(dv) <= band)
                ys, xs = np.nonzero(m)
                cells += [(li, int(y + j0), int(x + i0)) for y, x in zip(ys, xs)]
        if not cells:
            return None
        return np.array(sorted(set(cells)), dtype=np.int64)

    def _existing_groups(self, net: str, terms: list, with_vias: bool = False) -> list:
        """The net's terminals grouped by the copper already joining them
        (join_existing), each with the grid cells of that copper."""
        from .grid import fill_polygon
        spec = self.grid.spec
        li_of = self.grid.layer_index
        b = self.board
        items = []          # (kind, data)
        for k, t in enumerate(terms):
            items.append(("t", k))
        trs = [tr for tr in b.tracks if tr.net == net]
        vs = [v for v in b.vias if v.net == net]
        regs = [(l, pts) for l, n, pts in self.join_regions if n == net]
        for tr in trs:
            items.append(("tr", tr))
        for v in vs:
            items.append(("v", v))
        for r in regs:
            items.append(("r", r))
        n = len(items)
        par = list(range(n))

        def f(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        def pts_of(it):
            kind, d = it
            if kind == "t":
                pad = b.pads[terms[d].pad]
                return [((pad.x, pad.y), max(max(c.w, c.h) for c in pad.copper) / 2,
                         set(pad.layers()) if not pad.is_smd else {pad.copper[0].layer})]
            if kind == "tr":
                return [((d.x1, d.y1), d.width / 2, {d.layer}), ((d.x2, d.y2), d.width / 2, {d.layer})]
            if kind == "v":
                return [((d.x, d.y), d.diameter / 2, None)]
            return []

        shp_cache: dict = {}

        def shp_of(it):
            """(shape, layers) of a pad, track or via; layers None = every layer."""
            key_ = id(it[1]) if it[0] != "t" else ("t", it[1])
            if key_ in shp_cache:
                return shp_cache[key_]
            kind, d = it
            if kind == "t":
                pad = b.pads[terms[d].pad]
                if pad.is_smd:
                    out_ = [(pad.shape_on(c_.layer), {c_.layer}) for c_ in pad.copper]
                else:
                    out_ = [(pad.shape_on(pad.copper[0].layer), None)]
            elif kind == "tr":
                out_ = [(geom.capsule(d.x1, d.y1, d.x2, d.y2, d.width), {d.layer})]
            elif kind == "v":
                out_ = [(geom.circle(d.x, d.y, d.diameter), None)]
            else:
                out_ = []
            shp_cache[key_] = out_
            return out_

        def seg_d(p, a, c):
            dx, dy = c[0] - a[0], c[1] - a[1]
            ll = dx * dx + dy * dy
            u = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ll))
            return math.hypot(p[0] - a[0] - u * dx, p[1] - a[1] - u * dy)

        def inside(pt, poly):
            x, y = pt
            c = False
            for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    c = not c
            return c

        def touch(a, c):
            if a[0] == "r" or c[0] == "r":
                rr, o = (a, c) if a[0] == "r" else (c, a)
                if o[0] == "r":
                    return False
                lay, poly = rr[1]
                return any((ls is None or lay in ls) and inside(pt, poly) for pt, _, ls in pts_of(o))
            if a[0] == "tr" or c[0] == "tr":
                tt, o = (a, c) if a[0] == "tr" else (c, a)
                t_ = tt[1]
                if o[0] == "tr":
                    o_ = o[1]
                    if o_.layer != t_.layer:
                        return False
                    return min(seg_d(q, (o_.x1, o_.y1), (o_.x2, o_.y2)) for q in ((t_.x1, t_.y1), (t_.x2, t_.y2))) \
                        <= (t_.width + o_.width) / 2 + 0.5 or \
                        min(seg_d(q, (t_.x1, t_.y1), (t_.x2, t_.y2)) for q in ((o_.x1, o_.y1), (o_.x2, o_.y2))) \
                        <= (t_.width + o_.width) / 2 + 0.5
                ts = geom.capsule(t_.x1, t_.y1, t_.x2, t_.y2, t_.width)
                return any((ls is None or t_.layer in ls) and geom.bboxes_near(ts, so, 0.5)
                           and geom.clearance(ts, so) <= 0.5 for so, ls in shp_of(o))
            # pads, tracks and vias meet when their real copper meets (a pad as a circle of
            # max(w, h)/2 joined adjacent fine-pitch pins and pins beside an exposed pad, so
            # those pins were never routed)
            for sa, la in shp_of(a):
                for sc, lc in shp_of(c):
                    if la is not None and lc is not None and not (la & lc):
                        continue
                    if geom.bboxes_near(sa, sc, 0.5) and geom.clearance(sa, sc) <= 0.5:
                        return True
            return False

        for i in range(n):
            for j in range(i + 1, n):
                if f(i) != f(j) and touch(items[i], items[j]):
                    par[f(i)] = f(j)
        groups: dict = {}
        for i, it in enumerate(items):
            groups.setdefault(f(i), []).append(it)
        out = []
        for g in groups.values():
            tk = [d for kind, d in g if kind == "t"]
            if not tk:
                continue
            cells = []
            for kind, d in g:
                if kind == "tr" and d.layer in li_of:
                    L_ = max(2, int(math.hypot(d.x2 - d.x1, d.y2 - d.y1) / (self.pitch / 2)))
                    for k in range(L_ + 1):
                        x = d.x1 + (d.x2 - d.x1) * k / L_
                        y = d.y1 + (d.y2 - d.y1) * k / L_
                        ci, cj = spec.cell(x, y)
                        cells.append((li_of[d.layer], cj, ci))
                elif kind == "v":
                    ci, cj = spec.cell(d.x, d.y)
                    cells += [(li, cj, ci) for li in range(self.L)]
                elif kind == "r" and d[0] in li_of:
                    m = fill_polygon(spec, [d[1]])
                    ys, xs = np.nonzero(m)
                    cells += [(li_of[d[0]], int(y), int(x)) for y, x in zip(ys, xs)]
            cells = [c for c in cells if 0 <= c[1] < self.ny and 0 <= c[2] < self.nx]
            arr = np.array(sorted(set(cells)), dtype=np.int64).reshape(-1, 3)
            if with_vias:
                out.append((tk, arr, any(kind == "v" for kind, _ in g)))
            else:
                out.append((tk, arr))
        # the biggest group seeds the tree
        out.sort(key=lambda g: -(len(g[0]) + len(g[1])))
        return out

    def _in_plane(self, plane: np.ndarray, pad: Pad) -> bool:
        ci, cj = self.grid.spec.cell(pad.x, pad.y)
        return 0 <= ci < self.nx and 0 <= cj < self.ny and bool(plane[cj, ci])

    def _rank(self, job: NetJob) -> int:
        """0 for a high-current net (a rule asks it wider than most), 1 for
        a rail (a plane or pour joins it), 2 for the rest."""
        if job.width > 1.25 * self.w_def + EPS:
            return 0
        return 1 if job.name in self._rails else 2

    def _order(self, job: NetJob):
        """The order nets are routed in each round: by class first when
        the stages ask it, then shortest first."""
        span = (job.span, len(job.terminals))
        return (job.rank,) + span if self.options.class_order else span

    #: How far a via of a net keeps from that net's own pads that may not
    #: take one, edge to edge.
    VIA_PAD_MARGIN = 0.5

    def _no_via_cells(self, job: NetJob, pads: list[int]) -> list | None:
        """Per via style, the cells where a via of that style would overlap
        one of the net's own surface pads that may not take one.

        A via in a pad wicks the solder away from the joint unless it is
        filled and capped, which is a fabrication step of its own: a
        passive's pad never takes one, an IC's pin neither. Only a BGA's
        balls (where nothing else fits between them) and an exposed pad
        (a via array, for heat) may. Another net's via is kept out of the
        pad by the clearance already.
        """
        mode = self.options.pad_vias
        if mode == "any":
            return None
        spec = self.grid.spec
        out = [[] for _ in job.vias]
        for i in pads:
            pad = self.board.pads[i]
            if not pad.is_smd or i in self.via_pads:
                continue
            if mode == "ic" and not self._is_passive(pad):
                continue
            # "centre": every surface pad keeps vias off it except at its
            # centre (where _pad_vias registers one when it fits).
            for c in pad.copper:
                if c.layer not in self.grid.layer_index:
                    continue
                s = pad.shape_on(c.layer)
                for k, v in enumerate(job.vias):
                    r = v.diameter / 2
                    x0, y0, x1, y1 = s.bbox
                    g_ = self.via_pad_gap
                    i0, j0, i1, j1 = spec.window(x0 - r - g_, y0 - r - g_, x1 + r + g_, y1 + r + g_)
                    if i0 >= i1 or j0 >= j1:
                        continue
                    X = spec.x(np.arange(i0, i1))[None, :]
                    Y = spec.y(np.arange(j0, j1))[:, None]
                    # Short of touching by a margin: a via that grazes the
                    # pad by a ten-thousandth of a mil is in it all the same.
                    ys, xs = np.nonzero(shape_distance(s, X, Y) < r + self.via_pad_gap)
                    out[k].append(np.stack([ys + j0, xs + i0], axis=1))
        if not any(out):
            return None
        return [np.unique(np.concatenate(o), axis=0) if o else np.zeros((0, 2), dtype=np.int64)
                for o in out]

    def _is_passive(self, pad: Pad) -> bool:
        if not hasattr(self, "_pads_per_comp"):
            self._pads_per_comp: dict[str, int] = {}
            for p in self.board.pads:
                if p.comp:
                    self._pads_per_comp[p.comp] = self._pads_per_comp.get(p.comp, 0) + 1
        return bool(pad.comp) and self._pads_per_comp.get(pad.comp, 0) == 2

    def _pad_vias(self, job: NetJob) -> None:
        """Where each surface pad of the net would take a via: its centre,
        and the room there, judged from the nearest cell less the distance
        to it (distance to copper changes no faster than position)."""
        row = self.rr.clearance_row(job.id)
        spec = self.grid.spec
        rows = []
        for t in job.terminals:
            pad = self.board.pads[t.pad]
            if not pad.is_smd or not len(t.cells):
                continue
            if self.options.pad_vias == "bga" and t.pad not in self.via_pads:
                continue
            if self.options.pad_vias == "ic" and self._is_passive(pad) \
                    and t.pad not in self.via_pads:
                continue
            if self.options.pad_vias == "centre" and t.pad not in self.via_pads:
                # Only a pad the via (nearly) fits: centred in a 8 mil QFN pin
                # a 16 mil via is no via-in-pad, it is a via on the neighbours.
                dmin = min(v.diameter for v in job.vias)
                if min(min(c.w, c.h) for c in pad.copper) < 0.75 * dmin:
                    continue
            ci, cj = spec.cell(pad.x, pad.y)
            if not (0 <= ci < spec.nx and 0 <= cj < spec.ny):
                continue
            off = math.hypot(float(spec.x(ci)) - pad.x, float(spec.y(cj)) - pad.y)
            at = (slice(cj, cj + 1), slice(ci, ci + 1))
            room = min(float(self._slack(job.id, l, at, row)[0, 0]) for l in range(self.L)) - off
            if self.options.exact_pad_via:
                room = max(room, self._room_at(job.id, pad.x, pad.y, max(v.diameter for v in job.vias) / 2))
            # Only the cell nearest the centre: from any other cell of a
            # big pad, the via would move away from the track that meets
            # it on the next layer.
            if any(y == cj and x == ci for _, y, x in t.cells.tolist()):
                job.pad_via[(cj, ci)] = (pad.x, pad.y, room)
                rows.append((cj, ci, room))
        if rows:
            job.pv_arr = np.array(rows, dtype=np.float64)

    def _room_at(self, nid: int, x: float, y: float, most: float) -> float:
        """The largest via radius, up to ``most``, that a via of the net at
        (x, y) may have on every routing layer: the distance to each fixed
        shape of another net less the clearance it needs, to a keep-out,
        and to the board edge less its clearance; exactly, from the
        shapes.

        The room judged from the nearest cell, less the distance from it,
        never overstates and can understate by a mil: a public board's 12
        mil via in a 0.5 mm BGA ball, where the person put one, came out
        0.97 mil short of room and the ball was left unjoined."""
        point = geom.circle(x, y, 0.0)
        reach = most + max(float(self.rr.clearance_row(nid).max()), self.rr.edge_clearance) + EPS
        room = most
        for li in range(self.L):
            items, boxes = self._fixed_items(li)
            if not items:
                continue
            near = np.nonzero((boxes[:, 0] <= x + reach) & (boxes[:, 2] >= x - reach)
                              & (boxes[:, 1] <= y + reach) & (boxes[:, 3] >= y - reach))[0]
            for k in near:
                owner, sh = items[k]
                if owner == nid:
                    continue
                gap = geom.clearance(point, sh)
                if owner == KEEPOUT:
                    room = min(room, gap)
                elif owner == EDGE:
                    room = min(room, gap - self.rr.edge_clearance)
                else:
                    room = min(room, gap - self.rr.pair_clearance(nid, owner))
        return room

    def _fixed_items(self, li: int):
        """Every fixed shape on routing layer ``li``, with its owner, and
        their boxes; the board edge on every layer."""
        if self._fixed is None:
            self._fixed = {}
        if li not in self._fixed:
            items = [(o, sh) for l, o, sh in self.grid.shapes if l == li]
            items += [(EDGE, sh) for sh in self.grid.edge_shapes]
            boxes = np.array([sh.bbox for _, sh in items]) if items else np.zeros((0, 4))
            self._fixed[li] = (items, boxes)
        return self._fixed[li]

    def _neck_width(self, nid: int, pad: Pad, width: float) -> float | None:
        """The width a stub to this pad is laid at when the pad is narrower
        than the net's track, else None."""
        if not self.neck_pads:
            return None
        sizes = [min(c.w, c.h) for c in pad.copper if min(c.w, c.h) > 0]
        if not sizes or min(sizes) >= width - EPS:
            return None
        return max(self.rr.min_width(self.rr.names[nid]), min(sizes))

    def _terminal(self, idx: int, pad: Pad, nid: int, width: float) -> Terminal:
        g = self.grid
        cells = []
        deep = []
        inside_cells = []
        row = self.rr.clearance_row(nid)
        neck = self._neck_width(nid, pad, width)
        for c in pad.copper:
            li = g.layer_index.get(c.layer)
            if li is None:
                continue
            s = pad.shape_on(c.layer)
            i0, j0, i1, j1 = g.spec.window(*s.bbox)
            if i0 >= i1 or j0 >= j1:
                continue
            X = g.spec.x(np.arange(i0, i1))[None, :]
            Y = g.spec.y(np.arange(j0, j1))[:, None]
            inside = shape_distance(s, X, Y) <= EPS
            win = (slice(j0, j1), slice(i0, i1))
            slack = self._slack(nid, li, win, row) - width / 2
            ok = inside & (slack >= -EPS)
            if neck is not None:
                # A necked pad: only cells where the full track fits are
                # entered; the rest is reached by a stub at the neck width.
                iy, ix = np.nonzero(inside)
                inside_cells += [(li, int(y + j0), int(x + i0)) for y, x in zip(iy, ix)]
                if not inside.any():
                    ci, cj = g.spec.cell(pad.x, pad.y)
                    if 0 <= ci < g.spec.nx and 0 <= cj < g.spec.ny:
                        inside_cells.append((li, cj, ci))
            if neck is not None and not ok.any():
                continue
            if not ok.any() and inside.any():
                # Nothing inside clears every neighbour: take the best
                # cells inside and let the DRC judge.
                best = slack[inside].max()
                ok = inside & (slack >= best - EPS)
            if not ok.any():
                # A pad smaller than a cell: the nearest cell to its centre.
                ci, cj = g.spec.cell(pad.x, pad.y)
                if 0 <= ci < g.spec.nx and 0 <= cj < g.spec.ny:
                    cells.append((li, cj, ci))
                    deep.append(True)
                continue
            # Deep only where the whole track width is pad copper. Where
            # no cell holds it, none is: the most central were once taken
            # as deep, left out of the stamps, and another net's via came
            # 3.9 mil from a track overhanging a narrow pad there.
            depth = inner_depth(s, X, Y)
            is_deep = ok & (depth >= width / 2 - EPS)
            ys, xs = np.nonzero(ok)
            for y, x in zip(ys, xs):
                cells.append((li, int(y + j0), int(x + i0)))
                deep.append(bool(is_deep[y, x]))
        if neck is not None:
            stubs = self._escapes(pad, nid, width, cells + inside_cells, stub_width=neck,
                                  own_cells=inside_cells)
        else:
            stubs = self._escapes(pad, nid, width, cells)
        for c in stubs:
            cells.append(c)
            deep.append(False)
        if neck is not None and not cells and inside_cells:
            # Neither the full track nor a neck stub reaches the pad. Kept
            # as its own copper, so the net counts it as failed and the
            # branch pass (_branch_down) reaches it narrower: left with no
            # cells, the terminal was dropped from the net, the pad was
            # never counted, and a run reported 0 failed with it unrouted.
            cells = list(dict.fromkeys(inside_cells))
            deep = [False] * len(cells)
            return Terminal(idx, np.array(cells, dtype=np.int64).reshape(-1, 3), (pad.x, pad.y),
                            np.array(deep, dtype=bool), stubs, neck=None, blocked=True)
        return Terminal(idx, np.array(cells, dtype=np.int64).reshape(-1, 3), (pad.x, pad.y),
                        np.array(deep, dtype=bool), stubs,
                        neck=neck if (neck is not None and stubs) else None)

    #: How far past a pad's edge an escape along its axis may reach: the
    #: track's half-width and largest clearance, and this many cells more.
    ESCAPE_CELLS = 3

    def _escapes(self, pad: Pad, nid: int, width: float, cells, stub_width: float | None = None,
                 own_cells=None) -> dict:
        """Cells outside a pad that a straight stub out along one of its
        axes reaches, on each of its layers where no cell next to it has
        room for the track.

        A fine-pitch pad as wide as its track is left along its axis or
        not at all: the neighbours allow the track a mil or so either side
        of the centreline, and a grid of 5 mil cells has none there. Round
        a QFN's pads a band 12 mil deep had no cell a track fitted, and
        three nets failed with nothing else on the board. Each stub is
        checked exactly against every fixed shape; the cell it reaches
        has room on the grid.
        """
        g = self.grid
        half = width / 2
        need = half + self._bow_at(half + self.c) - EPS
        row = self.rr.clearance_row(nid)
        centre = (float(pad.x), float(pad.y))
        out: dict = {}
        # A neck stub (stub_width) is checked at its own width and reaches a
        # cell where the FULL track fits; the pad's own cells are all of it
        # (own_cells), not only those the full track fits in.
        sw = width if stub_width is None else stub_width
        if own_cells is not None:
            cells = own_cells
        for c in pad.copper:
            li = g.layer_index.get(c.layer)
            if li is None:
                continue
            own = {(y, x) for l, y, x in cells if l == li}
            if not own:
                continue
            ring = {(y + dy, x + dx) for y, x in own for dy in (-1, 0, 1) for dx in (-1, 0, 1)}
            ring = [(y, x) for y, x in ring - own if 0 <= y < g.spec.ny and 0 <= x < g.spec.nx]
            if ring and stub_width is None:
                ry = np.array([y for y, _ in ring])
                rx = np.array([x for _, x in ring])
                if (self._slack(nid, li, (ry, rx), row) >= need).any():
                    continue
            sh = pad.shape_on(c.layer)
            a = math.radians(pad.rotation)
            ux, uy = math.cos(a), math.sin(a)
            limit_extra = half + float(row.max()) + self.ESCAPE_CELLS * self.pitch
            dirs = ((ux, uy), (-ux, -uy), (-uy, ux), (uy, -ux))
            if self.pad_entry == "axis" and not self._is_passive(pad):
                fr = self._pad_frame(pad, c.layer)
                if fr is not None and fr[1] > 1.2 * fr[3]:
                    # an IC pin escapes only out of its short ends (side keep-out)
                    la = fr[0]
                    dirs = tuple(d_ for d_ in dirs if abs(d_[0] * la[0] + d_[1] * la[1]) > 0.9)
            for u in dirs:
                edge = max((px - centre[0]) * u[0] + (py - centre[1]) * u[1] for px, py in sh.pts) + sh.r
                # with pad_entry="axis" the stub runs straight until the track is clear
                # of the pad before it turns: turning at the pad's edge, half the track's
                # width still bent inside the pad and narrowed the mask web
                d = edge + (sw / 2 + 1.0 if self.pad_entry == "axis" else 0.0)
                found = False
                while d <= edge + limit_extra and not found:
                    p = (centre[0] + d * u[0], centre[1] + d * u[1])
                    ci, cj = g.spec.cell(*p)
                    for dj in (-1, 0, 1):
                        for di in (-1, 0, 1):
                            qi, qj = ci + di, cj + dj
                            if not (0 <= qi < g.spec.nx and 0 <= qj < g.spec.ny) or (qj, qi) in own:
                                continue
                            q = (float(g.spec.x(qi)), float(g.spec.y(qj)))
                            if math.hypot(q[0] - p[0], q[1] - p[1]) > self.pitch:
                                continue
                            room = self._slack(nid, li, (np.array([qj]), np.array([qi])), row)[0]
                            if room < need:
                                continue
                            pts = [centre, p, q] if math.hypot(q[0] - p[0], q[1] - p[1]) > EPS \
                                else [centre, q]
                            if all(self._static_clears(nid, sw, li, a_, b_)
                                   for a_, b_ in zip(pts, pts[1:])):
                                out[(li, qj, qi)] = pts
                                found = True
                    d += self.pitch / 2
        return out

    # -- costs ---------------------------------------------------------------

    def _window_costs(self, job: NetJob, win: Window, pres: float, hard: bool = False):
        """Cost of entering each region cell, (L, M), and of a via there, (M,)."""
        gy, gx = win.j0 + win.cy, win.i0 + win.cx
        at = (gy, gx)
        row = self.rr.clearance_row(job.id)
        slacks = [self._slack(job.id, l, at, row) for l in range(win.L)]
        room = np.minimum.reduce(slacks)
        if job.pv_arr is not None:
            pv = job.pv_arr
            py, px = pv[:, 0].astype(np.int64), pv[:, 1].astype(np.int64)
            inside = win.contains(py, px)
            room[win.rank[py[inside] - win.j0, px[inside] - win.i0]] = pv[inside, 2]
        cell = np.empty((win.L, win.M))
        need = job.width / 2 + self._bow(job) - EPS
        wq = self._track_class(job)
        lanes = None
        if self.lane_owner is not None:
            lo = self.lane_owner[:, gy, gx]
            lanes = (lo != 0) & (lo != job.id)
        walls = self._walls
        for l in range(win.L):
            occ = self.occ_t[wq, l][at]
            ok_t = slacks[l] >= need
            if hard:
                ok_t &= occ == 0
            elif walls is not None:
                ok_t &= walls[0][wq, l][at] == 0
            cell[l] = np.where(ok_t, self.layer_cost[l] * (1.0 + self.hist_t[l][at]) * (1.0 + pres * occ),
                               np.inf)
            if lanes is not None:
                # Another net's pin access lane: crossed only at a price.
                cell[l] = np.where(lanes[l], cell[l] * self.options.lane_cost, cell[l])
        # Each via size that fits is priced with the crowding round it at
        # that size, and the cheapest is taken. Chosen by fit alone, the
        # preferred size went in wherever it fitted, and a public board
        # whose rule prefers 26 mil got a cluster of them in the middle of
        # a BGA that the person had filled with 12 and 18 mil vias.
        base = self.via_base * (1.0 + self.hist_v[at])
        via = np.full(win.M, np.inf)
        style = np.full(win.M, -1, dtype=np.int64)
        for k, v in enumerate(job.vias):
            fits = room >= v.diameter / 2 - EPS
            if job.no_via is not None and len(job.no_via[k]):
                nv = job.no_via[k]
                inside = win.contains(nv[:, 0], nv[:, 1])
                fits[win.rank[nv[inside, 0] - win.j0, nv[inside, 1] - win.i0]] = False
            q = self._via_field(job, v.diameter / 2)
            occ_v = self.occ_v[q][:, gy, gx].max(axis=0)
            cost = base * (1.0 + pres * occ_v) * (1.0 + 0.15 * k)
            if hard:
                fits &= occ_v == 0
            elif walls is not None:
                fits &= walls[1][q][:, gy, gx].max(axis=0) == 0
            better = fits & (cost < via)
            via = np.where(better, cost, via)
            style = np.where(better, k, style)
        if job.side_bar is not None and len(job.side_bar):
            sb = job.side_bar
            inside = win.contains(sb[:, 1], sb[:, 2])
            if inside.any():
                c_ = sb[inside]
                r_ = win.rank[c_[:, 1] - win.j0, c_[:, 2] - win.i0]
                cell[c_[:, 0], r_] = np.inf
                via[r_] = np.inf
        if self.same_net_via_gap is not None or job.reuse:
            spec = self.grid.spec
            X = spec.x(gx).astype(np.float64)
            Y = spec.y(gy).astype(np.float64)
            x0, x1, y0, y1 = float(X.min()), float(X.max()), float(Y.min()), float(Y.max())
            pts = list(self._net_vias.get(job.name, ()))
            for path in job.paths:
                for l, y, x in path:
                    if l < 0:
                        vx, vy = job.reuse.get((y, x)) or (job.pad_via.get((y, x)) or
                                                           (float(spec.x(x)), float(spec.y(y))))[:2]
                        pts.append((vx, vy, job.vias[job.via_at.get((y, x), 0)].diameter))
            if self.same_net_via_gap is not None and pts:
                dmax = max(v.diameter for v in job.vias)
                reach = dmax + self.same_net_via_gap + max(d for _, _, d in pts)
                for vx, vy, d in pts:
                    if vx < x0 - reach or vx > x1 + reach or vy < y0 - reach or vy > y1 + reach:
                        continue
                    dist = np.hypot(X - vx, Y - vy)
                    for k, v in enumerate(job.vias):
                        bad = (dist < (v.diameter + d) / 2 + self.same_net_via_gap) & (dist > self.pitch * 0.75)
                        if bad.any():
                            # styles share the one via array: bar the cell for
                            # this style only when no other style fits there
                            via = np.where(bad & (style == k), np.inf, via)
                            style = np.where(bad & (style == k), -1, style)
            if self.reuse_vias and pts:
                # a new via near one of the net's vias costs more: a second via a
                # pitch from the first, where one would do (owner, 2026-10-18)
                for vx, vy, d in pts:
                    if vx < x0 - 50 or vx > x1 + 50 or vy < y0 - 50 or vy > y1 + 50:
                        continue
                    nearv = np.hypot(X - vx, Y - vy) < 50.0
                    via = np.where(nearv, via * 3.0, via)
            if job.reuse:
                for (ry, rx), _xy in job.reuse.items():
                    if win.contains(np.array([ry]), np.array([rx]))[0]:
                        r_ = win.rank[ry - win.j0, rx - win.i0]
                        via[r_] = self.via_base * 0.05
                        style[r_] = 0
            if self.reuse_vias:
                # joining at one of the net's own vias (another route's) is a via
                # already paid for
                for path in job.paths:
                    for l, y, x in path:
                        if l < 0 and win.contains(np.array([y]), np.array([x]))[0]:
                            r_ = win.rank[y - win.j0, x - win.i0]
                            via[r_] = self.via_base * 0.05
            self._last_style = (win, style)
        if job.plane_mask is not None:
            # A via into its own plane is what a plane net is FOR.
            via = np.where(job.plane_mask[at], via * 0.5, via)
        if lanes is not None:
            via = np.where(lanes.any(axis=0), via * self.options.lane_cost, via)
        self._last_style = (win, style)
        # A net's own pad copper is its own: a terminal cell is open to it
        # whatever the fixed copper round it. Where the track overhangs the
        # pad (not deep), another net's route there is a real conflict: a
        # pass that must add none (settle, repair, neck-down, joining a
        # pour) keeps off it, where forced open it laid a track 3.9 mil
        # from another net's via. While negotiating the cell stays open,
        # and a conflict there is found and priced like any other; priced
        # there too, nets round a chip-scale part lost room they needed.
        # Escape cells are outside the pad and judged like any other.
        for t in job.terminals:
            if t.blocked:
                # Opened, its cells took a track at full width straight
                # through a connector's pins (USB-C VBUS, 2026-10-18).
                continue
            keep = np.ones(len(t.cells), dtype=bool) if not t.stubs else \
                np.array([tuple(c) not in t.stubs for c in t.cells.tolist()], dtype=bool)
            keep &= win.contains(t.cells[:, 1], t.cells[:, 2])
            c = t.cells[keep]
            r = win.rank[c[:, 1] - win.j0, c[:, 2] - win.i0]
            cost = np.ones(len(c))
            if hard or walls is not None:
                shallow = ~t.deep[keep]
                gy, gx = win.j0 + win.cy[r], win.i0 + win.cx[r]
                occ_t = self.occ_t if hard else walls[0]
                ruled = shallow & (occ_t[wq, c[:, 0], gy, gx] > 0)
                cost = np.where(ruled, np.inf, cost)
            cell[c[:, 0], r] = cost
        return cell, via

    def _bow(self, job: NetJob) -> float:
        """How much closer to a convex corner a step between two cells can
        pass than either cell does. A step of length l whose ends are both
        D from a corner comes within sqrt(D^2 - (l/2)^2) of it; for a
        diagonal step (l^2 = 2 pitch^2) that is short of D by at most
        pitch^2 / (4 D). Unallowed for, diagonal tracks came a fraction
        of a mil inside the clearance at pad corners."""
        return self._bow_at(job.width / 2 + self.c)

    def _bow_at(self, D: float) -> float:
        return self.pitch ** 2 / (4.0 * D)

    @staticmethod
    def _width_classes(widths, most: int = 4) -> list[float]:
        """The track widths the occupancy is kept for: those in use, at
        most ``most`` of them. Past that, the closer of two neighbours goes
        and its nets are judged as the wider one, which can only keep
        them further apart."""
        out = sorted({round(w, 4) for w in widths}) or [6.0]
        while len(out) > most:
            k = min(range(len(out) - 1), key=lambda i: out[i + 1] / out[i])
            del out[k]
        return out

    #: Most track fields kept, over all clearance groups.
    MAX_TRACK_CLASSES = 8

    def object_grow(self, kind, net, comp, layer, smd=False, polygon=False) -> float:
        """How far a fixed object's shape is grown in the distance fields
        for a clearance rule its net alone does not answer: the most any
        routed net's track or pour needs (see RouteRules.grow)."""
        return self.rr.grow(kind, net, comp, layer, smd, polygon, self.group_reps)

    def _clearance_groups(self, nets, net_id) -> None:
        """Nets that every clearance rule treats alike form a group, and
        the clearance between two nets is the clearance between their
        groups: most boards have one group, a board with a high-voltage
        class two."""
        rr = self.rr
        index: dict[tuple, int] = {}
        reps: list[int] = []
        self.group_of: dict[int, int] = {}
        for n in nets:
            nid = net_id[n]
            sig = rr.group(nid)
            if sig not in index:
                index[sig] = len(reps)
                reps.append(nid)
            self.group_of[nid] = index[sig]
        self.group_reps = tuple(reps)
        # Owners that need the same clearance from every routed net, as a
        # track or as a pour, share distance fields (see RouteGrid).
        cols: dict[tuple, int] = {}
        self._field: dict[int, int] = {}
        for o in [NETLESS] + [net_id[n] for n in nets]:
            key = tuple(round(rr.pair_clearance(r, o, poly), 4) for r in reps for poly in (False, True))
            self._field[o] = cols.setdefault(key, len(cols))
        self.n_fields = max(1, len(cols))
        if not reps:
            self.gclear = np.array([[self.c]])
            self.max_clearance = self.c
            return
        self.gclear = np.array([[max(rr.pair_clearance(a, b), rr.pair_clearance(b, a))
                                 for b in reps] for a in reps], dtype=float)
        self.max_clearance = max(float(rr.clearance_row(r).max()) for r in reps)

    def _occupancy_classes(self, nets, net_id, widths) -> None:
        """The occupancy fields: one per clearance group and track width in
        use, and one per clearance group and via radius in use.

        Where another net's track may not go depends on that track's width
        and on the clearance between the two nets. Judged against the
        median width for all, a 41 mil power track was routed beside a 33
        mil one with 2.6 mil between them; judged against the default
        clearance for all, the tracks of a class that must keep 3 mm from
        the rest passed them at 50. A stamp for a field keeps out what
        reads it: tracks of that width, of that group.

        Past ``MAX_TRACK_CLASSES`` fields, the two groups whose clearances
        differ least share theirs, at the larger clearance of the two,
        which can only keep nets further apart.
        """
        G = len(self.gclear)
        w_of: list[list[float]] = [[] for _ in range(G)]
        r_of: list[list[float]] = [[] for _ in range(G)]
        for n, w in zip(nets, widths):
            g = self.group_of[net_id[n]]
            w_of[g].append(w)
            # A net necked down reads the field of its narrower width.
            w_of[g].append(min(w, self.rr.min_width(n)))
            r_of[g].extend(v.diameter / 2 for v in self.rr.vias(n))
        members = [[g] for g in range(G)]

        def fields():
            return sum(len(self._width_classes([w for g in m for w in w_of[g]])) for m in members)

        while len(members) > 1 and fields() > self.MAX_TRACK_CLASSES:
            cols = [self.gclear[:, m].max(axis=1) for m in members]
            _, i, j = min((float(np.abs(cols[i] - cols[j]).max()), i, j)
                          for i in range(len(members)) for j in range(i + 1, len(members)))
            members[i] = members[i] + members[j]
            del members[j]
        self.reader_of = np.zeros(G, dtype=np.int64)
        for k, m in enumerate(members):
            self.reader_of[m] = k
        # Clearance from a net of each group to each field's group.
        self.to_reader = np.stack([self.gclear[:, m].max(axis=1) for m in members], axis=1)
        self.t_classes: list[tuple[int, float]] = []
        self.v_classes: list[tuple[int, float]] = []
        for k, m in enumerate(members):
            self.t_classes += [(k, w) for w in self._width_classes([w for g in m for w in w_of[g]])]
            radii = sorted({round(r, 4) for g in m for r in r_of[g]}) or [10.0]
            self.v_classes += [(k, r) for r in radii]

    def field_of(self, owner: int) -> int:
        return self._field.get(owner, 0)

    def _reader(self, job: NetJob) -> int:
        return int(self.reader_of[self.group_of.get(job.id, 0)])

    #: Widths and radii are kept to 4 places, and a width from a metric
    #: rule has more: 0.55 mm is 21.65354 mil, kept as 21.6535. Read to
    #: the last place, that net read the next wider field.
    CLASS_TOL = 1e-3

    def _track_class(self, job: NetJob) -> int:
        """The field this net's tracks read: its group's, at the narrowest
        width kept that is at least as wide as the net's."""
        k = self._reader(job)
        mine = [(w, q) for q, (g, w) in enumerate(self.t_classes) if g == k]
        wider = [c for c in mine if c[0] >= job.width - self.CLASS_TOL]
        return min(wider)[1] if wider else max(mine)[1]

    def _via_field(self, job: NetJob, radius: float) -> int:
        """The field this net's via of ``radius`` reads, likewise."""
        k = self._reader(job)
        mine = [(r, q) for q, (g, r) in enumerate(self.v_classes) if g == k]
        wider = [c for c in mine if c[0] >= radius - self.CLASS_TOL]
        return min(wider)[1] if wider else max(mine)[1]

    def _slack(self, nid: int, layer: int, win, row) -> np.ndarray:
        return self.grid.slack(nid, layer, win, lambda who: RouteRules.lookup(row, who),
                               self.rr.edge_clearance)

    def _via_styles(self, job: NetJob, ws) -> np.ndarray:
        """Per cell, the first via style that clears every layer; -1: none."""
        g = self.grid
        row = self.rr.clearance_row(job.id)
        slack = None
        for l in range(self.L):
            sl = self._slack(job.id, l, ws, row)
            slack = sl if slack is None else np.minimum(slack, sl)
        return self._styles_from(job, slack)

    @staticmethod
    def _styles_from(job: NetJob, slack: np.ndarray) -> np.ndarray:
        """The first via style whose radius fits the room on every layer."""
        out = np.full(slack.shape, -1, dtype=np.int64)
        for k in range(len(job.vias) - 1, -1, -1):
            out = np.where(slack >= job.vias[k].diameter / 2 - EPS, k, out)
        return out

    def _via_class(self, job: NetJob, style: np.ndarray) -> np.ndarray:
        """Per cell, the via-radius field a via of that style is judged in."""
        fields = np.array([self._via_field(job, v.diameter / 2) for v in job.vias])
        return fields[np.maximum(style, 0)]

    def _style_at(self, job: NetJob, y: int, x: int) -> int:
        k = int(self._via_styles(job, (slice(y, y + 1), slice(x, x + 1)))[0, 0])
        return max(k, 0)

    # -- routing one net -----------------------------------------------------

    def _region(self, pts, half: float | None) -> Window:
        """Cells within ``half`` mils of the line between the two points,
        or, with ``half`` None, the whole board."""
        spec = self.grid.spec
        if half is None:
            return Window(0, 0, spec.nx, spec.ny, self.L)
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        i0, j0, i1, j1 = spec.window(min(x0, x1) - half, min(y0, y1) - half,
                                     max(x0, x1) + half, max(y0, y1) + half)
        X = spec.x(np.arange(i0, i1))[None, :]
        Y = spec.y(np.arange(j0, j1))[:, None]
        mask = _seg_dist(X, Y, x0, y0, x1, y1) <= half
        return Window(i0, j0, i1 - i0, j1 - j0, self.L, mask)

    def _corridor_window(self, tiles: np.ndarray) -> Window:
        T = self.gr.T
        cells = np.kron(tiles, np.ones((T, T), dtype=bool))[:self.ny, :self.nx]
        ys, xs = np.nonzero(cells)
        j0, j1, i0, i1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        return Window(int(i0), int(j0), int(i1 - i0), int(j1 - j0), self.L,
                      cells[j0:j1, i0:i1])

    def _connect(self, job: NetJob, src: np.ndarray, tree: set, tree_hubs: set,
                 pts, pres: float, plane_target: bool = False, hard: bool = False,
                 corridor: np.ndarray | None = None):
        span = max(abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]), 1.0)
        # The global route's corridor first, when there is one; then a
        # band round the straight line, widening to the whole board only
        # when nothing narrower holds a route.
        regions = [("corridor", corridor)] if corridor is not None and corridor.any() else []
        regions += [("band", h) for h in (max(30.0, 0.15 * span), max(90.0, 0.5 * span),
                                           max(250.0, 1.5 * span), None)]
        for kind, arg in regions:
            win = self._corridor_window(arg) if kind == "corridor" else self._region(pts, arg)
            s = src[win.contains(src[:, 1], src[:, 2])]
            if not len(s):
                continue
            sources = win.node(s[:, 0], s[:, 1], s[:, 2])
            if plane_target:
                gy, gx = win.j0 + win.cy, win.i0 + win.cx
                r = np.nonzero(job.plane_mask[gy, gx])[0]
                targets = win.n_layer + r
            else:
                t = np.array([c for c in tree if c[0] >= 0], dtype=np.int64).reshape(-1, 3)
                t = t[win.contains(t[:, 1], t[:, 2])]
                hubs = np.array(list(tree_hubs), dtype=np.int64).reshape(-1, 2)
                hubs = hubs[win.contains(hubs[:, 0], hubs[:, 1])]
                targets = np.concatenate([win.node(t[:, 0], t[:, 1], t[:, 2]),
                                          win.hub(hubs[:, 0], hubs[:, 1])])
            if not len(targets):
                continue
            cell, via = self._window_costs(job, win, pres, hard)
            style_of = self._last_style[1]
            sources = sources.astype(np.int64)
            # A search pays nothing for the cell it starts on, so a start
            # closed to this net is dropped first: a pour's join, which
            # must add no conflict, started on a pad cell another net's
            # via ruled out, 3.9 mil from it.
            sources = sources[np.isfinite(cell.reshape(-1)[sources])]
            if not len(sources):
                continue
            targets = np.asarray(targets, dtype=np.int64)
            h = self._potential(win, targets)
            graph = build_graph(win, cell, via, self.move_factor, self.pitch, h)
            # scipy's Dijkstra never stops at a target: it settles every
            # node under its limit. On reduced costs the limit is the
            # detour allowed beyond a straight line, so the search widens
            # in steps and a direct route costs a narrow band. Each step
            # is exact for any route whose detour fits it.
            path = None
            for a, b in self.DETOURS:
                path = shortest_path(graph, sources, targets, a * span + b)
                if path is not None:
                    break
            if path is not None:
                decoded = [win.decode(n) for n in path]
                chosen = {}
                for n, (l, y, x) in zip(path, decoded):
                    if l < 0:
                        chosen[(y, x)] = int(max(style_of[n - win.n_layer], 0))
                self._chosen_styles = chosen
                return decoded
        return None

    #: Detour limits, in mils of cost beyond the straight line: a
    #: modest one, then none. The region is already a band round the
    #: route, which bounds the search; each extra limit costs scipy's
    #: set-up over the whole region again (on a dense board, most of the
    #: time spent).
    DETOURS = ((0.3, 100.0), (0.0, 1e11))

    def _potential(self, win: Window, targets: np.ndarray) -> np.ndarray:
        """Straight-line distance, in mils, from each region cell to the
        nearest target: a lower bound on the cost to go, since every move
        costs at least its length."""
        far = np.ones((win.h, win.w), dtype=bool)
        r = targets % win.M                     # layer and via nodes alike
        far[win.cy[r], win.cx[r]] = False
        d = distance_transform_edt(far)
        return d[win.cy, win.cx] * (self.pitch * self.greed)

    def _barrel_in(self, mask: np.ndarray, pad: int) -> bool:
        """Whether existing copper takes ``pad`` to a via or plated hole
        standing inside ``mask`` (a plane's cells)."""
        spec = self.grid.spec
        for x, y in self.pad_barrels.get(pad, ()):
            i, j = spec.cell(x, y)
            if 0 <= j < mask.shape[0] and 0 <= i < mask.shape[1] and mask[j, i]:
                return True
        return False

    def _add_path(self, job: NetJob, path) -> None:
        job.paths.append(path)
        chosen = getattr(self, "_chosen_styles", {})
        for l, y, x in path:
            if l < 0 and (y, x) not in job.via_at:
                job.via_at[(y, x)] = chosen.get((y, x), self._style_at(job, y, x))

    def route_net(self, job: NetJob, pres: float, hard: bool = False) -> None:
        if job.groups is not None:
            pour_mod.stitch(self, job, pres, hard)
            return
        self._unstamp(job)
        job.paths = []
        job.path_w = []
        job.via_at = {}
        job.failed = 0
        if job.plane_mask is not None:
            for t in job.terminals:
                if self._barrel_in(job.plane_mask, t.pad):
                    # Its own stub and via already reach the plane: a plane
                    # joins every barrel of its net that stands in it.
                    continue
                path = self._connect(job, t.cells, set(), set(), [t.center, t.center],
                                     pres, plane_target=True, hard=hard)
                if path is None:
                    job.failed += 1
                else:
                    self._add_path(job, path)
            self._stamp(job)
            return
        # A blocked pad (neck_pads) is joined last, by the branch pass: as the
        # first pad of the tree every connection had to reach it, all failed,
        # and the whole net was necked down to the branch width.
        if job.pre_groups:
            self._route_groups(job, pres, hard)
            return
        terms = sorted(job.terminals, key=lambda t: t.blocked)
        reach = [t for t in terms if not t.blocked]
        if reach and len(reach) < len(terms):
            terms = reach
            job.failed += len(job.terminals) - len(reach)
        cx = np.array([t.center[0] for t in terms])
        cy = np.array([t.center[1] for t in terms])
        start = int(np.argmin(cx + cy))
        tree = {tuple(c) for c in terms[start].cells.tolist()}
        tree_hubs: set = set()
        for i, (k, q) in enumerate(prim_pairs(cx, cy)):
            corridor = self.gr.corridor(job, i) \
                if self.gr is not None and job.corridors else None
            path = self._connect(job, terms[k].cells, tree, tree_hubs,
                                 [terms[k].center, terms[q].center], pres, hard=hard,
                                 corridor=corridor)
            if path is None:
                job.failed += 1
                continue
            self._add_path(job, path)
            for l, y, x in path:
                if l < 0:
                    tree_hubs.add((y, x))
                else:
                    tree.add((l, y, x))
            tree.update(tuple(c) for c in terms[k].cells.tolist())
        self._stamp(job)

    def _route_groups(self, job: NetJob, pres: float, hard: bool) -> None:
        """join_existing: the first group (its pads and the copper joining
        them) seeds the tree; each other group is joined to it whole, the
        nearest first. A route ending on existing copper needs no stub."""
        spec = self.grid.spec
        groups = job.pre_groups
        terms = job.terminals

        def cells_of(g):
            tc = [terms[k].cells for k in g[0] if len(terms[k].cells)]
            allc = tc + ([g[1]] if len(g[1]) else [])
            return np.concatenate(allc) if allc else np.zeros((0, 3), dtype=np.int64)

        def centre(g):
            xs = [terms[k].center[0] for k in g[0]]
            ys = [terms[k].center[1] for k in g[0]]
            return (sum(xs) / len(xs), sum(ys) / len(ys))

        tree = {tuple(c) for c in cells_of(groups[0]).tolist()}
        hubs: set = set()
        tc = centre(groups[0])
        rest = sorted(groups[1:], key=lambda g: math.hypot(centre(g)[0] - tc[0], centre(g)[1] - tc[1]))
        for g in rest:
            src = cells_of(g)
            if not len(src):
                job.failed += 1
                continue
            path = self._connect(job, src, tree, hubs, [centre(g), tc], pres, hard=hard)
            if path is None:
                job.failed += 1
                continue
            self._add_path(job, path)
            for l, y, x in path:
                (hubs.add((y, x)) if l < 0 else tree.add((l, y, x)))
            tree.update(tuple(c) for c in src.tolist())
        self._stamp(job)

    # -- occupancy -----------------------------------------------------------

    def _cells(self, job: NetJob):
        tracks: dict[int, set] = {}
        vias: set = set()
        for path in job.paths:
            for l, y, x in path:
                if l < 0:
                    vias.add((y, x))
                else:
                    tracks.setdefault(l, set()).add((y, x))
        for l, cells in self._stub_cells(job).items():
            tracks.setdefault(l, set()).update(cells)
        return tracks, vias

    def _stub_cells(self, job: NetJob) -> dict[int, set]:
        """The cells an escape stub of this net's routes passes over, per
        layer: the stub is off the grid, and the cells nearest it stand in
        for it in the stamp, grown by how far they can lie from it."""
        esc = job.escapes
        if not esc:
            return {}
        out: dict[int, set] = {}
        spec = self.grid.spec
        for path in job.paths:
            for end in (path[0], path[-1]):
                pts = esc.get(tuple(end))
                if pts is None:
                    continue
                cells = out.setdefault(end[0], set())
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    n = max(1, int(math.ceil(math.hypot(bx - ax, by - ay) / (self.pitch / 4))))
                    for k in range(n + 1):
                        ci, cj = spec.cell(ax + (bx - ax) * k / n, ay + (by - ay) * k / n)
                        if 0 <= ci < spec.nx and 0 <= cj < spec.ny:
                            cells.add((cj, ci))
        return out

    def _radii(self, job: NetJob, style: int, other_via: float, other_width: float,
               clearance: float):
        """Exclusion radii in cells, against another net's track of width
        ``other_width`` and another net's via of radius ``other_via``, that
        net needing ``clearance`` from this one. A cell closer than the
        radius is excluded; one exactly at it is not.

        Where a track is involved, the corner margin (``_bow_at``) is added
        for the pair's centre distance: a step between two cells passes
        closer to what it turns past than either cell. Taken for the pair,
        not for one side, it is the same both ways round, so two nets agree
        on whether they conflict. Via to via is centre to centre."""
        p = self.pitch
        rv = job.vias[style].diameter / 2
        half = job.width / 2
        ow = other_width / 2
        tt = half + clearance + ow
        tv = half + clearance + other_via
        vt = rv + clearance + ow
        return ((tt + self._bow_at(tt)) / p,             # track excludes tracks
                (tv + self._bow_at(tv)) / p,             # track excludes vias
                (vt + self._bow_at(vt)) / p,             # via excludes tracks
                (rv + other_via + clearance) / p)        # via excludes vias

    def _stamp(self, job: NetJob) -> None:
        tracks, vias = self._cells(job)
        deep = job.deep_cells
        tracks = {l: {c for c in cells if (l, *c) not in deep} for l, cells in tracks.items()}
        grown = {l: {c for c in cells if (l, *c) not in deep}
                 for l, cells in self._stub_cells(job).items()}
        if not any(tracks.values()) and not vias:
            return
        cl = self.to_reader[self.group_of.get(job.id, 0)]
        big = max(r for _, r in self.v_classes)
        wide = max(w for _, w in self.t_classes)
        R = int(math.ceil(max(max(self._radii(job, k, big, wide, float(cl.max())))
                              for k in range(len(job.vias))))) + 1
        vy = np.array([v[0] for v in vias], dtype=np.int64)
        vx = np.array([v[1] for v in vias], dtype=np.int64)
        vk = np.array([job.via_at.get(v, 0) for v in vias], dtype=np.int64)
        # A via at a pad's centre is up to 0.71 of a cell from the cell it
        # was routed on: its exclusion grows by that much.
        vc = np.array([v in job.pad_via for v in vias], dtype=bool)
        for l in range(self.L):
            cells = tracks.get(l, set())
            if not cells and not len(vy):
                continue
            ty = np.array([c[0] for c in cells], dtype=np.int64)
            tx = np.array([c[1] for c in cells], dtype=np.int64)
            ally = np.concatenate([ty, vy])
            allx = np.concatenate([tx, vx])
            j0, j1 = max(0, ally.min() - R), min(self.ny, ally.max() + R + 1)
            i0, i1 = max(0, allx.min() - R), min(self.nx, allx.max() + R + 1)
            shape = (j1 - j0, i1 - i0)
            K, Q = len(self.v_classes), len(self.t_classes)
            mt = np.zeros((Q,) + shape, dtype=bool)
            mv = np.zeros((K,) + shape, dtype=bool)
            if len(ty):
                a = np.ones(shape, dtype=bool)
                a[ty - j0, tx - i0] = False
                dt = distance_transform_edt(a)
                for q, (g, width) in enumerate(self.t_classes):
                    mt[q] |= dt < self._radii(job, 0, 0.0, width, cl[g])[0] - 1e-9
                for q, (g, other) in enumerate(self.v_classes):
                    mv[q] |= dt < self._radii(job, 0, other, 0.0, cl[g])[1] - 1e-9
            if grown.get(l):
                # An escape stub lies up to 0.71 of a cell from the cells
                # standing in for it.
                gy = np.array([c[0] for c in grown[l]], dtype=np.int64)
                gx = np.array([c[1] for c in grown[l]], dtype=np.int64)
                a = np.ones(shape, dtype=bool)
                a[gy - j0, gx - i0] = False
                dg = distance_transform_edt(a)
                for q, (g, width) in enumerate(self.t_classes):
                    mt[q] |= dg < self._radii(job, 0, 0.0, width, cl[g])[0] + 0.71 - 1e-9
                for q, (g, other) in enumerate(self.v_classes):
                    mv[q] |= dg < self._radii(job, 0, other, 0.0, cl[g])[1] + 0.71 - 1e-9
            for st, centred in sorted({(int(a), bool(c)) for a, c in zip(vk, vc)}):
                sel = (vk == st) & (vc == centred)
                grow = 0.71 if centred else 0.0
                a = np.ones(shape, dtype=bool)
                a[vy[sel] - j0, vx[sel] - i0] = False
                dv = distance_transform_edt(a)
                for q, (g, width) in enumerate(self.t_classes):
                    mt[q] |= dv < self._radii(job, st, 0.0, width, cl[g])[2] + grow - 1e-9
                for q, (g, other) in enumerate(self.v_classes):
                    mv[q] |= dv < self._radii(job, st, other, 0.0, cl[g])[3] + grow - 1e-9
            win = (slice(j0, j1), slice(i0, i1))
            self.occ_t[(slice(None), l) + win] += mt
            self.occ_v[(slice(None), l) + win] += mv
            job.stamp.append((l, win, mt, mv))

    def _unstamp(self, job: NetJob) -> None:
        for l, win, mt, mv in job.stamp:
            self.occ_t[(slice(None), l) + win] -= mt
            self.occ_v[(slice(None), l) + win] -= mv
        job.stamp = []

    def _conflicts(self, job: NetJob) -> list[tuple[int, int, int]]:
        """Cells of this net's route that another net's route excludes."""
        tracks, vias = self._cells(job)
        own = job.deep_cells
        wq = self._track_class(job)
        bad = []
        for l, cells in tracks.items():
            for y, x in cells:
                if (l, y, x) in own:
                    continue
                if self.occ_t[wq, l, y, x] > 1:
                    bad.append((l, y, x))
        radius = [v.diameter / 2 for v in job.vias]
        for y, x in vias:
            r = radius[job.via_at.get((y, x), 0)]
            k = self._via_field(job, r)
            if (self.occ_v[k, :, y, x] > 1).any():
                bad.append((-1, y, x))
        return bad

    def _blame(self, job: NetJob, bad) -> list[NetJob]:
        """The other nets whose routes exclude these cells of ``job``'s."""
        out = []
        wq = self._track_class(job)
        for other in self.jobs:
            if other is job or not other.stamp:
                continue
            hit = False
            for l, win, mt, mv in other.stamp:
                (ys, xs) = win
                for bl, y, x in bad:
                    if not (ys.start <= y < ys.stop and xs.start <= x < xs.stop):
                        continue
                    yy, xx = y - ys.start, x - xs.start
                    if (bl == l and mt[wq, yy, xx]) or (bl < 0 and mv[:, yy, xx].any()):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                out.append(other)
        return out

    # -- the negotiation -----------------------------------------------------

    #: Rounds after which a negotiation still fighting over this share of
    #: what it started with is called stalled, when there is a plane to
    #: give back: rounds spent past that point on a board short of layers
    #: were minutes, and settled nothing.
    STALL_ROUNDS = 8
    STALL_SHARE = 0.5
    #: Or when the last STALL_WINDOW rounds did not bring the conflicts
    #: below STALL_PROGRESS of the best before them: a public board with two
    #: routing layers fell to half its first round and then sat at 11000
    #: cells for 18 rounds of 36 s each, before its planes were given back.
    STALL_WINDOW = 4
    STALL_PROGRESS = 0.9

    # -- warm start ----------------------------------------------------------

    def warm_from(self, prev: "Router") -> list[NetJob]:
        """Take over another attempt's routes where they still hold.

        Between two attempts only the layer set changes (a plane given
        back to routing), so most routes carry across as they are: cells
        are mapped by layer name, and a net's routes are kept only when
        every cell and via still clears the fixed copper here (a fanout
        via can be new) and the net is the same kind of job (a net that
        lost its plane must be routed as a tree now). Returns the nets
        that still need routing. Rerouting everything from scratch after
        each release took minutes on a large board over three attempts.
        """
        by_name = {j.name: j for j in prev.jobs}
        remap = {i: self.grid.layer_index.get(n) for i, n in enumerate(prev.grid.layers)}
        todo = []
        for job in self.jobs:
            old = by_name.get(job.name)
            # The same pads to join, with the same vias laid at them, too:
            # without a stage's dog-bones a plane net has pads the other
            # attempt never had to route.
            same = self.options.legacy() or (
                {t.pad for t in old.terminals} == {t.pad for t in job.terminals}
                and all(_at(prev.laid_via.get(t.pad)) == _at(self.laid_via.get(t.pad))
                        for t in job.terminals)) if old is not None else False
            if (old is None or old.failed or (old.plane_mask is None) != (job.plane_mask is None)
                    or not old.paths or not same):
                todo.append(job)
                continue
            paths = []
            ok = True
            for path in old.paths:
                new = []
                for l, y, x in path:
                    if l < 0:
                        new.append((l, y, x))
                        continue
                    nl = remap.get(l)
                    if nl is None:
                        ok = False
                        break
                    new.append((nl, y, x))
                if not ok:
                    break
                paths.append(new)
            if ok:
                # A route that ends on an escape cell joins its pad by the
                # stub; the cell must be an escape here too, the same one.
                was = {(remap.get(l), y, x): pts for (l, y, x), pts in old.escapes.items()}
                now = job.escapes
                ok = all(now.get(tuple(e)) == was[tuple(e)]
                         for p in paths for e in (p[0], p[-1]) if tuple(e) in was)
            if ok and job.plane_mask is not None:
                # Each route of a plane net ends in a via into its plane,
                # which must still be there: with a plane given back, a via
                # into it can stand in another net's island of the plane
                # that is left, and the pad was counted joined.
                ok = all(p[-1][0] < 0 and job.plane_mask[p[-1][1], p[-1][2]] for p in paths)
            if not ok or not self._paths_clear(job, paths, old.via_at):
                todo.append(job)
                continue
            job.paths = paths
            job.via_at = dict(old.via_at)
            job.failed = 0
            self._stamp(job)
        return todo

    def _paths_clear(self, job: NetJob, paths, via_at) -> bool:
        """Every cell of these routes clears this grid's fixed copper."""
        row = self.rr.clearance_row(job.id)
        need = job.width / 2 + self._bow(job) - EPS
        deep = job.deep_cells
        cells: dict[int, list] = {}
        vias = set()
        for path in paths:
            for l, y, x in path:
                if l < 0:
                    vias.add((y, x))
                elif (l, y, x) not in deep:
                    cells.setdefault(l, []).append((y, x))
        for l, yx in cells.items():
            ys = np.array([c[0] for c in yx])
            xs = np.array([c[1] for c in yx])
            if (self._slack(job.id, l, (ys, xs), row) < need).any():
                return False
        for (y, x) in vias:
            style = job.vias[min(via_at.get((y, x), 0), len(job.vias) - 1)]
            at = (slice(y, y + 1), slice(x, x + 1))
            room = min(float(self._slack(job.id, l, at, row)[0, 0]) for l in range(self.L))
            if (y, x) in job.pad_via:
                room = job.pad_via[(y, x)][2]
            if room < style.diameter / 2 - EPS:
                return False
        return True

    def run(self, stall_abort: bool = False, warm: "Router | None" = None) -> RouteReport:
        t0 = time.perf_counter()
        rep = RouteReport(nets=len(self.jobs), pitch=self.pitch,
                          grid=(self.L, self.ny, self.nx))
        if self.use_global:
            self.gr = GlobalRouter(self)
            rep.global_stats = self.gr.run(self.jobs, log=self.log,
                                           stall=self.options.global_stall)
        todo = list(self.jobs)
        if warm is not None:
            todo = self.warm_from(warm)
            kept = len(self.jobs) - len(todo)
            self.log(f"warm start: {kept} nets kept, {len(todo)} to route")
        quick = stall_abort and self.options.quick_release
        self._negotiate(todo, rep, t0, stall_abort, settle=not quick)
        if quick and rep.stalled:
            # The next attempt, with a plane given back, starts from these
            # routes and negotiates what still conflicts: settling,
            # repairing and pouring an attempt about to be replaced took
            # most of an hour on a dense six-layer public board.
            rep.unsettled = True
        else:
            if self.repair_rounds:
                self._repair(self.repair_rounds)
                self._branch_down()
                self._neck_down()
            if self.pours:
                self._pour_stage()
        rep.connections = sum(len(j.terminals) - (0 if j.plane_mask is not None else 1)
                              for j in self.jobs)
        rep.failed = sum(j.failed for j in self.jobs)
        rep.seconds = time.perf_counter() - t0
        return rep

    def _negotiate(self, todo: list, rep: RouteReport, t0: float,
                   stall_abort: bool = False, settle: bool = True) -> None:
        """Route ``todo``, then reroute whatever is in conflict at a rising
        price, until nothing is or the rounds run out; then settle (unless
        ``settle`` is false and the negotiation stalled)."""
        pres = 0.5
        mine: list[int] = []
        for it in range(self.max_iterations):
            for job in todo:
                self.route_net(job, pres)
            conflicted = []
            blamed = []
            n_bad = 0
            for job in self.jobs:
                bad = self._conflicts(job)
                if bad:
                    job.corridors = False
                    conflicted.append(job)
                    for other in self._blame(job, bad):
                        other.corridors = False
                        blamed.append(other)
                    n_bad += len(bad)
                    for l, y, x in bad:
                        if l < 0:
                            self.hist_v[y, x] += self.history_step
                        else:
                            self.hist_t[l, y, x] += self.history_step
            rep.conflicts.append(n_bad)
            mine.append(n_bad)
            rep.iterations = len(rep.conflicts)
            self.log(f"iteration {it + 1}: {len(todo)} nets routed, "
                     f"{len(conflicted)} in conflict ({n_bad} cells), "
                     f"{time.perf_counter() - t0:.1f}s")
            if not conflicted:
                break
            # With a plane to give back, a slow negotiation stops early so
            # the next attempt can have it; with none, one that has stopped
            # gaining stops too when the stages ask it, and what it leaves
            # is reported pad by pad (stages.unreached_pads).
            stop = stall_abort or self.options.stall_stop
            if (stop and it + 1 >= self.STALL_ROUNDS
                    and ((stall_abort and n_bad > self.STALL_SHARE * mine[0])
                         or self._plateau(mine))):
                rep.stalled = True
                self.log(f"stalled after {it + 1} rounds")
                if settle:
                    self._settle()
                break
            seen = set()
            todo = [j for j in conflicted + blamed if not (id(j) in seen or seen.add(id(j)))]
            todo.sort(key=self._order)
            pres *= self.pres_growth
        else:
            self._settle()

    def _plateau(self, conflicts: list[int]) -> bool:
        """The last STALL_WINDOW rounds did no better than STALL_PROGRESS
        of the best round before them."""
        w = self.STALL_WINDOW
        if len(conflicts) <= w:
            return False
        return min(conflicts[-w:]) > self.STALL_PROGRESS * min(conflicts[:-w])

    def _pour_stage(self) -> None:
        """Pour round the routes; take up the poured nets' routes the pours
        make redundant and give the room to what still fails; pour again,
        and join what the pours then leave apart.

        Tried first the other way round, the poured nets set aside while
        the rest were routed and joined after, a poured net got only the
        room left, or, joining pour islands while negotiating, chased
        pieces that another net's move had just cut off: boards came out
        worse than with the poured nets routed as tracks. Taking
        up only what the pour provably joins can only give room back.
        """
        jobs = [j for j in self.jobs if j.name in self.pour_nets]
        before = {id(j): (j.failed, list(j.paths), dict(j.via_at)) for j in jobs}
        pour_mod.pour(self)
        taken = sum(pour_mod.prune(self, j) for j in jobs)
        if taken and self.repair_rounds and any(
                j.failed for j in self.jobs if j.name not in self.pour_nets):
            self._repair(self.repair_rounds)
        pour_mod.pour(self)
        for job in jobs:
            if pour_mod.unjoined(self, job):
                job.groups = pour_mod.groups(self, job)
                pour_mod.stitch(self, job, 1.0, hard=True)
                job.groups = None
        pour_mod.pour(self)
        worse = []
        for job in jobs:
            job.failed = pour_mod.unjoined(self, job)
            if job.failed > before[id(job)][0]:
                worse.append(job)
        if worse:
            # A net the pour left worse joined than its tracks had it: the
            # nets repaired into the room taken back cut its pour apart.
            # Its routes go back where they conflict with nothing. On a
            # board whose released plane became a pour, three of its
            # connections were lost this way.
            for job in worse:
                _, paths, via_at = before[id(job)]
                have = {tuple(map(tuple, q)) for q in job.paths}
                for path in paths:
                    if tuple(map(tuple, path)) in have:
                        continue
                    self._unstamp(job)
                    job.paths.append(path)
                    for l, y, x in path:
                        if l < 0 and (y, x) in via_at:
                            job.via_at[(y, x)] = via_at[(y, x)]
                    self._stamp(job)
                    if self._conflicts(job):
                        self._unstamp(job)
                        job.paths.pop()
                        self._stamp(job)
            pour_mod.pour(self)
            for job in jobs:
                job.failed = pour_mod.unjoined(self, job)
        self.log(f"pours: {len(self.pours)} poured, "
                 f"{sum(j.failed for j in jobs)} connections of their nets unmade")

    # -- repair ------------------------------------------------------------

    def _save(self, job: NetJob):
        return (list(job.paths), dict(job.via_at), job.failed)

    def _restore(self, job: NetJob, saved) -> None:
        self._unstamp(job)
        job.paths, job.via_at, job.failed = list(saved[0]), dict(saved[1]), saved[2]
        self._stamp(job)

    def _repair(self, rounds: int) -> None:
        """Rip up what blocks each connection still unmade, and try again.

        A net that still fails is routed once more with sharing allowed,
        which shows the nets in its way. Those are ripped up, the failing
        net is routed with everything else as walls, then each ripped net
        the same way. The change is kept only when fewer connections fail
        in total and nothing conflicts; otherwise everything goes back.
        """
        for _ in range(rounds):
            failing = [j for j in self.jobs if j.failed and j.plane_mask is None]
            if not failing:
                return
            improved = False
            for job in failing:
                before = sum(j.failed for j in self.jobs)
                saved_job = self._save(job)
                self.route_net(job, 50.0)
                bad = self._conflicts(job)
                if not bad and self.options.repair_clean and job.failed < saved_job[2]:
                    # Routed again with sharing allowed, the net found a
                    # way that crosses nobody: room another net's repair
                    # or the pour gave back since the negotiation. Thrown
                    # away as having no blockers, such a route left
                    # connections open that a clean path existed for.
                    improved = True
                    continue
                blockers = self._blame(job, bad) if bad else []
                if not blockers:
                    self._restore(job, saved_job)
                    continue
                saved = {id(x): self._save(x) for x in blockers}
                for x in blockers:
                    self._unstamp(x)
                    x.paths, x.via_at = [], {}
                self.route_net(job, 1.0, hard=True)
                for x in blockers:
                    self.route_net(x, 1.0, hard=True)
                after = sum(j.failed for j in self.jobs)
                clean = not self._conflicts(job) and not any(self._conflicts(x) for x in blockers)
                if after < before and clean:
                    improved = True
                    continue
                if self.options.repair_negotiate and self._negotiate_locally(job, blockers, before):
                    improved = True
                    continue
                self._restore(job, saved_job)
                for x in blockers:
                    self._restore(x, saved[id(x)])
            if not improved:
                return

    #: Rounds of a repair's negotiation among the failing net and its
    #: blockers.
    LOCAL_ROUNDS = 6

    def _negotiate_locally(self, job: NetJob, blockers: list, before: int) -> bool:
        """Negotiate the failing net and the nets in its way among
        themselves, every other net a wall: each is routed again in turn,
        sharing with the others of the group at a price that rises each
        round, until none of them conflicts. Kept (True) only when that
        happens within LOCAL_ROUNDS and fewer connections fail in total;
        otherwise the caller puts them all back.

        Routed one after another with everything else a wall, the ripped
        nets of a repair could not trade places: the first took the room
        the second needed, however the room might have been shared."""
        group = [job] + list(blockers)
        for x in group:
            self._unstamp(x)
        self._walls = (self.occ_t.copy(), self.occ_v.copy())
        for x in group:
            self._stamp(x)
        pres = 1.0
        clean = False
        try:
            for _ in range(self.LOCAL_ROUNDS):
                for x in group:
                    self.route_net(x, pres)
                if not any(self._conflicts(x) for x in group):
                    clean = True
                    break
                pres *= self.pres_growth
        finally:
            self._walls = None
        return clean and sum(j.failed for j in self.jobs) < before

    def _branch_down(self) -> None:
        """neck_pads: a net that still misses pads keeps the trunk it has
        and reaches each missing pad on its own as a narrower branch, half
        the width and then the rule's minimum, joining the routed copper
        with nothing in conflict. The whole-net neck-down would have laid
        the trunk at the branch's width."""
        if not self.neck_pads:
            return
        spec = self.grid.spec
        for job in self.jobs:
            if not job.failed or job.plane_mask is not None or job.groups is not None or not job.paths \
                    or job.pre_groups:
                continue
            full = job.width
            job.path_w = (list(job.path_w) + [None] * len(job.paths))[:len(job.paths)]
            tree = {(l, y, x) for pth in job.paths for (l, y, x) in pth if l >= 0}
            hubs = {(y, x) for pth in job.paths for (l, y, x) in pth if l < 0}
            missing = []
            for t in job.terminals:
                cells = {tuple(c) for c in t.cells.tolist()}
                if cells & tree:
                    tree |= cells
                else:
                    missing.append(t)
            floor = self.rr.min_width(job.name)
            tl = np.array(sorted(tree), dtype=np.int64) if tree else None
            for t in missing:
                pad = self.board.pads[t.pad]
                w = full
                while w > floor + EPS:
                    w = max(floor, w / 2)
                    nt = self._terminal(t.pad, pad, job.id, w)
                    if not len(nt.cells) or tl is None:
                        continue
                    xs, ys = spec.x(tl[:, 2]), spec.y(tl[:, 1])
                    k = int(np.argmin((xs - pad.x) ** 2 + (ys - pad.y) ** 2))
                    self._unstamp(job)
                    job.width = w
                    try:
                        path = self._connect(job, nt.cells, tree, hubs,
                                             [nt.center, (float(xs[k]), float(ys[k]))], 1.0, hard=True)
                    finally:
                        job.width = full
                    if path is not None:
                        self._add_path(job, path)
                        job.path_w.append(w)
                        job.terminals[job.terminals.index(t)] = nt
                        for l, y, x in path:
                            (hubs.add((y, x)) if l < 0 else tree.add((l, y, x)))
                        tree |= {tuple(c) for c in nt.cells.tolist()}
                        tl = np.array(sorted(tree), dtype=np.int64)
                        job.failed -= 1
                        job._deep = None
                        self._stamp(job)
                        self.log(f"{job.name}: branch to {pad.comp}.{pad.name} at {w:g} mil")
                        break
                    self._stamp(job)

    def _neck_down(self) -> None:
        """A net that still fails is tried narrower, down to the rule's
        minimum: half its width, then half again, and the minimum itself
        where half would be below it. Kept only when it joins more and
        conflicts with nothing.

        The width a net starts at is the rule's preferred one capped by
        its pads, and a rule can prefer far more than the board has room
        for: nets on a narrow board failed at the preferred width where a
        person routes them far narrower.
        """
        fanned = set(self.laid_via)
        for job in [j for j in self.jobs if j.failed and j.plane_mask is None
                    and not any(t.pad in fanned for t in j.terminals)
                    and not (self.neck_pads and j.paths)]:
            floor = self.rr.min_width(job.name)
            saved = (self._save(job), job.width, list(job.terminals), job.pad_via, job.pv_arr)
            width = job.width
            # Halving alone never tried a 7.87 mil minimum under 11.81:
            # half of that is below the rule, so nothing was tried.
            while width > floor + 1e-9 and job.failed:
                width = max(floor, width / 2)
                self._unstamp(job)
                job.width = width
                pads = [self.board.pads[t.pad] for t in saved[2]]
                job.terminals = [self._terminal(t.pad, p, job.id, width)
                                 for t, p in zip(saved[2], pads)]
                job._deep = None
                self.route_net(job, 1.0, hard=True)
                if job.failed < saved[0][2] and not self._conflicts(job):
                    self.log(f"{job.name}: necked down to {width:g} mil")
                    break
                if width <= floor + 1e-9:
                    break
            if job.failed >= saved[0][2] or self._conflicts(job):
                self._unstamp(job)
                job.width, job.terminals = saved[1], saved[2]
                job.pad_via, job.pv_arr = saved[3], saved[4]
                job._deep = None
                self._restore(job, saved[0])

    def _settle(self) -> None:
        """Out of rounds: make what remains legal.

        Each net still in conflict first loses only the connections that
        conflict. Then it is routed again from scratch with every other
        net's route as a wall, and that attempt is kept when it is clean
        and joins at least as much. What cannot fit is left unrouted
        rather than shorted."""
        bad = [j for j in self.jobs if self._conflicts(j)]
        for j in bad:
            cells = set(self._conflicts(j))
            self._unstamp(j)
            keep = [p for p in j.paths if not cells.intersection(p)]
            j.failed += len(j.paths) - len(keep)
            j.paths = keep
            self._stamp(j)
        for j in bad:
            saved = (list(j.paths), dict(j.via_at), j.failed)
            self._unstamp(j)
            self.route_net(j, 1.0, hard=True)
            if self._conflicts(j) or j.failed > saved[2]:
                self._unstamp(j)
                j.paths, j.via_at, j.failed = saved
                self._stamp(j)

    # -- output --------------------------------------------------------------

    def apply(self, board: LayoutBoard | None = None, pours: bool = True) -> LayoutBoard:
        """The routes as tracks and vias on (a copy of) the board, and the
        pours as regions when ``pours``."""
        from ..bench import copy_board

        out = copy_board(board or self.board)
        spec = self.grid.spec
        layers = self.grid.layers
        self._routed = None     # the routes as they stand now, for the stubs' checks
        self._routed_grid = None
        # The planes the routes rely on. In Altium their pours are already
        # there and are repoured; here they stand in for that copper.
        for p in self.pour_planes:
            out.regions.append(Region(p.layer, list(p.outline), [list(h) for h in p.holes],
                                      p.net, "plane", source="plane:pour"))
        copper = out.copper_layers()
        self._keep = {}
        self._neck_tracks: set = set()
        index = self._routed = self._routed_index()
        bends = self.options.bends
        straight = self.options.straight
        laid = []
        self._job_vias: dict = {}
        self._via_snap: dict = {}
        for job in self.jobs:
            vias_done = set()
            t0, v0 = len(out.tracks), len(out.vias)
            junctions = self._junctions(job) if bends or straight else None
            lines = [] if straight else None
            for k, path in enumerate(job.paths):
                w0 = job.width
                if k < len(job.path_w) and job.path_w[k]:
                    job.width = job.path_w[k]
                try:
                    self._emit_path(job, path, out, spec, layers, vias_done, copper, junctions,
                                    lines, k)
                finally:
                    job.width = w0
            self._job_vias[job.id] = (v0, len(out.vias))
            if not (bends or straight):
                continue
            # The nets after this one are checked against its copper as
            # laid, corners cut and stubs in place, not its grid runs.
            self._index_job(index, job, out.tracks[t0:], out.vias[v0:])
            laid.append((job, t0, len(out.tracks), out.vias[v0:], lines, junctions))
        if straight and laid:
            self._straighten_all(out, index, laid)
        if self.pad_entry == "axis":
            self._square_off(out, len(self.board.tracks))
            self._repair_entries(out, len(self.board.tracks))
            self._chamfer_corners(out, len(self.board.tracks))
        if self.power_vias > 0:
            self._add_power_vias(out)
        if self.gloss:
            from .gloss import gloss_board
            gloss_board(out, clearance=self.rules.default_clearance(),
                        nets={j.name for j in self.jobs}, first=len(self.board.tracks))
        self._keep = None
        if pours:
            out.regions.extend(pour_mod.regions(self))
        return out

    def _square_off(self, out: LayoutBoard, first: int) -> None:
        """Every routed segment not at a multiple of 45 degrees laid again
        as an L, or as a 45 and a straight piece, where that clears every
        other net's copper exactly; left as it is where nothing does. The
        grid's cell centres sit up to half a pitch off a pad's centre, and
        the last piece into a pad came out at whatever angle that left."""
        index = self._routed if self._routed is not None else self._routed_index()
        self._routed = index
        ids = {j.name: j.id for j in self.jobs}

        def off(a, b):
            if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.05:
                return False
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
            return min(ang, 45.0 - ang) > 0.1

        new = []
        for t in out.tracks[first:]:
            a, b = (t.x1, t.y1), (t.x2, t.y2)
            nid = ids.get(t.net)
            li = self.grid.layer_index.get(t.layer)
            if nid is None or li is None or not off(a, b):
                new.append(t)
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            m = min(abs(dx), abs(dy))
            sx, sy = (1.0 if dx > 0 else -1.0), (1.0 if dy > 0 else -1.0)
            options = [[a, (a[0] + sx * m, a[1] + sy * m), b], [a, (b[0] - sx * m, b[1] - sy * m), b],
                       [a, (b[0], a[1]), b], [a, (a[0], b[1]), b]]
            done = False
            for pts in options:
                pts = [pts[0]] + [q for p_, q in zip(pts, pts[1:]) if math.hypot(q[0] - p_[0], q[1] - p_[1]) > 0.05]
                if all(self._static_clears(nid, t.width, li, p_, q) and
                       self._routed_clears(nid, t.width, li, p_, q) for p_, q in zip(pts, pts[1:])):
                    for p_, q in zip(pts, pts[1:]):
                        new.append(Track(t.layer, p_[0], p_[1], q[0], q[1], t.width, t.net))
                        index.add(li, nid, geom.capsule(p_[0], p_[1], q[0], q[1], t.width), nid)
                    done = True
                    break
            if not done:
                new.append(t)
        out.tracks[first:] = new

    POWER_VIA_PER = 15.0     # mil of track width per via at a layer change
    POWER_VIA_MAX = 4

    def _add_power_vias(self, out: LayoutBoard) -> None:
        """At every layer change of a net at least ``power_vias`` wide, add
        vias beside the one the route made until there is about one per 15
        mil of width (at most 4): a 40 mil trunk through one 18 mil via is a
        neck in its current and its inductance. Each sits one via pitch
        (diameter plus the same-net gap) from the first, joined on each of
        its layers by a stub at the net's width, and is checked exactly
        against every other net's copper, fixed and routed, on every layer."""
        gap = self.same_net_via_gap if self.same_net_via_gap is not None else self.c
        index = self._routed if self._routed is not None else self._routed_index()
        self._routed = index
        for job in self.jobs:
            if job.width < self.power_vias - EPS or job.id not in self._job_vias:
                continue
            v0, v1 = self._job_vias[job.id]
            want = min(self.POWER_VIA_MAX, max(1, int(round(job.width / self.POWER_VIA_PER))))
            mine = [v for v in out.vias if v.net == job.name]
            for via in list(out.vias[v0:v1]):
                d = via.diameter
                pitch = d + gap
                ends = []
                for t in out.tracks:
                    if t.net != job.name or t.layer not in self.grid.layer_index:
                        continue
                    for (ax, ay), (bx, by) in (((t.x1, t.y1), (t.x2, t.y2)), ((t.x2, t.y2), (t.x1, t.y1))):
                        if math.hypot(ax - via.x, ay - via.y) < 0.5 and math.hypot(bx - ax, by - ay) > EPS:
                            ll = math.hypot(bx - ax, by - ay)
                            ends.append((t.layer, ((bx - ax) / ll, (by - ay) / ll)))
                layers = sorted({l for l, _ in ends})
                if len(layers) < 2:
                    continue
                cands = []
                for _, (ux, uy) in ends:
                    for k in (1, 2):
                        cands += [(via.x - ux * pitch * k, via.y - uy * pitch * k),
                                  (via.x - uy * pitch * k, via.y + ux * pitch * k),
                                  (via.x + uy * pitch * k, via.y - ux * pitch * k)]
                have = 1
                placed = [(via.x, via.y)]
                for cx, cy in cands:
                    if have >= want:
                        break
                    near = min(math.hypot(cx - px, cy - py) for px, py in placed)
                    if near > pitch * 1.01 + EPS:
                        continue        # chain from a via already placed
                    # every via of the net, this round's included: a candidate came up twice
                    # (two tracks leaving the via the same way) and was laid twice
                    if any(math.hypot(cx - o.x, cy - o.y) < (d + o.diameter) / 2 + gap - 1e-3
                           for o in mine):
                        continue
                    if not self._via_pad_ok(job.name, cx, cy, d):
                        continue
                    ok = all(self._static_clears(job.id, d, li, (cx, cy), (cx, cy))
                             and self._routed_clears(job.id, d, li, (cx, cy), (cx, cy))
                             for li in range(self.L))
                    px, py = min(placed, key=lambda q: math.hypot(cx - q[0], cy - q[1]))
                    ok = ok and all(self._static_clears(job.id, job.width, self.grid.layer_index[l],
                                                        (px, py), (cx, cy))
                                    and self._routed_clears(job.id, job.width, self.grid.layer_index[l],
                                                            (px, py), (cx, cy))
                                    for l in layers)
                    if not ok:
                        continue
                    nv = Via(cx, cy, d, via.hole, via.low_layer, via.high_layer, job.name)
                    out.vias.append(nv)
                    mine.append(nv)
                    for li in range(self.L):
                        index.add(li, job.id, geom.circle(cx, cy, d), job.id)
                    for l in layers:
                        out.tracks.append(Track(l, px, py, cx, cy, job.width, job.name))
                        index.add(self.grid.layer_index[l], job.id,
                                  geom.capsule(px, py, cx, cy, job.width), job.id)
                    placed.append((cx, cy))
                    have += 1

    def _via_pad_ok(self, net: str, x: float, y: float, d: float) -> bool:
        """A via near a surface pad of its own net is centred in it or clear
        of it by via_pad_gap (pad_vias="centre"); a real exposed pad takes
        one anywhere."""
        if self.options.pad_vias != "centre":
            return True
        via = geom.circle(x, y, d)
        for i, p_ in enumerate(self.board.pads):
            if p_.net != net or not p_.is_smd or i in self.via_pads:
                continue
            if abs(p_.x - x) > 150 or abs(p_.y - y) > 150:
                continue
            if math.hypot(p_.x - x, p_.y - y) <= 0.6:
                continue
            for c in p_.copper:
                sh = p_.shape_on(c.layer)
                if sh is not None and geom.clearance(via, sh) < self.via_pad_gap - EPS:
                    return False
        return True

    def _index_job(self, index, job: NetJob, tracks, vias) -> None:
        """Swap the net's shapes in the routed index for these."""
        index.remove_tag(job.id)
        for t in tracks:
            index.add(self.grid.layer_index[t.layer], job.id, t.shape(), job.id)
        for v in vias:
            for li in range(self.L):
                index.add(li, job.id, geom.circle(v.x, v.y, v.diameter), job.id)

    def _straighten_all(self, out, index, laid) -> None:
        """Lay every net's runs again as straight and 45 degree pieces,
        each checked against every other net's copper as finally laid.

        A second pass, once every net's stubs and corners are in place:
        checked during the first, a net's pieces saw the nets after it only
        as grid runs, and a stub laid later beside a pad could come inside
        the clearance of a piece laid before it.

        Every track of a line laid again that the first pass did not lay
        as it stands is checked against the copper round it, whatever made
        it: a stretch kept as the grid laid it can lose the corner cut the
        first pass gave it, and a stub of another net may sit where that
        corner's tip now reaches. A line any such track fails keeps its
        copper from the first pass, which every net's copper was checked
        against."""
        layers = self.grid.layers
        new = []
        for job, t0, t1, vias, lines, junctions in laid:
            if not lines:
                new.append(out.tracks[t0:t1])
                continue
            meets = self._junction_cells(job)
            # Neck stubs (Terminal.neck) are laid once, at their own width,
            # and are not part of any line: kept as the first pass laid them.
            tracks = [t for t in out.tracks[t0:t1] if id(t) in getattr(self, "_neck_tracks", ())]
            w0 = job.width
            for l, pre, cells, post, k, first in lines:
                job.width = job.path_w[k] if k < len(job.path_w) and job.path_w[k] else w0
                pts = self._straighten(job, l, cells, meets.get(l, {}), pre, post, k)
                pts = [p for i, p in enumerate(pts)
                       if i == 0 or math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > EPS]
                if len(pts) >= 2 and self.options.bends:
                    pts = self._legalise(job, l, pts, (junctions or {}).get(l, ()))
                pts = self._snap_line(job, pts)
                if len(pts) >= 2:
                    pts = self._centre_entries(job, l, pts)
                if len(pts) < 2 and len(first) < 2:
                    continue
                if len(pts) >= 2 and self.pad_entry == "axis" and len(first) >= 2                         and self._entries_bad(job, l, first) < self._entries_bad(job, l, pts):
                    pts = first     # straightening spoiled a pad entry the first pass had right
                if len(pts) < 2 or not self._line_holds(job, l, pts, first):
                    pts = first
                for a, b in zip(pts, pts[1:]):
                    tracks.append(Track(layers[l], a[0], a[1], b[0], b[1], job.width, job.name))
            job.width = w0
            self._index_job(index, job, tracks, vias)
            new.append(tracks)
        first, last = laid[0][1], laid[-1][2]
        out.tracks[first:last] = [t for ts in new for t in ts]

    def _line_holds(self, job: NetJob, li: int, pts, first) -> bool:
        """Every piece of the polyline ``pts`` that is not a piece of
        ``first`` clears every other net's copper, exactly."""
        def key(a, b):
            a, b = (round(a[0], 6), round(a[1], 6)), (round(b[0], 6), round(b[1], 6))
            return (a, b) if a <= b else (b, a)

        old = {key(a, b) for a, b in zip(first, first[1:])}
        return all(key(a, b) in old or self._piece_clears(job, li, a, b)
                   for a, b in zip(pts, pts[1:]))

    def _junction_cells(self, job: NetJob) -> dict[int, dict]:
        """Per routing layer, the cells where a route of the net ends or
        changes layer (see _junctions), each with the routes that end there
        (-1 for a via). A run is laid through every such cell another route
        ends at, or that route is left short of the copper it joined."""
        out: dict[int, dict] = {}
        for k, path in enumerate(job.paths):
            for l, y, x in (path[0], path[-1]):
                if l >= 0:
                    out.setdefault(l, {}).setdefault((y, x), set()).add(k)
            for l, y, x in path:
                if l < 0:
                    for li in range(self.L):
                        out.setdefault(li, {}).setdefault((y, x), set()).add(-1)
        return out

    def _emit_path(self, job, path, out, spec, layers, vias_done, copper, junctions=None,
                   lines_out=None, number=0):
        # The pad each cell lies in, on the pad's own layers only. A fanned
        # out ball is reached on the other layers at its via, whose copper
        # already covers the cell: a stub there ran from the ball's centre
        # across a layer the router never checked it on.
        # Copper laid from the pad (a pair's track the route joins) is no
        # part of the pad either: a route ending on it meets it there, and
        # a stub from the pad's centre to it would be a second track.
        term_of = {}
        for t in job.terminals:
            own = {self.grid.layer_index[n] for n in self.board.pads[t.pad].layers()
                   if n in self.grid.layer_index}
            laid = {tuple(c) for c in self.laid_cells.get(t.pad, ())}
            for c in t.cells.tolist():
                if c[0] in own and (not laid or tuple(c) not in laid):
                    term_of[tuple(c)] = t
        if self.options.bends:
            path = self._smooth(job, path)
        path, ends = self._trim_ends(job, path, term_of, spec, layers)
        runs: list[tuple[int, list[tuple[int, int]]]] = []
        for l, y, x in path:
            if l < 0:
                vx, vy = float(spec.x(x)), float(spec.y(y))
                if (y, x) in job.pad_via:
                    vx, vy = job.pad_via[(y, x)][:2]
                if (y, x) in job.reuse:
                    vx, vy = job.reuse[(y, x)]
                    vias_done.add((round(vx, 4), round(vy, 4)))
                cxy = (float(spec.x(x)), float(spec.y(y)))
                if math.dist(cxy, (vx, vy)) > 0.01:
                    # a via moved off its cell (a pad's centre, or one reused): the
                    # runs either side end on it, not on the cell (_snap_line)
                    self._via_snap.setdefault(job.id, {})[(round(cxy[0], 3), round(cxy[1], 3))] = (vx, vy)
                key = (round(vx, 4), round(vy, 4))
                if key not in vias_done:
                    vias_done.add(key)
                    style = job.vias[job.via_at.get((y, x), 0)]
                    out.vias.append(Via(vx, vy, style.diameter, style.hole,
                                        copper[0], copper[-1], job.name))
                runs.append((-1, []))
                continue
            if not runs or runs[-1][0] != l:
                runs.append((l, []))
            runs[-1][1].append((y, x))
        # Stubs from pad centres to the cells the path starts and ends on,
        # joined to the run they lead into, so a corner where a stub meets
        # its run is cut like any other.
        stubs = [None, None]
        for side, (end, t) in enumerate(ends):
            if t is not None and end[0] >= 0:
                q = (float(spec.x(end[2])), float(spec.y(end[1])))
                neck_pts = t.stubs.get(tuple(end)) if t.neck is not None else None
                if neck_pts and len(neck_pts) >= 2:
                    # Laid at the neck width on its own; the run starts at
                    # full width on the cell the stub reaches.
                    for a_, b_ in zip(neck_pts, neck_pts[1:]):
                        if math.hypot(b_[0] - a_[0], b_[1] - a_[1]) > EPS:
                            tr = Track(layers[end[0]], a_[0], a_[1], b_[0], b_[1], t.neck, job.name)
                            out.tracks.append(tr)
                            self._neck_tracks.add(id(tr))
                    continue
                pts = t.stubs.get(tuple(end)) or self._stub(job, t, layers[end[0]], q) or []
                if len(pts) >= 2:
                    stubs[side] = [tuple(p) for p in pts]
        lines: list[tuple] = []
        for k, (l, cells) in enumerate(runs):
            if l < 0:
                continue
            pts = [(float(spec.x(x)), float(spec.y(y))) for y, x in
                   ([cells[0]] if len(cells) < 2 else
                    [c for seg in _straight_runs(cells) for c in seg][::2] + [cells[-1]])]
            pre = stubs[0][:-1] if k == 0 and stubs[0] else []
            post = stubs[1][::-1][1:] if k == len(runs) - 1 and stubs[1] else []
            pts = pre + pts + post
            pts = [p for i, p in enumerate(pts)
                   if i == 0 or math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > EPS]
            if len(pts) >= 2:
                lines.append((l, pts, pre, cells, post))
        for l, pts, pre, cells, post in lines:
            if self.options.bends:
                pts = self._legalise(job, l, pts, (junctions or {}).get(l, ()))
            pts = self._snap_line(job, pts)
            if len(pts) < 2:
                continue
            pts = self._centre_entries(job, l, pts)
            if lines_out is not None:
                # What the second pass lays again, and what it falls back
                # on (_straighten_all).
                lines_out.append((l, pre, list(cells), post, number, pts))
            for a, b in zip(pts, pts[1:]):
                out.tracks.append(Track(layers[l], a[0], a[1], b[0], b[1], job.width, job.name))

    # -- bends ---------------------------------------------------------------

    def _junctions(self, job: NetJob) -> dict[int, list]:
        """Per routing layer, the points where a route of the net meets
        another or changes layer: the ends of every route and its vias. A
        corner there is not cut, or the route meeting it would be left
        short of the copper it joined."""
        spec = self.grid.spec
        out: dict[int, list] = {}
        for path in job.paths:
            for l, y, x in (path[0], path[-1]):
                if l >= 0:
                    out.setdefault(l, []).append((float(spec.x(x)), float(spec.y(y))))
            for l, y, x in path:
                if l < 0:
                    for li in range(self.L):
                        out.setdefault(li, []).append((float(spec.x(x)), float(spec.y(y))))
        return out

    def _smooth(self, job: NetJob, path):
        """The route with its sharp grid corners cut: where it turns by
        more than 45 degrees at a cell that is no junction and in no pad,
        the corner cell goes and its two neighbours are joined by one step.
        Both are cells of the route, so the step is as legal as any the
        search takes (see _bow)."""
        if len(path) < 3:
            return path
        cache = getattr(self, "_keep", None)
        keep = cache.get(job.id) if cache is not None else None
        if keep is None:
            keep = {tuple(c) for t in job.terminals for c in t.cells.tolist()}
            for p in job.paths:
                keep.add(tuple(p[0]))
                keep.add(tuple(p[-1]))
            if cache is not None:
                cache[job.id] = keep
        out = [tuple(c) for c in path]
        i = 1
        while i < len(out) - 1:
            a, b, c = out[i - 1], out[i], out[i + 1]
            if a[0] < 0 or b[0] < 0 or c[0] < 0 or not (a[0] == b[0] == c[0]) \
                    or b in keep or a in keep or c in keep:
                i += 1
                continue
            d1 = (b[1] - a[1], b[2] - a[2])
            d2 = (c[1] - b[1], c[2] - b[2])
            dot = d1[0] * d2[0] + d1[1] * d2[1]
            sharp = dot * dot < 0.5 * (d1[0] ** 2 + d1[1] ** 2) * (d2[0] ** 2 + d2[1] ** 2) \
                or dot < 0
            jump = (c[1] - a[1], c[2] - a[2])
            if sharp and max(abs(jump[0]), abs(jump[1])) <= 1:
                if jump == (0, 0):
                    del out[i:i + 2]
                else:
                    del out[i]
                i = max(1, i - 1)
                continue
            i += 1
        return out

    def _own_cover(self, job: NetJob, li: int) -> list:
        """The net's own pad and via copper on routing layer ``li``: a turn
        under it is not a bend anyone sees."""
        layer = self.grid.layers[li]
        out = [self.board.pads[t.pad].shape_on(layer) for t in job.terminals]
        out += [v.shape_on(layer) for v in self.board.vias if v.net == job.name]
        return [s for s in out if s is not None]

    def _legalise(self, job: NetJob, li: int, pts, fixed=()) -> list:
        """The polyline with every corner that turns by more than 45 degrees
        cut by short segments, each turn then 45 degrees or less, where
        the cut clears every other net's copper exactly. A corner under
        the net's own pad or via, or at a junction, stays."""
        from .exact import BEND_TOL, turn

        pts = list(pts)
        if len(pts) < 3:
            return pts
        cover = None
        i = 1
        while i < len(pts) - 1:
            deg = turn(pts[i - 1], pts[i], pts[i + 1])
            if deg <= 45.0 + BEND_TOL or deg >= 179.0:
                i += 1
                continue
            if cover is None:
                cover = self._own_cover(job, li)
            point = geom.circle(pts[i][0], pts[i][1], 0.0)
            if any(geom.clearance(point, s) <= 1e-6 for s in cover) \
                    or any(math.hypot(pts[i][0] - fx, pts[i][1] - fy) <= EPS for fx, fy in fixed):
                i += 1
                continue
            cut = self._cut_corner(job, li, pts, i, deg, fixed)
            if cut is None:
                i += 1
                continue
            pts[i:i + 1] = cut
            i += len(cut)
        return pts

    def _cut_corner(self, job: NetJob, li: int, pts, i: int, deg: float, fixed):
        """The points that replace corner ``i``: a run of k - 1 short
        segments, turning by deg / k at each of k points, k the fewest
        that keeps each turn at 45 degrees or less. The largest that
        clears, or None."""
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        la, lc = math.hypot(b[0] - a[0], b[1] - a[1]), math.hypot(c[0] - b[0], c[1] - b[1])
        u0 = ((b[0] - a[0]) / la, (b[1] - a[1]) / la)
        uk = ((c[0] - b[0]) / lc, (c[1] - b[1]) / lc)
        k = int(math.ceil(deg / 45.0 - 1e-9))
        phi = math.radians(deg / k) * (1.0 if u0[0] * uk[1] - u0[1] * uk[0] > 0 else -1.0)
        dirs = [(u0[0] * math.cos(j * phi) - u0[1] * math.sin(j * phi),
                 u0[0] * math.sin(j * phi) + u0[1] * math.cos(j * phi)) for j in range(k + 1)]
        sx = sum(d[0] for d in dirs[1:k])
        sy = sum(d[1] for d in dirs[1:k])
        bis = math.hypot(u0[0] + uk[0], u0[1] + uk[1])
        if bis < 1e-6:
            return None
        ratio = math.hypot(sx, sy) / bis       # t = ratio * s
        # Leave the neighbouring corners half of each segment, unless the
        # neighbour is an end of the polyline.
        room_a = la - EPS if i - 1 == 0 else la / 2
        room_c = lc - EPS if i + 1 == len(pts) - 1 else lc / 2
        t_max = min(room_a, room_c)
        if t_max <= EPS:
            return None
        s = t_max / ratio
        while s * ratio > 0.05:
            t = s * ratio
            start = (b[0] - t * u0[0], b[1] - t * u0[1])
            chain = [start]
            for d in dirs[1:k]:
                p = chain[-1]
                chain.append((p[0] + s * d[0], p[1] + s * d[1]))
            # A junction on the copper cut away would be left behind.
            cut_ok = not any(_seg_dist(np.array(fx), np.array(fy), *start, *b) <= EPS
                             or _seg_dist(np.array(fx), np.array(fy), *b, *chain[-1]) <= EPS
                             for fx, fy in fixed)
            if cut_ok and all(self._static_clears(job.id, job.width, li, p, q)
                              and self._routed_clears(job.id, job.width, li, p, q)
                              for p, q in zip(chain, chain[1:])):
                return chain
            s /= 2.0
        return None

    #: How many bend points of a run one straight or 45 degree piece may
    #: reach across, looking for the farthest that clears.
    STRAIGHT_REACH = 48
    #: The longest stretch of a piece checked at once, mils: a long
    #: diagonal's box holds much of the board, its stretches' boxes do not.
    CHECK_PIECE = 100.0
    #: A straight or 45 degree part shorter than this, mils, is no part of
    #: its own: the piece runs straight to its end, a little off the angle.
    #: Only a pad's centre, off the grid, leaves so short a part; between
    #: two cells a part is a whole number of cells.
    SNAP = 1.0

    def _straighten(self, job: NetJob, li: int, cells, meets=None, pre=(), post=(),
                    number: int = 0) -> list:
        """One run, with the stubs to the pads at its ends (``pre``,
        ``post``), laid again as straight and 45 degree pieces in place of
        the grid's staircase of cell steps: the points of the polyline.

        The nodes are the run's bend points and, where a stub leads to a
        pad, the pad's centre. From each node, the farthest node ahead one
        piece reaches: straight, or a straight and a 45 degree part in
        either order, where every part clears every other net's copper,
        fixed and routed, exactly. Where none does, the run's own stretch
        to the next node as it was laid, which was already legal. A piece
        never cuts past a cell where another route of the net ends or a via
        stands (``meets``, cell -> the routes ending there, -1 a via; this
        run's own route is ``number``), and turns no more sharply where it
        meets the rest of the run than the run did there, or 45 degrees. A
        turn at a pad's centre is under the pad."""
        from .exact import BEND_TOL, turn

        spec = self.grid.spec
        meets = meets or {}
        pre = [tuple(p) for p in pre]
        post = [tuple(p) for p in post]
        cells = [tuple(c) for c in cells]
        n = len(cells)

        def at(c):
            return (float(spec.x(c[1])), float(spec.y(c[0])))

        # The run's bend points, and every cell on it another route ends at.
        idx = [0]
        step = None
        for k in range(1, n):
            d = (cells[k][0] - cells[k - 1][0], cells[k][1] - cells[k - 1][1])
            if step is not None and d != step:
                idx.append(k - 1)
            step = d
        if n > 1:
            idx.append(n - 1)
        inner = {k for k in range(1, n - 1) if cells[k] in meets}
        idx = sorted(set(idx) | inner)

        # Nodes: [point, cell (None for a pad's centre), held, the points
        # after it to the next node as laid]. A held node is never cut past.
        nodes = []
        if pre:
            nodes.append([pre[0], None, False, pre[1:] + [at(cells[0])]])
        for j, k in enumerate(idx):
            nxt = [at(cells[idx[j + 1]])] if j + 1 < len(idx) else list(post)
            hold = k in inner or bool(meets.get(cells[k], set()) - {number})
            nodes.append([at(cells[k]), cells[k], hold, nxt])
        if post:
            nodes.append([post[-1], None, False, []])
        m = len(nodes)
        P = [nd[0] for nd in nodes]
        if m < 3:
            return [P[0]] + [p for nd in nodes for p in nd[3]]
        limit = 45.0 + BEND_TOL
        # The points just before and after each node along the line as laid.
        before_of = [None] * m
        after_of = [None] * m
        for a in range(m):
            if nodes[a][3]:
                after_of[a] = nodes[a][3][0]
                if a + 1 < m:
                    seg = [P[a]] + nodes[a][3]
                    before_of[a + 1] = seg[-2]

        def bound(a):
            """The sharpest turn a piece may make at node ``a``."""
            if nodes[a][1] is None:
                return 180.0
            b, c = before_of[a], after_of[a]
            was = turn(b, P[a], c) if b is not None and c is not None else 0.0
            return max(limit, was)

        # Another net's pin access lane is crossed only where the run
        # already crossed it: the router paid to keep out of it.
        lanes = None if self.lane_owner is None else self.lane_owner[li]
        run = set(cells)

        def in_lane(pts) -> bool:
            if lanes is None:
                return False
            for p, q in zip(pts, pts[1:]):
                k = max(1, int(math.ceil(math.hypot(q[0] - p[0], q[1] - p[1])
                                         / (self.pitch / 4))))
                for s in range(k + 1):
                    ci, cj = spec.cell(p[0] + (q[0] - p[0]) * s / k,
                                       p[1] + (q[1] - p[1]) * s / k)
                    if not (0 <= ci < spec.nx and 0 <= cj < spec.ny) or (cj, ci) in run:
                        continue
                    o = lanes[cj, ci]
                    if o != 0 and o != job.id:
                        return True
            return False

        def piece(a, b, before):
            """The points after node ``a`` of a piece from it to node ``b``
            that clears and keeps the turns, or None."""
            (xa, ya), (xb, yb) = P[a], P[b]
            dx, dy = xb - xa, yb - ya
            sx, sy = math.copysign(1.0, dx), math.copysign(1.0, dy)
            nd, no = min(abs(dx), abs(dy)), abs(abs(dx) - abs(dy))
            if min(nd, no) <= self.SNAP:
                options = [[P[b]]]
            else:
                ox, oy = (sx, 0.0) if abs(dx) > abs(dy) else (0.0, sy)
                straight_first = [(xa + no * ox, ya + no * oy), P[b]]
                diagonal_first = [(xa + nd * sx, ya + nd * sy), P[b]]
                # From a pad's centre the short part goes first, under the
                # pad; into one, last.
                options = [straight_first, diagonal_first] if no < nd \
                    else [diagonal_first, straight_first]
                if nodes[b][1] is None and nodes[a][1] is not None:
                    options.reverse()
            after = after_of[b]
            ranked = []
            for i, v in enumerate(options):
                pts = [P[a]] + v
                t_in = turn(before, P[a], pts[1]) if before is not None else 0.0
                if t_in > bound(a):
                    continue
                if after is not None and turn(pts[-2], P[b], after) > bound(b):
                    continue
                ranked.append((round(t_in, 6), i, v, pts))
            for _, _, v, pts in sorted(ranked):
                if in_lane(pts):
                    continue
                if all(self._piece_clears(job, li, p, q) for p, q in zip(pts, pts[1:])):
                    return v
            return None

        out = [P[0]]
        a = 0
        while a < m - 1:
            before = out[-2] if len(out) >= 2 else None
            hi = a + 1
            while hi < m - 1 and not nodes[hi][2] and hi - a < self.STRAIGHT_REACH:
                hi += 1
            got, b = None, a + 1
            if hi > a + 1:
                got = piece(a, hi, before)
                if got is not None:
                    b = hi
                else:
                    # Farthest first, then halving: what clears need not
                    # shrink with the reach, but each try is checked.
                    good, bad = a + 1, hi
                    while bad - good > 1:
                        mid = (good + bad) // 2
                        v = piece(a, mid, before)
                        if v is not None:
                            good, got = mid, v
                        else:
                            bad = mid
                    b = good
            if got is None:
                got, b = list(nodes[a][3]), a + 1
            out.extend(got)
            a = b
        # Collinear pieces in a row become one.
        merged = [out[0]]
        for k in range(1, len(out) - 1):
            p, q, r = merged[-1], out[k], out[k + 1]
            d1 = (q[0] - p[0], q[1] - p[1])
            d2 = (r[0] - q[0], r[1] - q[1])
            cross = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(cross) <= 1e-9 * (math.hypot(*d1) * math.hypot(*d2) + 1e-12) \
                    and d1[0] * d2[0] + d1[1] * d2[1] > 0:
                continue
            merged.append(q)
        merged.append(out[-1])
        return merged

    def _piece_clears(self, job: NetJob, li: int, a, b) -> bool:
        """Whether a track of the net from ``a`` to ``b`` clears every other
        net's copper, fixed and routed, exactly; checked a stretch of
        CHECK_PIECE at a time, which is the same test (the track is the
        union of its stretches)."""
        n = max(1, int(math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / self.CHECK_PIECE)))
        for k in range(n):
            p = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
            q = (a[0] + (b[0] - a[0]) * (k + 1) / n, a[1] + (b[1] - a[1]) * (k + 1) / n)
            if not (self._static_clears(job.id, job.width, li, p, q)
                    and self._routed_clears(job.id, job.width, li, p, q)):
                return False
        return True

    def _trim_ends(self, job: NetJob, path, term_of, spec, layers):
        """The route with the cells at either end dropped that lie in the
        pad it leaves but have too little room there for the track, and
        the pad each end belongs to.

        A pad can be narrower than the grid can centre a track in: two
        21.65 mil pads one clearance apart leave a 19.7 mil track about a
        mil either side of the centreline, and no cell fell there. The
        best cells inside were taken, and a track through them came 1.4
        mil out of the pad's side into the clearance to the next. The
        stub from the pad's centre, checked exactly, runs to the first
        cell with room instead; where it cannot, the end stays as it was.
        """
        path = list(path)
        ends = [(path[0], term_of.get(tuple(path[0]))), (path[-1], term_of.get(tuple(path[-1])))]
        if len(path) < 3:
            return path, ends
        row = self.rr.clearance_row(job.id)
        half = job.width / 2

        def short(c) -> bool:
            l, y, x = c
            room = self._slack(job.id, l, (np.array([y]), np.array([x])), row)[0]
            return room < half - EPS

        out = path
        for side in (0, 1):
            t = ends[side][1]
            if t is None or out[0 if side == 0 else -1][0] < 0:
                continue
            seq = out if side == 0 else out[::-1]
            k = 0
            while (k + 2 < len(seq) and seq[k][0] >= 0 and term_of.get(tuple(seq[k])) is t
                   and short(seq[k])):
                k += 1
            if k == 0 or seq[k][0] < 0:
                continue
            reach = 1 if self.options.legacy() else self.TRIM_MORE + 1
            found = None
            for kk in range(k, min(len(seq) - 1, k + reach)):
                if seq[kk][0] != seq[k][0]:
                    break
                q = (float(spec.x(seq[kk][2])), float(spec.y(seq[kk][1])))
                if self._stub(job, t, layers[seq[kk][0]], q) is not None:
                    found = kk
                    break
            if found is None:
                if self.options.legacy():
                    continue
                # No stub reaches the pad from here without coming inside
                # someone's clearance, and the cells left in the pad are
                # short of room by definition: the end is left unjoined,
                # an open connection the report names, rather than a track
                # through the clearance. Kept, between two neighbours whose
                # own stubs left a fine-pitch pin no room, the end ran 1.4
                # mil inside the clearance to the next pin.
                seq = seq[k:]
                out = seq if side == 0 else seq[::-1]
                ends[side] = (seq[0], None)
                continue
            seq = seq[found:]
            out = seq if side == 0 else seq[::-1]
            ends[side] = (seq[0], t)
        return out, ends

    #: How many cells past the first with room an end may be trimmed back
    #: to, looking for one a stub from the pad reaches cleanly.
    TRIM_MORE = 4

    def _stub(self, job: NetJob, t: Terminal, layer: str, q, loose=None) -> list | None:
        """The points of a stub from a pad's centre to the cell at ``q``,
        the first that clears every fixed shape of: straight, where the
        track fits wholly in the pad at ``q`` (the pad is convex, so the
        whole stub is pad copper); out along the pad to the point nearest
        ``q`` where the track still fits, then to ``q``; along the pad's
        long axis until level with ``q`` and across to it, as a person
        leaves a fine-pitch pad past its end. Straight too where the track
        fits nowhere in the pad. ``[]`` when ``q`` is the centre, None when
        none clears.

        A straight stub to a cell the track overhangs the pad at runs
        slantwise out of the pad's side, where the grid never judged it
        against the other nets' routes: one passed a via of another net
        at 5.4 mil where 6 was required.

        The cell a route ends on was judged, the stub to it was not: from
        the centre of a long pad to a cell at its end, one ran slantwise out
        of the pad's side, 0.6 mil inside the clearance to the next pad.
        Without the stub the route still joins the pad: the cell it ends on
        lies inside the pad's copper.

        Once the output checks each net against the copper the nets before
        it finally laid (stubs and cut corners, ``bends``), a stub can be
        refused for a neighbour's stub laid first, where checked against
        that neighbour's grid route it was clear. ``loose`` checks it
        against the grid routes, as the router before the stages did.
        """
        if loose is None:
            return (self._stub(job, t, layer, q, False)
                    or (None if self.options.legacy() else self._stub(job, t, layer, q, True)))
        c = (float(t.center[0]), float(t.center[1]))
        if math.hypot(q[0] - c[0], q[1] - c[1]) <= EPS:
            return []
        li = self.grid.layer_index[layer]
        pad = self.board.pads[t.pad]
        half = job.width / 2
        sh = pad.shape_on(layer)
        fits = sh is not None and float(inner_depth(sh, np.array([[q[0]]]),
                                                    np.array([[q[1]]]))[0, 0]) >= half - EPS
        e = self._exit_point(pad, layer, half, q)
        options = []
        if self.pad_entry == "axis":
            options += self._axis_entries(pad, layer, c, q)
        if fits:
            options.append([c, q])
        if e is not None:
            options.append([c, e, q])
        options.append([c, self._axis_point(pad, layer, q), q])
        if e is None and not fits:
            options.append([c, q])
        for pts in options:
            if any(pt is None for pt in pts):
                continue
            pts = [pts[0]] + [b for a, b in zip(pts, pts[1:])
                              if math.hypot(b[0] - a[0], b[1] - a[1]) > EPS]
            if len(pts) < 2:
                continue
            if all(self._static_clears(job.id, job.width, li, a, b)
                   and self._routed_clears(job.id, job.width, li, a, b, loose)
                   for a, b in zip(pts, pts[1:])):
                return pts
        return None

    def _repair_entries(self, out: LayoutBoard, first: int) -> None:
        """On the finished copper: every chain of routed tracks leaving an
        elongated IC pin's centre that does not run straight out along the
        pin's long axis until clear of it gets its head rebuilt that way
        (_centre_entry), checked exactly. Whatever pass laid the line
        (straightening re-lays lines in free geometry and moved one into the
        pin's side band), the entry is judged as laid."""
        jobs = {j.name: j for j in self.jobs}
        tracks = out.tracks
        key = lambda p_: (round(p_[0], 3), round(p_[1], 3))
        for pad in self.board.pads:
            job = jobs.get(pad.net)
            if job is None or not pad.is_smd or self._is_passive(pad):
                continue
            for c_ in pad.copper:
                li = self.grid.layer_index.get(c_.layer)
                if li is None:
                    continue
                fr = self._pad_frame(pad, c_.layer)
                if fr is None or fr[1] <= 1.2 * fr[3]:
                    continue
                c = (float(pad.x), float(pad.y))
                ends = {}
                for i in range(first, len(tracks)):
                    t = tracks[i]
                    if t is None or t.net != pad.net or t.layer != c_.layer:
                        continue
                    for a_ in ((t.x1, t.y1), (t.x2, t.y2)):
                        ends.setdefault(key(a_), []).append(i)
                for i0 in list(ends.get(key(c), ())):
                    t0 = tracks[i0]
                    if t0 is None:
                        continue
                    seq, used, cur, idx = [c], [], c, i0
                    reach = fr[1] + job.width + 2 * self.pitch + 10.0
                    while idx is not None and len(used) < 40:
                        t = tracks[idx]
                        a_, b_ = (t.x1, t.y1), (t.x2, t.y2)
                        nxt = b_ if key(a_) == key(cur) else a_
                        seq.append(nxt); used.append(idx); cur = nxt
                        if math.dist(c, nxt) >= reach:
                            break
                        nb = [j_ for j_ in ends.get(key(nxt), ()) if j_ not in used and tracks[j_] is not None]
                        idx = nb[0] if len(nb) == 1 else None
                    if len(seq) < 2 or self._entries_bad(job, li, [seq[0], seq[1]]) == 0:
                        continue
                    w0 = job.width
                    job.width = t0.width
                    try:
                        new = self._centre_entry(job, li, c_.layer, seq)
                    finally:
                        job.width = w0
                    if new is None:
                        continue
                    for i in used:
                        tracks[i] = None
                    for p_, q_ in zip(new, new[1:]):
                        tracks.append(Track(c_.layer, p_[0], p_[1], q_[0], q_[1], t0.width, pad.net))
                        if self._routed is not None:
                            self._routed.add(li, job.id, geom.capsule(p_[0], p_[1], q_[0], q_[1], t0.width), job.id)
        out.tracks[:] = [t for t in tracks if t is not None]

    CHAMFER = 12.0      # mil cut off each leg of a 90 degree corner, at most

    def _chamfer_corners(self, out: LayoutBoard, first: int) -> None:
        """Every 90 degree corner between two routed tracks of one net (one
        layer, one width, nothing else meeting there, not inside a pad)
        cut to two 45s, where the cut clears every other net's copper: an
        L read as untidy where a 45 would do (owner, 2026-10-18)."""
        jobs = {j.name: j for j in self.jobs}
        tracks = out.tracks
        key = lambda p_: (round(p_[0], 3), round(p_[1], 3))
        ends: dict = {}
        for i in range(first, len(tracks)):
            t = tracks[i]
            for a_ in ((t.x1, t.y1), (t.x2, t.y2)):
                ends.setdefault((t.net, t.layer, key(a_)), []).append(i)
        viapts = {(v.net, key((v.x, v.y))) for v in out.vias}
        padbox = {}
        for p_ in self.board.pads:
            padbox.setdefault(p_.net, []).append(p_)
        done = set()
        for (net, layer, pt), idx in list(ends.items()):
            if len(idx) != 2 or (net, pt) in viapts or net not in jobs:
                continue
            i, j = idx
            if i in done or j in done:
                continue
            ti, tj = tracks[i], tracks[j]
            if ti is None or tj is None or abs(ti.width - tj.width) > 1e-6:
                continue
            far_i = (ti.x2, ti.y2) if key((ti.x1, ti.y1)) == pt else (ti.x1, ti.y1)
            far_j = (tj.x2, tj.y2) if key((tj.x1, tj.y1)) == pt else (tj.x1, tj.y1)
            v_ = (pt[0], pt[1])
            li_, lj = math.dist(v_, far_i), math.dist(v_, far_j)
            if li_ < 1.0 or lj < 1.0:
                continue
            ui = ((far_i[0] - v_[0]) / li_, (far_i[1] - v_[1]) / li_)
            uj = ((far_j[0] - v_[0]) / lj, (far_j[1] - v_[1]) / lj)
            if abs(ui[0] * uj[0] + ui[1] * uj[1]) > 0.01:
                continue                        # only square corners
            if any(math.hypot(p_.x - v_[0], p_.y - v_[1]) < max(max(c.w, c.h) for c in p_.copper) / 2 + ti.width
                   for p_ in padbox.get(net, ())):
                continue                        # near a pad: the entry stays straight
            cut = min(self.CHAMFER, li_ / 2, lj / 2)
            if far_i in [(p_.x, p_.y) for p_ in padbox.get(net, ())]:
                cut = min(cut, li_ - 1.0)
            A = (v_[0] + ui[0] * cut, v_[1] + ui[1] * cut)
            B = (v_[0] + uj[0] * cut, v_[1] + uj[1] * cut)
            job = jobs[net]
            li = self.grid.layer_index.get(layer)
            if li is None:
                continue
            w0 = job.width
            job.width = ti.width
            try:
                ok = self._piece_clears(job, li, A, B)
            finally:
                job.width = w0
            if not ok:
                continue
            tracks[i] = Track(layer, far_i[0], far_i[1], A[0], A[1], ti.width, net)
            tracks[j] = Track(layer, far_j[0], far_j[1], B[0], B[1], tj.width, net)
            tracks.append(Track(layer, A[0], A[1], B[0], B[1], ti.width, net))
            if self._routed is not None:
                self._routed.add(li, job.id, geom.capsule(A[0], A[1], B[0], B[1], ti.width), job.id)
            done.update((i, j))

    def _entries_ok(self, job: NetJob, li: int, pts) -> bool:
        return self._entries_bad(job, li, pts) == 0

    def _entries_bad(self, job: NetJob, li: int, pts) -> int:
        """How many ends of the line sit at an elongated IC pin's centre and do
        not leave straight along the pin's long axis until clear of it."""
        layer = self.grid.layers[li]
        bad = 0
        for a_, b_ in ((pts[0], pts[1]), (pts[-1], pts[-2])):
            pad = self._pad_centres().get((round(a_[0], 2), round(a_[1], 2)))
            if pad is None or pad.net != job.name or self._is_passive(pad):
                continue
            fr = self._pad_frame(pad, layer)
            if fr is None or fr[1] <= 1.2 * fr[3]:
                continue
            ua, a = fr[0], fr[1]
            ll = math.dist(a_, b_)
            if ll < EPS or abs(((b_[0] - a_[0]) * ua[0] + (b_[1] - a_[1]) * ua[1]) / ll) < 0.999                     or ll < a + job.width / 2 - 0.5:
                bad += 1
        return bad

    def _snap_line(self, job: NetJob, pts) -> list:
        """A line's ends that sit on the cell of a via the output moved (to a
        pad's centre, or onto a via reused) moved onto the via; a line left
        with no length dropped. The tracks had started on the cell, up to
        half a pitch off the via, and a stub of a mil ran from the pad's
        centre to the cell."""
        mp = getattr(self, "_via_snap", {}).get(job.id)
        if not mp or len(pts) < 1:
            return list(pts)
        out = list(pts)
        for i in (0, len(out) - 1):
            v = mp.get((round(out[i][0], 3), round(out[i][1], 3)))
            if v is not None:
                out[i] = v
        out = [out[0]] + [q for p_, q in zip(out, out[1:]) if math.dist(p_, q) > EPS]
        if len(out) >= 2 and math.dist(out[0], out[-1]) <= EPS                 and max(math.dist(out[0], q) for q in out) < 2 * self.pitch:
            return []       # out to the cell and back onto the via: no copper needed
        return out if len(out) >= 2 else []

    @staticmethod
    def _multiple_of_45(a, b) -> bool:
        if math.dist(a, b) < 0.05:
            return True
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
        return min(ang, 45.0 - ang) <= 0.1

    def _pad_centres(self) -> dict:
        if getattr(self, "_pad_centre_map", None) is None:
            self._pad_centre_map = {(round(float(p_.x), 2), round(float(p_.y), 2)): p_
                                    for p_ in self.board.pads}
        return self._pad_centre_map

    @staticmethod
    def _pad_frame(pad: Pad, layer: str):
        """The pad's two axes and its half-length along each, long first:
        ((ux, uy), half_long, (vx, vy), half_short)."""
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
            (ax, ay), (bx, by) = sh.pts[0], sh.pts[-1]
            ll = math.hypot(bx - ax, by - ay)
            if ll == 0:
                return None
            ua = ((bx - ax) / ll, (by - ay) / ll)
            ub = (-ua[1], ua[0])
            a, b = ll / 2 + sh.r, sh.r
        else:
            r_ = math.radians(pad.rotation or 0.0)
            ua, ub = (math.cos(r_), math.sin(r_)), (-math.sin(r_), math.cos(r_))
            a = b = sh.r
        return (ua, a, ub, b) if a >= b else (ub, b, ua, a)

    def _centre_entries(self, job: NetJob, li: int, pts) -> list:
        """pad_entry="axis": a line that ends at a pad's centre leaves it
        straight along the pad's long axis, through the middle of its short
        edge, until the track is clear of the pad, and only then jogs onto
        the route (45 or 90 degrees). The grid's cells sit up to half a
        pitch off a pad's centre: the route came in parallel but offset and
        cut into the centre at 45 degrees inside the pad, narrowing the
        mask web to the next pin (owner, 2026-10-18). Checked exactly; the
        line is kept as it was where the new one does not clear."""
        if self.pad_entry != "axis" or len(pts) < 2:
            return list(pts)
        layer = self.grid.layers[li]
        out = list(pts)
        for end in (0, 1):
            seq = out if end == 0 else out[::-1]
            new = self._centre_entry(job, li, layer, seq)
            if new is not None:
                out = new if end == 0 else new[::-1]
        return out

    def _centre_entry(self, job: NetJob, li: int, layer: str, seq):
        c = seq[0]
        pad = self._pad_centres().get((round(c[0], 2), round(c[1], 2)))
        if pad is None or pad.net != job.name:
            return None
        fr = self._pad_frame(pad, layer)
        if fr is None:
            return None
        ua, a, ub, b = fr
        m = job.width / 2 + 2.0
        reach = a + m + job.width / 2 + 2 * self.pitch
        # the point where the route has got clear of the pad
        P, k = None, None
        for i in range(len(seq) - 1):
            p0, p1 = seq[i], seq[i + 1]
            d0, d1 = math.dist(c, p0), math.dist(c, p1)
            if d1 >= reach:
                seg = math.dist(p0, p1)
                lo, hi = 0.0, 1.0
                for _ in range(30):         # where along this piece the distance reaches `reach`
                    mid = (lo + hi) / 2
                    q = (p0[0] + (p1[0] - p0[0]) * mid, p0[1] + (p1[1] - p0[1]) * mid)
                    if math.dist(c, q) < reach:
                        lo = mid
                    else:
                        hi = mid
                P = (p0[0] + (p1[0] - p0[0]) * hi, p0[1] + (p1[1] - p0[1]) * hi) if seg > EPS else p1
                k = i + 1
                break
        if P is None:
            # a short line (to a via just off the pad): its far end, if that is past the pad
            if math.dist(c, seq[-1]) > a + m - EPS:
                P, k = seq[-1], len(seq)
            else:
                return None
        two_pin = self._is_passive(pad)
        axes = [(ua, a)] if (a > 1.2 * b and not two_pin) else sorted(
            [(ua, a), (ub, b)], key=lambda t: -abs((P[0] - c[0]) * t[0][0] + (P[1] - c[1]) * t[0][1]))
        for (ax, h) in axes:
            dot = (P[0] - c[0]) * ax[0] + (P[1] - c[1]) * ax[1]
            sgn = 1.0 if dot >= 0 else -1.0
            u = (ax[0] * sgn, ax[1] * sgn)
            e = (c[0] + u[0] * (h + m), c[1] + u[1] * (h + m))
            du = (P[0] - e[0]) * u[0] + (P[1] - e[1]) * u[1]
            dv = (P[0] - e[0]) * -u[1] + (P[1] - e[1]) * u[0]
            vp = (-u[1], u[0])
            mids = []
            if du > EPS:
                if abs(dv) < 0.05:
                    mids.append([])
                elif du >= abs(dv):
                    mids.append([(e[0] + u[0] * (du - abs(dv)), e[1] + u[1] * (du - abs(dv)))])
                else:
                    # 45 degrees off the axis, then square onto the route: two 45s, no 90
                    sg = 1.0 if dv > 0 else -1.0
                    mids.append([(e[0] + u[0] * du + vp[0] * sg * du, e[1] + u[1] * du + vp[1] * sg * du)])
                mids.append([(e[0] + u[0] * du, e[1] + u[1] * du)])
            elif abs(dv) > b + m:
                # the route heads off sideways: out through the end first, then round
                mids.append([(e[0] + vp[0] * dv, e[1] + vp[1] * dv)])
            for mid in mids:
                head = [c, e] + mid + [P]
                head = [head[0]] + [q for p_, q in zip(head, head[1:]) if math.dist(p_, q) > EPS]
                if any(not self._multiple_of_45(p_, q) for p_, q in zip(head, head[1:])):
                    continue
                if all(self._piece_clears(job, li, p_, q) for p_, q in zip(head, head[1:])):
                    rest = [q for q in seq[k:] if math.dist(q, P) > EPS]
                    return head + rest
        return None

    @staticmethod
    def _axis_entries(pad: Pad, layer: str, c, q) -> list:
        """Stubs that leave the pad along one of its axes and turn once, at
        45 or 90 degrees, onto ``q``: straight when ``q`` lies on an axis.
        Tried first (pad_entry="axis"); a slantwise stub from the centre
        read as a track leaving the pad at a random angle."""
        a = math.radians(pad.rotation or 0.0)
        axes = [(math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))]
        dx, dy = q[0] - c[0], q[1] - c[1]
        out = []
        for ux, uy in axes:
            du = dx * ux + dy * uy
            dv = dx * -uy + dy * ux
            if abs(dv) < 0.25:
                out.append([c, q])
                continue
            if abs(du) < 0.25:
                continue
            sgn = 1.0 if du > 0 else -1.0
            # along the axis, then 45 degrees onto q
            run = abs(du) - abs(dv)
            if run >= 0:
                out.append([c, (c[0] + sgn * run * ux, c[1] + sgn * run * uy), q])
            # along the axis to level with q, then square onto it
            out.append([c, (c[0] + du * ux, c[1] + du * uy), q])
        # nearest first: the axis q is most nearly on
        out.sort(key=lambda pts: sum(math.hypot(b[0] - a_[0], b[1] - a_[1]) for a_, b in zip(pts, pts[1:])))
        return out

    @staticmethod
    def _axis_point(pad: Pad, layer: str, q):
        """Where the line along the pad's long axis comes level with ``q``:
        the corner of a stub that runs out along the pad and then across.
        A TQFP pad as wide as its track, left 2 mil off its axis at the
        pad's end, came 1.4 mil inside the clearance to the next pad;
        along the axis past the end and then across clears it."""
        s = pad.shape_on(layer)
        if s is None:
            return None
        if s.kind == "segment":
            (ax, ay), (bx, by) = s.pts[0], s.pts[-1]
        elif s.kind == "poly" and len(s.pts) == 4:
            p0, p1, _, p3 = s.pts
            far = p1 if math.dist(p0, p1) >= math.dist(p0, p3) else p3
            (ax, ay), (bx, by) = p0, far
        else:
            return None
        ll = math.hypot(bx - ax, by - ay)
        if ll == 0:
            return None
        ux, uy = (bx - ax) / ll, (by - ay) / ll
        cx, cy = float(pad.x), float(pad.y)
        k = (q[0] - cx) * ux + (q[1] - cy) * uy
        return (cx + k * ux, cy + k * uy)

    @staticmethod
    def _exit_point(pad: Pad, layer: str, half: float, q):
        """The point nearest ``q`` where a track of half-width ``half``
        lies wholly inside the pad's copper; None if it fits nowhere.

        Every pad shape is a core (a point, a segment or a rectangle)
        grown by a radius; the track fits where the core, grown by the
        radius less the half-width, reaches (shrunk, for a rectangle,
        when that is negative)."""
        s = pad.shape_on(layer)
        if s is None:
            return None
        m = s.r - half
        qx, qy = q
        if s.kind in ("point", "segment"):
            if m < 0:
                return None
            (ax, ay) = s.pts[0]
            (bx, by) = s.pts[-1]
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            u = 0.0 if ll == 0 else max(0.0, min(1.0, ((qx - ax) * dx + (qy - ay) * dy) / ll))
            core = (ax + u * dx, ay + u * dy)
        elif s.kind == "poly" and len(s.pts) == 4:
            p0, p1, _, p3 = s.pts
            cx = sum(p[0] for p in s.pts) / 4
            cy = sum(p[1] for p in s.pts) / 4
            lx = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            ly = math.hypot(p3[0] - p0[0], p3[1] - p0[1])
            if lx == 0 or ly == 0:
                return None
            ux = ((p1[0] - p0[0]) / lx, (p1[1] - p0[1]) / lx)
            uy = ((p3[0] - p0[0]) / ly, (p3[1] - p0[1]) / ly)
            hx, hy = lx / 2 + min(m, 0.0), ly / 2 + min(m, 0.0)
            if hx < 0 or hy < 0:
                return None
            a = max(-hx, min(hx, (qx - cx) * ux[0] + (qy - cy) * ux[1]))
            b = max(-hy, min(hy, (qx - cx) * uy[0] + (qy - cy) * uy[1]))
            core = (cx + a * ux[0] + b * uy[0], cy + a * ux[1] + b * uy[1])
            m = max(m, 0.0)
        else:
            return None
        d = math.hypot(qx - core[0], qy - core[1])
        if d <= m:
            return (qx, qy)
        return (core[0] + (qx - core[0]) * m / d, core[1] + (qy - core[1]) * m / d)

    def _routed_clears(self, nid: int, width: float, li: int, a, b, loose: bool = False) -> bool:
        """Whether a track from ``a`` to ``b`` on routing layer ``li``
        clears every other net's routes, exactly: a stub is off the grid,
        and no stamp covers it. One run out along a pad and across to its
        cell passed another net's via at 7.63 mil where 7.874 was
        required.

        ``loose`` checks against the other nets' routes as the grid laid
        them, not as the output has laid them so far (see _stub)."""
        if self._routed is None:
            self._routed = self._routed_index()
        index = self._routed
        if loose:
            if getattr(self, "_routed_grid", None) is None:
                self._routed_grid = self._routed_index()
            index = self._routed_grid
        stub = geom.capsule(a[0], a[1], b[0], b[1], width)
        reach = float(self.rr.clearance_row(nid).max())
        return index.clears(stub, li, {nid}, lambda o: self.rr.pair_clearance(nid, o),
                            reach=reach)

    def _routed_index(self):
        """Every net's routed copper per routing layer, as the output lays
        it before its stubs and cut corners: straight runs between cells,
        and vias. Each net's shapes carry its id as their tag, so the
        output can swap them for the copper it finally lays."""
        from .exact import CopperIndex

        spec = self.grid.spec
        idx = CopperIndex(self.L)
        for job in self.jobs:
            for path in job.paths:
                run: list = []
                route = self._smooth(job, path) if self.options.bends else path
                for l, y, x in list(route) + [(-2, 0, 0)]:
                    if run and (l != run[0][0]):
                        cells = [(yy, xx) for _, yy, xx in run]
                        if len(cells) == 1:
                            (yy, xx), = cells
                            idx.add(run[0][0], job.id, geom.circle(float(spec.x(xx)),
                                                                   float(spec.y(yy)), job.width),
                                    job.id)
                        for (ya, xa), (yb, xb) in (_straight_runs(cells) if len(cells) > 1 else []):
                            idx.add(run[0][0], job.id,
                                    geom.capsule(float(spec.x(xa)), float(spec.y(ya)),
                                                 float(spec.x(xb)), float(spec.y(yb)), job.width),
                                    job.id)
                        run = []
                    if l == -1:
                        vx, vy = float(spec.x(x)), float(spec.y(y))
                        if (y, x) in job.pad_via:
                            vx, vy = job.pad_via[(y, x)][:2]
                        if (y, x) in job.reuse:
                            vx, vy = job.reuse[(y, x)]
                        d = job.vias[job.via_at.get((y, x), 0)].diameter
                        for li in range(self.L):
                            idx.add(li, job.id, geom.circle(vx, vy, d), job.id)
                    elif l >= 0:
                        run.append((l, y, x))
        return idx

    def _static_clears(self, nid: int, width: float, li: int, a, b) -> bool:
        """Whether a track of ``width`` from ``a`` to ``b`` on routing layer
        ``li`` clears every fixed shape of another net by its clearance,
        overlaps no keep-out and keeps its distance from the board edge:
        exactly, for copper off the grid."""
        stub = geom.capsule(a[0], a[1], b[0], b[1], width)
        items, boxes = self._fixed_items(li)
        if not items:
            return True
        reach = max(float(self.rr.clearance_row(nid).max()), self.rr.edge_clearance)
        x0, y0, x1, y1 = stub.bbox
        near = np.nonzero((boxes[:, 0] <= x1 + reach) & (boxes[:, 2] >= x0 - reach)
                          & (boxes[:, 1] <= y1 + reach) & (boxes[:, 3] >= y0 - reach))[0]
        for k in near:
            owner, sh = items[k]
            if owner == nid:
                continue
            gap = geom.clearance(stub, sh)
            if owner == KEEPOUT:
                if gap < 0.0:
                    return False
            elif gap < self.rr.pair_clearance(nid, owner) - EPS:
                return False
        return True


def _at(via):
    """Where a via is, to compare two attempts' vias; None for no via."""
    return None if via is None else (round(via.x, 4), round(via.y, 4))


def _straight_runs(cells):
    """Collapse a cell chain into maximal straight segments."""
    out = []
    start = cells[0]
    prev = cells[0]
    direction = None
    for c in cells[1:]:
        d = (c[0] - prev[0], c[1] - prev[1])
        if direction is None:
            direction = d
        elif d != direction:
            out.append((start, prev))
            start = prev
            direction = d
        prev = c
    out.append((start, prev))
    return out


def releasable(router: Router) -> str | None:
    """The plane layer to give back to routing, if any may go.

    First a net's second plane or more. Of those, a plane lying between
    two other planes first, since a plane next to an outer layer is that
    layer's reference; then the one furthest down the stack. On the
    public boards the person routed on exactly such a middle ground layer
    and kept the outer ones whole.

    Then, when no net has a plane to spare, a net's only plane, as a
    layer shared by that pour and the routing (its net is then routed in
    tracks, and the pour fills round everything afterwards). The layer
    whose nets have the fewest pads goes first, and the board keeps at
    least one plane. A dense BGA can need exactly that: the only way out
    from under its balls is through a supply pour's layer.
    """
    per_net: dict[str, list[str]] = {}
    for p in router.pour_planes:
        per_net.setdefault(p.net, []).append(p.layer)
    order = router.board.copper_layers()
    planes = {p.layer for p in router.pour_planes}
    candidates = []
    for net, layers in per_net.items():
        if len(set(layers)) < 2:
            continue
        for layer in set(layers):
            i = order.index(layer)
            sandwiched = order[i - 1] in planes and order[i + 1] in planes
            candidates.append((not sandwiched, -i, layer))
    if candidates:
        return min(candidates)[2]
    if len(planes) < 2:
        return None
    pads = router.board.pads_by_net()
    load = {}
    for p in router.pour_planes:
        load[p.layer] = load.get(p.layer, 0) + len(pads.get(p.net, []))
    return min(load, key=lambda layer: (load[layer], -order.index(layer)))


def route_adaptive(board: LayoutBoard, log: Callable[[str], None] | None = None,
                   **kwargs) -> Router:
    """Route with every covered inner layer as a plane; while that leaves
    connections unmade, give a redundant plane back to routing and route
    again. The best attempt is kept."""
    log = log or (lambda s: None)
    released: set = set()
    best = None
    prev = None
    while True:
        r = Router(board, released=frozenset(released), log=log, **kwargs)
        r.report = r.run(stall_abort=releasable(r) is not None, warm=prev)
        prev = r
        # After the clean-up nothing conflicts: what could not be made
        # legal is counted as failed. The count before it is no score.
        score = math.inf if r.report.unsettled else r.report.failed
        log(f"planes {[p.layer for p in r.pour_planes]}: {r.report.failed} failed")
        if best is None or score < best[0]:
            best = (score, r)
        if score == 0:
            break
        layer = releasable(r)
        if layer is None:
            break
        released.add(layer)
    return _fallback(board, best[1], log, kwargs)


def _fallback(board: LayoutBoard, best: Router, log, kwargs) -> Router:
    """When the planned stages leave connections unmade, route once more
    without the stages that only constrain (the dog-bones laid before the
    signals, the class order, IC pins kept free of vias), starting from the
    routes that still hold, and keep whichever joins more.

    A plan that does not close is not a plan a person keeps: the dog-bones
    were placed before anything else was known, and on a dense board some
    sat where the last few signals had to pass. Pairs and buses stay as
    planned; a passive's pad never takes a via either way. The report
    says when this happened (``stage_report.fallback``).
    """
    from dataclasses import replace

    opts = best.options
    loose = replace(opts, plane_fanout=False, class_order=False,
                    pad_vias="ic" if opts.pad_vias == "bga" else opts.pad_vias, fallback=False)
    if not opts.fallback or not best.report.failed or loose == replace(opts, fallback=False):
        return best
    kw = dict(kwargs)
    kw["stages"] = loose
    r = Router(board, released=best.released, log=log, **kw)
    r.report = r.run(stall_abort=False, warm=best)
    log(f"without the constraining stages: {r.report.failed} failed "
        f"(with them {best.report.failed})")
    if r.report.failed < best.report.failed:
        gained = best.report.failed - r.report.failed
        r.stage_report.fallback = (f"routed again without the dog-bones laid first, the class "
                                   f"order and the IC pins kept free of vias: {gained} more "
                                   f"connection{'s' if gained > 1 else ''} made")
        return r
    best.stage_report.fallback = ""
    return best


def route_board(board: LayoutBoard, **kwargs) -> LayoutBoard:
    """The benchmark engine: route every net, return the routed board."""
    return route_adaptive(board, **kwargs).apply()
