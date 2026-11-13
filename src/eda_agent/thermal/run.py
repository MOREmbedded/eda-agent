# SPDX-License-Identifier: Apache-2.0
"""One thermal run, from plain inputs to a report and files.

What the MCP tool (``tools.thermal``) calls. Everything here takes and
returns plain JSON-shaped values, so a run can be described in a file,
repeated, and tested without Altium.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Optional

from ..emc.stack import Stack
from ..layout.model import LayoutBoard
from . import physics as ph
from .analysis import HeatMap, analyze, parse_packages
from .solver import Clamp, HeatArea, Result, Setup

#: ``Setup`` fields a caller may set through ``options``.
OPTION_KEYS = ("up_deg", "convection", "emissivity", "h_top", "h_bottom", "radiation",
               "edges", "dielectric", "k_xy", "k_z", "solder_mask", "max_sublayer_mils",
               "plating_um", "via_fill", "pressure_pa", "tol_k", "max_iter")
#: Keys of an inputs file.
INPUT_KEYS = ("powers", "packages", "heat_areas", "clamps", "options")


def runs_dir(named: str = "") -> Path:
    """Where run folders go: ``named``, else ``EDA_AGENT_THERMAL_RUNS``,
    else ``~/EDA Agent/thermal``."""
    base = named or os.environ.get("EDA_AGENT_THERMAL_RUNS", "")
    return Path(base) if base else Path.home() / "EDA Agent" / "thermal"


def run_folder(base: Path, board_name: str) -> Path:
    stem = re.sub(r"[^A-Za-z0-9_.+-]+", "_", Path(board_name or "board").stem) or "board"
    folder = base / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}"
    k = 1
    while folder.exists():
        k += 1
        folder = base / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}_{k}"
    return folder


def load_inputs(path: str | Path) -> dict:
    """An inputs file: a JSON object with any of ``powers``, ``packages``,
    ``heat_areas``, ``clamps`` and ``options``."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from None
    except ValueError as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must hold a JSON object")
    unknown = set(raw) - set(INPUT_KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}; use {list(INPUT_KEYS)}")
    return raw


def make_setup(ambient_c: float = 25.0, orientation: str = "horizontal",
               airflow_ms: float = 0.0, airflow_deg: float = 0.0,
               altitude_m: float = 0.0, options: Optional[dict] = None) -> Setup:
    """A ``Setup`` from the tool's arguments; ``options`` sets the rest."""
    opts = dict(options or {})
    unknown = set(opts) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(f"unknown options {sorted(unknown)}; use {list(OPTION_KEYS)}")
    if altitude_m and "pressure_pa" in opts:
        raise ValueError("give altitude_m or options.pressure_pa, not both")
    kinds = {f.name: f.type for f in fields(Setup)}
    clean: dict[str, Any] = {}
    for key, value in opts.items():
        if value is None:
            continue
        if key in ("convection", "dielectric", "via_fill"):
            clean[key] = str(value)
        elif key in ("radiation", "edges", "solder_mask"):
            if not isinstance(value, bool):
                raise ValueError(f"options.{key} must be true or false")
            clean[key] = value
        elif key == "max_iter":
            clean[key] = int(value)
        else:
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"options.{key} must be a number") from None
        assert key in kinds
    if altitude_m:
        clean["pressure_pa"] = ph.pressure_at_altitude(float(altitude_m))
    setup = Setup(ambient_c=float(ambient_c), orientation=str(orientation),
                  airflow_ms=max(0.0, float(airflow_ms)), airflow_deg=float(airflow_deg),
                  **clean)
    setup.check()
    return setup


def _rect(row: Any, what: str, need: tuple[str, ...]) -> dict:
    if not isinstance(row, dict):
        raise ValueError(f"each of {what} must be an object")
    missing = [k for k in need if k not in row]
    if missing:
        raise ValueError(f"{what}: missing {missing}; each needs {list(need)}")
    return row


def parse_heat_areas(raw: Optional[list]) -> list[HeatArea]:
    """[{layer, x0, y0, x1, y1, power_w}] (mils) to HeatAreas."""
    out = []
    for row in raw or []:
        r = _rect(row, "heat_areas", ("layer", "x0", "y0", "x1", "y1", "power_w"))
        if float(r["power_w"]) < 0:
            raise ValueError("heat_areas: power_w must be >= 0")
        out.append(HeatArea(str(r["layer"]), float(r["x0"]), float(r["y0"]),
                            float(r["x1"]), float(r["y1"]), float(r["power_w"])))
    return out


