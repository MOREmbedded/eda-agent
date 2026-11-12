# SPDX-License-Identifier: Apache-2.0
"""openEMS: build a field model of a few nets, run it, read the results.

The model is the copper of the chosen nets, the return copper around them
(planes, pours, ground tracks, pads and vias) inside a box drawn round
those nets, the real dielectric stack under it, and a lumped port on each
chosen pad, from the pad down (or up) to the nearest return copper.

Three steps, so a long solve never holds a tool call:

1. ``build_model`` writes ``model.json`` (geometry in mm, origin at the
   box centre) and copies the runner script beside it. Pure; no openEMS.
2. ``start_run`` starts the runner in openEMS's own Python as a separate
   process and returns at once. The runner excites each port in turn and
   writes ``status.json`` as it goes, then ``results.json`` (or
   ``error.json``).
3. ``collect_results`` turns ``results.json`` into a Touchstone file, a
   summary and, when matplotlib is installed, plots.

openEMS (GPL-3) is never imported here. The runner imports it in its own
interpreter; this side reads files.

Modelling choices, said once:

* Copper is a zero-thickness perfect conductor on its interface. Loss in
  the copper is left out; dielectric loss is in, from the loss tangent.
* A pour or plane polygon's holes are cut with a zero-width slit to the
  outer ring (a keyhole), since a CSXCAD polygon has no holes.
* Round ends and pad corners are hulls of 8 directions: good to a few
  per cent of a track width, which is below the mesh at any sane size.
* Nets other than the chosen ones and the return nets are left out. A
  neighbour that couples strongly (a diff-pair partner) should be named.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ..layout import geom
from ..layout.audit import return_nets
from ..layout.model import LayoutBoard
from .stack import Stack, assumed_stack

MM_PER_MIL = 0.0254
C0 = 299_792_458.0

RUNNER = Path(__file__).with_name("openems_runner.py")

#: Default model limits.
DEFAULT_MARGIN_MILS = 200.0
DEFAULT_MAX_CELLS = 20_000_000
#: Mesh: cells per wavelength in the densest dielectric, and growth ratio.
CELLS_PER_WAVELENGTH = 20
MESH_RATIO = 1.4


class ModelError(ValueError):
    """The model cannot be built as asked; the message says why."""


# ---------------------------------------------------------------------------
# Polygons
# ---------------------------------------------------------------------------

_DIRS8 = [(math.cos(math.radians(a)), math.sin(math.radians(a)))
          for a in range(0, 360, 45)]


def shape_polygon(s: geom.Shape) -> list[tuple[float, float]]:
    """A convex shape (pad, track piece) as a polygon: the hull of its core
    points grown by ``r`` in eight directions."""
    if s.kind == "poly" and s.r <= 0:
        return list(s.pts)
    r = s.r
    pts = [(x + r * dx, y + r * dy) for x, y in s.pts for dx, dy in _DIRS8] if r > 0 \
        else list(s.pts)
    return geom.convex_hull(pts)


def clip_rect(poly: list, x0: float, y0: float, x1: float, y1: float) -> list:
    """Sutherland-Hodgman clip of any polygon to an axis-aligned box."""
    def clip(points, inside, cross):
        out = []
        n = len(points)
        for i in range(n):
            cur, prev = points[i], points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(xv):
        return lambda p, q: (xv, p[1] + (q[1] - p[1]) * (xv - p[0]) / (q[0] - p[0]))

    def at_y(yv):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (yv - p[1]) / (q[1] - p[1]), yv)

    pts = [tuple(p) for p in poly]
    for inside, cross in ((lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
                          (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1))):
        if len(pts) < 3:
            return []
        pts = clip(pts, inside, cross)
    return pts if len(pts) >= 3 else []


def keyhole(outer: list, holes: list) -> list:
    """One simple ring for a polygon with holes: each hole joined to the
    ring by a zero-width slit from its nearest vertex."""
    ring = [tuple(p) for p in outer]
    for hole in holes:
        if len(hole) < 3:
            continue
        hole = [tuple(p) for p in hole]
        if _signed_area(hole) * _signed_area(ring) > 0:
            hole.reverse()  # opposite winding: a hole under either fill rule
        best = None
        for i, p in enumerate(ring):
            for j, q in enumerate(hole):
                d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, i, j)
        _, i, j = best
        loop = hole[j:] + hole[:j] + [hole[j]]
        ring = ring[:i + 1] + loop + [ring[i]] + ring[i + 1:]
    return ring


def _signed_area(pts) -> float:
    n = len(pts)
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
                     for i in range(n))


def _bbox(points) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def _find_pad(board: LayoutBoard, ref: str):
    comp, _, pin = ref.partition(".")
    if not pin:
        comp, _, pin = ref.partition("-")
    for p in board.pads:
        if p.comp == comp and str(p.name) == pin:
            return p
    raise ModelError(f"no pad {ref!r} on the board (write ports as DESIGNATOR.PIN, "
                     "e.g. J1.1)")


def _outer_layer(pad, stack: Stack) -> str:
    names = [l.name for l in stack.layers]
    layers = pad.layers()
    for name in (names[:1] + names[-1:]):
        if name in layers:
            return name
    for name in names:
        if name in layers:
            return name
    raise ModelError(f"pad {pad.comp}.{pad.name} has no copper on a stack layer")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def build_model(board: LayoutBoard, nets: list[str], ports: list[str],
                stack: Optional[Stack] = None, *, f_max_hz: float = 6e9,
                f_min_hz: float = 0.0, mode: str = "sparams",
                margin_mils: float = DEFAULT_MARGIN_MILS,
                area_mils: Optional[list[float]] = None,
                include_free_copper: Optional[bool] = None,
                port_ohms: float = 50.0, points: int = 401,
                max_cells: int = DEFAULT_MAX_CELLS,
                end_criteria_db: float = -40.0) -> dict:
    """The openEMS model of ``nets`` as a JSON-ready dict (mm, origin at
    the box centre). Raises ``ModelError`` when it cannot be built."""
    if mode not in ("sparams", "antenna"):
        raise ModelError("mode is 'sparams' or 'antenna'")
    if f_max_hz <= f_min_hz or f_max_hz <= 0:
        raise ModelError("f_max must be above f_min and above zero")
    stack = stack or assumed_stack(board.layers)
    if len(stack.layers) < 2:
        raise ModelError("the stack has fewer than two copper layers")
    nets = [n for n in dict.fromkeys(nets) if n]
    board_nets = set(board.nets())
    missing = [n for n in nets if n not in board_nets]
    if missing:
        raise ModelError(f"net(s) not on the board: {missing}")
    if not nets:
        raise ModelError("name at least one net")
    if not ports:
        ports = _default_ports(board, nets)
    if include_free_copper is None:
        include_free_copper = mode == "antenna"
    returns = return_nets(board) - set(nets)
    chosen = set(nets)
    z_of = {l.name: l.z_mils for l in stack.layers}

    # -- the box ------------------------------------------------------------
    if area_mils:
        if len(area_mils) != 4:
            raise ModelError("area_mils is [x1, y1, x2, y2]")
        bx0, by0 = min(area_mils[0], area_mils[2]), min(area_mils[1], area_mils[3])
        bx1, by1 = max(area_mils[0], area_mils[2]), max(area_mils[1], area_mils[3])
    else:
        pts = []
        for t in board.tracks:
            if t.net in chosen:
                pts += [(t.x1, t.y1), (t.x2, t.y2)]
        for a in board.arcs:
            if a.net in chosen:
                pts += [(a.cx - a.radius, a.cy - a.radius), (a.cx + a.radius, a.cy + a.radius)]
        for p in board.pads:
            if p.net in chosen:
                pts.append((p.x, p.y))
        for v in board.vias:
            if v.net in chosen:
                pts.append((v.x, v.y))
        for r in board.regions:
            if r.net in chosen and len(r.outline) >= 3:
                pts += list(r.outline)
        if not pts:
            raise ModelError(f"no copper on {nets}")
        bx0, by0, bx1, by1 = _bbox(pts)
        bx0, by0, bx1, by1 = bx0 - margin_mils, by0 - margin_mils, bx1 + margin_mils, by1 + margin_mils
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2

    def mm(x, y):
        return [round((x - cx) * MM_PER_MIL, 5), round((y - cy) * MM_PER_MIL, 5)]

    def zmm(layer):
        return round(z_of[layer] * MM_PER_MIL, 5)

    def in_box(x, y, pad=0.0):
        return bx0 - pad <= x <= bx1 + pad and by0 - pad <= y <= by1 + pad

    sheets: list[dict] = []          # {layer, z, pts, role}
    edges_x: set[float] = set()
    edges_y: set[float] = set()

    def add_sheet(layer, poly, role, net):
        if layer not in z_of:
            return
        clipped = clip_rect(poly, bx0, by0, bx1, by1)
        if len(clipped) < 3:
            return
        sheets.append({"layer": layer, "z": zmm(layer), "net": net, "role": role,
                       "pts": [mm(x, y) for x, y in clipped]})
        if role == "signal":
            x0, y0, x1, y1 = _bbox(clipped)
            for v in (x0, x1):
                edges_x.add(round((v - cx) * MM_PER_MIL, 4))
            for v in (y0, y1):
                edges_y.add(round((v - cy) * MM_PER_MIL, 4))

    def role_of(net):
        if net in chosen:
            return "signal"
        if net in returns:
            return "return"
        if not net and include_free_copper:
            return "free"
        return None

    for t in board.tracks:
        role = role_of(t.net)
        if role and not t.keepout and (in_box(t.x1, t.y1, t.width) or in_box(t.x2, t.y2, t.width)):
            add_sheet(t.layer, shape_polygon(t.shape()), role, t.net)
    for a in board.arcs:
        role = role_of(a.net)
        if role and not a.keepout and in_box(a.cx, a.cy, a.radius + a.width):
            pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, tol=1.0)
            for p, q in zip(pts, pts[1:]):
                add_sheet(a.layer, shape_polygon(geom.capsule(*p, *q, a.width)), role, a.net)
    for p in board.pads:
        role = role_of(p.net)
        if role and in_box(p.x, p.y, 200):
            for c in p.copper:
                s = p.shape_on(c.layer)
                if s is not None:
                    add_sheet(c.layer, shape_polygon(s), role, p.net)
    for r in board.regions:
        role = role_of(r.net)
        if not role or r.keepout or r.kind in ("cutout", "keepout") or len(r.outline) < 3:
            continue
        x0, y0, x1, y1 = _bbox(r.outline)
        if x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1:
            continue
        outer = clip_rect(r.outline, bx0, by0, bx1, by1)
        holes = [clip_rect(h, bx0, by0, bx1, by1) for h in r.holes]
        holes = [h for h in holes if len(h) >= 3 and _hole_inside(h, outer)]
        if len(outer) >= 3:
            add_sheet(r.layer, keyhole(outer, holes), role, r.net)
    solid_planes = []
    has_regions = {r.layer for r in board.regions if r.kind == "plane"}
    for layer in board.layers:
        if layer.kind == "plane" and layer.plane_net in returns and layer.name not in has_regions:
            outline = board.outline if len(board.outline) >= 3 else \
                [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
            add_sheet(layer.name, list(outline), "return", layer.plane_net)
            solid_planes.append(layer.name)

    vias = []
    for v in board.vias:
        role = role_of(v.net)
        if not role or not in_box(v.x, v.y):
            continue
        lo = v.low_layer if v.low_layer in z_of else stack.layers[-1].name
        hi = v.high_layer if v.high_layer in z_of else stack.layers[0].name
        if zmm(lo) == zmm(hi):
            continue
        vias.append({"xy": mm(v.x, v.y), "z1": min(zmm(lo), zmm(hi)),
                     "z2": max(zmm(lo), zmm(hi)),
                     "r": round(max(v.hole, 4.0) / 2 * MM_PER_MIL, 5), "net": v.net,
                     "role": role})

    # -- ports --------------------------------------------------------------
    port_rows = []
    ref_cover = _Cover(sheets)
    for k, name in enumerate(ports, start=1):
        pad = _find_pad(board, name)
        if pad.net not in chosen:
            raise ModelError(f"port {name} is on net {pad.net!r}, not one of {nets}")
        layer = _outer_layer(pad, stack)
        xy = mm(pad.x, pad.y)
        ref_layer = _port_reference(stack, layer, xy, ref_cover)
        if ref_layer is None:
            raise ModelError(
                f"port {name}: no return copper under or over the pad on any "
                "layer; pour ground under it or widen the box")
        s = pad.shape_on(layer)
        x0, y0, x1, y1 = s.bbox if s else (pad.x - 10, pad.y - 10, pad.x + 10, pad.y + 10)
        half = max(4.0, min(x1 - x0, y1 - y0) / 2)
        half_mm = round(half * MM_PER_MIL, 5)
        za, zb = zmm(ref_layer), zmm(layer)
        port_rows.append({"number": k, "name": name, "net": pad.net, "layer": layer,
                          "reference_layer": ref_layer, "ohms": port_ohms,
                          "start": [xy[0] - half_mm, xy[1] - half_mm, za],
                          "stop": [xy[0] + half_mm, xy[1] + half_mm, zb]})
        edges_x.update({round(xy[0] - half_mm, 4), round(xy[0] + half_mm, 4)})
        edges_y.update({round(xy[1] - half_mm, 4), round(xy[1] + half_mm, 4)})

    # -- dielectrics and the air round them ----------------------------------
    half_w, half_h = (bx1 - bx0) / 2 * MM_PER_MIL, (by1 - by0) / 2 * MM_PER_MIL
    er_max = max([d.er for d in stack.dielectrics] or [1.0])
    f_c = (f_max_hz + f_min_hz) / 2
    lam_max_mm = C0 / f_max_hz * 1e3
    res_mm = lam_max_mm / math.sqrt(er_max) / CELLS_PER_WAVELENGTH
    dielectrics = [{"z1": round(d.z_bottom_mils * MM_PER_MIL, 5),
                    "z2": round(d.z_top_mils * MM_PER_MIL, 5),
                    "er": d.er, "tan_d": d.tan_d,
                    "xy1": [-half_w, -half_h], "xy2": [half_w, half_h]}
                   for d in stack.dielectrics]
    thick = stack.thickness_mils * MM_PER_MIL
    if mode == "antenna":
        f_low = f_min_hz if f_min_hz > 0 else f_c / 2
        air = C0 / f_low * 1e3 / 4
    else:
        air = max(lam_max_mm / 8, 10 * thick if thick else 5.0)
    sim_box = {"x": [-half_w - (air if mode == "antenna" else 0), half_w + (air if mode == "antenna" else 0)],
               "y": [-half_h - (air if mode == "antenna" else 0), half_h + (air if mode == "antenna" else 0)],
               "z": [-air, thick + air]}
    z_lines = sorted({round(l.z_mils * MM_PER_MIL, 5) for l in stack.layers}
                     | {sim_box["z"][0], sim_box["z"][1]})

    cells = _cell_estimate(sim_box, res_mm, len(edges_x), len(edges_y), len(z_lines),
                           thick, er_max, lam_max_mm)
    if cells > max_cells:
        raise ModelError(
            f"about {cells / 1e6:.1f} M cells, over the {max_cells / 1e6:.0f} M limit: "
            "lower f_max, shrink the box (margin_mils / area_mils) or raise max_cells")

    return {
        "format": "eda-agent-openems/1",
        "board": board.name,
        "mode": mode,
        "nets": nets,
        "units": "mm",
        "origin_mils": [round(cx, 3), round(cy, 3)],
        "box_mils": [round(bx0, 3), round(by0, 3), round(bx1, 3), round(by1, 3)],
        "f_min_hz": f_min_hz, "f_max_hz": f_max_hz, "points": int(points),
        "excitation": {"f0_hz": f_c, "fc_hz": (f_max_hz - f_min_hz) / 2},
        "end_criteria": 10 ** (end_criteria_db / 10),
        "boundary": ["PML_8"] * 6 if mode == "antenna" else ["MUR"] * 6,
        "sim_box": sim_box,
        "mesh": {"res_mm": round(res_mm, 5), "ratio": MESH_RATIO,
                 "x": sorted(edges_x | set(sim_box["x"])),
                 "y": sorted(edges_y | set(sim_box["y"])),
                 "z": z_lines,
                 "z_res_mm": round(max(min([d["z2"] - d["z1"] for d in dielectrics] or [res_mm]) / 3,
                                       1e-3), 5)},
        "dielectrics": dielectrics,
        "sheets": sheets,
        "vias": vias,
        "ports": port_rows,
        "nf2ff": mode == "antenna",
        "estimate": {"cells": int(cells)},
        "stack": stack.to_dict(),
        "notes": ([f"plane layer(s) {solid_planes} modelled solid over the board outline"]
                  if solid_planes else []) + list(stack.notes),
    }


def _hole_inside(hole, outer) -> bool:
    x, y = sum(p[0] for p in hole) / len(hole), sum(p[1] for p in hole) / len(hole)
    return geom._inside_ring(x, y, outer)


class _Cover:
    """Return copper sheets per z, for finding a port's reference."""

    def __init__(self, sheets):
        self.by_z: dict[float, list] = {}
        for s in sheets:
            if s["role"] == "return":
                self.by_z.setdefault(s["z"], []).append(geom.polygon(s["pts"]))

    def covers(self, z, x, y) -> bool:
        return any(geom.point_in_poly(x, y, poly) for poly in self.by_z.get(z, ()))


