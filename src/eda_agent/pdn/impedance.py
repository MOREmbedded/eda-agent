# SPDX-License-Identifier: Apache-2.0
"""AC impedance of a power distribution network, as the load sees it,
against a target impedance.

Two models, the second used whenever the board has a plane pair for the
rail:

``lumped``: the VRM, every decoupling capacitor (with its own mounting
inductance) and the plane capacitance in parallel at one node. Right
below the planes' first resonance and when the rail has no plane.

``cavity``: the plane pair as a two-dimensional transmission-line
cavity on its real shape, solved by finite differences in the frequency
domain (FDFD). Every grid node carries the plate capacitance of its cell
with the dielectric's complex permittivity; every link between neighbours
carries the plane pair's series impedance per square,

    Z_sq = j w mu0 d + Zs_top + Zs_bottom,

with Zs the exact surface impedance of each copper plane (skin effect from
DC to GHz, ``physics.slab_surface_impedance``). That is the telegrapher
form of the cavity's Helmholtz equation; its eigenfrequencies converge to
the rectangular cavity's (m c / 2 a sqrt(er)) at second order in the
cell. Capacitors, the VRM and the load connect at the nodes nearest
their vias, so spreading inductance, plane resonances and the position
of every capacitor are in the answer, not estimated.

The dielectric is the causal wideband Debye model (Djordjevic-Sarkar)
fitted to the stackup's er and a loss tangent at 1 GHz, so the
capacitance and loss vary with frequency as real laminate does.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ..layout.model import LayoutBoard, Pad
from . import physics as ph
from .decap import (BULK_KINDS, Decap, bulk_kind, default_esl, default_esr,
                    is_capacitor_ref, package_of, parse_capacitance, touchstone_impedance)
from .mounting import VIA_SEARCH_MILS, Copper, mount

#: Cavity grid: at least this many cells across the longer side, at most
#: this many cells in all, and no coarser than lambda / CELLS_PER_WAVE at
#: the top frequency.
MIN_CELLS_ACROSS = 48
MAX_CAVITY_CELLS = 30_000
CELLS_PER_WAVE = 20


class PDNError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Material
# ---------------------------------------------------------------------------

def djordjevic_sarkar(freq_hz, er: float, tand: float, f_ref: float = 1e9,
                      m1: float = 3.0, m2: float = 12.0) -> np.ndarray:
    """Complex relative permittivity e' - j e'' of a laminate with ``er``
    and ``tand`` at ``f_ref``, by the wideband Debye model of Djordjevic
    and Sarkar (2001): causal, near-constant loss tangent from 10^m1 to
    10^m2 Hz, e' falling slowly with frequency."""
    f = np.atleast_1d(np.asarray(freq_hz, dtype=float))
    w1, w2 = 2 * np.pi * 10 ** m1, 2 * np.pi * 10 ** m2
    k = 1.0 / (math.log(10) * (m2 - m1))

    def F(w):
        return k * np.log((w2 + 1j * w) / (w1 + 1j * w))
    fr = F(2 * np.pi * f_ref)
    d_eps = -er * tand / fr.imag
    e_inf = er - d_eps * fr.real
    return e_inf + d_eps * F(2 * np.pi * f)


def surface_impedance(f: np.ndarray, thickness_m: float, rho: float) -> np.ndarray:
    return np.array([ph.slab_surface_impedance(float(x), thickness_m, rho) for x in f])


# ---------------------------------------------------------------------------
# Cavity
# ---------------------------------------------------------------------------

@dataclass
class Cavity:
    power_layer: str
    ground_layer: str
    d_mils: float
    er: float
    tand: float
    t_power_mils: float
    t_ground_mils: float
    x0: float
    y0: float
    cell_mils: float
    cells: np.ndarray                      # (ny, nx) bool
    ids: np.ndarray                        # (ny, nx) node id or -1
    z_power: float = 0.0
    z_ground: float = 0.0
    notes: list[str] = field(default_factory=list)
    #: Share of each cell where both planes have copper (None: all of it).
    fill: Optional[np.ndarray] = None
    #: How whole the copper is across each link between neighbours, 0..1,
    #: shaped (ny, nx-1) and (ny-1, nx) (None: whole).
    link_x: Optional[np.ndarray] = None
    link_y: Optional[np.ndarray] = None

    @property
    def n(self) -> int:
        return int(self.cells.sum())

    @property
    def area_mils2(self) -> float:
        if self.fill is None:
            return self.n * self.cell_mils ** 2
        return float(self.fill[self.cells].sum()) * self.cell_mils ** 2

    def node_fill(self) -> np.ndarray:
        """Overlap share per node, in node order."""
        out = np.ones(self.n)
        if self.fill is not None:
            out[self.ids[self.cells]] = self.fill[self.cells]
        return out

    def extent_mils(self) -> tuple[float, float]:
        iy, ix = np.nonzero(self.cells)
        return (float(ix.max() - ix.min() + 1) * self.cell_mils,
                float(iy.max() - iy.min() + 1) * self.cell_mils)

    def first_mode_hz(self) -> Optional[float]:
        """Lowest resonance of the plane pair as it is shaped (open edges):
        the smallest non-zero eigenvalue of the grid's own Laplacian,
        f = c sqrt(lambda) / (2 pi h sqrt(er)). c / (2 a sqrt(er)) for a
        rectangle of longer side a."""
        import scipy.sparse as sp
        from scipy.sparse.linalg import eigsh
        n = self.n
        if n < 3:
            return None
        L = _laplacian(self)
        M = sp.diags(self.node_fill()).tocsc()
        try:
            vals = eigsh(L, k=min(3, n - 1), M=M, sigma=-1e-3, which="LM",
                         return_eigenvectors=False)
        except Exception:  # noqa: BLE001 - an estimate; never fail the run for it
            return None
        lam = sorted(float(v) for v in vals if v > 1e-9)
        if not lam:
            return None
        return ph.C0 * math.sqrt(lam[0]) / (2 * math.pi * self.cell_mils * ph.MIL
                                            * math.sqrt(self.er))

    @property
    def capacitance_f(self) -> float:
        return ph.EPS0 * self.er * self.area_mils2 * ph.MIL ** 2 / (self.d_mils * ph.MIL)

    def node_near(self, x: float, y: float) -> tuple[int, float]:
        """Nearest cavity node to a point, and its distance in mils."""
        iy, ix = np.nonzero(self.cells)
        cx = self.x0 + (ix + 0.5) * self.cell_mils
        cy = self.y0 + (iy + 0.5) * self.cell_mils
        d2 = (cx - x) ** 2 + (cy - y) ** 2
        k = int(np.argmin(d2))
        return int(self.ids[iy[k], ix[k]]), float(math.sqrt(d2[k]))

    def to_dict(self) -> dict:
        return {"power_layer": self.power_layer, "ground_layer": self.ground_layer,
                "separation_mils": round(self.d_mils, 3), "er_1ghz": self.er,
                "tan_d": self.tand, "area_in2": round(self.area_mils2 / 1e6, 4),
                "capacitance_nf": round(self.capacitance_f * 1e9, 4),
                "cell_mils": round(self.cell_mils, 2), "nodes": self.n,
                "notes": list(self.notes)}


#: A cell with less overlap of the two planes than this is not a node.
MIN_OVERLAP = 0.1
#: A neck this share of what round holes would leave is still taken as
#: round holes (the fine grid places a hole's edge to about a sample).
NECK_SLACK = 0.7
#: The planes are sampled this finely to find clearances and necks, and
#: with no more samples than this on the largest boards.
SAMPLE_MILS = 2.0
MAX_SAMPLES = 40_000_000


