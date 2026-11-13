# SPDX-License-Identifier: Apache-2.0
"""Steady-state 3D heat conduction in a board: a finite-volume network.

The board is cut into the copper raster's XY cells and, through its
thickness, into planes: one per copper layer and two or more per
dielectric (``max_sublayer_mils`` each), so a thick core carries heat
sideways as well as through. Every (plane, cell) is a node.

Conduction (all conductivities from ``physics``):

* In-plane, between neighbouring cells of a copper plane: the link's
  effective conductivity from the raster's slices (``CopperRaster.
  link_conductivity``): copper and the resin beside it in parallel within
  a slice, slices in series along the link. A track along the link
  conducts with its width; a track or a clearance across it does not
  bridge the cells. Dielectric planes use the laminate's in-plane value.
* Through-plane, between a cell and the cell below: the two half
  thicknesses in series. A cell only partly covered by copper conducts
  to the laminate under its copper and under the fringe beside each
  copper edge, not under the whole cell (``_fringe_cover``), which keeps
  a pad from being smeared over the cells it touches on a coarse grid.
* Plated holes are their own chain of nodes, one per plane they pass
  through, joined by the barrel's copper annulus (and fill). Each barrel
  node joins the ring of cells around it by radial conduction,
  2 pi k t / ln(rho / r) shared among the ring's cells (a well model:
  on a coarse grid it reproduces Peaceman's equivalent radius, on a fine
  one the annulus between the barrel and the first cells outside it).
  r is the land's radius on a signal layer and the barrel's wall on a
  plane the hole joins or in laminate; on a plane of another net the
  antipad's resin comes first, which is what keeps a signal via from
  shorting heat into a ground plane it does not touch.
* Parts: a powered part is a junction node joined to the cells under its
  pads by ``theta_jb`` (shared by pad area) and, when ``theta_jc`` is
  given, to a case node that loses heat from the part's body. With no
  ``theta_jb`` the junction is tied tightly to its pads, which makes them
  one isothermal source, and the reply gives the pad temperature only.

Surfaces. Both faces, the board's edges and part cases lose heat by
convection and radiation, the faces through the solder mask's own
resistance:

* Natural convection: Lloyd-Moran for a face looking up, Raithby-Hollands
  for one looking down, Churchill-Chu for a vertical board. With
  ``convection="local"`` (the default) each cell uses its own temperature,
  and on a vertical board its own height above the lower edge, so the top
  of the board, in air the bottom has already warmed, cools less well.
  The local coefficient over a cell from y1 to y2 is
  (F(y2) - F(y1)) / (y2 - y1) with F(y) = y h_avg(y): for an isothermal
  board the cells then sum exactly to the average correlation.
* Forced convection: the flat plate in parallel flow, local in the same
  way along ``airflow_deg`` from the leading edge, so parts downstream run
  hotter. Natural and forced combine by Churchill's cube rule.
* Radiation: eps sigma (T^4 - Ta^4) per cell, exact (no linearisation
  error is left at convergence).

The surface laws are nonlinear, so the system is solved by Newton's
method: each step solves ``(K + dQ/dT) T = b - Q(T) + dQ/dT T`` by
conjugate gradients preconditioned with algebraic multigrid. A heat
balance (power in against every way out, with the final nonlinear
fluxes) is returned with each solution; it closes to solver precision
and is the first check of a run.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from ..emc.stack import Stack
from ..layout import geom
from ..layout.copper_raster import CopperRaster
from ..layout.model import BOTTOM, LayoutBoard
from . import physics as ph

MIL = ph.MIL
#: Every dielectric is cut into at least this many planes: with one, the
#: sideways spread of heat inside a thin prepreg under a pad's edge is
#: misjudged by a few percent of the pad-to-plane drop.
MIN_SUBLAYERS = 2
#: Smallest ln(rho / r) used for a cell centre just outside a barrel.
LN_MIN = 0.05
#: Extra half-width, in dielectric thicknesses, over which the edge of a
#: wide copper shape on an insulated surface conducts down to an
#: isothermal plane beneath it: 2 ln 2 / pi, from the conformal-mapping
#: result G/k = w/h + (2/pi) ln 4 for a strip of width w >> h.
FRINGE = 2.0 * math.log(2.0) / math.pi
#: Conductance tying a junction to its pads when no theta_jb is known, W/K.
TIGHT_W_PER_K = 1.0e3
#: A heat area with less than this share of copper in it heats the bare
#: laminate instead.
HEAT_AREA_MIN_COPPER = 0.01
#: Contact conductance of a clamp to a chassis, W/m^2/K (a thermal pad).
CLAMP_H = 5000.0
#: Below this many unknowns a direct solve is as quick as multigrid.
AMG_MIN_UNKNOWNS = 20_000
#: Linear solve tolerance (relative residual) and iteration cap.
CG_RTOL = 1e-10
CG_RTOL_EARLY = 1e-6
CG_MAXITER = 400
#: Temperature step for the numerical surface-flux derivative, K.
_DT = 1e-3

ORIENTATIONS = ("horizontal", "inverted", "vertical")
CONVECTION_MODELS = ("local", "mean")


@dataclass
class Setup:
    """The board's surroundings and the model's knobs."""

    ambient_c: float = 25.0
    #: horizontal (top side up), inverted (top side down) or vertical.
    orientation: str = "horizontal"
    #: Vertical board: the board direction that points up, degrees
    #: counter-clockwise from +x (90 = +y is up).
    up_deg: float = 90.0
    airflow_ms: float = 0.0
    #: Direction the air moves towards, degrees counter-clockwise from +x.
    airflow_deg: float = 0.0
    convection: str = "local"            # local | mean
    emissivity: float = ph.EMISSIVITY_MASK
    #: Fixed convection coefficients, W/m^2/K, instead of the correlations.
    h_top: Optional[float] = None
    h_bottom: Optional[float] = None
    radiation: bool = True
    edges: bool = True
    dielectric: str = ph.DEFAULT_DIELECTRIC
    k_xy: Optional[float] = None         # override the laminate's
    k_z: Optional[float] = None
    solder_mask: bool = True
    max_sublayer_mils: float = 10.0
    plating_um: float = ph.PLATING_M * 1e6
    via_fill: str = "none"
    pressure_pa: float = ph.P_ATM
    tol_k: float = 1e-4
    max_iter: int = 40

    def check(self) -> None:
        if self.orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {ORIENTATIONS}")
        if self.convection not in CONVECTION_MODELS:
            raise ValueError(f"convection must be one of {CONVECTION_MODELS}")
        if self.via_fill not in ph.VIA_FILL_K:
            raise ValueError(f"via_fill must be one of {sorted(ph.VIA_FILL_K)}")
        if not 0.0 <= self.emissivity <= 1.0:
            raise ValueError("emissivity must be between 0 and 1")
        if self.max_sublayer_mils <= 0:
            raise ValueError("max_sublayer_mils must be > 0")
        self.laminate()

    def laminate(self) -> tuple[float, float]:
        d = ph.DIELECTRICS.get(self.dielectric)
        if d is None:
            raise ValueError(f"unknown dielectric {self.dielectric!r}; "
                             f"use one of {sorted(ph.DIELECTRICS)} or k_xy/k_z")
        kxy = self.k_xy if self.k_xy is not None else d.k_xy
        kz = self.k_z if self.k_z is not None else d.k_z
        if kxy <= 0 or kz <= 0:
            raise ValueError("k_xy and k_z must be > 0")
        return kxy, kz


@dataclass
class Package:
    """A part's thermal resistances, from its datasheet, K/W."""

    theta_jb: Optional[float] = None     # junction to board (or psi_jb)
    theta_jc: Optional[float] = None     # junction to case top
    tj_max_c: Optional[float] = None
    heatsink_theta_sa: Optional[float] = None   # case to ambient through a sink