def _port_reference(stack: Stack, layer: str, xy, cover: _Cover) -> Optional[str]:
    me = stack.get(layer)
    others = sorted((l for l in stack.layers if l.name != layer),
                    key=lambda l: abs(l.z_mils - me.z_mils))
    for l in others:
        if cover.covers(round(l.z_mils * MM_PER_MIL, 5), xy[0], xy[1]):
            return l.name
    return None


def _default_ports(board: LayoutBoard, nets: list[str]) -> list[str]:
    pads = [p for p in board.pads if p.net == nets[0] and p.comp]
    if not pads:
        raise ModelError(f"net {nets[0]} has no component pads; name the ports")
    if len(pads) > 4:
        raise ModelError(f"net {nets[0]} has {len(pads)} pads; name the ports "
                         "(DESIGNATOR.PIN), at most a few")
    return [f"{p.comp}.{p.name}" for p in pads]


def _cell_estimate(box, res, fixed_x, fixed_y, fixed_z, thick, er, lam) -> float:
    nx = (box["x"][1] - box["x"][0]) / res + fixed_x
    ny = (box["y"][1] - box["y"][0]) / res + fixed_y
    air_res = lam / CELLS_PER_WAVELENGTH
    nz = thick / max(res / 2, 1e-3) + (box["z"][1] - box["z"][0] - thick) / air_res + fixed_z
    return max(nx, 1) * max(ny, 1) * max(nz, 1)