def _area_fill(board: LayoutBoard, net: str, cell: float):
    from ..layout.copper_raster import rasterize
    return rasterize(board, cell, nets=[net], kinds={"regions", "planes"}, supersample=8)


def cavity_cell_mils(board: LayoutBoard, f_max_hz: float, er: float) -> float:
    from ..layout.copper_raster import _bounds
    x0, y0, x1, y1 = _bounds(board)
    w, h = x1 - x0, y1 - y0
    lam = ph.C0 / (f_max_hz * math.sqrt(er)) / ph.MIL
    cell = min(lam / CELLS_PER_WAVE, max(w, h) / MIN_CELLS_ACROSS)
    floor = math.sqrt(w * h / MAX_CAVITY_CELLS)
    return max(cell, floor, 2.0)


def find_cavity(board: LayoutBoard, stack, net: str, ground: str, f_max_hz: float,
                cell_mils: float = 0.0, tand: Optional[float] = None,
                near: Optional[list[tuple[float, float]]] = None) -> Optional[Cavity]:
    """The power/ground plane pair carrying the rail: of all pairs of a
    layer with ``net`` area copper and a layer with ``ground`` area copper,
    the one with the largest overlap capacitance. None when the rail has no
    area copper over a ground layer.

    Perforated copper (antipads under a BGA) stays in the model as what
    it is. The planes are closer than an antipad is wide, so a hole in
    either plane is a hole in the pair: a cell holds the capacitance of
    the share of it where both planes have copper, and a link between two
    cells conducts as a sheet with that share of round holes does. A slot
    through either plane opens the link, however narrow, and a neck
    throttles it. When the pair is in several islands the one under most
    of the points ``near`` (the load's pins) is modelled, else the
    largest."""
    if stack is None or not stack.layers:
        return None
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    er0 = max((d.er for d in stack.dielectrics), default=4.3)
    cell = cell_mils or cavity_cell_mils(board, f_max_hz, er0)
    rp = _area_fill(board, net, cell)
    rg = _area_fill(board, ground, cell)
    best = None
    for lp in rp.layers:
        if not (rp.fill[lp] > 0).any():
            continue
        for lg in rg.layers:
            if lg == lp:
                continue
            d = stack.gap_mils(lp, lg)
            if not d:
                continue
            # to choose the pair, the two fills as independent (the
            # overlap itself is measured below, on a finer grid)
            over = rp.fill[lp] * rg.fill[lg]
            c = float(over.sum()) / d
            if c > 0 and (best is None or c > best[0]):
                best = (c, lp, lg, over, d)
    if best is None:
        return None
    _, lp, lg, over, d = best

    # The pair on a grid fine enough to see a clearance. Between two thin
    # planes the field, and so the current, is only where BOTH have copper:
    # a hole in either plane is a hole in the pair (the planes are closer
    # than an antipad is wide). So the pair is one sheet whose voids are the
    # union of the two planes' voids.
    #  * A cell holds the capacitance of its share of that sheet.
    #  * Round holes smaller than a cell thin the sheet without cutting it:
    #    a link conducts as a sheet with a square array of round holes of
    #    that void share does (``ph.holed_sheet_conductance``).
    #  * A slot is another matter. The narrowest place on the way between
    #    two cell centres is measured; where it is narrower than round
    #    holes of that void share leave, the link is throttled by the
    #    square root of the shortfall, and a slot right across opens it,
    #    however thin. The square root is what a short neck does (a row of
    #    antipads nearly touching, a split with a bridge across it): fine
    #    solutions of both are met within about 15 % from a neck of a
    #    tenth of the way to none. A long narrow channel conducts less
    #    than this says.
    from ..layout.copper_raster import MAX_CELLS, rasterize
    ny_, nx_ = over.shape
    ratio = int(min(16, max(4, math.ceil(cell / 12.0))))
    ratio += ratio % 2
    while ratio > 4 and nx_ * ny_ * ratio * ratio > MAX_CELLS:
        ratio -= 2
    fine = cell / ratio
    # samples of 2 mil keep a copper web of 4 mil between two antipads
    # whole; a larger board is sampled as finely as memory allows
    sample = max(SAMPLE_MILS, math.sqrt(nx_ * ny_ * cell * cell / MAX_SAMPLES))
    sub = max(2, math.ceil(fine / sample - 1e-9))
    sample = fine / sub
    fp_ = rasterize(board, fine, nets=[net], layers=[lp], kinds={"regions", "planes"},
                    supersample=sub, links=True)
    fg_ = rasterize(board, fine, nets=[ground], layers=[lg], kinds={"regions", "planes"},
                    supersample=sub, links=True)

    def on_fine_grid(arr: np.ndarray) -> np.ndarray:
        big = np.zeros((ny_ * ratio, nx_ * ratio))
        big[:min(arr.shape[0], big.shape[0]), :min(arr.shape[1], big.shape[1])] = \
            arr[:big.shape[0], :big.shape[1]]
        return big
    # In a fine cell both planes fill in part, the smaller share: exact
    # where the two are cut by the same hole (a via of another net through
    # both), and two different holes seldom meet inside one fine cell.
    copper = on_fine_grid(np.minimum(fp_.fill[lp], fg_.fill[lg]))
    over = copper.reshape(ny_, ratio, nx_, ratio).mean(axis=(1, 3))
    both = over >= MIN_OVERLAP
    if not both.any():
        return None
    solid = copper >= 0.5
    half = ratio // 2

    def neck(axis: int) -> np.ndarray:
        """Open share of the narrowest place between two cells.

        Each fine row across the way is followed from one cell's centre
        to the next. Where a cell's centre is off the copper (a part cell
        at the copper's edge, an antipad on the centre) the row starts at
        the copper nearest the centre instead: the cell is its copper,
        wherever in the cell that lies. A gap further on is a gap."""
        lk_p = fp_.link_x[lp] if axis == 0 else fp_.link_y[lp]
        lk_g = fg_.link_x[lg] if axis == 0 else fg_.link_y[lg]
        lk = on_fine_grid(np.minimum(lk_p, lk_g))       # link k joins fine cells k and k + 1
        cu = solid
        n_al, n_ac = nx_, ny_
        if axis == 1:
            lk, cu, n_al, n_ac = lk.T, solid.T, ny_, nx_
        c = cu.reshape(n_ac * ratio, n_al, ratio)
        left, right = c[..., :half][..., ::-1], c[..., half:]
        # empty fine cells between the centre and the copper, each way
        dl = np.where(left.any(-1), left.argmax(-1), ratio)
        dr = np.where(right.any(-1), right.argmax(-1), ratio)
        k = np.arange(ratio)
        off = (dl > 0) | (dr > 0)
        to_right = ((off & (dr < ratio) & (dr <= dl))[..., None]
                    & (k >= half - 1) & (k <= (half + dr - 1)[..., None]))
        to_left = ((off & (dl < dr))[..., None]
                   & (k >= (half - 1 - dl)[..., None]) & (k <= half - 1))
        lk = np.maximum(lk, (to_right | to_left).reshape(n_ac * ratio, n_al * ratio))
        blk = lk[:, half:half + (n_al - 1) * ratio]
        out = blk.reshape(n_ac, ratio, n_al - 1, ratio).mean(axis=1).min(axis=-1)
        return out if axis == 0 else out.T

    def links(axis: int) -> np.ndarray:
        pair = ((over[:, :-1] + over[:, 1:]) if axis == 0 else (over[:-1, :] + over[1:, :])) / 2.0
        void = np.clip(1.0 - pair, 0.0, 1.0)
        k = ph.holed_sheet_conductance(void)
        # what a square array of round holes of this void share leaves open
        round_neck = np.maximum(1.0 - 2.0 * np.sqrt(void / np.pi), 0.05)
        k = k * np.sqrt(np.clip(neck(axis) / (NECK_SLACK * round_neck), 0.0, 1.0))
        a_, b_ = (both[:, :-1], both[:, 1:]) if axis == 0 else (both[:-1, :], both[1:, :])
        return np.where(a_ & b_, np.clip(k, 0.0, 1.0), 0.0)
    lx, ly = links(0), links(1)
    idx = np.full(both.shape, -1, dtype=np.int64)
    idx[both] = np.arange(int(both.sum()))
    ea = np.concatenate([idx[:, :-1][lx > 0], idx[:-1, :][ly > 0]])
    eb = np.concatenate([idx[:, 1:][lx > 0], idx[1:, :][ly > 0]])
    n_all = int(both.sum())
    n_isl, lab = connected_components(
        coo_matrix((np.ones(ea.size), (ea, eb)), shape=(n_all, n_all)), directed=False)
    notes = []
    if sample > 1.5 * SAMPLE_MILS:
        notes.append(f"the planes are sampled every {sample:.1f} mil (a large board): a copper "
                     f"web under {2 * sample:.0f} mil between two antipads may read as cut")
    if n_isl > 1:
        area = np.bincount(lab, weights=over[both], minlength=n_isl)
        keep = int(np.argmax(area))
        how = "the largest"
        if near:
            iy, ix = np.nonzero(both)
            cx = rp.x0 + (ix + 0.5) * cell
            cy = rp.y0 + (iy + 0.5) * cell
            votes = np.zeros(n_isl)
            for (px, py) in near:
                d2 = (cx - px) ** 2 + (cy - py) ** 2
                k = int(np.argmin(d2))
                if d2[k] <= (1.5 * cell) ** 2:
                    votes[lab[k]] += 1
            if votes.any() and int(np.argmax(votes)) != keep:
                keep = int(np.argmax(votes))
                how = "the one under the load"
        # islands worth a word: more than a few cells
        real = int((area * cell * cell >= max(4 * cell * cell, 0.01e6)).sum())
        if real > 1:
            notes.append(f"the plane pair is in {real} separate islands; {how} "
                         f"({area[keep] * cell * cell / 1e6:.2f} in2) is modelled")
        mask = np.zeros(both.shape, dtype=bool)
        mask[both] = lab == keep
        both = mask
        lx = np.where(both[:, :-1] & both[:, 1:], lx, 0.0)
        ly = np.where(both[:-1, :] & both[1:, :], ly, 0.0)
    ids = np.full(both.shape, -1, dtype=np.int64)
    ids[both] = np.arange(int(both.sum()))
    er = stack.er_between(lp, lg)
    cp, cg = stack.get(lp), stack.get(lg)
    tan_d = tand if tand is not None else (
        next((x.tan_d for x in stack.dielectrics), 0.02))
    return Cavity(lp, lg, d, er, tan_d, cp.copper_mils if cp else 1.4,
                  cg.copper_mils if cg else 1.4, rp.x0, rp.y0, cell, both, ids,
                  cp.z_mils if cp else 0.0, cg.z_mils if cg else 0.0, notes,
                  fill=np.where(both, over, 0.0), link_x=lx, link_y=ly)


