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
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ..layout import geom
from ..layout.model import LayoutBoard, Pad
from . import physics as ph
from .decap import (Decap, default_esl, default_esr, is_bulk, mounting_inductance,
                    package_of, parse_capacitance, touchstone_impedance)

#: Cavity grid: at least this many cells across the longer side, at most
#: this many cells in all, and no coarser than lambda / CELLS_PER_WAVE at
#: the top frequency.
MIN_CELLS_ACROSS = 48
MAX_CAVITY_CELLS = 30_000
CELLS_PER_WAVE = 20
VIA_SEARCH_MILS = 200.0


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

    @property
    def n(self) -> int:
        return int(self.cells.sum())

    @property
    def area_mils2(self) -> float:
        return self.n * self.cell_mils ** 2

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


def _area_fill(board: LayoutBoard, net: str, cell: float):
    from ..layout.copper_raster import rasterize
    return rasterize(board, cell, nets=[net], kinds={"regions", "planes"}, supersample=3)


def cavity_cell_mils(board: LayoutBoard, f_max_hz: float, er: float) -> float:
    from ..layout.copper_raster import _bounds
    x0, y0, x1, y1 = _bounds(board)
    w, h = x1 - x0, y1 - y0
    lam = ph.C0 / (f_max_hz * math.sqrt(er)) / ph.MIL
    cell = min(lam / CELLS_PER_WAVE, max(w, h) / MIN_CELLS_ACROSS)
    floor = math.sqrt(w * h / MAX_CAVITY_CELLS)
    return max(cell, floor, 2.0)


def find_cavity(board: LayoutBoard, stack, net: str, ground: str, f_max_hz: float,
                cell_mils: float = 0.0, tand: Optional[float] = None) -> Optional[Cavity]:
    """The power/ground plane pair carrying the rail: of all pairs of a
    layer with ``net`` area copper and a layer with ``ground`` area copper,
    the one with the largest overlap capacitance. None when the rail has no
    area copper over a ground layer."""
    if stack is None or not stack.layers:
        return None
    er0 = max((d.er for d in stack.dielectrics), default=4.3)
    cell = cell_mils or cavity_cell_mils(board, f_max_hz, er0)
    rp = _area_fill(board, net, cell)
    rg = _area_fill(board, ground, cell)
    best = None
    for lp in rp.layers:
        fp = rp.fill[lp] >= 0.5
        if not fp.any():
            continue
        for lg in rg.layers:
            if lg == lp:
                continue
            fg = rg.fill[lg] >= 0.5
            both = fp & fg
            if not both.any():
                continue
            d = stack.gap_mils(lp, lg)
            if not d:
                continue
            c = both.sum() / d
            if best is None or c > best[0]:
                best = (c, lp, lg, both, d)
    if best is None:
        return None
    _, lp, lg, both, d = best
    from scipy.ndimage import label
    lab, n = label(both)
    notes = []
    if n > 1:
        sizes = np.bincount(lab.ravel())[1:]
        keep = int(np.argmax(sizes)) + 1
        notes.append(f"the plane pair is in {n} separate islands; the largest "
                     f"({sizes[keep - 1] * cell * cell / 1e6:.2f} in2) is modelled")
        both = lab == keep
    ids = np.full(both.shape, -1, dtype=np.int64)
    ids[both] = np.arange(int(both.sum()))
    er = stack.er_between(lp, lg)
    cp, cg = stack.get(lp), stack.get(lg)
    tan_d = tand if tand is not None else (
        next((x.tan_d for x in stack.dielectrics), 0.02))
    return Cavity(lp, lg, d, er, tan_d, cp.copper_mils if cp else 1.4,
                  cg.copper_mils if cg else 1.4, rp.x0, rp.y0, cell, both, ids,
                  cp.z_mils if cp else 0.0, cg.z_mils if cg else 0.0, notes)


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
    a, b = [], []
    for A, B in ((ids[:, :-1], ids[:, 1:]), (ids[:-1, :], ids[1:, :])):
        m = (A >= 0) & (B >= 0)
        a.append(A[m]); b.append(B[m])
    a = np.concatenate(a); b = np.concatenate(b)
    n = cav.n
    ones = np.ones(a.size)
    L = sp.coo_matrix((np.concatenate([ones, ones, -ones, -ones]),
                       (np.concatenate([a, b, a, b]), np.concatenate([a, b, b, a]))),
                      shape=(n, n)).tocsc()
    return L


