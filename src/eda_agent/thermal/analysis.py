# SPDX-License-Identifier: Apache-2.0
"""A board's thermal analysis, start to finish: grid, solve, report.

``analyze`` grids the board (``layout.copper_raster``), solves it
(``solver``), and turns the field into what a designer reads: the peak,
each layer's maximum, hotspots with the part beside them, every powered
part's pad, junction and case temperature against its limit, the hottest
unpowered parts (the electrolytic next to the regulator), the heat balance
and the convection coefficients that came out.

``refine=True`` solves again at half the cell size and reports how far
each number moved. A finite-volume solution on copper edges converges at
first order at worst, so the move between the two grids is also the
estimate of what is left in the finer one: ``estimated_error_k``. That is
the honest error bar of the discretisation; it says nothing about the
inputs (powers, theta values, the air's real movement), which usually
matter more.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..emc.stack import CopperLayer, Dielectric, Stack, assumed_stack
from ..layout.copper_raster import MAX_CELLS, CopperRaster, rasterize
from ..layout.model import BOTTOM, LayoutBoard
from .physics import MIL
from .solver import Clamp, HeatArea, Package, Result, Setup, match_powers, solve

#: Cells the automatic grid aims for: a second or two per solve.
AUTO_TARGET_CELLS = 40_000
#: Cell sizes the automatic choice picks from, mils.
NICE_CELLS = (5.0, 10.0, 12.5, 20.0, 25.0, 40.0, 50.0, 100.0, 200.0)
#: A hotspot is a local maximum at least this share of the peak rise.
HOTSPOT_MIN_SHARE = 0.25
#: Hotspots are kept this far apart, mils (5 mm).
HOTSPOT_WINDOW_MILS = 200.0


@dataclass
class HeatMap:
    """Heat per cell on a grid of its own, W: the Joule heating an IR-drop
    solution found (an .npz with one ``W_<layer>`` array per copper layer
    and ``x0``, ``y0``, ``cell_mils``). ``on`` moves it to the thermal grid, keeping every watt."""

    x0: float
    y0: float
    cell_mils: float
    layers: dict[str, np.ndarray]            # copper layer -> (ny, nx) watts

    @classmethod
    def load(cls, path: str | Path) -> "HeatMap":
        """From an .npz with ``x0``, ``y0``, ``cell_mils`` and one
        ``W_<layer>`` array per copper layer."""
        with np.load(Path(path), allow_pickle=False) as z:
            missing = [k for k in ("x0", "y0", "cell_mils") if k not in z.files]
            if missing:
                raise ValueError(f"{path}: no {', '.join(missing)} in the file")
            layers = {k[2:]: np.nan_to_num(np.asarray(z[k], dtype=float))
                      for k in z.files if k.startswith("W_")}
            if not layers:
                raise ValueError(f"{path}: no W_<layer> heat arrays in the file")
            return cls(float(z["x0"]), float(z["y0"]), float(z["cell_mils"]), layers)

    @property
    def power_w(self) -> float:
        return float(sum(a.sum() for a in self.layers.values()))

    def on(self, raster: CopperRaster) -> tuple[dict[str, np.ndarray], float]:
        """({layer: (ny, nx) watts on ``raster``}, watts that fell outside
        its grid). Each source cell's heat is shared among the cells it
        overlaps, by area."""
        out: dict[str, np.ndarray] = {}
        lost = 0.0
        for name, w in self.layers.items():
            ny, nx = w.shape
            # overlap of every source column with every target column, as
            # a share of the source cell; the same for rows
            sx = _overlap(self.x0, self.cell_mils, nx, raster.x0, raster.cell_mils, raster.nx)
            sy = _overlap(self.y0, self.cell_mils, ny, raster.y0, raster.cell_mils, raster.ny)
            moved = sy.T @ w @ sx
            lost += float(w.sum() - moved.sum())
            out[name] = moved
        return out, lost


def _overlap(a0: float, da: float, na: int, b0: float, db: float, nb: int) -> np.ndarray:
    """(na, nb): the share of source interval i that lies in target
    interval j, for two regular 1D grids."""
    a_lo = a0 + np.arange(na) * da
    b_lo = b0 + np.arange(nb) * db
    lo = np.maximum(a_lo[:, None], b_lo[None, :])
    hi = np.minimum(a_lo[:, None] + da, b_lo[None, :] + db)
    return np.clip(hi - lo, 0.0, None) / da


#: Copper thickness taken when the stackup gives none, mils (1 oz).
DEFAULT_COPPER_MILS = 1.4


def usable_stack(stack: Stack) -> Stack:
    """``stack`` with what a thermal model cannot do without filled in.

    A board whose layer stack was never set up in the PCB editor reports
    copper layers with no dielectric between them, or no copper
    thickness. Solved as read, it would be a board a few mils thick. Each
    missing dielectric takes the height the same gap has in an evenly
    built 1.6 mm board, each missing copper thickness 1 oz, and the notes
    say which."""
    n = len(stack.layers)
    gaps = [stack.layers[i].z_mils - stack.layers[i + 1].z_mils for i in range(n - 1)]
    thin = [l.name for l in stack.layers if l.copper_mils <= 0]
    if all(g > 0 for g in gaps) and not thin:
        return stack
    from types import SimpleNamespace
    ref = assumed_stack([SimpleNamespace(name=l.name, order=i, kind=l.kind)
                         for i, l in enumerate(stack.layers)])
    notes = [x for x in stack.notes if "no dielectric beneath" not in x]
    missing = []
    for i, g in enumerate(gaps):
        if g <= 0:
            gaps[i] = ref.layers[i].z_mils - ref.layers[i + 1].z_mils
            missing.append(f"{stack.layers[i].name}/{stack.layers[i + 1].name} "
                           f"{gaps[i]:g} mil")
    if missing:
        notes.append("the stackup gives no dielectric between " + ", ".join(missing[:8])
                     + (" ..." if len(missing) > 8 else "")
                     + " (assumed, from an evenly built 1.6 mm board). Set the layer "
                       "stack in the PCB editor for real numbers.")
    if thin:
        notes.append(f"no copper thickness for {', '.join(thin[:8])}: "
                     f"{DEFAULT_COPPER_MILS:g} mil (1 oz) assumed")
    out = Stack(assumed=stack.assumed or (bool(missing) and len(missing) == n - 1), notes=notes)
    z = 0.0
    layers, diel = [], []
    for i in range(n - 1, -1, -1):
        src = stack.layers[i]
        if i < n - 1:
            old = next((d for d in stack.dielectrics
                        if d.z_bottom_mils >= stack.layers[i + 1].z_mils - 1e-9
                        and d.z_top_mils <= src.z_mils + 1e-9 and d.height_mils > 0), None)
            diel.append(Dielectric(z, z + gaps[i], *((old.er, old.tan_d, old.kind) if old
                                                      else ())))
            z += gaps[i]
        layers.append(CopperLayer(src.name, src.label, z,
                                  src.copper_mils if src.copper_mils > 0 else DEFAULT_COPPER_MILS,
                                  src.kind))
    out.layers = layers[::-1]
    out.dielectrics = diel[::-1]
    return out


def board_area_mils2(board: LayoutBoard) -> float:
    from ..layout import geom
    if len(board.outline) >= 3:
        a = abs(geom.ring_area(board.outline))
        a -= sum(abs(geom.ring_area(c)) for c in board.cutouts if len(c) >= 3)
        return max(a, 0.0)
    return 0.0


def auto_cell_mils(board: LayoutBoard, target_cells: int = AUTO_TARGET_CELLS) -> float:
    """The smallest nice cell size that keeps the grid near ``target_cells``."""
    area = board_area_mils2(board)
    if area <= 0:
        return 25.0
    want = math.sqrt(area / max(target_cells, 1))
    for c in NICE_CELLS:
        if c >= want * 0.999:
            return c
    return NICE_CELLS[-1]


def parse_packages(raw: Optional[dict]) -> dict[str, Package]:
    """{ref: {theta_jb, theta_jc, tj_max_c, heatsink_theta_sa}} to Packages.
    ``psi_jb`` is accepted for ``theta_jb``."""
    out: dict[str, Package] = {}
    allowed = {"theta_jb", "psi_jb", "theta_jc", "tj_max_c", "heatsink_theta_sa"}
    for ref, row in (raw or {}).items():
        if not isinstance(row, dict):
            raise ValueError(f"packages[{ref!r}] must be an object")
        unknown = set(row) - allowed
        if unknown:
            raise ValueError(f"packages[{ref!r}]: unknown keys {sorted(unknown)}; "
                             f"use {sorted(allowed)}")
        vals = {}
        for k, v in row.items():
            if v is None:
                continue
            try:
                vals[k] = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"packages[{ref!r}].{k} must be a number") from None
        if "psi_jb" in vals:
            vals.setdefault("theta_jb", vals.pop("psi_jb"))
            vals.pop("psi_jb", None)
        out[ref] = Package(**vals)
    return out


def _outline_distance(points: list, x: float, y: float) -> float:
    """Distance from (x, y) to a closed outline; 0 inside it."""
    n = len(points)
    inside = False
    best = math.inf
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
        dx, dy = x1 - x0, y1 - y0
        q = dx * dx + dy * dy
        u = 0.0 if q <= 0 else min(1.0, max(0.0, ((x - x0) * dx + (y - y0) * dy) / q))
        best = min(best, math.hypot(x - x0 - u * dx, y - y0 - u * dy))
    return 0.0 if inside else best


def _nearest_component(board: LayoutBoard, x: float, y: float, side: str
                       ) -> tuple[str, float]:
    """The part on ``side`` whose outline is nearest to (x, y), and how
    far its outline is (0 inside it). A part with no outline counts by
    its centre; among parts that hold the point the nearest centre wins."""
    best, best_key = "", (math.inf, math.inf)
    for c in board.components:
        if c.side != side:
            continue
        centre = math.hypot(c.x - x, c.y - y)
        d = _outline_distance(c.courtyard, x, y) if len(c.courtyard) >= 3 else centre
        if (d, centre) < best_key:
            best, best_key = c.ref, (d, centre)
    return best, (best_key[0] if best else 0.0)


def find_hotspots(result: Result, board: LayoutBoard, limit: int = 10) -> list[dict]:
    """Local maxima of the two outer layers, hottest first."""
    from scipy.ndimage import maximum_filter

    raster = result.raster
    rise_max = result.t_max_c - result.ambient_c
    if rise_max <= 0:
        return []
    win = max(3, int(round(HOTSPOT_WINDOW_MILS / raster.cell_mils)) | 1)
    out = []
    outer = [raster.layers[0]] + ([raster.layers[-1]] if len(raster.layers) > 1 else [])
    for layer, side in zip(outer, ("top", BOTTOM)):
        t = result.temps[layer]
        filled = np.where(np.isnan(t), -np.inf, t)
        peaks = (filled == maximum_filter(filled, size=win, mode="constant", cval=-np.inf))
        peaks &= np.isfinite(filled)
        peaks &= (filled - result.ambient_c) >= HOTSPOT_MIN_SHARE * rise_max
        for iy, ix in zip(*np.nonzero(peaks)):
            x, y = raster.centre(int(ix), int(iy))
            ref, dist = _nearest_component(board, x, y, side)
            out.append({"layer": layer, "x_mils": round(x, 1), "y_mils": round(y, 1),
                        "t_c": round(float(t[iy, ix]), 2),
                        "rise_k": round(float(t[iy, ix]) - result.ambient_c, 2),
                        "nearest": ref, "distance_mils": round(dist, 1)})
    out.sort(key=lambda h: -h["t_c"])
    return out[:limit]


def component_temps(result: Result, board: LayoutBoard) -> list[dict]:
    """Every component's board temperature under its pads (mean of the
    cells at its pad centres on its own side), hottest first."""
    raster = result.raster
    top, bot = raster.layers[0], raster.layers[-1]
    acc: dict[str, list[float]] = {}
    side = {c.ref: c.side for c in board.components}
    for p in board.pads:
        if not p.comp or p.comp not in side:
            continue
        layer = bot if side[p.comp] == BOTTOM else top
        t = result.copper_at(layer, p.x, p.y)
        if np.isfinite(t):
            acc.setdefault(p.comp, []).append(float(t))
    for c in board.components:
        if c.ref not in acc:
            layer = bot if c.side == BOTTOM else top
            ix, iy = raster.cell_of(c.x, c.y)
            t = result.temps[layer][iy, ix]
            if np.isfinite(t):
                acc[c.ref] = [float(t)]
    rows = [{"ref": ref, "side": side[ref], "t_c": round(sum(v) / len(v), 2),
             "t_max_c": round(max(v), 2)} for ref, v in acc.items()]
    rows.sort(key=lambda r: -r["t_c"])
    return rows


def _layer_rows(result: Result) -> list[dict]:
    rows = []
    raster = result.raster
    for name in raster.layers:
        t = result.temps[name]
        iy, ix = np.unravel_index(np.nanargmax(t), t.shape)
        x, y = raster.centre(int(ix), int(iy))
        rows.append({"name": name, "t_max_c": round(float(np.nanmax(t)), 2),
                     "t_mean_c": round(float(np.nanmean(t)), 2),
                     "t_min_c": round(float(np.nanmin(t)), 2),
                     "max_at_mils": [round(x, 1), round(y, 1)],
                     "copper_fraction": round(float(raster.fill[name].sum()
                                                    / max(raster.mask.sum(), 1e-9)), 4)})
    return rows


def _solve_once(board, stack, cell_mils, powers, packages, setup, heat_areas, clamps,
                extra_heat_w, report_parts, heat_maps=None) -> Result:
    t0 = time.perf_counter()
    raster = rasterize(board, cell_mils=cell_mils, links=True, supersample=0, islands=True)
    raster_s = time.perf_counter() - t0
    extra = {k: np.asarray(v, dtype=float) for k, v in (extra_heat_w or {}).items()}
    notes = []
    for hm in heat_maps or []:
        moved, lost = hm.on(raster)
        for layer, arr in moved.items():
            if layer not in raster.layers:
                raise ValueError(f"heat map layer {layer!r} is not a copper layer of the board")
            extra[layer] = extra[layer] + arr if layer in extra else arr
        if lost > 1e-9:
            notes.append(f"{lost:.3g} W of the heat map lies outside the board's grid "
                         f"and is dropped")
    t0 = time.perf_counter()
    result = solve(board, stack, raster, powers=powers, packages=packages, setup=setup,
                   extra_heat_w=extra or None, heat_areas=heat_areas, clamps=clamps,
                   report_parts=report_parts)
    result.solver.update(
        supersample=raster.supersample,
        sample_mils=round(raster.cell_mils / raster.supersample, 3),
        raster_s=round(raster_s, 2), solve_s=round(time.perf_counter() - t0, 2))
    result.notes += notes
    return result


def analyze(board: LayoutBoard, stack: Stack,
            powers: Optional[dict[str, float]] = None,
            packages: Optional[dict[str, Package]] = None,
            setup: Optional[Setup] = None,
            cell_mils: float = 0.0,
            refine: bool = False,
            heat_areas: Optional[list[HeatArea]] = None,
            clamps: Optional[list[Clamp]] = None,
            extra_heat_w: Optional[dict[str, np.ndarray]] = None,
            limit_c: Optional[float] = None,
            top_n: int = 15,
            heat_maps: Optional[list[HeatMap]] = None) -> tuple[dict[str, Any], Result]:
    """Run the analysis; return (report, Result of the finest grid).

    ``cell_mils`` 0 picks a grid of about 40,000 cells. ``refine`` solves
    again at half the cell and adds the convergence table. ``limit_c``
    flags every component whose board temperature is above it.
    ``heat_maps`` add heat found on another grid (IR-drop Joule heating).
    ``extra_heat_w`` is heat already on this grid; it is tied to it, so
    it cannot be refined.
    """
    setup = setup or Setup()
    setup.check()
    stack = usable_stack(stack)
    packages = packages or {}
    pw = match_powers(board, powers or {})
    total = sum(pw.values()) + sum(h.power_w for h in heat_areas or []) \
        + sum(float(np.sum(a)) for a in (extra_heat_w or {}).values()) \
        + sum(hm.power_w for hm in heat_maps or [])
    if total <= 0 and not clamps:
        raise ValueError("nothing dissipates: give powers ({designator: watts}), "
                         "heat_areas or a heat map")
    auto = cell_mils <= 0
    cell = auto_cell_mils(board) if auto else float(cell_mils)
    if refine and extra_heat_w:
        raise ValueError("refine cannot be used with extra_heat_w (it is tied to its grid)")
    report_parts = sorted(set(pw) | set(packages))
    args = (pw, packages, setup, heat_areas, clamps)
    result = _solve_once(board, stack, cell, *args, extra_heat_w, report_parts, heat_maps)
    notes = list(result.notes)

    refinement = None
    if refine:
        area = board_area_mils2(board)
        if area and area / (cell / 2) ** 2 > MAX_CELLS:
            notes.append(f"refine skipped: a {cell / 2:g} mil grid is over {MAX_CELLS} cells")
        else:
            fine = _solve_once(board, stack, cell / 2, *args, None, report_parts, heat_maps)
            refinement = _refinement(result, fine, cell)
            notes = list(fine.notes)
            result = fine
            cell = cell / 2

    comps = component_temps(result, board)
    powered = {p["ref"] for p in result.parts if p["power_w"] > 0}
    parts = sorted(result.parts, key=lambda p: -(p.get("t_junction_c") or p["t_pads_c"]))
    over = [p["ref"] for p in parts if p.get("margin_k") is not None and p["margin_k"] < 0]
    hot_board = []
    if limit_c is not None:
        hot_board = [c["ref"] for c in comps if c["t_c"] > limit_c]
    judged = limit_c is not None or any(p.get("margin_k") is not None for p in parts)
    if not judged:
        notes.append("no verdict: nothing to judge against. Give tj_max_c for the parts "
                     "(packages) or limit_c for the board")
    layers = _layer_rows(result)
    peak = max(layers, key=lambda r: r["t_max_c"])
    report: dict[str, Any] = {
        "board": board.name,
        "ambient_c": setup.ambient_c,
        "power_w": result.balance["power_in_w"],
        "t_max_c": peak["t_max_c"],
        "t_max_layer": peak["name"],
        "t_max_at_mils": peak["max_at_mils"],
        "rise_max_k": round(peak["t_max_c"] - setup.ambient_c, 2),
        "pass": (not over and not hot_board) if judged else None,
        "over_tj_max": over,
        "layers": layers,
        "hotspots": find_hotspots(result, board, limit=top_n),
        "parts": parts,
        "hottest_components": [c for c in comps if c["ref"] not in powered][:top_n],
        "balance": result.balance,
        "convection": result.h_used,
        "grid": {"cell_mils": cell, "auto": auto, "nx": result.raster.nx,
                 "ny": result.raster.ny, **result.solver},
        "setup": {k: v for k, v in asdict(setup).items() if v is not None},
        "stackup": {"assumed": stack.assumed,
                    "thickness_mils": round(sum(pl.t_m for pl in result.planes) / MIL, 2),
                    "dielectric_mils": round(stack.thickness_mils, 2),
                    "layers": len(stack.layers)},
        "notes": notes + list(stack.notes),
    }
    if limit_c is not None:
        report["limit_c"] = limit_c
        report["over_limit"] = hot_board[:50]
    if refinement is not None:
        report["refinement"] = refinement
    if not any(p.theta_jb for p in packages.values()) and pw:
        report["notes"].append(
            "no theta_jb given: temperatures are the board's under each part, not "
            "junctions. Pass packages={ref: {theta_jb, tj_max_c}} from the datasheet.")
    return report, result


def _refinement(coarse: Result, fine: Result, cell: float) -> dict:
    """How far the numbers moved between the two grids."""
    by_ref = {p["ref"]: p for p in coarse.parts}
    rows = []
    worst = 0.0
    for p in fine.parts:
        c = by_ref.get(p["ref"])
        if c is None:
            continue
        key = "t_junction_c" if p.get("t_junction_c") is not None \
            and c.get("t_junction_c") is not None else "t_pads_c"
        d = p[key] - c[key]
        worst = max(worst, abs(d))
        rows.append({"ref": p["ref"], "quantity": key, "coarse_c": c[key], "fine_c": p[key],
                     "delta_k": round(d, 3), "extrapolated_c": round(p[key] + d, 2)})
    d_max = fine.t_max_c - coarse.t_max_c
    worst = max(worst, abs(d_max))
    rows.sort(key=lambda r: -abs(r["delta_k"]))
    return {
        "cells_mils": [cell, cell / 2],
        "t_max_c": {"coarse_c": round(coarse.t_max_c, 2), "fine_c": round(fine.t_max_c, 2),
                    "delta_k": round(d_max, 3),
                    "extrapolated_c": round(fine.t_max_c + d_max, 2)},
        "parts": rows,
        "estimated_error_k": round(worst, 2),
        "method": "two grids, cell and cell/2; first-order convergence assumed, so the "
                  "move between them estimates the error left in the finer one",
    }
