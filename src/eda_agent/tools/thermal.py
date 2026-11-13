# SPDX-License-Identifier: Apache-2.0
"""Board thermal analysis tool (``thermal`` package). An offline solver:
it reads the board and changes nothing on it."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .emc import _load_board

__all__ = ["register_thermal_tools"]


def register_thermal_tools(mcp) -> None:

    @mcp.tool()
    async def sim_thermal_analysis(
        powers: Optional[dict] = None,
        packages: Optional[dict] = None,
        expect_file: str = "",
        board_json_path: str = "",
        stackup_json_path: str = "",
        ambient_c: float = 25.0,
        orientation: str = "horizontal",
        airflow_ms: float = 0.0,
        airflow_deg: float = 0.0,
        altitude_m: float = 0.0,
        cell_mils: float = 0.0,
        refine: bool = False,
        limit_c: float = 0.0,
        heat_areas: Optional[list[dict]] = None,
        clamps: Optional[list[dict]] = None,
        heat_map_npz: Optional[list[str]] = None,
        inputs_json_path: str = "",
        options: Optional[dict] = None,
        write_maps: bool = True,
        runs_dir: str = "",
        limit: int = 15,
    ) -> dict[str, Any]:
        """Steady-state temperatures of a board from the power its parts
        dissipate. Offline, changes nothing, needs no external program.

        Solves 3D heat conduction in the real copper (pours, planes with
        their antipads, tracks, pads), the laminate and every plated hole,
        with natural or forced convection and radiation from both faces,
        the edges and part cases. Reports the peak, each layer's maximum,
        hotspots with the nearest part, every powered part's pad, junction
        and case temperature against ``tj_max_c``, the hottest unpowered
        parts, the heat balance, and (``refine``) a grid-convergence error
        estimate. Writes a heat map per layer.

        Reads the whole board: on a very large board ask the user first.

        Args:
            powers: {designator or glob: watts}, e.g. {"U1": 1.2,
                "R1*": 0.1}. Exact names win over globs.
            packages: {designator: {theta_jb (or psi_jb), theta_jc,
                tj_max_c, heatsink_theta_sa}} from the datasheet, K/W.
                Without theta_jb a part reports its board temperature
                only, not its junction.
            expect_file: full path of the focused .PcbDoc, for a live
                read (the real stackup is read too).
            board_json_path: a saved LayoutBoard instead.
            stackup_json_path: a saved pcb_get_layer_stackup reply; with
                neither, a 1.6 mm FR-4 stack is assumed and said so.
            ambient_c: air and surroundings, deg C.
            orientation: horizontal (top side up), inverted, vertical.
            airflow_ms: forced air along the board, m/s (0 = still air).
            airflow_deg: direction the air moves towards, degrees
                counter-clockwise from +x.
            altitude_m: thins the air (standard atmosphere).
            cell_mils: grid cell; 0 picks about 40,000 cells.
            refine: solve again at half the cell and report how far each
                temperature moved (the discretisation error estimate).
            limit_c: flag every part whose board temperature is above it.
            heat_areas: [{layer, x0, y0, x1, y1, power_w}] in mils, heat
                made in the copper of a layer inside a rectangle (in the
                bare laminate when under 1 % of it is copper).
            clamps: [{x0, y0, x1, y1, temp_c, side, h_contact}] a
                rectangle of a face held to a chassis at temp_c.
            heat_map_npz: .npz files of an IR-drop run; their Joule heat
                (W_<layer>, watts per cell, with x0, y0, cell_mils) is
                added.
            inputs_json_path: a JSON file with powers, packages,
                heat_areas, clamps, options; arguments here win.
            options: emissivity, dielectric (fr4, high_tg_fr4, polyimide,
                rogers_4350b, rogers_4003c, ptfe, imsl), k_xy, k_z,
                via_fill (none, epoxy, conductive, silver, copper),
                plating_um, solder_mask, radiation, edges, convection
                (local, mean), h_top, h_bottom (fixed W/m2K), up_deg,
                pressure_pa, max_sublayer_mils, tol_k, max_iter.
            write_maps: write the PNG heat maps, result.npz, report.json
                and inputs.json to a new run folder.
            runs_dir: where the run folder goes (EDA_AGENT_THERMAL_RUNS,
                else ~/EDA Agent/thermal).
            limit: rows of hotspots and unpowered parts to return.
        """
        from ..thermal import run as tr

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        try:
            report, result, used = await asyncio.to_thread(
                tr.run, board, stack, powers=powers, packages=packages,
                heat_areas=heat_areas, clamps=clamps, heat_map_npz=heat_map_npz,
                inputs_json_path=inputs_json_path, ambient_c=ambient_c,
                orientation=orientation, airflow_ms=airflow_ms, airflow_deg=airflow_deg,
                altitude_m=altitude_m, options=options, cell_mils=cell_mils,
                refine=refine, limit_c=limit_c, top_n=limit)
        except (ValueError, MemoryError) as exc:
            return {"ok": False, "error": str(exc) or type(exc).__name__}
        out: dict[str, Any] = {"ok": True, "source": source, **report}
        out["assumptions"] = [
            "steady state: the temperatures the board settles at, not a transient",
            "thermal reliefs taken as fully connected",
            "copper at nominal thickness; laminate conductivity from the table "
            f"({report['setup'].get('dielectric', 'fr4')})",
            "the air is still or moves evenly along the board; an enclosure, nearby "
            "boards and ducting are not modelled (raise ambient_c to the air inside)",
        ]
        if write_maps:
            try:
                folder = tr.run_folder(tr.runs_dir(runs_dir), board.name)
                out["files"] = await asyncio.to_thread(
                    tr.write_files, report, result, board, folder, used)
                out["run_dir"] = str(folder)
            except OSError as exc:
                out["files_error"] = str(exc)
        return out