@dataclass
class Clamp:
    """A rectangle of one face held to a chassis at ``temp_c``."""

    x0: float
    y0: float
    x1: float
    y1: float
    temp_c: float
    side: str = "bottom"                 # top | bottom
    h_contact: float = CLAMP_H


@dataclass
class HeatArea:
    """``power_w`` spread evenly over a rectangle of a copper layer."""

    layer: str
    x0: float
    y0: float
    x1: float
    y1: float
    power_w: float


@dataclass
class Plane:
    kind: str                # cu | diel
    name: str                # copper layer name, or "diel a/b i/n"
    t_m: float
    kxy: np.ndarray          # per active cell
    kz: np.ndarray
    z_mid_m: float = 0.0     # depth of the plane's middle below the top face
    fill: Optional[np.ndarray] = None    # cu: copper fraction per active cell
    edge_m: Optional[np.ndarray] = None  # cu: copper edge length per active cell, m
    k_res: float = 0.0       # cu: what lies between the copper, W/m/K
    gap_m: float = 0.0       # diel: the whole dielectric between its copper layers


@dataclass
class Result:
    temps: dict[str, np.ndarray]                 # copper layer -> (ny, nx), NaN outside
    plane_temps: list[np.ndarray]
    planes: list[Plane]
    parts: list[dict]
    balance: dict
    iterations: int
    h_used: dict
    notes: list[str] = field(default_factory=list)
    raster: Optional[CopperRaster] = None
    barrels: list[dict] = field(default_factory=list)
    solver: dict = field(default_factory=dict)
    ambient_c: float = 25.0

    @property
    def t_max_c(self) -> float:
        return float(max(np.nanmax(t) for t in self.plane_temps))

    @property
    def t_min_c(self) -> float:
        return float(min(np.nanmin(t) for t in self.plane_temps))

    def at(self, layer: str, x: float, y: float) -> float:
        """Temperature of ``layer`` in the cell holding (x, y), deg C."""
        ix, iy = self.raster.cell_of(x, y)
        return float(self.temps[layer][iy, ix])


def match_powers(board: LayoutBoard, powers: dict[str, float]) -> dict[str, float]:
    """``powers`` keyed by designator or glob (``R1*``); exact names win."""
    refs = [c.ref for c in board.components]
    known = set(refs)
    out: dict[str, float] = {}
    for key, w in powers.items():
        if any(ch in key for ch in "*?["):
            hits = [r for r in refs if fnmatch.fnmatchcase(r, key)]
            if not hits:
                raise ValueError(f"no component matches {key!r}")
            for r in hits:
                if r not in powers:
                    out[r] = float(w)
    for key, w in powers.items():
        if any(ch in key for ch in "*?["):
            continue
        if key not in known:
            raise ValueError(f"no component {key!r} on the board")
        out[key] = float(w)
    for ref, w in out.items():
        if not math.isfinite(w) or w < 0:
            raise ValueError(f"power of {ref} must be a number >= 0, got {w!r}")
    return {k: v for k, v in out.items() if v > 0.0}


def _k_between(setup: Setup, i: int, n_cu: int) -> float:
    """What lies between the copper of layer ``i``: solder mask on an
    outer layer, laminate resin on an inner one."""
    outer = i == 0 or i == n_cu - 1
    if outer and setup.solder_mask:
        return ph.K_SOLDER_MASK
    return setup.laminate()[0]


def _build_planes(raster: CopperRaster, stack: Stack, setup: Setup,
                  act: np.ndarray) -> tuple[list[Plane], dict[str, int], list[str]]:
    kxy_d, kz_d = setup.laminate()
    notes = []
    names = raster.layers
    sl = {l.name: l for l in stack.layers}
    missing = [n for n in names if n not in sl]
    if missing:
        raise ValueError(f"layers {missing} are not in the stackup")
    planes: list[Plane] = []
    cu_index: dict[str, int] = {}
    n_cu = len(names)
    for i, name in enumerate(names):
        layer = sl[name]
        f = raster.fill[name][act]
        outer_mask = (i == 0 or i == n_cu - 1) and setup.solder_mask
        k_b = _k_between(setup, i, n_cu)
        cu_index[name] = len(planes)
        k_res = k_b if outer_mask else kz_d
        edge = raster.edge_mils[name][act] * MIL if name in raster.edge_mils else None
        planes.append(Plane("cu", name, max(layer.copper_mils, 0.05) * MIL,
                            ph.K_COPPER * f + k_b * (1 - f),
                            ph.K_COPPER * f + k_res * (1 - f),
                            fill=f, edge_m=edge, k_res=k_res))
        if i + 1 < n_cu:
            h = layer.z_mils - sl[names[i + 1]].z_mils
            if h <= 0:
                notes.append(f"no dielectric between {name} and {names[i + 1]}: "
                             f"0.1 mil assumed")
                h = 0.1
            n = max(MIN_SUBLAYERS, int(math.ceil(h / setup.max_sublayer_mils - 1e-9)))
            for k in range(n):
                planes.append(Plane("diel", f"diel {name}/{names[i + 1]} {k + 1}/{n}",
                                    h / n * MIL, np.full(f.shape, kxy_d),
                                    np.full(f.shape, kz_d), gap_m=h * MIL))
    z = 0.0
    for pl in planes:
        pl.z_mid_m = z + pl.t_m / 2
        z += pl.t_m
    return planes, cu_index, notes