def solve_cavity(cav: Cavity, freqs: np.ndarray, ports: list[tuple[int, np.ndarray]],
                 load_nodes: list[int], rho: float = ph.RHO_CU_20C,
                 extra_shunt: Optional[np.ndarray] = None,
                 observe_nodes: Optional[list[int]] = None) -> np.ndarray:
    """Impedance at the load nodes (1 A split equally, mean voltage) with
    each port (node, Z(f) array) shunting its node to the ground plane.
    ``observe_nodes`` reads the voltage elsewhere instead: the transfer
    impedance from the load to those nodes."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import splu

    n = cav.n
    L = _laplacian(cav)
    d_m = cav.d_mils * ph.MIL
    area = (cav.cell_mils * ph.MIL) ** 2
    eps = djordjevic_sarkar(freqs, cav.er, cav.tand)
    zs_p = surface_impedance(freqs, cav.t_power_mils * ph.MIL, rho)
    zs_g = surface_impedance(freqs, cav.t_ground_mils * ph.MIL, rho)
    rhs = np.zeros(n, dtype=complex)
    for k in load_nodes:
        rhs[k] += 1.0 / len(load_nodes)
    out = np.empty(len(freqs), dtype=complex)
    port_nodes = np.array([p[0] for p in ports], dtype=np.int64)
    for i, f in enumerate(freqs):
        w = 2 * np.pi * f
        zsq = 1j * w * ph.MU0 * d_m + zs_p[i] + zs_g[i]
        yc = 1j * w * ph.EPS0 * eps[i] * area / d_m
        diag = np.full(n, yc, dtype=complex)
        for (node, z) in ports:
            diag[node] += 1.0 / z[i]
        if extra_shunt is not None:
            diag += extra_shunt[i]
        A = (L / zsq + sp.diags(diag)).tocsc()
        v = splu(A).solve(rhs)
        out[i] = np.mean(v[observe_nodes if observe_nodes else load_nodes])
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


def _smd_layer(board: LayoutBoard, pad: Pad) -> str:
    if pad.hole > 0:
        return ""
    ls = pad.layers()
    return ls[0] if ls else board.copper_layers()[0]


def _near_vias(board: LayoutBoard, net: str, x: float, y: float, reach: float):
    """Barrels of ``net`` (vias and plated TH pads) within ``reach``, nearest first,
    as (distance, x, y, hole)."""
    out = []
    for v in board.vias:
        if v.net == net and v.hole > 0:
            d = math.hypot(v.x - x, v.y - y)
            if d <= reach:
                out.append((d, v.x, v.y, v.hole))
    for p in board.pads:
        if p.net == net and p.hole > 0 and p.plated:
            d = math.hypot(p.x - x, p.y - y)
            if d <= reach:
                out.append((d, p.x, p.y, p.hole))
    out.sort()
    return out


def _track_width_at(board: LayoutBoard, net: str, pad: Pad, layer: str) -> float:
    shape = pad.shape_on(layer)
    best = 0.0
    for t in board.tracks:
        if t.net != net or t.layer != layer or shape is None:
            continue
        for (x, y) in ((t.x1, t.y1), (t.x2, t.y2)):
            if geom.clearance(geom.circle(x, y, 0.0), shape) <= 0:
                best = max(best, t.width)
    if best:
        return best
    sizes = [min(c.w, c.h) for c in pad.copper if c.layer == layer]
    return min(sizes) if sizes else 10.0


def _ref_height(stack, layer: str) -> float:
    nb = stack.neighbours(layer) if stack is not None else []
    return abs(stack.get(layer).z_mils - nb[0].z_mils) if nb else 8.0


def mount_for(board: LayoutBoard, stack, cav: Optional[Cavity], net: str, ground: str,
              pwr_pads: list[Pad], gnd_pads: list[Pad], plating: float,
              reach: float = VIA_SEARCH_MILS) -> dict:
    """Mounting inductance of a two-terminal (or multi-pin) part between
    its pads and the cavity."""
    if not pwr_pads or not gnd_pads:
        return {"_total": 1e-9, "total_nh": 1.0, "note": "pads not found; 1 nH assumed"}
    pp, pg = pwr_pads[0], gnd_pads[0]
    layer = _smd_layer(board, pp) or (cav.power_layer if cav else "")
    cell = cav.cell_mils if cav else 50.0
    if cav is not None and stack is not None and stack.get(layer) is not None:
        zc = stack.get(layer).z_mils
        zp, zg = cav.z_power, cav.z_ground
        z_near = zp if abs(zc - zp) < abs(zc - zg) else zg
        d = cav.d_mils
    else:
        zc, z_near, d = 0.0, _ref_height(stack, layer) if stack is not None and stack.get(layer) else 8.0, 0.0
    vp = [v for p in pwr_pads for v in _near_vias(board, net, p.x, p.y, reach)]
    vg = [v for p in gnd_pads for v in _near_vias(board, ground, p.x, p.y, reach)]
    if pp.hole > 0 and pg.hole > 0:
        vp = vp or [(0.0, pp.x, pp.y, pp.hole)]
        vg = vg or [(0.0, pg.x, pg.y, pg.hole)]
    if not vp or not vg:
        side = "power" if not vp else "ground"
        return {"_total": 1e-9, "total_nh": 1.0,
                "note": f"no {side} via within {reach:g} mil of the pads; 1 nH assumed"}
    vp.sort(); vg.sort()
    # vias that belong to this part: within 30 mil of the nearest one
    vp_use = [v for v in vp if v[0] <= vp[0][0] + 30.0]
    vg_use = [v for v in vg if v[0] <= vg[0][0] + 30.0]
    s = min(math.hypot(a[1] - b[1], a[2] - b[2]) for a in vp_use for b in vg_use)
    r = vp_use[0][3] / 2.0 + plating
    h_ref = _ref_height(stack, layer) if (stack is not None and stack.get(layer)) else 8.0
    half = lambda p: max([max(c.w, c.h) for c in p.copper] or [0.0]) / 2.0  # noqa: E731
    lt_p = max(0.0, vp_use[0][0] - half(pp))
    lt_g = max(0.0, vg_use[0][0] - half(pg))
    w_p = _track_width_at(board, net, pp, layer) if layer else 10.0
    w_g = _track_width_at(board, ground, pg, layer) if layer else 10.0
    n_each = min(len(vp_use), len(vg_use))
    # Each fan-out trace counted with its own width
    m1 = mounting_inductance(cap_z_mils=zc, plane_z_mils=z_near, cavity_mils=d,
                             via_spacing_mils=s, via_radius_mils=r, n_vias_each=n_each,
                             trace_len_mils=lt_p, trace_w_mils=w_p, ref_h_mils=h_ref,
                             cell_mils=cell)
    lt2 = ph.MU0 * h_ref * lt_g * ph.MIL / (w_g + 2 * h_ref) if lt_g > 0 else 0.0
    total = m1["_total"] + lt2
    return {"_total": total, "total_nh": round(total * 1e9, 4),
            "vias_nh": m1["vias_nh"], "traces_nh": round(m1["traces_nh"] + lt2 * 1e9, 4),
            "cavity_nh": m1["cavity_nh"], "via_pairs": n_each,
            "via_spacing_mils": round(s, 2), "height_mils": round(abs(zc - z_near), 2),
            "fanout_mils": [round(lt_p, 1), round(lt_g, 1)]}


def find_decaps(board: LayoutBoard, net: str, ground: str) -> list[tuple[str, list[Pad], list[Pad]]]:
    """Parts with pads on both the rail and its ground whose comment reads
    as a capacitance (or whose designator starts with C)."""
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
        comp = board.component(ref)
        comment = comp.comment if comp else ""
        if len(pads) <= 3 and (parse_capacitance(comment) is not None
                               or ref.upper().startswith("C")):
            out.append((ref, pw, gn))
    return out


def build_decaps(board: LayoutBoard, stack, cav, net: str, ground: str,
                 overrides: Optional[list[dict]], plating: float,
                 exclude: Optional[list[str]] = None) -> tuple[list[Decap], list[str]]:
    notes = []
    over = {o["ref"]: o for o in (overrides or []) if o.get("ref")}
    excl = set(exclude or [])
    caps: list[Decap] = []
    found = find_decaps(board, net, ground) if board is not None else []
    found_refs = {r for r, _, _ in found}
    for ref, pw, gn in found:
        if ref in excl:
            continue
        comp = board.component(ref)
        comment = comp.comment if comp else ""
        fp = comp.footprint if comp else ""
        o = over.get(ref, {})
        c = o.get("c_farads") or (o.get("c_uf") and o["c_uf"] * 1e-6) or parse_capacitance(comment)
        if not c:
            notes.append(f"{ref}: no capacitance in its comment {comment!r}; skipped "
                         f"(pass it in decaps)")
            continue
        bulk = is_bulk(fp, comment)
        pkg = o.get("package") or package_of(fp)
        esr = o.get("esr_ohms", default_esr(c, bulk))
        esl = o["esl_nh"] * 1e-9 if "esl_nh" in o else default_esl(pkg, bulk)
        src = "user" if o else "defaults"
        d = Decap(ref, float(c), float(esr), float(esl), pkg,
                  x=sum(p.x for p in pw + gn) / len(pw + gn),
                  y=sum(p.y for p in pw + gn) / len(pw + gn),
                  layer=_smd_layer(board, pw[0]), source=src)
        if not pkg and not bulk and "esl_nh" not in o:
            d.notes.append("package not recognised from the footprint; 0.5 nH ESL assumed")
        if o.get("touchstone"):
            d.touchstone = touchstone_impedance(o["touchstone"], o.get("config", "shunt"))
            d.source = "touchstone"
        mt = mount_for(board, stack, cav, net, ground, pw, gn, plating)
        if "mount_nh" in o:
            d.mount_l = o["mount_nh"] * 1e-9
            mt = {"total_nh": o["mount_nh"], "note": "given"}
        else:
            d.mount_l = mt.pop("_total")
        d.mount_detail = {k: v for k, v in mt.items() if not k.startswith("_")}
        caps.append(d)
    # capacitors given that are not on the board (what-if, or schematic stage)
    for ref, o in over.items():
        if ref in found_refs or ref in excl:
            continue
        c = o.get("c_farads") or (o.get("c_uf") and o["c_uf"] * 1e-6)
        if not c:
            continue
        n = int(o.get("count", 1))
        pkg = o.get("package", "")
        d = Decap(ref, float(c) * n, float(o.get("esr_ohms", default_esr(c))) / n,
                  (o["esl_nh"] * 1e-9 if "esl_nh" in o else default_esl(pkg)) / n, pkg,
                  mount_l=float(o.get("mount_nh", 0.5)) * 1e-9 / n,
                  x=float(o.get("x", 0.0)), y=float(o.get("y", 0.0)), source="user")
        if o.get("touchstone"):
            d.touchstone = touchstone_impedance(o["touchstone"], o.get("config", "shunt"))
            d.source = "touchstone"
        if n > 1:
            d.notes.append(f"{n} in parallel")
        if "x" not in o and board is not None:
            d.notes.append("not on the board and no x/y given: attached at the load")
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
                c_plane: float, load_l: float, title: str) -> None:
    """A lumped SPICE deck of the network (LTspice and ngspice): every
    capacitor as R-L-C with its mounting, the VRM, the plane capacitance,
    and a 1 A AC source at the load, so V(load) reads ohms."""
    lines = [f"* {title}", "* PDN lumped equivalent written by eda-agent (sim_pdn_impedance)",
             "* V(load) in volts equals |Z| in ohms (1 A AC injection)."]
    for i, d in enumerate(decaps, 1):
        lines.append(f"R{i} pdn n{i}a {d.esr:.6g}")
        lines.append(f"L{i} n{i}a n{i}b {d.esl + d.mount_l:.6g}")
        lines.append(f"C{i} n{i}b 0 {d.c:.6g}  ; {d.ref}")
    if vrm is not None:
        lines.append(f"RVRM pdn nv {vrm[0]:.6g}")
        lines.append(f"LVRM nv 0 {vrm[1]:.6g}")
    if c_plane > 0:
        lines.append(f"CPLANE pdn 0 {c_plane:.6g}")
    lines.append(f"LLOAD load pdn {max(load_l, 1e-15):.6g}")
    lines.append("I1 0 load AC 1")
    lines.append(f".ac dec 50 {freqs[0]:.6g} {freqs[-1]:.6g}")
    lines.append(".end")
    from pathlib import Path
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plot(path, f, zmag, target, zlump=None, title=""):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 - optional extra
        return False
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
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
    plt.close(fig)
    return True


def analyse(board: Optional[LayoutBoard], stack, net: str, ground: str, *,
            load: dict, target_ohms: float = 0.0, volts: float = 0.0,
            ripple_pct: float = 0.0, step_amps: float = 0.0,
            f_min_hz: float = 1e3, f_max_hz: float = 1e9, points: int = 241,
            decaps: Optional[list[dict]] = None, exclude: Optional[list[str]] = None,
            vrm: Optional[dict] = None, mode: str = "auto", cell_mils: float = 0.0,
            plating_mils: float = ph.DEFAULT_PLATING_MILS, temp_c: float = 25.0,
            tand: Optional[float] = None, out_dir=None) -> dict:
    if mode not in ("auto", "cavity", "lumped"):
        raise PDNError("mode is auto, cavity or lumped")
    if f_min_hz <= 0 or f_max_hz <= f_min_hz:
        raise PDNError("need 0 < f_min_hz < f_max_hz")
    freqs = np.logspace(math.log10(f_min_hz), math.log10(f_max_hz), int(points))
    rho = ph.copper_resistivity(temp_c)
    notes: list[str] = []
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

    cav = None
    if board is not None and mode in ("auto", "cavity"):
        cav = find_cavity(board, stack, net, ground, f_max_hz, cell_mils, tand)
        if cav is None and mode == "cavity":
            raise PDNError(f"no plane pair found for {net} over {ground}: the rail "
                           f"needs area copper (plane, pour) facing a {ground} layer. "
                           f"Use mode='lumped'.")
        if cav is None:
            notes.append(f"{net} has no plane facing {ground}: lumped model, no "
                         f"spreading inductance or plane resonance")
    caps, cap_notes = build_decaps(board, stack, cav, net, ground, decaps, plating_mils,
                                   exclude) if board is not None else build_decaps(
        None, stack, None, net, ground, decaps, plating_mils, exclude)
    notes += cap_notes
    if not caps:
        notes.append("no decoupling capacitors found or given")
    if any(c.source == "defaults" for c in caps):
        notes.append("capacitors on default ESR/ESL and NOMINAL capacitance: MLCC "
                     "DC-bias derating is not applied; pass c_uf/esr_ohms/esl_nh "
                     "or a touchstone per part for sign-off numbers")

    # VRM
    v = dict(vrm or {})
    r_vrm = float(v.get("r_mohm", target * 1e3 * 0.5)) * 1e-3
    f_bw = float(v.get("bandwidth_khz", 50.0)) * 1e3
    l_vrm = float(v["l_nh"]) * 1e-9 if "l_nh" in v else target / (2 * math.pi * f_bw)
    vrm_used = {"r_mohm": round(r_vrm * 1e3, 4), "l_nh": round(l_vrm * 1e9, 4),
                "model": "given" if vrm else "default: R = target/2, L = target / (2 pi 50 kHz)"}
    if v.get("disable"):
        vrm_used = None
    z_vrm = r_vrm + 1j * 2 * np.pi * freqs * l_vrm

    # load
    load_pads: list[Pad] = []
    load_l = 0.0
    load_info: dict[str, Any] = {}
    if board is not None:
        from .ir_drop import resolve_pads
        load_pads = resolve_pads(board, net, load)
        if not load_pads:
            raise PDNError(f"load {load} has no pads on {net}")
        gpads = resolve_pads(board, ground, {"ref": load.get("ref") or
                                             sorted({p.comp for p in load_pads})})
        if load.get("include_vias", True):
            mt = mount_for(board, stack, cav, net, ground, load_pads, gpads, plating_mils,
                           reach=100.0)
            load_l = mt.pop("_total")
            load_info["mount"] = mt
    if "mount_nh" in load:
        load_l = float(load["mount_nh"]) * 1e-9
        load_info["mount"] = {"total_nh": load["mount_nh"], "note": "given"}

    zl = 1j * 2 * np.pi * freqs * load_l
    if cav is not None:
        ports = []
        attach = []
        for c in caps:
            if c.x == 0.0 and c.y == 0.0 and c.ref not in {r for r, _, _ in find_decaps(board, net, ground)}:
                xs = sum(p.x for p in load_pads) / len(load_pads)
                ys = sum(p.y for p in load_pads) / len(load_pads)
                node, dist = cav.node_near(xs, ys)
            else:
                node, dist = cav.node_near(c.x, c.y)
            ports.append((node, c.z(freqs)))
            if dist > 2 * cav.cell_mils:
                c.notes.append(f"{dist:.0f} mil outside the plane pair; attached at its edge")
        if vrm_used is not None:
            src = load.get("_vrm_at")
            vx, vy = None, None
            if v.get("ref") or v.get("pads"):
                from .ir_drop import resolve_pads as rp
                vp = rp(board, net, v)
                if vp:
                    vx, vy = sum(p.x for p in vp) / len(vp), sum(p.y for p in vp) / len(vp)
            if vx is None:
                # no position given: the largest capacitor's place stands in
                big = max(caps, key=lambda c: c.c) if caps else None
                vx, vy = (big.x, big.y) if big else (cav.x0, cav.y0)
                vrm_used["at"] = "largest capacitor (pass vrm.ref for the real place)"
            else:
                vrm_used["at"] = [round(vx, 1), round(vy, 1)]
            node, _ = cav.node_near(vx, vy)
            ports.append((node, z_vrm))
        lx = [p.x for p in load_pads]; ly = [p.y for p in load_pads]
        load_nodes = sorted({cav.node_near(x, y)[0] for x, y in zip(lx, ly)})
        z_plane = solve_cavity(cav, freqs, ports, load_nodes, rho)
        model = "cavity"
    else:
        z_plane = None
        model = "lumped"
    branches = [c.z(freqs) for c in caps] + ([z_vrm] if vrm_used is not None else [])
    c_pl = cav.capacitance_f if cav is not None else 0.0
    er = cav.er if cav is not None else 4.3
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
    result = {
        "net": net, "ground": ground, "model": model,
        "target": tgt, "pass": not bands,
        "worst": ({"mhz": round(float(freqs[int(np.argmax(zmag / target))]) / 1e6, 5),
                   "mohm": round(float(zmag.max()) * 1e3, 4),
                   "over_target_x": round(float(zmag.max() / target), 3)}),
        "minimum": {"mhz": round(float(freqs[k_min]) / 1e6, 5),
                    "mohm": round(float(zmag[k_min]) * 1e3, 4)},
        "violations": bands,
        "anti_resonances": _peaks(freqs, zmag),
        "suggestions": _suggest(bands, target, float(mount_avg), f_max_hz),
        "cavity": cav.to_dict() if cav is not None else None,
        "decaps": [c.to_dict() for c in caps],
        "vrm": vrm_used,
        "load": {"pads": [f"{p.comp}.{p.name}" for p in load_pads][:20],
                 "mount_nh_included": round(load_l * 1e9, 4), **load_info},
        "curve": curve,
        "notes": notes + (cav.notes if cav is not None else []),
    }
    if cav is not None:
        f1 = ph.C0 / (2 * max(cav.cells.shape[1], cav.cells.shape[0]) * cav.cell_mils
                      * ph.MIL * math.sqrt(er))
        result["cavity"]["first_mode_mhz_estimate"] = round(f1 / 1e6, 3)
    if out_dir is not None:
        from pathlib import Path
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = net.replace("/", "_").replace("\\", "_")
        csv = folder / f"{stem}_pdn_impedance.csv"
        with open(csv, "w", encoding="utf-8") as fh:
            fh.write("freq_hz,z_re_ohm,z_im_ohm,z_mag_ohm,z_lumped_mag_ohm,target_ohm\n")
            for i in range(len(freqs)):
                fh.write(f"{freqs[i]:.6g},{z[i].real:.6g},{z[i].imag:.6g},{zmag[i]:.6g},"
                         f"{abs(z_lumped[i] + zl[i]):.6g},{target:.6g}\n")
        files = [str(csv)]
        png = folder / f"{stem}_pdn_impedance.png"
        if _plot(png, freqs, zmag, target, z_lumped + zl if z_plane is not None else None,
                 f"{net} PDN impedance ({model})"):
            files.append(str(png))
        cir = folder / f"{stem}_pdn_lumped.cir"
        write_spice(cir, freqs, caps, (r_vrm, l_vrm) if vrm_used is not None else None,
                    c_pl, load_l, f"{net} PDN")
        files.append(str(cir))
        result["files"] = files
    return result