def parse_clamps(raw: Optional[list]) -> list[Clamp]:
    """[{x0, y0, x1, y1, temp_c, side, h_contact}] (mils) to Clamps."""
    out = []
    for row in raw or []:
        r = _rect(row, "clamps", ("x0", "y0", "x1", "y1", "temp_c"))
        kw = {}
        if r.get("h_contact") is not None:
            kw["h_contact"] = float(r["h_contact"])
            if kw["h_contact"] <= 0:
                raise ValueError("clamps: h_contact must be > 0")
        out.append(Clamp(float(r["x0"]), float(r["y0"]), float(r["x1"]), float(r["y1"]),
                         float(r["temp_c"]), side=str(r.get("side", "bottom")), **kw))
    return out


def run(board: LayoutBoard, stack: Stack, *,
        powers: Optional[dict] = None, packages: Optional[dict] = None,
        heat_areas: Optional[list] = None, clamps: Optional[list] = None,
        heat_map_npz: Optional[list[str]] = None, inputs_json_path: str = "",
        ambient_c: float = 25.0, orientation: str = "horizontal",
        airflow_ms: float = 0.0, airflow_deg: float = 0.0, altitude_m: float = 0.0,
        options: Optional[dict] = None, cell_mils: float = 0.0, refine: bool = False,
        limit_c: float = 0.0, top_n: int = 15) -> tuple[dict, Result, dict]:
    """Solve; return (report, Result, the inputs as used).

    Arguments given here win over the same key in ``inputs_json_path``;
    ``powers`` and ``packages`` are merged per designator.
    """
    filed = load_inputs(inputs_json_path) if inputs_json_path else {}
    pw = {**(filed.get("powers") or {}), **(powers or {})}
    pk = {**(filed.get("packages") or {}), **(packages or {})}
    ha = heat_areas if heat_areas is not None else filed.get("heat_areas")
    cl = clamps if clamps is not None else filed.get("clamps")
    opts = {**(filed.get("options") or {}), **(options or {})}
    for name, value in (("powers", pw), ("packages", pk), ("options", opts)):
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
    try:
        pw = {str(k): float(v) for k, v in pw.items()}
    except (TypeError, ValueError):
        raise ValueError("powers must map designators to watts") from None
    setup = make_setup(ambient_c, orientation, airflow_ms, airflow_deg, altitude_m, opts)
    maps = [HeatMap.load(p) for p in heat_map_npz or []]
    report, result = analyze(
        board, stack, powers=pw, packages=parse_packages(pk), setup=setup,
        cell_mils=float(cell_mils), refine=bool(refine),
        heat_areas=parse_heat_areas(ha), clamps=parse_clamps(cl),
        limit_c=float(limit_c) if limit_c else None, top_n=max(1, int(top_n)),
        heat_maps=maps or None)
    used = {"powers": pw, "packages": pk, "heat_areas": ha or [], "clamps": cl or [],
            "heat_map_npz": list(heat_map_npz or []), "ambient_c": setup.ambient_c,
            "orientation": setup.orientation, "airflow_ms": setup.airflow_ms,
            "airflow_deg": setup.airflow_deg, "altitude_m": float(altitude_m),
            "options": opts, "cell_mils": report["grid"]["cell_mils"], "refine": bool(refine),
            "limit_c": float(limit_c)}
    return report, result, used


def write_files(report: dict, result: Result, board: LayoutBoard, folder: str | Path,
                used: Optional[dict] = None) -> list[str]:
    """Heat maps (one PNG per copper layer, one scale), the copper as
    solved (one PNG per copper layer), ``result.npz`` (the fields),
    ``report.json`` and ``inputs.json`` into ``folder``."""
    from .render import render_all, render_copper_all, save_npz

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    files = list(render_all(result, board, folder).values())
    files += list(render_copper_all(result, board, folder).values())
    files.append(str(save_npz(result, folder / "result.npz")))
    (folder / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    files.append(str(folder / "report.json"))
    if used is not None:
        (folder / "inputs.json").write_text(json.dumps(used, indent=1), encoding="utf-8")
        files.append(str(folder / "inputs.json"))
    return files