def _fringe_cover(fill: np.ndarray, edge_m: np.ndarray, fringe_m: float,
                  cell_area_m2: float) -> np.ndarray:
    """Share of each cell that conducts between its copper and the
    dielectric next to it.

    A single node per cell would let a pad's heat leave over the whole of
    every cell the pad touches, a pad smeared by up to a cell on each
    side. The heat really leaves under the copper and under a fringe
    beside each copper edge, ``fringe_m`` wide: for a wide shape on an
    insulated surface over an isothermal plane at depth h the fringe is
    (2 ln 2 / pi) h, stretched by sqrt(k_xy / k_z) in an anisotropic
    laminate. So the share is the copper fraction plus edge length times
    fringe width, at most 1. On a grid finer than the fringe it is 1 in
    every cell with copper and the plain scheme is back, with the grid
    resolving the fringe itself; dense tracks over a plane also reach 1,
    as they should."""
    return np.minimum(1.0, fill + edge_m * fringe_m / cell_area_m2)


def _edge_faces(act: np.ndarray) -> np.ndarray:
    """Number of exposed side faces per active cell (0..4)."""
    pad = np.pad(act, 1, constant_values=False)
    n = np.zeros(act.shape, dtype=int)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        n += ~pad[1 + dy:1 + dy + act.shape[0], 1 + dx:1 + dx + act.shape[1]]
    return n[act]


def _pad_cells(board: LayoutBoard, raster: CopperRaster, ref: str, layer: str,
               idx: np.ndarray) -> dict[int, float]:
    """Active cell -> copper area (m^2) of ``ref``'s pads on ``layer``.

    A pad's heat enters its copper: the area counted in a cell is the
    pad's there, and no more than the copper the layer has in that cell.
    So a cell inside the drilled hole of a via-in-pad takes none, and one
    the hole's edge crosses takes its copper's share. Without that the
    hole cells, which the copper around them does not reach, would be
    heated and stand out as false peaks on a fine grid."""
    out: dict[int, float] = {}
    fill = raster.fill[layer]
    cell_area = raster.cell_mils ** 2
    for p in board.pads:
        if p.comp != ref:
            continue
        s = p.shape_on(layer)
        if s is None:
            continue
        for (ix, iy), a in raster.shape_area_by_cell(s).items():
            c = int(idx[iy, ix])
            if c >= 0:
                out[c] = out.get(c, 0.0) + a
    if not out:
        return out
    cells = np.fromiter(out.keys(), dtype=np.int64)
    area = np.fromiter(out.values(), dtype=float)
    copper = fill[idx >= 0][cells] * cell_area
    area = np.minimum(area, copper)
    keep = area > 1e-9 * cell_area
    if not keep.any():
        return {c: a * MIL * MIL for c, a in out.items()}
    return {int(c): float(a) * MIL * MIL for c, a in zip(cells[keep], area[keep])}


def _ring(raster: CopperRaster, idx: np.ndarray, x: float, y: float, r_mils: float,
          reach: Optional[float] = None) -> tuple[np.ndarray, np.ndarray]:
    """Active cells whose centres lie more than ``r_mils`` and at most
    ``r_mils`` plus one cell from (x, y), and those distances in mils.
    ``reach`` sets the outer radius instead; with ``r_mils`` 0 the cells
    are the disc of that radius."""
    cell = raster.cell_mils
    reach = r_mils + cell if reach is None else reach
    i0 = max(0, int(math.floor((x - reach - raster.x0) / cell)))
    i1 = min(raster.nx - 1, int(math.floor((x + reach - raster.x0) / cell)))
    j0 = max(0, int(math.floor((y - reach - raster.y0) / cell)))
    j1 = min(raster.ny - 1, int(math.floor((y + reach - raster.y0) / cell)))
    if i0 > i1 or j0 > j1:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    xs = raster.x0 + (np.arange(i0, i1 + 1) + 0.5) * cell - x
    ys = raster.y0 + (np.arange(j0, j1 + 1) + 0.5) * cell - y
    rho = np.hypot(xs[None, :], ys[:, None])
    c = idx[j0:j1 + 1, i0:i1 + 1]
    sel = (c >= 0) & ((rho > r_mils) | (r_mils <= 0)) & (rho <= reach + 1e-9)
    return c[sel], rho[sel]


def _body(board: LayoutBoard, ref: str) -> tuple[list, float, float]:
    """A part's outline ring, its top area and its side area, m^2."""
    comp = board.component(ref)
    ring: list = []
    height = 0.0
    if comp is not None:
        height = max(comp.height, 0.0)
        best = 0.0
        for b in comp.bodies or []:
            o = b.get("outline") if isinstance(b, dict) else None
            if o and len(o) >= 3:
                a = abs(geom.ring_area(o))
                if a > best:
                    best, ring = a, [tuple(pt) for pt in o]
                    height = max(height, float(b.get("height") or 0.0))
        if not ring and len(comp.courtyard) >= 3:
            ring = list(comp.courtyard)
    if not ring:
        pts = [(p.x, p.y) for p in board.pads if p.comp == ref]
        if len(pts) >= 3:
            ring = geom.convex_hull(pts)
        if len(ring) < 3:
            cx, cy = (pts[0] if pts else ((comp.x, comp.y) if comp else (0.0, 0.0)))
            ring = [(cx - 20, cy - 20), (cx + 20, cy - 20), (cx + 20, cy + 20), (cx - 20, cy + 20)]
    top = abs(geom.ring_area(ring)) * MIL * MIL
    perim = sum(math.hypot(ring[i][0] - ring[i - 1][0], ring[i][1] - ring[i - 1][1])
                for i in range(len(ring))) * MIL
    return ring, top, perim * height * MIL