def rectangular_cavity(a_mils: float, b_mils: float, d_mils: float, er: float,
                       tand: float = 0.02, cell_mils: float = 0.0,
                       t_mils: float = 1.4) -> Cavity:
    """A bare rectangular plane pair (for checks and what-ifs)."""
    cell = cell_mils or max(a_mils, b_mils) / MIN_CELLS_ACROSS
    nx, ny = int(round(a_mils / cell)), int(round(b_mils / cell))
    cells = np.ones((ny, nx), dtype=bool)
    ids = np.arange(nx * ny).reshape(ny, nx)
    return Cavity("P", "G", d_mils, er, tand, t_mils, t_mils, 0.0, 0.0,
                  a_mils / nx if nx else cell, cells, ids)


def _laplacian(cav: Cavity):
    import scipy.sparse as sp
    ids = cav.ids
    a, b, g = [], [], []
    for A, B, lk in ((ids[:, :-1], ids[:, 1:], cav.link_x),
                     (ids[:-1, :], ids[1:, :], cav.link_y)):
        m = (A >= 0) & (B >= 0)
        if lk is not None:
            m &= lk > 0
        a.append(A[m]); b.append(B[m])
        g.append(np.ones(int(m.sum())) if lk is None else lk[m].astype(float))
    a = np.concatenate(a); b = np.concatenate(b); g = np.concatenate(g)
    n = cav.n
    L = sp.coo_matrix((np.concatenate([g, g, -g, -g]),
                       (np.concatenate([a, b, a, b]), np.concatenate([a, b, b, a]))),
                      shape=(n, n)).tocsc()
    return L


