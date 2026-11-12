# SPDX-License-Identifier: Apache-2.0
"""Power distribution network tools (``pdn`` package): DC IR drop and
AC impedance against a target. Offline solvers; they change nothing on
the board."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional

from .emc import _load_board

__all__ = ["register_pdn_tools"]


def _folder(runs_dir: str, board_name: str, net: str):
    from ..pdn import runs_dir as base
    stem = re.sub(r"[^A-Za-z0-9_.+-]+", "_", f"{board_name or 'board'}_{net}")
    return base(runs_dir) / f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}"


def register_pdn_tools(mcp) -> None:

    @mcp.tool()
    async def sim_pdn_ir_drop(
        net: str,
        sources: list[dict],
        sinks: list[dict],
        return_net: str = "",
        expect_file: str = "",
        board_json_path: str = "",
        stackup_json_path: str = "",
        cell_mils: float = 0.0,
        temp_c: float = 25.0,
        plating_mils: float = 0.79,
        sink_model: str = "per_pin",
        max_drop_pct: float = 0.0,
        temp_rise_c: float = 10.0,
        write_maps: bool = True,
        runs_dir: str = "",
        limit: int = 15,
    ) -> dict[str, Any]:
        """DC IR drop, current density and copper loss of a power net.
        Offline, changes nothing.

        Solves the net's real copper as a resistor network: pours, planes
        and pads on a fine grid, tracks as exact resistors, via barrels
        from the stackup. Reports the voltage at every sink pin, the
        worst tracks and vias by current (flagged against IPC-2221), loss
        per layer, and a grid-convergence error estimate. With
        ``return_net`` the ground rise is solved too and added.

        Reads the whole board: on a very large board ask the user first.

        Args:
            net: the power net (e.g. "+3V3").
            sources: [{ref or pads, volts}] regulator outputs / connectors,
                e.g. [{"ref": "U1", "volts": 3.3}] or {"pads": ["J1.1"]}.
                All pads of ``ref`` on the net (and on return_net) are used.
            sinks: [{ref or pads, amps, name}] loads and their DC current.
            return_net: e.g. "GND" to include the return path's rise.
            expect_file / board_json_path / stackup_json_path: as for
                sim_emc_prescan.
            cell_mils: grid cell; 0 picks one (a quarter of the smallest
                pad, within a node budget). Smaller is more exact, slower.
            temp_c: copper temperature (resistivity rises 0.39 %/K).
            plating_mils: via barrel plating (0.79 = IPC class 2 minimum).
            sink_model: "per_pin" (current split equally among a sink's
                pins) or "equipotential" (pins shorted inside the part).
            max_drop_pct: fail when a sink's total drop exceeds this; 0
                reports only.
            temp_rise_c: rise used for the IPC-2221 capacity flags.
            write_maps: write voltage / current-density PNGs and an .npz
                (EDA_AGENT_PDN_RUNS, else ~/EDA Agent/pdn).
        """
        from ..pdn import ir_drop as ir

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        if net not in board.nets():
            return {"ok": False, "error": f"net {net!r} is not on the board"}
        try:
            res = await asyncio.to_thread(
                ir.analyse, board, net, stack, list(sources), list(sinks),
                return_net=return_net, cell_mils=float(cell_mils), temp_c=float(temp_c),
                plating_mils=float(plating_mils), sink_model=sink_model,
                temp_rise_c=float(temp_rise_c), limit=max(1, int(limit)))
        except ir.IRDropError as exc:
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        out: dict[str, Any] = {"ok": True, "source": source,
                               "stack_assumed": bool(stack.assumed),
                               "supply": ir.public(res["supply"])}
        if "return" in res:
            out["return"] = ir.public(res["return"])
            out["rails"] = res["rails"]
            worst = max(r["total_drop_pct"] or 0.0 for r in res["rails"])
        else:
            worst = max((s["drop_pct"] or 0.0) for s in res["supply"]["sinks"])
        out["worst_drop_pct"] = round(worst, 4)
        if max_drop_pct and max_drop_pct > 0:
            out["limit_pct"] = float(max_drop_pct)
            out["pass"] = worst <= float(max_drop_pct)
        out["assumptions"] = [
            "thermal reliefs taken as fully connected",
            "copper at nominal thickness (no etch-back or plating build-up)",
            f"copper at {float(temp_c):g} C everywhere (no self-heating feedback)",
        ] + (["stackup assumed (1.6 mm FR-4): via lengths are estimates"]
             if stack.assumed else [])
        if write_maps:
            try:
                folder = _folder(runs_dir, board.name, net)
                files = await asyncio.to_thread(ir.write_maps, res["supply"], folder)
                if "return" in res:
                    files += await asyncio.to_thread(ir.write_maps, res["return"], folder)
                out["files"] = files
            except OSError as exc:
                out["files_error"] = str(exc)
        return out

    @mcp.tool()
    async def sim_pdn_impedance(
        net: str,
        load: dict,
        ground: str = "GND",
        target_mohm: float = 0.0,
        volts: float = 0.0,
        ripple_pct: float = 0.0,
        step_amps: float = 0.0,
        f_min_hz: float = 1e3,
        f_max_hz: float = 1e9,
        points: int = 241,
        decaps: Optional[list[dict]] = None,
        exclude: Optional[list[str]] = None,
        vrm: Optional[dict] = None,
        mode: str = "auto",
        expect_file: str = "",
        board_json_path: str = "",
        stackup_json_path: str = "",
        tan_d: float = 0.0,
        write_files: bool = True,
        runs_dir: str = "",
    ) -> dict[str, Any]:
        """AC impedance of a power rail seen by a load, against a target
        impedance. Offline, changes nothing.

        Finds the rail's decoupling capacitors on the board (value from
        the comment, package from the footprint), works out each one's
        mounting inductance from its real vias and fan-out, and solves the
        power/ground plane pair as a field cavity on its real shape, so
        plane resonances, spreading inductance and capacitor positions are
        in the curve. Reports where |Z| exceeds the target, the
        anti-resonances, and what each failing band needs. Writes a CSV,
        a plot and a lumped SPICE deck (LTspice / ngspice).

        Args:
            net / ground: the rail and its return.
            load: {ref} or {pads: [...]} of the consuming part;
                {mount_nh} overrides its via inductance.
            target_mohm: the target, or give volts + ripple_pct +
                step_amps (Z = V * ripple / I_step).
            decaps: per-part real data and extra parts:
                [{ref, c_uf, esr_ohms, esl_nh, mount_nh, touchstone,
                count, x, y}]. Use the DC-bias derated c_uf for sign-off.
            exclude: designators to leave out (what-if).
            vrm: {r_mohm, l_nh, bandwidth_khz, ref, disable}. Default
                R = target/2 with a 50 kHz loop.
            mode: auto (cavity when the rail has a plane pair), cavity,
                lumped.
            tan_d: laminate loss tangent at 1 GHz (0 = 0.02).
            expect_file / board_json_path / stackup_json_path: as for
                sim_emc_prescan.
        """
        from ..pdn import impedance as im

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        nets = board.nets()
        for n in (net, ground):
            if n not in nets:
                return {"ok": False, "error": f"net {n!r} is not on the board"}
        folder = _folder(runs_dir, board.name, net) if write_files else None
        try:
            res = await asyncio.to_thread(
                im.analyse, board, stack, net, ground, load=dict(load),
                target_ohms=float(target_mohm) * 1e-3, volts=float(volts),
                ripple_pct=float(ripple_pct), step_amps=float(step_amps),
                f_min_hz=float(f_min_hz), f_max_hz=float(f_max_hz),
                points=max(21, min(2001, int(points))), decaps=decaps, exclude=exclude,
                vrm=vrm, mode=mode, tand=float(tan_d) or None, out_dir=folder)
        except (im.PDNError, ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        res["ok"] = True
        res["source"] = source
        res["stack_assumed"] = bool(stack.assumed)
        return res
