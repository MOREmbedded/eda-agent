# SPDX-License-Identifier: Apache-2.0
"""EMI/EMC pre-scan and openEMS field-solver tools (``emc`` package).

``sim_emc_prescan`` judges a board without any external program.
``sim_em_build_model`` / ``sim_em_run`` / ``sim_em_results`` take a few
nets through openEMS, which runs as its own process. ``sim_read_touchstone``
summarises any .sNp file, an openEMS result or a vendor's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ..bridge import get_bridge

__all__ = ["register_emc_tools"]


async def _load_board(expect_file: str, board_json_path: str, stackup_json_path: str):
    """(board, stack, source) or an error dict."""
    from ..emc.stack import assumed_stack, stack_from_stackup
    from ..layout.model import LayoutBoard

    stackup = None
    if board_json_path:
        path = Path(board_json_path)
        if not path.is_file():
            return {"ok": False, "error": f"no such file: {board_json_path}"}
        try:
            board = await asyncio.to_thread(LayoutBoard.load, path)
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return {"ok": False, "error": f"could not load {board_json_path}: {exc}"}
        source = "file"
    elif expect_file:
        from ..layout.read_altium import WrongBoard, read_live_board
        try:
            board = await asyncio.to_thread(read_live_board, expect_file)
        except WrongBoard as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return {"ok": False, "error": f"board read failed: {exc}"}
        source = "live"
        if not stackup_json_path:
            try:
                reply = await get_bridge().send_command_async("pcb.get_layer_stackup", {})
                if isinstance(reply, dict) and reply.get("layers"):
                    stackup = reply
            except Exception:  # noqa: BLE001 - fall back to the assumed stack
                stackup = None
    else:
        return {"ok": False, "error": "pass expect_file (the focused board's full path) "
                                      "to read it live, or board_json_path for a saved "
                                      "LayoutBoard"}
    if stackup_json_path:
        import json
        try:
            stackup = json.loads(Path(stackup_json_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"could not read {stackup_json_path}: {exc}"}
    stack = stack_from_stackup(stackup, board.layers) if stackup else assumed_stack(board.layers)
    return board, stack, source


def register_emc_tools(mcp) -> None:

    @mcp.tool()
    async def sim_emc_prescan(
        expect_file: str = "",
        board_json_path: str = "",
        stackup_json_path: str = "",
        nets: Optional[list[str]] = None,
        drives: Optional[dict] = None,
        checks: Optional[list[str]] = None,
        edge_mils: float = 100.0,
        return_via_mils: float = 40.0,
        limit_line: str = "cispr32_b_3m",
        limit: int = 30,
    ) -> dict[str, Any]:
        """EMI pre-scan of a routed board. Offline, changes nothing, needs
        no external program.

        Checks the fast nets (diff pairs, high-speed classes, clock and
        fast-bus names, or ``nets``):
        - ``reference_gaps``: track crossing a split or slot in its
          reference plane, or with no plane beside it.
        - ``edge_proximity``: fast track nearer the board edge than
          ``edge_mils``.
        - ``return_vias``: layer change with no return via within
          ``return_via_mils``.
        - ``emission``: differential-mode radiated estimate (Ott) against
          ``limit_line`` for nets in ``drives``. Advisory, ranks nets.

        Reads the whole board: on a very large board ask the user first.

        Args:
            expect_file: full path of the focused .PcbDoc, for a live read
                (the real stackup is read too).
            board_json_path: a saved LayoutBoard instead.
            stackup_json_path: a saved pcb_get_layer_stackup reply; with
                neither, a 1.6 mm FR-4 stack is assumed and said so.
            nets: check these instead of the automatic choice.
            drives: {net: {freq_mhz, rise_ns, volts, duty, z_ohms}}.
            checks: subset of the four; None runs all.
            limit_line: cispr32_b_3m, cispr32_b_10m, cispr32_a_3m,
                cispr32_a_10m, fcc15_b_3m, fcc15_a_10m.
        """
        from ..emc.prescan import run_prescan

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        try:
            result = await asyncio.to_thread(
                run_prescan, board, stack, nets=nets, drives=drives, checks=checks,
                edge_mils=float(edge_mils), return_via_mils=float(return_via_mils),
                limit_name=limit_line, limit=max(1, int(limit)))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        result["ok"] = True
        result["source"] = source
        return result

    @mcp.tool()
    async def sim_em_build_model(
        nets: list[str],
        ports: Optional[list[str]] = None,
        expect_file: str = "",
        board_json_path: str = "",
        stackup_json_path: str = "",
        mode: str = "sparams",
        f_max_mhz: float = 6000.0,
        f_min_mhz: float = 0.0,
        margin_mils: float = 200.0,
        area_mils: Optional[list[float]] = None,
        port_ohms: float = 50.0,
        runs_dir: str = "",
        max_cells_m: float = 20.0,
    ) -> dict[str, Any]:
        """Build an openEMS field model of a few nets. Writes files only.

        The model holds the nets' copper, the return copper (planes, pours,
        ground copper and vias) in a box round them, the real stackup, and
        a lumped port on each pad in ``ports``. Run it with sim_em_run.

        Args:
            nets: the nets to model (a trace, a diff pair, an antenna feed).
            ports: pads as DESIGNATOR.PIN (e.g. J1.1, U3.7); None uses the
                first net's pads when it has at most four.
            expect_file / board_json_path / stackup_json_path: as for
                sim_emc_prescan.
            mode: "sparams" (S-parameters) or "antenna" (adds air, PML and
                the far field at the S11 dip; includes no-net copper).
            f_max_mhz / f_min_mhz: band. Higher f_max means a finer mesh.
            margin_mils: box margin round the nets; area_mils
                [x1, y1, x2, y2] sets the box instead.
            runs_dir: where the run folder goes (EDA_AGENT_EM_RUNS, else
                ~/EDA Agent/em). Put it on a drive with room.
            max_cells_m: refuse models above this many million cells.
        """
        from ..emc.openems import ModelError, build_model, write_model

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        try:
            model = await asyncio.to_thread(
                build_model, board, list(nets), list(ports or []), stack,
                f_max_hz=float(f_max_mhz) * 1e6, f_min_hz=float(f_min_mhz) * 1e6,
                mode=mode, margin_mils=float(margin_mils), area_mils=area_mils,
                port_ohms=float(port_ohms), max_cells=int(float(max_cells_m) * 1e6))
        except ModelError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            folder = write_model(model, runs_dir or None)
        except OSError as exc:
            return {"ok": False, "error": f"could not write the model: {exc}"}
        return {
            "ok": True, "folder": str(folder), "source": source, "mode": mode,
            "nets": model["nets"],
            "ports": [{k: p[k] for k in ("number", "name", "layer", "reference_layer", "ohms")}
                      for p in model["ports"]],
            "box_mils": model["box_mils"], "sheets": len(model["sheets"]),
            "vias": len(model["vias"]), "cells_estimate": model["estimate"]["cells"],
            "stack_assumed": model["stack"]["assumed"], "notes": model["notes"],
            "next_step": "sim_em_run(folder) to solve it",
        }

    @mcp.tool()
    async def sim_em_run(folder: str, threads: int = 0) -> dict[str, Any]:
        """Start openEMS on a model folder from sim_em_build_model.

        Returns at once; the solve runs as its own process and can take
        minutes to hours. Poll sim_em_results. Without openEMS installed
        it says how to install it (the user's decision) and starts nothing.

        Args:
            folder: the model folder.
            threads: solver threads; 0 lets openEMS choose.
        """
        from ..emc.openems import start_run
        return await asyncio.to_thread(start_run, folder, "", int(threads))

    @mcp.tool()
    async def sim_em_results(folder: str, wait_s: float = 0.0,
                             plot: bool = True) -> dict[str, Any]:
        """State of an openEMS run; when done, its results.

        Done: writes the Touchstone file (.sNp) beside the run and returns
        return loss per port, S21, the S11 dip, the antenna's directivity,
        efficiency and pattern (antenna mode), and plot paths.

        Args:
            folder: the model folder.
            wait_s: wait up to this long (max 100 s) for the run to finish.
            plot: write sparams.png (and pattern.png) when matplotlib is here.
        """
        from ..emc.openems import collect_results, run_status

        deadline = asyncio.get_running_loop().time() + min(max(float(wait_s), 0.0), 100.0)
        status = run_status(folder)
        while status["state"] == "running" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(2.0)
            status = run_status(folder)
        if status["state"] != "done":
            return {"ok": status["state"] != "failed", **status}
        return await asyncio.to_thread(collect_results, folder, bool(plot))

    @mcp.tool()
    async def sim_read_touchstone(path: str,
                                  at_mhz: Optional[list[float]] = None) -> dict[str, Any]:
        """Summarise a Touchstone S-parameter file (.s1p .. .sNp).

        Return loss per port and where it is best (an antenna's
        resonance, with Zin there), S21 insertion loss, and the values at
        ``at_mhz``. Reads Touchstone 1.x S-parameters.
        """
        from ..emc.touchstone import read_touchstone, summarize
        try:
            data = await asyncio.to_thread(read_touchstone, path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        at = [float(f) * 1e6 for f in at_mhz or []]
        return {"ok": True, "path": path, **summarize(data, at or None)}