@dataclass
class _Surface:
    """A set of nodes that lose heat to the surroundings."""

    name: str
    kind: str                        # top | bottom | edge | case
    nodes: np.ndarray
    area: np.ndarray                 # m^2 per node
    orient: str                      # up | down | vertical
    length: float                    # natural-convection length, m
    flow_len: float                  # forced-flow length, m
    r_series: float = 0.0            # m^2 K / W in front of the surface
    fixed_h: Optional[float] = None
    y1: Optional[np.ndarray] = None  # vertical board: cell's span above the lower edge
    y2: Optional[np.ndarray] = None
    x1: Optional[np.ndarray] = None  # span from the leading edge along the flow
    x2: Optional[np.ndarray] = None
    g_lin: Optional[np.ndarray] = None   # extra linear conductance to ambient, W/K


class _Boundary:
    def __init__(self, setup: Setup):
        self.setup = setup
        self.ta = setup.ambient_c
        self.surfaces: list[_Surface] = []

    def h_conv(self, s: _Surface, T: np.ndarray) -> np.ndarray:
        st = self.setup
        if s.fixed_h is not None:
            return np.full(T.shape, float(s.fixed_h))
        local = st.convection == "local" or s.kind in ("edge", "case")
        if local:
            tref = T
        else:
            tref = np.full(T.shape, float((T * s.area).sum() / s.area.sum()))
        p = st.pressure_pa
        if local and s.y2 is not None:
            hn = self._span(lambda y: ph.h_natural(tref, self.ta, y, "vertical", p), s.y1, s.y2)
        else:
            hn = ph.h_natural(tref, self.ta, s.length, s.orient, p)
        if st.airflow_ms <= 0:
            return np.asarray(hn, dtype=float) + np.zeros(T.shape)
        if local and s.x2 is not None:
            hf = self._span(lambda x: ph.h_forced(tref, self.ta, st.airflow_ms, x, p), s.x1, s.x2)
        else:
            hf = ph.h_forced(tref, self.ta, st.airflow_ms, s.flow_len, p)
        return np.asarray(ph.h_mixed(hn, hf), dtype=float) + np.zeros(T.shape)

    @staticmethod
    def _span(h_avg, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Mean local coefficient between distances ``a`` and ``b`` from
        the edge the boundary layer starts at, from the average
        coefficient ``h_avg(L)`` of a plate of length L."""
        f2 = b * h_avg(b)
        f1 = np.where(a > 0, a * h_avg(np.maximum(a, 1e-9)), 0.0)
        return (f2 - f1) / (b - a)

    def flux(self, s: _Surface, T: np.ndarray):
        """Heat leaving each node, W, and the two coefficients used.

        The air and the surroundings see the outside of the solder mask,
        which is cooler than the node beneath it by the flux times the
        mask's resistance: the coefficients are evaluated there (two
        fixed-point passes; the mask's drop is a fraction of a kelvin)."""
        ts = T
        for _ in range(3 if s.r_series > 0 else 1):
            hc = self.h_conv(s, ts)
            hr = ph.h_radiation(ts, self.ta, self.setup.emissivity) if self.setup.radiation \
                else np.zeros(T.shape)
            u = 1.0 / (s.r_series + 1.0 / np.maximum(hc + hr, 1e-12))
            ts = T - u * (T - self.ta) * s.r_series
        q = s.area * u * (T - self.ta)
        if s.g_lin is not None:
            q = q + s.g_lin * (T - self.ta)
        return q, hc, hr


def _linear_solve(K: sp.csr_matrix, rhs: np.ndarray, x0: np.ndarray, info: dict,
                  rtol: float = CG_RTOL) -> np.ndarray:
    """Solve the symmetric positive definite system K x = rhs.

    Conjugate gradients preconditioned by classical (Ruge-Stuben)
    algebraic multigrid from pyamg. Copper against laminate is a
    conductivity contrast of about 1000 in a thin, layered grid: plain or
    ILU-preconditioned CG crawls on it and a sparse direct factorisation
    fills in badly, while AMG-CG converges in a few tens of iterations
    whatever the grid size.

    Small systems, a missing pyamg and a CG that fails to converge all go
    to SuperLU, which is exact but slow on large grids."""
    n = K.shape[0]
    if n > AMG_MIN_UNKNOWNS:
        try:
            import pyamg  # noqa: PLC0415 - imported only when a large grid needs it
        except ImportError:
            pyamg = None
            info["pyamg_missing"] = True
        if pyamg is not None:
            ml = pyamg.ruge_stuben_solver(K)
            count = [0]

            def _count(_xk):
                count[0] += 1

            x, flag = spla.cg(K, rhs, x0=x0, M=ml.aspreconditioner(cycle="V"),
                              rtol=rtol, atol=0.0, maxiter=CG_MAXITER, callback=_count)
            info["cg_iterations"] = info.get("cg_iterations", 0) + count[0]
            if flag == 0:
                info["method"] = "amg-cg"
                return x
            info["cg_failures"] = info.get("cg_failures", 0) + 1
    info["method"] = "superlu"
    return spla.spsolve(K.tocsc(), rhs)


def solve(board: LayoutBoard, stack: Stack, raster: CopperRaster,
          powers: Optional[dict[str, float]] = None,
          packages: Optional[dict[str, Package]] = None,
          setup: Optional[Setup] = None,
          extra_heat_w: Optional[dict[str, np.ndarray]] = None,
          heat_areas: Optional[list[HeatArea]] = None,
          clamps: Optional[list[Clamp]] = None,
          report_parts: Optional[list[str]] = None) -> Result:
    """Solve the board's steady temperature field.

    ``powers``: {designator or glob: watts}. ``packages``: {designator:
    Package}. ``extra_heat_w``: {copper layer: (ny, nx) watts per cell},
    for example Joule heating from an IR-drop solution on the same grid.
    ``heat_areas``: rectangles of a layer that dissipate. ``clamps``:
    rectangles of a face held to a chassis. ``report_parts``: unpowered
    parts whose temperature is wanted as well.
    """
    setup = setup or Setup()
    setup.check()
    packages = packages or {}
    pw = match_powers(board, powers or {})
    for ref in packages:
        if board.component(ref) is None:
            raise ValueError(f"no component {ref!r} on the board")
    notes: list[str] = []
    act = raster.mask >= 0.5
    if not act.any():
        raise ValueError("no cell of the grid is inside the board outline")
    idx = -np.ones(act.shape, dtype=np.int64)
    M = int(act.sum())
    idx[act] = np.arange(M)
    cy, cx = np.nonzero(act)
    wmask = np.clip(raster.mask[act], 0.0, 1.0)
    dx = raster.cell_mils * MIL
    A = dx * dx * wmask                                  # face area per cell

    planes, cu_index, pnotes = _build_planes(raster, stack, setup, act)
    notes += pnotes
    P = len(planes)
    n_cu = len(raster.layers)
    kxy_d, _kz_d = setup.laminate()

    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    diag_n: list[np.ndarray] = []
    diag_g: list[np.ndarray] = []

    def link(a, b, g) -> None:
        a = np.atleast_1d(np.asarray(a, dtype=np.int64))
        b = np.atleast_1d(np.asarray(b, dtype=np.int64))
        g = np.atleast_1d(np.asarray(g, dtype=float))
        if g.shape != a.shape:
            g = np.full(a.shape, float(g[0]))
        rows.extend([a, b])
        cols.extend([b, a])
        vals.extend([-g, -g])
        diag_n.extend([a, b])
        diag_g.extend([g, g])

    # in-plane conduction
    mx = act[:, :-1] & act[:, 1:]
    my = act[:-1, :] & act[1:, :]
    ax, bx = idx[:, :-1][mx], idx[:, 1:][mx]
    ay, by = idx[:-1, :][my], idx[1:, :][my]
    have_links = bool(raster.slice_x)
    if not have_links:
        notes.append("the raster has no link slices (rasterize(links=True)): copper "
                     "conduction between cells uses the cells' area fractions")
    for p, pl in enumerate(planes):
        if pl.kind == "cu" and have_links:
            i_cu = raster.layers.index(pl.name)
            kx, ky = raster.link_conductivity(pl.name, ph.K_COPPER, _k_between(setup, i_cu, n_cu))
            gx, gy = kx[mx] * pl.t_m, ky[my] * pl.t_m
        else:
            s = pl.kxy * pl.t_m
            gx = 2.0 * s[ax] * s[bx] / (s[ax] + s[bx])
            gy = 2.0 * s[ay] * s[by] / (s[ay] + s[by])
        link(p * M + ax, p * M + bx, gx)
        link(p * M + ay, p * M + by, gy)
    # through-plane conduction
    cells = np.arange(M)
    aniso = math.sqrt(kxy_d / _kz_d)
    cu_planes = [p for p, pl in enumerate(planes) if pl.kind == "cu"]

    def end_cover(p_cu: int, gap: float) -> np.ndarray:
        cu = planes[p_cu]
        if cu.edge_m is None:
            return np.ones(M)
        cov = _fringe_cover(cu.fill, cu.edge_m, FRINGE * aniso * gap, dx * dx)
        return np.where(cu.fill > 0, cov, 1.0)

    # The laminate is one body from face to face, and its nodes hold its
    # mean temperature in each cell: every link between two of them takes
    # the whole cell. Copper exchanges heat with the laminate next to it
    # under its own area and the fringe beside it (_fringe_cover); a cell
    # with no copper on a layer is laminate there too.
    #
    # Outer copper, where the parts are, is the source: its heat crosses
    # the whole gap to the next layer in a tube of that cover, not just
    # the first sublayer, so the rest of the gap's resistance at the
    # tube's width in place of the cell's is added to its link. (Narrowing
    # every link of the column instead leaves the laminate nodes at the
    # tube's temperature, and they then leak sideways into their
    # neighbours a second time: a 20 mil pad on 50 mil cells read 32 %
    # low.) Inner copper lies in the heat's way and gathers it only close
    # by; what it does not cover lets the heat pass (``bare``), the
    # laminate above the layer joined to the laminate below it directly.
    gap_of = {pa: (planes[pa + 1].gap_m if pb > pa + 1 else 0.0)
              for pa, pb in zip(cu_planes[:-1], cu_planes[1:])}
    c_dn = {pa: end_cover(pa, gap_of[pa]) for pa in cu_planes[:-1]}
    c_up = {pb: end_cover(pb, gap_of[pa]) for pa, pb in zip(cu_planes[:-1], cu_planes[1:])}
    outer = (cu_planes[0], cu_planes[-1])
    for p in cu_planes[1:-1]:
        if planes[p - 1].kind == "diel" and planes[p + 1].kind == "diel":
            bare = 1.0 - np.maximum(c_up[p], c_dn[p])
            sel = bare > 1e-9
            if sel.any():
                r = (planes[p - 1].t_m / (2 * planes[p - 1].kz) + planes[p].t_m / planes[p].k_res
                     + planes[p + 1].t_m / (2 * planes[p + 1].kz))
                link((p - 1) * M + cells[sel], (p + 1) * M + cells[sel], (A * bare / r)[sel])
    for pa, pb in zip(cu_planes[:-1], cu_planes[1:]):
        ca, cb = c_dn[pa], c_up[pb]
        if pb == pa + 1:
            # no laminate between them: copper on copper
            r = sum(pl.t_m / (2 * np.where(pl.fill > 0, ph.K_COPPER, pl.k_res))
                    for pl in (planes[pa], planes[pb]))
            link(pa * M + cells, pb * M + cells, A * np.minimum(ca, cb) / r)
            continue
        r_gap = sum(planes[p].t_m / planes[p].kz for p in range(pa + 1, pb))
        for p in range(pa, pb):
            r = np.zeros(M)
            for pl in (planes[p], planes[p + 1]):
                if pl.kind == "cu":
                    r += pl.t_m / (2 * np.where(pl.fill > 0, ph.K_COPPER, pl.k_res))
                else:
                    r += pl.t_m / (2 * pl.kz)
            for end, c_end, first in ((pa, ca, pa + 1), (pb, cb, pb - 1)):
                if p == end or p + 1 == end:
                    r = r / c_end
                    if end in outer:
                        rest = r_gap - planes[first].t_m / (2 * planes[first].kz)
                        r = r + rest * (1.0 / c_end - 1.0)
            link(p * M + cells, (p + 1) * M + cells, A / r)

    # plated holes
    plating = setup.plating_um * 1e-6
    layer_kind = {l.name: l for l in board.layers}
    plane_region_nets = {(r.layer, r.net) for r in board.regions if r.kind == "plane" and r.net}
    barrel_nodes: list[tuple[Any, list[int]]] = []
    next_node = P * M
    two_pi = 2.0 * math.pi
    for b in raster.barrels:
        c0 = idx[b.iy, b.ix]
        ps = [cu_index[l] for l in b.layers if l in cu_index]
        if c0 < 0 or len(ps) < 2 or b.hole_mils <= 0:
            continue
        p0, p1 = min(ps), max(ps)
        hole_m = b.hole_mils * MIL
        r_wall = b.hole_mils / 2.0                        # mils
        r_land = max(b.diameter_mils / 2.0, r_wall)
        r_anti = r_wall + raster.plane_clearance_mils
        chain = list(range(next_node, next_node + (p1 - p0 + 1)))
        next_node += len(chain)
        for k in range(len(chain) - 1):
            dz = (planes[p0 + k].t_m + planes[p0 + k + 1].t_m) / 2
            link(chain[k], chain[k + 1],
                 ph.barrel_conductance(hole_m, dz, plating, setup.via_fill))
        rings: dict[float, tuple[np.ndarray, np.ndarray]] = {}
        for k, node in enumerate(chain):
            pl = planes[p0 + k]
            lk = layer_kind.get(pl.name) if pl.kind == "cu" else None
            is_plane = lk is not None and lk.kind == "plane"
            connects = pl.kind == "cu" and (not is_plane or (
                bool(b.net) and (b.net == lk.plane_net or (pl.name, b.net) in plane_region_nets)))
            # Where the barrel meets this plane: at its land on a signal
            # layer, at its wall on a plane it joins and in laminate.
            r_c = r_land if (connects and not is_plane) else r_wall
            if r_c not in rings:
                rings[r_c] = _ring(raster, idx, b.x, b.y, r_c)
            rc, rho = rings[r_c]
            if not len(rc):
                rc, rho = np.array([c0]), np.array([r_c * 1.5])
            sheet = pl.kxy[rc] * pl.t_m
            if pl.kind == "cu" and not connects:
                # through the antipad's resin, then the plane's copper
                inner = np.log(np.minimum(rho, r_anti) / r_wall) / (two_pi * kxy_d * pl.t_m)
                outer = np.log(np.maximum(rho / r_anti, 1.0)) / (two_pi * sheet)
                g = 1.0 / (len(rc) * np.maximum(inner + outer, 1e-12))
            else:
                g = two_pi * sheet / (len(rc) * np.maximum(np.log(rho / r_c), LN_MIN))
            link(np.full(rc.shape, node), (p0 + k) * M + rc, g)
            if connects and not is_plane and r_land > r_wall:
                # The land is the barrel's own copper: the cells under it
                # sit at the barrel's temperature. On a grid finer than the
                # land they lie inside the ring above and nothing else
                # would tie them to it.
                lc, _ = _ring(raster, idx, b.x, b.y, 0.0, reach=r_land)
                if len(lc):
                    link(np.full(lc.shape, node), (p0 + k) * M + lc,
                         two_pi * pl.kxy[lc] * pl.t_m / (len(lc) * LN_MIN))
        barrel_nodes.append((b, chain))

    # parts
    top_p, bot_p = 0, cu_index[raster.layers[-1]]
    sources: list[tuple[int, float]] = []
    part_rows: list[dict] = []
    cell_area_m2 = dx * dx
    for ref in sorted(set(pw) | set(packages) | set(report_parts or [])):
        comp = board.component(ref)
        if comp is None:
            raise ValueError(f"no component {ref!r} on the board")
        side_layer = raster.layers[-1] if comp.side == BOTTOM else raster.layers[0]
        sp_ = cu_index[side_layer]
        pads = _pad_cells(board, raster, ref, side_layer, idx)
        ring, a_top, a_side = _body(board, ref)
        row = {"ref": ref, "side": comp.side, "layer": side_layer, "power_w": pw.get(ref, 0.0),
               "plane": sp_, "pkg": packages.get(ref, Package()), "a_top": a_top,
               "a_side": a_side}
        if not pads:
            shape = geom.polygon(ring)
            for (ix, iy), a in raster.shape_area_by_cell(shape).items():
                if idx[iy, ix] >= 0:
                    pads[int(idx[iy, ix])] = a * MIL * MIL
            if pads and ref in pw:
                notes.append(f"{ref}: no pads on {side_layer}; its heat enters under its outline")
        if not pads:
            if ref in pw:
                raise ValueError(f"{ref} is powered but lies outside the board")
            continue
        row["pad_cells"] = pads
        pkg = row["pkg"]
        if ref in pw or pkg.theta_jb is not None or pkg.theta_jc is not None:
            j = next_node
            next_node += 1
            row["j"] = j
            tot = sum(pads.values())
            if pkg.theta_jb is not None and pkg.theta_jb <= 0:
                raise ValueError(f"{ref}: theta_jb must be > 0")
            g_jb = (1.0 / pkg.theta_jb) if pkg.theta_jb else TIGHT_W_PER_K
            pc = np.fromiter(pads.keys(), dtype=np.int64)
            pa = np.fromiter(pads.values(), dtype=float)
            link(np.full(pc.shape, j), sp_ * M + pc, g_jb * pa / tot)
            sources.append((j, pw.get(ref, 0.0)))
            if pkg.theta_jc is not None:
                if pkg.theta_jc <= 0:
                    raise ValueError(f"{ref}: theta_jc must be > 0")
                row["case"] = next_node
                next_node += 1
                link(j, row["case"], 1.0 / pkg.theta_jc)
            if ref in pw and tot < 9 * cell_area_m2:
                row["under_resolved"] = True
        part_rows.append(row)

    N = next_node
    b_vec = np.zeros(N)
    for node, w in sources:
        b_vec[node] += w
    for layer, arr in (extra_heat_w or {}).items():
        if layer not in cu_index:
            raise ValueError(f"extra heat on unknown layer {layer!r}")
        arr = np.asarray(arr, dtype=float)
        if arr.shape != act.shape:
            raise ValueError(f"extra heat for {layer} is {arr.shape}, the grid is {act.shape}")
        lost = float(arr[~act].sum())
        if abs(lost) > 1e-12:
            notes.append(f"{lost:.3g} W of extra heat on {layer} lies outside the board "
                         f"and is dropped")
        b_vec[cu_index[layer] * M:(cu_index[layer] + 1) * M] += arr[act]
    for ha in heat_areas or []:
        if ha.layer not in cu_index:
            raise ValueError(f"heat area on unknown layer {ha.layer!r}")
        rect = geom.polygon([(ha.x0, ha.y0), (ha.x1, ha.y0), (ha.x1, ha.y1), (ha.x0, ha.y1)])
        got = {int(idx[iy, ix]): a for (ix, iy), a in raster.shape_area_by_cell(rect).items()
               if idx[iy, ix] >= 0}
        if not got:
            raise ValueError(f"heat area on {ha.layer} lies outside the board")
        # The heat is made in the layer's copper inside the rectangle, each
        # cell taking its copper's share, so the answer does not depend on
        # whether a cell holds copper alone or copper and bare laminate.
        # A rectangle with next to no copper heats the laminate's surface.
        fill_l = planes[cu_index[ha.layer]].fill
        in_cu = {c: a * float(fill_l[c]) for c, a in got.items()}
        if sum(in_cu.values()) >= HEAT_AREA_MIN_COPPER * sum(got.values()):
            got = {c: a for c, a in in_cu.items() if a > 0.0}
        tot = sum(got.values())
        for c, a in got.items():
            b_vec[cu_index[ha.layer] * M + c] += ha.power_w * a / tot

    # clamps: a contact conductance to a fixed temperature
    d_clamp = np.zeros(N)
    b_clamp = np.zeros(N)
    clamp_terms: list[tuple[np.ndarray, np.ndarray, float]] = []
    xs = raster.x0 + (cx + 0.5) * raster.cell_mils
    ys = raster.y0 + (cy + 0.5) * raster.cell_mils
    for cl in clamps or []:
        if cl.side not in ("top", "bottom"):
            raise ValueError("a clamp's side must be top or bottom")
        p = top_p if cl.side == "top" else bot_p
        inside = ((xs >= min(cl.x0, cl.x1)) & (xs <= max(cl.x0, cl.x1))
                  & (ys >= min(cl.y0, cl.y1)) & (ys <= max(cl.y0, cl.y1)))
        cidx = np.nonzero(inside)[0]
        if not len(cidx):
            notes.append(f"clamp at ({cl.x0:g},{cl.y0:g})-({cl.x1:g},{cl.y1:g}) covers no "
                         f"board cell")
            continue
        g = cl.h_contact * A[cidx]
        np.add.at(d_clamp, p * M + cidx, g)
        np.add.at(b_clamp, p * M + cidx, g * cl.temp_c)
        clamp_terms.append((p * M + cidx, g, cl.temp_c))

    K0 = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                       shape=(N, N))
    d0 = np.bincount(np.concatenate(diag_n), weights=np.concatenate(diag_g), minlength=N)
    d0 += d_clamp
    b0 = b_vec + b_clamp

    # surfaces
    ta = setup.ambient_c
    bnd = _Boundary(setup)
    all_edges = _edge_faces(act)
    area_tot = float(A.sum())
    perim = float(all_edges.sum()) * dx
    L_plate = ph.plate_length(area_tot, perim)
    board_t = sum(pl.t_m for pl in planes)
    half = raster.cell_mils / 2.0

    def _along(deg: float) -> tuple[np.ndarray, np.ndarray, float]:
        """Each cell's span (m) from the upstream edge along a direction."""
        ux, uy = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        s = xs * ux + ys * uy
        e = (abs(ux) + abs(uy)) * half
        lo = float((s - e).min())
        return (np.maximum(s - e - lo, 0.0) * MIL, (s + e - lo) * MIL,
                float((s + e).max() - lo) * MIL)

    fx1, fx2, flow_len = _along(setup.airflow_deg)
    vy1, vy2, height = _along(setup.up_deg)
    r_mask = (ph.SOLDER_MASK_MILS * MIL / ph.K_SOLDER_MASK) if setup.solder_mask else 0.0
    face_orient = {"horizontal": ("up", "down"), "inverted": ("down", "up"),
                   "vertical": ("vertical", "vertical")}[setup.orientation]
    vertical = setup.orientation == "vertical"
    for which, p, orient, fixed in (("top", top_p, face_orient[0], setup.h_top),
                                    ("bottom", bot_p, face_orient[1], setup.h_bottom)):
        bnd.surfaces.append(_Surface(
            which, which, p * M + cells, A, orient, height if vertical else L_plate,
            flow_len, r_mask, fixed,
            vy1 if vertical else None, vy2 if vertical else None, fx1, fx2))
    if setup.edges and all_edges.any():
        e_cells = np.nonzero(all_edges)[0]
        for p, pl in enumerate(planes):
            bnd.surfaces.append(_Surface(
                "edges", "edge", p * M + e_cells, all_edges[e_cells] * dx * pl.t_m,
                "vertical", max(board_t, 1e-5), flow_len))
    for row in part_rows:
        if "case" not in row:
            continue
        pkg = row["pkg"]
        on_top = row["side"] != BOTTOM
        orient = face_orient[0] if on_top else face_orient[1]
        area = max(row["a_top"] + row["a_side"], 1e-9)
        g_sink = None
        if pkg.heatsink_theta_sa is not None:
            if pkg.heatsink_theta_sa <= 0:
                raise ValueError(f"{row['ref']}: heatsink_theta_sa must be > 0")
            g_sink = np.array([1.0 / pkg.heatsink_theta_sa])
        bnd.surfaces.append(_Surface(
            f"case {row['ref']}", "case", np.array([row["case"]]), np.array([area]), orient,
            height if vertical else L_plate, flow_len,
            fixed_h=setup.h_top if on_top else setup.h_bottom, g_lin=g_sink))

    # Newton
    T = np.full(N, ta + 10.0)
    info: dict = {}
    it = 0
    delta = math.inf
    converged = False
    while it < setup.max_iter:
        it += 1
        Q = np.zeros(N)
        dQ = np.zeros(N)
        for s in bnd.surfaces:
            Ts = T[s.nodes]
            q, _, _ = bnd.flux(s, Ts)
            q2, _, _ = bnd.flux(s, Ts + _DT)
            Q[s.nodes] += q
            dQ[s.nodes] += np.maximum((q2 - q) / _DT, 0.0)
        Kmat = (K0 + sp.diags(d0 + dQ)).tocsr()
        # Inexact Newton: far from the solution a loose linear solve moves
        # the iterate just as far; only the last steps are solved tightly,
        # and only a tight step may declare convergence.
        tight = delta < 1.0
        T_new = _linear_solve(Kmat, b0 - Q + dQ * T, T, info,
                              CG_RTOL if tight else CG_RTOL_EARLY)
        delta = float(np.max(np.abs(T_new - T)))
        T = T_new
        if (tight or info.get("method") == "superlu") and delta < setup.tol_k:
            converged = True
            break
    if not converged:
        notes.append(f"not converged after {it} Newton iterations (last change {delta:.3g} K)")

    # heat balance with the final nonlinear fluxes
    out: dict[str, float] = {}
    h_used: dict[str, dict] = {}
    names = {"top": "top", "bottom": "bottom", "edge": "edges", "case": "part_cases"}
    for s in bnd.surfaces:
        Ts = T[s.nodes]
        q, hc, hr = bnd.flux(s, Ts)
        lin = (s.g_lin * (Ts - ta)) if s.g_lin is not None else 0.0
        qs = q - lin
        frac_c = hc / np.maximum(hc + hr, 1e-12)
        key = names[s.kind]
        out[f"{key}_convection_w"] = out.get(f"{key}_convection_w", 0.0) \
            + float((qs * frac_c).sum())
        out[f"{key}_radiation_w"] = out.get(f"{key}_radiation_w", 0.0) \
            + float((qs * (1 - frac_c)).sum())
        if s.g_lin is not None:
            out["heatsinks_w"] = out.get("heatsinks_w", 0.0) + float(np.sum(lin))
        if s.kind in ("top", "bottom"):
            a = s.area
            h_used[s.name] = {
                "convection_w_m2k_mean": round(float((hc * a).sum() / a.sum()), 4),
                "convection_w_m2k_min": round(float(hc.min()), 4),
                "convection_w_m2k_max": round(float(hc.max()), 4),
                "radiation_w_m2k_mean": round(float((hr * a).sum() / a.sum()), 4),
                "mean_surface_c": round(float((Ts * a).sum() / a.sum()), 3),
                "faces": s.orient,
            }
    if clamp_terms:
        out["clamps_w"] = float(sum((g * (T[nodes] - tc)).sum() for nodes, g, tc in clamp_terms))
    p_in = float(b_vec.sum())
    p_out = float(sum(out.values()))
    balance = {"power_in_w": round(p_in, 6), **{k: round(v, 6) for k, v in out.items()},
               "power_out_w": round(p_out, 6),
               "error_pct": round(100 * abs(p_out - p_in) / p_in, 6) if p_in else 0.0}

    plane_temps = []
    for p in range(P):
        arr = np.full(act.shape, np.nan)
        arr[act] = T[p * M:(p + 1) * M]
        plane_temps.append(arr)
    temps = {name: plane_temps[cu_index[name]] for name in raster.layers}

    parts = []
    for row in part_rows:
        pads = row["pad_cells"]
        pc = np.fromiter(pads.keys(), dtype=np.int64)
        pa = np.fromiter(pads.values(), dtype=float)
        tp = T[row["plane"] * M + pc]
        pkg = row["pkg"]
        d_ = {"ref": row["ref"], "side": row["side"], "power_w": row["power_w"],
              "t_pads_c": round(float((tp * pa).sum() / pa.sum()), 3),
              "t_pads_max_c": round(float(tp.max()), 3)}
        if "j" in row and pkg.theta_jb is not None:
            d_["t_junction_c"] = round(float(T[row["j"]]), 3)
        if "case" in row:
            d_["t_case_c"] = round(float(T[row["case"]]), 3)
        if pkg.tj_max_c is not None:
            d_["tj_max_c"] = pkg.tj_max_c
            ref_t = d_.get("t_junction_c", d_["t_pads_c"])
            d_["margin_k"] = round(pkg.tj_max_c - ref_t, 3)
        if row.get("under_resolved"):
            d_["under_resolved"] = True
        parts.append(d_)

    barrels = [{"x": bb.x, "y": bb.y, "net": bb.net, "kind": bb.kind,
                "t_top_c": float(T[chain[0]]), "t_bottom_c": float(T[chain[-1]])}
               for bb, chain in barrel_nodes]
    small = [d_["ref"] for d_ in parts if d_.get("under_resolved")]
    if small:
        notes.append(f"{', '.join(small[:8])}{' ...' if len(small) > 8 else ''}: pads cover "
                     f"fewer than 9 cells of {raster.cell_mils:g} mil; the peak under them "
                     f"is smoothed. Use a smaller cell_mils or refine.")
    if info.get("pyamg_missing"):
        notes.append("pyamg is not installed: a direct solver was used, which is slow on "
                     "fine grids (pip install pyamg)")
    solver = {"cells": M, "planes": P, "plated_holes": len(barrel_nodes), "unknowns": N,
              "newton_iterations": it, "converged": converged,
              "linear_solver": info.get("method", ""),
              "cg_iterations": info.get("cg_iterations", 0)}
    return Result(temps, plane_temps, planes, parts, balance, it, h_used, notes, raster,
                  barrels, solver, ta)