# ---------------------------------------------------------------------------
# Writing and running
# ---------------------------------------------------------------------------

def default_runs_dir() -> Path:
    named = os.environ.get("EDA_AGENT_EM_RUNS", "")
    if named:
        return Path(named)
    return Path.home() / "EDA Agent" / "em"


def write_model(model: dict, runs_dir: Optional[str | Path] = None, name: str = "") -> Path:
    """Write ``model.json`` and the runner into a new folder; return it."""
    base = Path(runs_dir) if runs_dir else default_runs_dir()
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in (name or f"{Path(model.get('board') or 'board').stem}_"
                              f"{'_'.join(model['nets'])[:40]}"))
    folder = base / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}"
    k = 1
    while folder.exists():
        k += 1
        folder = base / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}_{k}"
    folder.mkdir(parents=True)
    (folder / "model.json").write_text(json.dumps(model, indent=1), encoding="utf-8")
    shutil.copyfile(RUNNER, folder / "openems_runner.py")
    return folder


def _python_can_import(python: str) -> bool:
    try:
        from ..noconsole import no_window
        proc = subprocess.run([python, "-c", "import openEMS, CSXCAD"],
                              capture_output=True, timeout=60, env=_run_env(), **no_window())
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_env() -> dict:
    env = dict(os.environ)
    home = os.environ.get("EDA_AGENT_OPENEMS_DIR", "")
    if home:
        env["PATH"] = home + os.pathsep + env.get("PATH", "")
        py = Path(home) / "python"
        if py.is_dir():
            env["PYTHONPATH"] = str(py) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def find_openems_python() -> dict:
    """The Python that can import openEMS: ``EDA_AGENT_OPENEMS_PYTHON``,
    else this interpreter, else ``python`` / ``python3`` on PATH."""
    named = os.environ.get("EDA_AGENT_OPENEMS_PYTHON", "")
    candidates = [named] if named else []
    candidates += [sys.executable, shutil.which("python") or "", shutil.which("python3") or ""]
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if _python_can_import(c):
            return {"available": True, "python": c,
                    "openems_dir": os.environ.get("EDA_AGENT_OPENEMS_DIR", "")}
    return {"available": False, "python": "",
            "tried": [c for c in seen],
            "how": "install openEMS (https://github.com/thliebig/openEMS-Project/releases; "
                   "Windows: unzip it and set EDA_AGENT_OPENEMS_DIR to that folder), "
                   "or point EDA_AGENT_OPENEMS_PYTHON at a Python that can "
                   "`import openEMS`"}