def solve_cavity(cav: Cavity, freqs: np.ndarray, ports: list[tuple[int, np.ndarray]],
                 load_nodes: list[int], rho: float = ph.RHO_CU_20C,
                 extra_shunt: Optional[np.ndarray] = None,
                 observe_nodes: Optional[list[int]] = None,
                 load_weights: Optional[list[float]] = None) -> np.ndarray:
    """Impedance at the load nodes with each port (node, Z(f) array)
    shunting its node to the ground plane. 1 A goes in split by
    ``load_weights`` (the pins at each node; equal when not given) and the
    voltage read is the mean over the nodes with the same weights.
    ``observe_nodes`` reads the voltage elsewhere instead: the transfer
    impedance from the load to those nodes."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import splu

    n = cav.n
    L = _laplacian(cav)
    d_m = cav.d_mils * ph.MIL
    area = (cav.cell_mils * ph.MIL) ** 2 * cav.node_fill()
    eps = djordjevic_sarkar(freqs, cav.er, cav.tand)
    zs_p = surface_impedance(freqs, cav.t_power_mils * ph.MIL, rho)
    zs_g = surface_impedance(freqs, cav.t_ground_mils * ph.MIL, rho)
    wts = np.ones(len(load_nodes)) if load_weights is None else np.asarray(load_weights, float)
    if len(wts) != len(load_nodes) or not (wts.sum() > 0):
        raise PDNError("one positive weight per load node")
    wts = wts / wts.sum()
    rhs = np.zeros(n, dtype=complex)
    for k, wt in zip(load_nodes, wts):
        rhs[k] += wt
    out = np.empty(len(freqs), dtype=complex)
    for i, f in enumerate(freqs):
        w = 2 * np.pi * f
        zsq = 1j * w * ph.MU0 * d_m + zs_p[i] + zs_g[i]
        diag = 1j * w * ph.EPS0 * eps[i] * area / d_m
        for (node, z) in ports:
            diag[node] += 1.0 / z[i]
        if extra_shunt is not None:
            diag += extra_shunt[i]
        A = (L / zsq + sp.diags(diag)).tocsc()
        v = splu(A).solve(rhs)
        if observe_nodes:
            out[i] = np.mean(v[observe_nodes])
        else:
            out[i] = np.dot(wts, v[load_nodes])
    return out


def lumped(freqs: np.ndarray, branches: list[np.ndarray], c_plane: float = 0.0,
           er: float = 4.3, tand: float = 0.02) -> np.ndarray:
    y = np.zeros(len(freqs), dtype=complex)
    if c_plane > 0:
        eps = djordjevic_sarkar(freqs, er, tand)
        y += 1j * 2 * np.pi * freqs * c_plane * eps / er
    for z in branches:
        y += 1.0 / z
    return 1.0 / y


# ---------------------------------------------------------------------------
# Finding the parts
# ---------------------------------------------------------------------------

def _pads_of(board: LayoutBoard, ref: str) -> list[Pad]:
    return [p for p in board.pads if p.comp == ref]


def mount_for(board: LayoutBoard, stack, cav: Optional[Cavity], net: str, ground: str,
              pwr_pads: list[Pad], gnd_pads: list[Pad], plating: float,
              reach: float = VIA_SEARCH_MILS, copper: Optional[Copper] = None) -> dict:
    """Mounting inductance of a part between its pads and the cavity; see
    ``mounting``. ``_total`` is the inductance in henries."""
    copper = copper or Copper(board, stack, plating)
    out = mount(copper, cav, net, ground, pwr_pads, gnd_pads, reach)
    out["_total"] = out["_l"]
    return out


def decaps_without_a_value(board: LayoutBoard, net: str, ground: str,
                           given: Optional[list[dict]] = None) -> list[str]:
    """Capacitors across the rail whose comment is not a capacitance and
    for which the caller gave none: the ones worth asking the project's
    parameters about."""
    have = {o["ref"] for o in (given or [])
            if isinstance(o, dict) and o.get("ref") and (o.get("c_uf") or o.get("c_farads"))}
    out = []
    for ref, _pw, _gn in find_decaps(board, net, ground):
        comp = board.component(ref)
        if ref not in have and parse_capacitance(comp.comment if comp else "") is None:
            out.append(ref)
    return out


def _shares_ground(pads: list[Pad], net: str, ground: str) -> bool:
    """A four-pad part with one pad on the rail, one on one other net and
    two on ground: a feedthrough capacitor (in, out, ground twice) or two
    capacitors in one body. Either way the rail sees the part's value to
    ground."""
    rest = [p.net for p in pads if p.net not in (net, ground)]
    return (sum(p.net == net for p in pads) == 1 and sum(p.net == ground for p in pads) == 2
            and len(rest) == 1 and bool(rest[0]))


def find_decaps(board: LayoutBoard, net: str, ground: str, also=(),
                skipped: Optional[list[str]] = None) -> list[tuple[str, list[Pad], list[Pad]]]:
    """Capacitors across the rail and its ground: parts with pads on both
    whose designator is a capacitor's (C1, CB3; not CN1 or CR2) or whose
    comment reads as a capacitance. Two- and three-pad parts are taken,
    parts with more pads when all of them are on these two nets (a
    low-inductance or polymer part with doubled terminals), and a
    four-pad part with two ground pads and one pad on one other net (a
    feedthrough capacitor). Any other array with
    other nets on it is not: it is named in ``skipped`` (pass it in
    ``decaps`` to count it). ``also``: designators the caller gave data
    for, taken whatever they look like."""
    by_ref: dict[str, list[Pad]] = {}
    for p in board.pads:
        if p.comp:
            by_ref.setdefault(p.comp, []).append(p)
    out = []
    for ref, pads in by_ref.items():
        pw = [p for p in pads if p.net == net]
        gn = [p for p in pads if p.net == ground]
        if not pw or not gn:
            continue
        if ref in also:
            out.append((ref, pw, gn))
            continue
        comp = board.component(ref)
        comment = comp.comment if comp else ""
        if not (is_capacitor_ref(ref) or parse_capacitance(comment) is not None):
            continue
        others = sorted({p.net for p in pads if p.net and p.net not in (net, ground)})
        if len(pads) <= 3 or not others or _shares_ground(pads, net, ground):
            out.append((ref, pw, gn))
        elif skipped is not None:
            skipped.append(f"{ref}: {len(pads)} pads, also on {', '.join(others[:3])}"
                           + (" ..." if len(others) > 3 else "")
                           + " (an array?); not counted: pass it in decaps to count it")
    return out


def _centre(points) -> Optional[tuple[float, float]]:
    """Mean of [(x, y, weight)]."""
    wsum = sum(p[2] for p in points)
    if not points or wsum <= 0:
        return None
    return (sum(p[0] * p[2] for p in points) / wsum, sum(p[1] * p[2] for p in points) / wsum)


def _kind_of(o: dict, fp: str, comment: str, c: float, th: bool) -> str:
    """The part's kind: given (kind: mlcc | polymer | tantalum |
    electrolytic), else read from its footprint, comment and value."""
    given = str(o.get("kind", "") or "").strip().lower()
    if given:
        if given in ("mlcc", "ceramic"):
            return ""
        if given not in BULK_KINDS:
            raise PDNError(f"{o.get('ref', 'decap')}: kind {o['kind']!r} is not one of "
                           f"mlcc, {', '.join(BULK_KINDS)}")
        return given
    return bulk_kind(fp, comment, c, th)


def build_decaps(board: LayoutBoard, stack, cav, net: str, ground: str,
                 overrides: Optional[list[dict]], plating: float,
                 exclude: Optional[list[str]] = None,
                 copper: Optional[Copper] = None,
                 values: Optional[dict] = None) -> tuple[list[Decap], list[str]]:
    """``values``: {ref: (farads, where it was read)} for parts whose
    comment is not their value (read from the project's parameters)."""
    notes: list[str] = []
    values = values or {}
    from_values: list[str] = []
    over = {o["ref"]: o for o in (overrides or []) if o.get("ref")}
    excl = set(exclude or [])
    caps: list[Decap] = []
    skipped: list[str] = []
    found = find_decaps(board, net, ground, set(over), skipped) if board is not None else []
    found_refs = {r for r, _, _ in found}
    notes += [s for s in skipped if s.split(":")[0] not in excl]
    if board is not None and copper is None:
        copper = Copper(board, stack, plating)
    assumed: list[str] = []
    guessed: list[str] = []
    for ref, pw, gn in found:
        if ref in excl:
            continue
        comp = board.component(ref)
        comment = comp.comment if comp else ""
        fp = comp.footprint if comp else ""
        o = over.get(ref, {})
        c = o.get("c_farads") or (o.get("c_uf") and o["c_uf"] * 1e-6) or parse_capacitance(comment)
        where = ""
        if not c and values.get(ref):
            c, where = values[ref]
            from_values.append(ref)
        if not c:
            notes.append(f"{ref}: no capacitance in its comment {comment!r}; skipped "
                         f"(pass it in decaps)")
            continue
        th = any(p.hole > 0 for p in pw + gn)
        kind = _kind_of(o, fp, comment, float(c), th)
        pkg = o.get("package") or package_of(fp)
        esr = o.get("esr_ohms", default_esr(c, kind))
        esl = o["esl_nh"] * 1e-9 if "esl_nh" in o else default_esl(pkg, kind, th)
        src = "user" if o else "defaults"
        d = Decap(ref, float(c), float(esr), float(esl), pkg,
                  x=sum(p.x for p in pw + gn) / len(pw + gn),
                  y=sum(p.y for p in pw + gn) / len(pw + gn),
                  layer=copper.pad_layer(pw[0]), source=src, kind=kind or "mlcc")
        typical = [f"{name} {val:g} {unit}" for key, name, val, unit in
                   (("esr_ohms", "ESR", round(esr * 1e3, 1), "mohm"),
                    ("esl_nh", "ESL", round(esl * 1e9, 2), "nH")) if key not in o]
        if where:
            d.notes.append(f"value from {where}")
        mine = [p for p in board.pads if p.comp == ref]
        if _shares_ground(mine, net, ground):
            other = next(p.net for p in mine if p.net not in (net, ground))
            d.notes.append(f"four pads, one on {other}: taken as a feedthrough capacitor or "
                           f"two in one body, its value to ground counted on {net}; the "
                           f"path through it to {other} is not modelled")
        if o.get("touchstone"):
            d.touchstone = touchstone_impedance(o["touchstone"], o.get("config", "shunt"))
            d.source = "touchstone"
        elif kind and typical:
            d.notes.append(f"{kind} part on typical values ({', '.join(typical)}): these "
                           f"vary several times between series, read the data sheet")
        elif not kind and not pkg and "esl_nh" not in o:
            d.notes.append("package not recognised from the footprint; 0.5 nH ESL assumed"
                           + ("; 10 uF or more taken as a ceramic chip: say kind if it "
                              "is not" if c >= 10e-6 and "kind" not in o else ""))
        mt = mount(copper, cav, net, ground, pw, gn)
        at = _centre(mt.get("_at", []) + mt.get("_at_ground", []))
        if at is not None:
            d.x, d.y = at                    # where its vias meet the planes
        if "mount_nh" in o:
            d.mount_l = o["mount_nh"] * 1e-9
            mt = {"total_nh": o["mount_nh"], "note": "given"}
        else:
            d.mount_l = mt["_l"]
            d.mount_r = mt["_r"]
            if mt.get("assumed"):
                assumed.append(ref)
            elif "nearest via" in mt.get("how", []):
                guessed.append(ref)
        d.mount_detail = {k: v for k, v in mt.items() if not k.startswith("_")}
        caps.append(d)
    if from_values:
        notes.append(f"{len(from_values)} capacitor(s) with no value in the comment: "
                     f"the value is read from the part's parameters")
    if guessed:
        notes.append(f"{len(guessed)} capacitor(s) with no copper drawn from a pad to a via: "
                     f"the nearest via of the net is taken as theirs: {', '.join(guessed[:8])}"
                     + (" ..." if len(guessed) > 8 else ""))
    if assumed:
        notes.append(f"{len(assumed)} capacitor(s) with no via found by their pads, "
                     f"1 nH mounting assumed: {', '.join(assumed[:8])}"
                     + (" ..." if len(assumed) > 8 else ""))
    # capacitors given that are not on the board (what-if, or schematic stage)
    on_board = {p.comp for p in board.pads} if board is not None else set()
    for ref, o in over.items():
        if ref in found_refs or ref in excl:
            continue
        c = o.get("c_farads") or (o.get("c_uf") and o["c_uf"] * 1e-6)
        if not c:
            notes.append(f"decaps: {ref} is not a part across {net} and {ground} and has "
                         f"no c_uf; ignored")
            continue
        n = int(o.get("count", 1))
        pkg = o.get("package", "")
        kind = _kind_of(o, pkg, "", float(c), False)
        d = Decap(ref, float(c) * n, float(o.get("esr_ohms", default_esr(c, kind))) / n,
                  (o["esl_nh"] * 1e-9 if "esl_nh" in o else default_esl(pkg, kind)) / n, pkg,
                  mount_l=float(o.get("mount_nh", 0.5)) * 1e-9 / n,
                  x=float(o.get("x", 0.0)), y=float(o.get("y", 0.0)), source="user",
                  kind=kind or "mlcc")
        if o.get("touchstone"):
            d.touchstone = touchstone_impedance(o["touchstone"], o.get("config", "shunt"))
            d.source = "touchstone"
        if n > 1:
            d.notes.append(f"{n} in parallel")
        if ref in on_board:
            d.notes.append(f"on the board but not across {net} and {ground}: taken as an "
                           f"added part")
        if "mount_nh" not in o:
            d.notes.append("added part: 0.5 nH mounting assumed (give mount_nh)")
        if "x" not in o and board is not None:
            d.notes.append("no x/y given: attached at the load")
        caps.append(d)
    return caps, notes


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _bands(f: np.ndarray, zmag: np.ndarray, target: float) -> list[dict]:
    over = zmag > target
    bands = []
    i = 0
    while i < len(f):
        if over[i]:
            j = i
            while j + 1 < len(f) and over[j + 1]:
                j += 1
            k = i + int(np.argmax(zmag[i:j + 1]))
            bands.append({"from_mhz": round(f[i] / 1e6, 5), "to_mhz": round(f[j] / 1e6, 5),
                          "peak_mhz": round(f[k] / 1e6, 5),
                          "peak_mohm": round(zmag[k] * 1e3, 4),
                          "over_target_x": round(zmag[k] / target, 3)})
            i = j + 1
        else:
            i += 1
    return bands


def _peaks(f: np.ndarray, zmag: np.ndarray, limit: int = 8) -> list[dict]:
    from scipy.signal import find_peaks
    lz = np.log(zmag)
    idx, props = find_peaks(lz, prominence=0.15)
    order = np.argsort(-props["prominences"])[:limit]
    return sorted([{"mhz": round(float(f[idx[k]]) / 1e6, 5),
                    "mohm": round(float(zmag[idx[k]]) * 1e3, 4)} for k in order],
                  key=lambda p: p["mhz"])


def _suggest(bands: list[dict], target: float, part_l: float, f_max: float) -> list[dict]:
    """What each band over the target needs, from the two limits that hold
    for any parallel bank: below the band, enough capacitance
    (C >= 1 / (2 pi f_low Zt)); across it, little enough inductance
    (L <= Zt / (2 pi f_high)), counted in mounted capacitors of the
    average inductance ``part_l``."""
    out = []
    for b in bands:
        f_lo = b["from_mhz"] * 1e6
        f_hi = min(b["to_mhz"] * 1e6, f_max)
        c_min = 1.0 / (2 * math.pi * f_lo * target)
        l_max = target / (2 * math.pi * f_hi)
        n = max(1, math.ceil(part_l / l_max))
        item = {"band_mhz": [b["from_mhz"], b["to_mhz"]],
                "needs_c_uf_at_least": round(c_min * 1e6, 4),
                "needs_l_ph_at_most": round(l_max * 1e12, 3),
                "mounted_caps_in_parallel": n}
        if f_hi > 50e6:
            item["note"] = ("above ~50 MHz the plane pair and the package carry the "
                            "load; PCB capacitors rarely help past the frequency where "
                            "their mounted inductance meets the plane capacitance. "
                            "Lower the inductance (via-in-pad, shorter fan-out, more "
                            "via pairs, thinner dielectric to the planes) or relax "
                            "the target's top frequency to what the package covers.")
        elif f_lo < 1e6:
            item["note"] = "low-frequency band: VRM loop bandwidth and bulk capacitance"
        out.append(item)
    return out


def write_spice(path, freqs, decaps: list[Decap], vrm: Optional[tuple[float, float]],
                c_plane: float, load_l: float, title: str, load_r: float = 0.0) -> None:
    """A lumped SPICE deck of the network (LTspice and ngspice): every
    capacitor as R-L-C with its mounting, the VRM, the plane capacitance,
    and a 1 A AC source at the load, so V(load) reads ohms."""
    lines = [f"* {title}", "* PDN lumped equivalent written by eda-agent (sim_pdn_impedance)",
             "* V(load) in volts equals |Z| in ohms (1 A AC injection)."]
    for i, d in enumerate(decaps, 1):
        lines.append(f"R{i} pdn n{i}a {d.esr + d.mount_r:.6g}")
        lines.append(f"L{i} n{i}a n{i}b {d.esl + d.mount_l:.6g}")
        lines.append(f"C{i} n{i}b 0 {d.c:.6g}  ; {d.ref}")
    if vrm is not None:
        lines.append(f"RVRM pdn nv {vrm[0]:.6g}")
        lines.append(f"LVRM nv 0 {vrm[1]:.6g}")
    if c_plane > 0:
        lines.append(f"CPLANE pdn 0 {c_plane:.6g}")
    lines.append(f"LLOAD load nl {max(load_l, 1e-15):.6g}")
    lines.append(f"RLOAD nl pdn {max(load_r, 1e-9):.6g}")
    lines.append("I1 0 load AC 1")
    lines.append(f".ac dec 50 {freqs[0]:.6g} {freqs[-1]:.6g}")
    lines.append(".end")
    from pathlib import Path
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


#: matplotlib's text layout (the 10^n tick labels go through its mathtext
#: parser) keeps state between calls, so two figures drawn at once fail
#: even on the object interface: one at a time.
_PLOT_LOCK = threading.Lock()


def _plot(path, f, zmag, target, zlump=None, title="") -> bool:
    """The curve as a PNG. Drawn with matplotlib's object interface (no
    pyplot figure registry) and under a lock, so two runs at once neither
    draw into each other's figure nor trip the text parser. False when
    matplotlib is not installed."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception:  # noqa: BLE001 - optional extra
        return False
    with _PLOT_LOCK:
        fig = Figure(figsize=(8, 4.5), dpi=120)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.loglog(f, zmag * 1e3, label="|Z| at the load", color="#1f5fbf", lw=1.8)
        if zlump is not None:
            ax.loglog(f, np.abs(zlump) * 1e3, label="lumped (no spreading)",
                      color="#999999", lw=1.0, ls="--")
        if target:
            ax.axhline(target * 1e3, color="#c0392b", lw=1.2, label="target")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("|Z| (mohm)")
        ax.set_title(title)
        ax.grid(True, which="both", alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(path)
    return True


#: Width taken for the copper that joins a part to a plane pair it is not
#: over, when the board does not say (mils).
OFF_PLANE_TRACK_MILS = 10.0
#: How far along the copper a load pin's via may be before the search
#: widens (mils).
LOAD_VIA_SEARCH_MILS = 100.0
MAX_POINTS = 2001
#: More capacitors than this are listed by kind in the reply; the file has
#: them all.
LIST_DECAPS = 30

_DECAP_KEYS = {"ref", "c_uf", "c_farads", "esr_ohms", "esl_nh", "mount_nh", "touchstone",
               "config", "package", "kind", "count", "x", "y"}
_VRM_KEYS = {"r_mohm", "l_nh", "bandwidth_khz", "ref", "refs", "pads", "disable"}
_LOAD_KEYS = {"ref", "refs", "pads", "mount_nh", "include_vias", "return_pads", "name"}
_POSITIVE = {"c_uf", "c_farads", "count", "bandwidth_khz"}


def _clean(kind: str, spec, keys: set, numeric: tuple, notes: list[str]) -> dict:
    """A user dict with its numbers as numbers: a wrong type or a negative
    value is an error here, not a traceback or a silent default later; a
    key the tool does not know is said, not dropped."""
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise PDNError(f"{kind} is a dict, got {spec!r}")
    out = dict(spec)
    unknown = sorted(k for k in out if k not in keys and not str(k).startswith("_"))
    if unknown:
        notes.append(f"{kind}: {unknown} not known and ignored; known keys: {sorted(keys)}")
    for k in numeric:
        if k in out and out[k] is not None:
            try:
                v = float(out[k])
            except (TypeError, ValueError):
                raise PDNError(f"{kind}: {k} must be a number, got {out[k]!r}") from None
            if not math.isfinite(v) or v < 0 or (k in _POSITIVE and v <= 0):
                raise PDNError(f"{kind}: {k} must be {'above 0' if k in _POSITIVE else '0 or more'}, "
                               f"got {out[k]!r}")
            out[k] = v
    return out


def _decap_list(caps: list[Decap]) -> dict:
    """The capacitors for the reply: each one, or by kind when there are many."""
    rows = [c.to_dict() for c in caps]
    if len(rows) <= LIST_DECAPS:
        return {"decaps": rows}
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["c_uf"], r["package"], r["kind"], r["model"], r["esr_mohm"],
                           r["esl_nh"]), []).append(r)
    out = []
    for (c_uf, pkg, kind, model, esr, esl), rs in sorted(groups.items(),
                                                         key=lambda kv: -len(kv[1])):
        mounts = [r["mount_nh"] for r in rs]
        srf = [r["srf_mhz"] for r in rs]
        out.append({"count": len(rs), "c_uf": c_uf, "package": pkg, "kind": kind, "model": model,
                    "esr_mohm": esr, "esl_nh": esl,
                    "mount_nh": [min(mounts), max(mounts)],
                    "srf_mhz": [min(srf), max(srf)],
                    "refs": [r["ref"] for r in rs][:12]})
    return {"decap_count": len(rows), "decap_groups": out,
            "decaps_note": f"{len(rows)} capacitors, listed by kind; every one is in the "
                           f"decaps CSV (write_files)"}


def analyse(board: Optional[LayoutBoard], stack, net: str, ground: str, *,
            load: dict, target_ohms: float = 0.0, volts: float = 0.0,
            ripple_pct: float = 0.0, step_amps: float = 0.0,
            f_min_hz: float = 1e3, f_max_hz: float = 1e9, points: int = 241,
            decaps: Optional[list[dict]] = None, exclude: Optional[list[str]] = None,
            vrm: Optional[dict] = None, mode: str = "auto", cell_mils: float = 0.0,
            plating_mils: float = ph.DEFAULT_PLATING_MILS, temp_c: float = 25.0,
            tand: Optional[float] = None, out_dir=None,
            cap_values: Optional[dict] = None) -> dict:
    notes: list[str] = []
    if mode not in ("auto", "cavity", "lumped"):
        raise PDNError("mode is auto, cavity or lumped")
    try:
        f_min_hz, f_max_hz, points = float(f_min_hz), float(f_max_hz), int(points)
    except (TypeError, ValueError):
        raise PDNError("f_min_hz, f_max_hz and points are numbers") from None
    if not (0 < f_min_hz < f_max_hz) or not math.isfinite(f_max_hz):
        raise PDNError("need 0 < f_min_hz < f_max_hz")
    if net == ground:
        raise PDNError("the rail and its ground are the same net")
    if not (plating_mils > 0):
        raise PDNError("plating_mils must be above 0")
    if points < 16 or points > MAX_POINTS:
        clamped = min(MAX_POINTS, max(16, points))
        notes.append(f"points {points} taken as {clamped} (16 to {MAX_POINTS})")
        points = clamped
    load = _clean("load", load, _LOAD_KEYS, ("mount_nh",), notes)
    v = _clean("vrm", vrm, _VRM_KEYS, ("r_mohm", "l_nh", "bandwidth_khz"), notes)
    if decaps is not None and not isinstance(decaps, list):
        raise PDNError("decaps is a list of dicts")
    given = []
    for k, o in enumerate(decaps or [], 1):
        o = _clean(f"decaps[{k}]", o, _DECAP_KEYS,
                   ("c_uf", "c_farads", "esr_ohms", "esl_nh", "mount_nh", "count", "x", "y"), notes)
        if not o.get("ref"):
            if not (o.get("c_uf") or o.get("c_farads")):
                raise PDNError(f"decaps[{k}] needs a ref (a capacitor on the board) or a value "
                               f"(c_uf) to add one")
            o["ref"] = f"ADDED{k}"
        o["ref"] = str(o["ref"])
        given.append(o)
    if board is not None:
        for name in (net, ground):
            if name not in board.nets():
                raise PDNError(f"net {name!r} is not on the board")
    freqs = np.logspace(math.log10(f_min_hz), math.log10(f_max_hz), int(points))
    rho = ph.copper_resistivity(temp_c)
    # target
    tgt = None
    if target_ohms and target_ohms > 0:
        target = float(target_ohms)
        tgt = {"target_mohm": round(target * 1e3, 4), "from": "given"}
    elif volts and ripple_pct and step_amps:
        from ..engineering.pcb_calc import pdn_target_impedance
        r = pdn_target_impedance(volts, ripple_pct, step_amps)
        if not r.get("ok"):
            raise PDNError(r.get("error", "bad target inputs"))
        target = r["target_impedance_ohm"]
        tgt = {"target_mohm": r["target_impedance_mohm"], "from": "volts*ripple/step",
               "volts": volts, "ripple_pct": ripple_pct, "step_amps": step_amps}
    else:
        raise PDNError("give target_ohms, or volts, ripple_pct and step_amps")
    if not (target > 0) or not math.isfinite(target):
        raise PDNError("the target impedance must be above 0")

    # the load's pads first: the plane island under them is the one solved
    from .ir_drop import resolve_pads, safe_name
    copper = Copper(board, stack, plating_mils, rho) if board is not None else None
    load_pads: list[Pad] = []
    gpads: list[Pad] = []
    if board is not None:
        load_pads = resolve_pads(board, net, load)
        if not load_pads:
            raise PDNError(f"load {load} has no pads on {net}")
        if load.get("return_pads"):
            gpads = resolve_pads(board, ground, {"pads": load["return_pads"]})
        else:
            refs = sorted({p.comp for p in load_pads if p.comp})
            gpads = resolve_pads(board, ground, {"ref": refs}) if refs else []

    cav = None
    if board is not None and mode in ("auto", "cavity"):
        cav = find_cavity(board, stack, net, ground, f_max_hz, cell_mils, tand,
                          near=[(p.x, p.y) for p in load_pads])
        if cav is None and mode == "cavity":
            raise PDNError(f"no plane pair found for {net} over {ground}: the rail "
                           f"needs area copper (plane, pour) facing a {ground} layer. "
                           f"Use mode='lumped'.")
        if cav is None:
            notes.append(f"{net} has no plane facing {ground}: lumped model, no "
                         f"spreading inductance or plane resonance")
    caps, cap_notes = build_decaps(board, stack, cav, net, ground, given, plating_mils,
                                   exclude, copper, cap_values)
    notes += cap_notes
    if not caps:
        notes.append("no decoupling capacitors found or given")
    if any(c.source == "defaults" for c in caps):
        notes.append("capacitors on default ESR/ESL and NOMINAL capacitance: MLCC "
                     "DC-bias derating is not applied; pass c_uf/esr_ohms/esl_nh "
                     "or a touchstone per part for sign-off numbers")

    # VRM
    r_vrm = float(v.get("r_mohm", target * 1e3 * 0.5)) * 1e-3
    f_bw = float(v.get("bandwidth_khz", 50.0)) * 1e3
    l_vrm = float(v["l_nh"]) * 1e-9 if "l_nh" in v else target / (2 * math.pi * f_bw)
    vrm_used = {"r_mohm": round(r_vrm * 1e3, 4), "l_nh": round(l_vrm * 1e9, 4),
                "model": "given" if v else "default: R = target/2, L = target / (2 pi 50 kHz)"}
    if v.get("disable"):
        vrm_used = None
    elif r_vrm <= 0 and l_vrm <= 0:
        raise PDNError("a VRM with neither resistance nor inductance is a short: give "
                       "r_mohm or l_nh above 0, or disable it")
    z_vrm = r_vrm + 1j * 2 * np.pi * freqs * l_vrm

    # load
    load_l = load_r = 0.0
    load_info: dict[str, Any] = {}
    load_at = [(p.x, p.y, 1) for p in load_pads]
    if board is not None and load.get("include_vias", True):
        mt = mount(copper, cav, net, ground, load_pads, gpads, LOAD_VIA_SEARCH_MILS)
        load_l, load_r = mt["_l"], mt["_r"]
        if mt.get("_at"):
            load_at = mt["_at"]
        if mt.get("assumed"):
            notes.append(f"load: {mt['note']}")
        load_info["mount"] = {k: val for k, val in mt.items() if not k.startswith("_")}
    if "mount_nh" in load:
        load_l, load_r = float(load["mount_nh"]) * 1e-9, 0.0
        load_info["mount"] = {"total_nh": load["mount_nh"], "note": "given"}

    off_caps: list[tuple[str, float]] = []
    if cav is not None:
        def attach(x: float, y: float, layer: str) -> tuple[int, float, float, float]:
            """(node, H, ohm, mils off) for a point: a part more than a cell
            past the plane pair is joined to its edge by a track."""
            node, dist = cav.node_near(x, y)
            extra = dist - cav.cell_mils
            if extra <= 0:
                return node, 0.0, 0.0, 0.0
            l_h, r_ohm = copper._strip(layer or cav.power_layer, extra, OFF_PLANE_TRACK_MILS)
            return node, l_h, r_ohm, dist
        ports = []
        on_board = {r for r, _, _ in find_decaps(board, net, ground, {o["ref"] for o in given})}
        lcx, lcy = _centre(load_at) or (cav.x0, cav.y0)
        for c in caps:
            if c.ref not in on_board and c.x == 0.0 and c.y == 0.0:
                node, l_off, r_off, dist = attach(lcx, lcy, "")
            else:
                node, l_off, r_off, dist = attach(c.x, c.y, c.layer)
            if dist > 0:
                c.mount_l += l_off
                c.mount_r += r_off
                c.notes.append(f"{dist:.0f} mil off the plane pair; joined to its edge as a "
                               f"{OFF_PLANE_TRACK_MILS:g} mil track (+{l_off * 1e9:.2f} nH)")
                off_caps.append((c.ref, dist))
            ports.append((node, c.z(freqs)))
        if off_caps:
            notes.append(f"{len(off_caps)} capacitor(s) are not over the plane pair (up to "
                         f"{max(d for _, d in off_caps):.0f} mil off): each is joined to its "
                         f"edge as a {OFF_PLANE_TRACK_MILS:g} mil track of that length: "
                         + ", ".join(r for r, _ in off_caps[:8])
                         + (" ..." if len(off_caps) > 8 else ""))
        if vrm_used is not None:
            vx = vy = None
            if v.get("ref") or v.get("refs") or v.get("pads"):
                vp = resolve_pads(board, net, v)
                if vp:
                    vx, vy = sum(p.x for p in vp) / len(vp), sum(p.y for p in vp) / len(vp)
                else:
                    notes.append(f"vrm: {v.get('ref') or v.get('refs') or v.get('pads')} has "
                                 f"no pad on {net}; its place is taken as below")
            if vx is None:
                # no position given: the largest capacitor's place stands in
                big = max(caps, key=lambda c: c.c) if caps else None
                vx, vy = (big.x, big.y) if big else (lcx, lcy)
                vrm_used["at"] = "largest capacitor (pass vrm.ref for the real place)"
            else:
                vrm_used["at"] = [round(vx, 1), round(vy, 1)]
            node, _ = cav.node_near(vx, vy)
            ports.append((node, z_vrm))
        # the load: 1 A split by pins over the nodes its legs land on
        weights: dict[int, float] = {}
        offs = []
        for (x, y, n_pads) in load_at:
            node, l_off, r_off, dist = attach(x, y, copper.pad_layer(load_pads[0]))
            weights[node] = weights.get(node, 0.0) + n_pads
            offs.append((l_off, r_off, dist))
        if offs and all(d > 0 for _, _, d in offs):
            l_off = 1.0 / sum(1.0 / l for l, _, _ in offs)
            r_off = 1.0 / sum(1.0 / r for _, r, _ in offs)
            far = min(d for _, _, d in offs)
            load_l += l_off
            load_r += r_off
            load_info["off_plane_mils"] = round(far, 1)
            notes.append(f"the load is {far:.0f} mil off the plane pair: joined to its edge as "
                         f"a {OFF_PLANE_TRACK_MILS:g} mil track of that length "
                         f"(+{l_off * 1e9:.2f} nH). Give load.mount_nh, or route it, for a "
                         f"better number")
        elif any(d > 0 for _, _, d in offs):
            n_off = sum(1 for _, _, d in offs if d > 0)
            notes.append(f"{n_off} of the load's {len(offs)} connections are off the plane "
                         f"pair and taken at its edge")
        load_nodes = sorted(weights)
        for (node, zp) in ports:
            if not np.all(np.abs(zp) > 0):
                raise PDNError("a branch of zero impedance shorts the rail: give every "
                               "capacitor and the VRM some resistance or inductance")
        z_plane = solve_cavity(cav, freqs, ports, load_nodes, rho,
                               load_weights=[weights[k] for k in load_nodes])
        model = "cavity"
    else:
        z_plane = None
        model = "lumped"
    zl = load_r + 1j * 2 * np.pi * freqs * load_l
    branches = [c.z(freqs) for c in caps] + ([z_vrm] if vrm_used is not None else [])
    if any(not np.all(np.abs(zb) > 0) for zb in branches):
        raise PDNError("a branch of zero impedance shorts the rail: give every "
                       "capacitor and the VRM some resistance or inductance")
    c_pl = cav.capacitance_f if cav is not None else 0.0
    er = cav.er if cav is not None else 4.3
    if not branches and c_pl <= 0:
        raise PDNError("nothing on the rail to hold it: no capacitor, no VRM, no plane")
    z_lumped = lumped(freqs, branches, c_pl, er, cav.tand if cav else 0.02)
    z = (z_plane if z_plane is not None else z_lumped) + zl
    zmag = np.abs(z)
    bands = _bands(freqs, zmag, target)
    mount_avg = (np.mean([c.esl + c.mount_l for c in caps]) if caps else 1e-9)
    # a compact curve: about 60 points
    step = max(1, len(freqs) // 60)
    curve = [{"hz": float(f"{freqs[i]:.6g}"), "mohm": round(float(zmag[i]) * 1e3, 4)}
             for i in range(0, len(freqs), step)]
    k_min = int(np.argmin(zmag))
    k_max = int(np.argmax(zmag / target))
    result = {
        "net": net, "ground": ground, "model": model,
        "target": tgt, "pass": not bands,
        "worst": ({"mhz": round(float(freqs[k_max]) / 1e6, 5),
                   "mohm": round(float(zmag[k_max]) * 1e3, 4),
                   "over_target_x": round(float(zmag[k_max] / target), 3)}),
        "minimum": {"mhz": round(float(freqs[k_min]) / 1e6, 5),
                    "mohm": round(float(zmag[k_min]) * 1e3, 4)},
        "violations": bands,
        "anti_resonances": _peaks(freqs, zmag),
        "suggestions": _suggest(bands, target, float(mount_avg), f_max_hz),
        "cavity": cav.to_dict() if cav is not None else None,
        **_decap_list(caps),
        "vrm": vrm_used,
        "load": {"pads": [f"{p.comp}.{p.name}" for p in load_pads][:20],
                 "pad_count": len(load_pads),
                 "mount_nh_included": round(load_l * 1e9, 4),
                 "mount_mohm_included": round(load_r * 1e3, 4), **load_info},
        "curve": curve,
        "notes": notes + (cav.notes if cav is not None else []),
    }
    if cav is not None:
        f1 = cav.first_mode_hz()
        result["cavity"]["first_mode_mhz"] = round(f1 / 1e6, 3) if f1 else None
        a_mils, b_mils = cav.extent_mils()
        result["cavity"]["extent_mils"] = [round(a_mils, 1), round(b_mils, 1)]
    if out_dir is not None:
        from pathlib import Path
        folder = Path(out_dir)
        stem = safe_name(net)
        files: list[str] = []
        result["files"] = files
        try:                # the numbers stand whatever happens to the files
            folder.mkdir(parents=True, exist_ok=True)
            csv = folder / f"{stem}_pdn_impedance.csv"
            with open(csv, "w", encoding="utf-8") as fh:
                fh.write("freq_hz,z_re_ohm,z_im_ohm,z_mag_ohm,z_lumped_mag_ohm,target_ohm\n")
                for i in range(len(freqs)):
                    fh.write(f"{freqs[i]:.6g},{z[i].real:.6g},{z[i].imag:.6g},{zmag[i]:.6g},"
                             f"{abs(z_lumped[i] + zl[i]):.6g},{target:.6g}\n")
            files.append(str(csv))
            dcsv = folder / f"{stem}_pdn_decaps.csv"
            with open(dcsv, "w", encoding="utf-8") as fh:
                fh.write("ref,c_uf,esr_mohm,esl_nh,mount_nh,mount_mohm,srf_mhz,package,kind,"
                         "model,x_mils,y_mils,layer,notes\n")
                for c in caps:
                    r_ = c.to_dict()
                    fh.write(",".join(str(x) for x in (
                        r_["ref"], r_["c_uf"], r_["esr_mohm"], r_["esl_nh"], r_["mount_nh"],
                        r_["mount_mohm"], r_["srf_mhz"], r_["package"], r_["kind"], r_["model"],
                        r_["at"][0], r_["at"][1], r_["layer"],
                        '"' + "; ".join(c.notes).replace('"', "'") + '"')) + "\n")
            files.append(str(dcsv))
            cir = folder / f"{stem}_pdn_lumped.cir"
            write_spice(cir, freqs, caps, (r_vrm, l_vrm) if vrm_used is not None else None,
                        c_pl, load_l, f"{net} PDN", load_r)
            files.append(str(cir))
        except OSError as exc:
            result["files_error"] = f"{type(exc).__name__}: {exc}"
            return result
        png = folder / f"{stem}_pdn_impedance.png"
        try:
            if _plot(png, freqs, zmag, target, z_lumped + zl if z_plane is not None else None,
                     f"{net} PDN impedance ({model})"):
                files.append(str(png))
            else:
                result["notes"].append("no plot: matplotlib is not installed "
                                       "(pip install eda-agent[render]); the CSV has the curve")
        except Exception as exc:  # noqa: BLE001 - the numbers stand without the picture
            result["files_error"] = f"plot not written: {type(exc).__name__}: {exc}"
    return result