def start_run(folder: str | Path, python: str = "", threads: int = 0) -> dict:
    """Start the runner on ``folder`` as a detached process."""
    folder = Path(folder)
    if not (folder / "model.json").is_file():
        return {"ok": False, "error": f"no model.json in {folder}"}
    status = run_status(folder)
    if status["state"] == "running":
        return {"ok": False, "error": "a run is already going in this folder", **status}
    if not python:
        found = find_openems_python()
        if not found["available"]:
            return {"ok": False, "error": "openEMS not found", **found}
        python = found["python"]
    for stale in ("status.json", "results.json", "error.json"):
        (folder / stale).unlink(missing_ok=True)
    from ..noconsole import no_window
    log = open(folder / "run.log", "w", encoding="utf-8")
    cmd = [python, str(folder / "openems_runner.py"), str(folder / "model.json")]
    if threads:
        cmd += ["--threads", str(int(threads))]
    proc = subprocess.Popen(cmd, cwd=str(folder), stdout=log, stderr=subprocess.STDOUT,
                            env=_run_env(), **no_window())
    (folder / "run.json").write_text(json.dumps({"pid": proc.pid, "python": python,
                                                 "started": time.time(), "cmd": cmd}),
                                     encoding="utf-8")
    return {"ok": True, "folder": str(folder), "pid": proc.pid, "python": python}


def _alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001 - gone, or no access: treat as finished
        return False


def _read(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_status(folder: str | Path) -> dict:
    folder = Path(folder)
    if (folder / "results.json").is_file():
        return {"state": "done", "folder": str(folder)}
    err = _read(folder / "error.json")
    if err:
        return {"state": "failed", "folder": str(folder), **err}
    run = _read(folder / "run.json")
    if not run:
        return {"state": "not_started", "folder": str(folder)}
    status = _read(folder / "status.json") or {}
    if _alive(int(run.get("pid", 0))):
        return {"state": "running", "folder": str(folder),
                "elapsed_s": round(time.time() - run.get("started", time.time())), **status}
    tail = ""
    try:
        tail = (folder / "run.log").read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        pass
    return {"state": "failed", "folder": str(folder),
            "error": "the runner stopped without results", "log_tail": tail}


def collect_results(folder: str | Path, plot: bool = True) -> dict:
    """Touchstone, summary and plots from a finished run."""
    from .touchstone import summarize, write_touchstone

    folder = Path(folder)
    res = _read(folder / "results.json")
    if not res:
        return {"ok": False, **run_status(folder)}
    model = _read(folder / "model.json") or {}
    freqs = res["freq_hz"]
    n = len(res["ports"])
    s = [[[complex(*res["s"][i][j][k]) for j in range(n)] for i in range(n)]
         for k in range(len(freqs))]
    stem = folder / (folder.name)
    ts = write_touchstone(stem, freqs, s, z0=res["ports"][0]["ohms"],
                          comment=f"eda-agent openEMS run\nboard {model.get('board', '')}\n"
                                  f"nets {', '.join(model.get('nets', []))}\n"
                                  f"ports {', '.join(p['name'] for p in res['ports'])}")
    data = {"ports": n, "z0": res["ports"][0]["ohms"], "freq_hz": freqs, "s": s}
    out = {"ok": True, "folder": str(folder), "touchstone": str(ts),
           "ports": [{"number": p["number"], "name": p["name"]} for p in res["ports"]],
           "summary": summarize(data), "elapsed_s": res.get("elapsed_s")}
    if res.get("antenna"):
        out["antenna"] = res["antenna"]
    if plot:
        out.update(_plots(folder, data, res.get("antenna")))
    return out


def _plots(folder: Path, data: dict, antenna: Optional[dict]) -> dict:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {"plot_note": "install the render extra (matplotlib) for plots"}
    f = [x / 1e6 for x in data["freq_hz"]]
    n = data["ports"]
    fig, ax = plt.subplots(figsize=(7, 4))
    for i in range(n):
        for j in range(n):
            if i == j or j == 0:
                ax.plot(f, [20 * math.log10(max(abs(m[i][j]), 1e-15)) for m in data["s"]],
                        label=f"S{i + 1}{j + 1}")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("dB")
    ax.grid(True, alpha=0.3)
    ax.legend()
    path = folder / "sparams.png"
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    out = {"plot": str(path)}
    if antenna and antenna.get("pattern"):
        fig = plt.figure(figsize=(7, 3.6))
        for k, (cut, label) in enumerate((("phi0", "phi = 0"), ("phi90", "phi = 90"))):
            axp = fig.add_subplot(1, 2, k + 1, projection="polar")
            th = [math.radians(t) for t in antenna["pattern"]["theta_deg"]]
            axp.plot(th, antenna["pattern"][cut])
            axp.set_title(f"{label} (dBi)", fontsize=9)
            axp.set_theta_zero_location("N")
        fig.tight_layout()
        p2 = folder / "pattern.png"
        fig.savefig(p2, dpi=110)
        plt.close(fig)
        out["pattern_plot"] = str(p2)
    return out
