# SPDX-License-Identifier: Apache-2.0
"""Power distribution network tools (``pdn`` package): DC IR drop and
AC impedance against a target. Offline solvers; they change nothing on
the board."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..bridge import get_bridge
from .emc import _load_board

__all__ = ["register_pdn_tools"]

log = logging.getLogger(__name__)


def _failed(what: str, exc: Exception) -> dict[str, Any]:
    """An error nobody foresaw, as a reply and not a traceback."""
    log.exception("%s failed", what)
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _drop_empty(folder) -> None:
    """A run that failed leaves no empty folder behind."""
    if folder is not None:
        try:
            folder.rmdir()
        except OSError:
            pass


def ir_verdict(res: dict, max_drop_pct: float) -> dict[str, Any]:
    """worst_drop_pct, and pass against the limit when one is set. A sink
    whose percentage could not be worked out is never counted as 0 %: the
    worst is then unknown and there is no pass."""
    if "rails" in res:
        vals = [r.get("total_drop_pct") for r in res["rails"]]
    else:
        vals = [s.get("drop_pct") for s in res["supply"]["sinks"]]
    out: dict[str, Any] = {}
    limit = float(max_drop_pct or 0.0)
    if limit > 0:
        out["limit_pct"] = limit
    if not vals or any(v is None for v in vals):
        out["worst_drop_pct"] = None
        out["verdict_note"] = ("no drop percentage for every sink, so no worst value"
                               + (" and no pass" if limit > 0 else ""))
        if limit > 0:
            out["pass"] = None
        return out
    worst = max(vals)
    out["worst_drop_pct"] = round(worst, 4)
    if limit > 0:
        out["pass"] = bool(worst <= limit)
    return out


async def _values_from_project(refs: list[str]) -> tuple[dict, str]:
    """({ref: (farads, where)}, note) for capacitors whose comment is not
    their value, from the open project's component parameters. Never
    fails the run: what could not be read is said in the note."""
    if not refs:
        return {}, ""
    from ..pdn.decap import capacitance_from_parameters
    try:
        reply = await get_bridge().send_command_async(
            "project.get_component_info_batch",
            {"designators": "~~".join(refs), "with_pin_nets": "false"}, timeout=120.0)
    except Exception as exc:  # noqa: BLE001 - the run goes on without them
        return {}, (f"{len(refs)} capacitor(s) have no value in the comment and the project's "
                    f"parameters could not be read ({type(exc).__name__}: {exc})")
    out = {}
    for row in (reply or {}).get("components", []) if isinstance(reply, dict) else []:
        got = capacitance_from_parameters(row.get("parameters"))
        if got and row.get("designator") in refs:
            out[row["designator"]] = (got[0], f"the parameter {got[1]!r}")
    return out, ""


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
                e.g. [{"ref": "U1", "volts": 3.3}] or
                [{"pads": ["J1.1"], "volts": 5.0, "return_pads": ["J1.2"]}].
                All pads of ``ref`` on the net (and on return_net) are
                used. volts is required.
            sinks: [{ref or pads, amps, name}] loads and their DC current,
                e.g. [{"ref": "U5", "amps": 1.2}]. amps is required.
            return_net: e.g. "GND" to include the return path's rise. A
                source or sink given by pads names its return pins with
                return_pads.
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
            write_maps: write voltage / current-density PNGs, an .npz and
                a CSV of every sink pin, each run in its own folder
                (runs_dir, else EDA_AGENT_PDN_RUNS, else ~/EDA Agent/pdn).
            limit: how many tracks, vias and pins per sink to list, worst
                first (every pin is in the CSV).
        """
        from ..pdn import ir_drop as ir
        from ..pdn import run_folder

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        if net not in board.nets():
            return {"ok": False, "error": f"net {net!r} is not on the board"}
        try:
            res = await asyncio.to_thread(
                ir.analyse, board, net, stack, list(sources or []), list(sinks or []),
                return_net=return_net, cell_mils=float(cell_mils), temp_c=float(temp_c),
                plating_mils=float(plating_mils), sink_model=sink_model,
                temp_rise_c=float(temp_rise_c), limit=max(1, int(limit)))
            out: dict[str, Any] = {"ok": True, "source": source,
                                   "stack_assumed": bool(stack.assumed),
                                   "supply": ir.public(res["supply"])}
            if "return" in res:
                out["return"] = ir.public(res["return"])
                out["rails"] = res["rails"]
            out.update(ir_verdict(res, max_drop_pct))
        except (ir.IRDropError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return _failed("sim_pdn_ir_drop", exc)
        out["assumptions"] = [
            "thermal reliefs taken as fully connected",
            "copper at nominal thickness (no etch-back or plating build-up)",
            f"copper at {float(temp_c):g} C everywhere (no self-heating feedback)",
        ] + (["stackup assumed (1.6 mm FR-4): via lengths are estimates"]
             if stack.assumed else [])
        if write_maps:
            files: list[str] = []
            out["files"] = files
            try:
                folder = run_folder(runs_dir, board.name, net)
                files += await asyncio.to_thread(ir.write_maps, res["supply"], folder)
                if "return" in res:
                    files += await asyncio.to_thread(ir.write_maps, res["return"], folder)
            except Exception as exc:  # noqa: BLE001 - the numbers stand without the files
                out["files_error"] = f"{type(exc).__name__}: {exc}"
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
        the comment, package and kind from the footprint), works out each
        pin's mounting inductance from the copper that really joins it to
        the planes, and solves the power/ground plane pair as a field
        cavity on its real shape, perforations included, so plane
        resonances, spreading inductance and capacitor positions are in
        the curve. Reports where |Z| exceeds the target, the
        anti-resonances, and what each failing band needs. Writes the
        curve and the capacitor table as CSV, a plot and a lumped SPICE
        deck (LTspice / ngspice), each run in its own folder.

        Reads the whole board: on a very large board ask the user first.

        Args:
            net / ground: the rail and its return.
            load: {ref} or {pads: [...], return_pads: [...]} of the
                consuming part; {mount_nh} overrides its via inductance.
            target_mohm: the target, or give volts + ripple_pct +
                step_amps (Z = V * ripple / I_step).
            decaps: per-part real data and extra parts:
                [{ref, c_uf, esr_ohms, esl_nh, mount_nh, kind, package,
                touchstone, count, x, y}]. kind: mlcc, polymer, tantalum
                or electrolytic. A ref on the board is counted even when
                it was not found as a capacitor. Use the DC-bias derated
                c_uf for sign-off. Over 30 capacitors are listed by
                group; the CSV has every one.
            exclude: designators to leave out (what-if).
            points: frequency points, 16 to 2001.
            vrm: {r_mohm, l_nh, bandwidth_khz, ref, disable}. Default
                R = target/2 with a 50 kHz loop.
            mode: auto (cavity when the rail has a plane pair), cavity,
                lumped.
            tan_d: laminate loss tangent at 1 GHz (0 = 0.02).
            expect_file / board_json_path / stackup_json_path: as for
                sim_emc_prescan.
        """
        from ..pdn import impedance as im
        from ..pdn import run_folder

        loaded = await _load_board(expect_file, board_json_path, stackup_json_path)
        if isinstance(loaded, dict):
            return loaded
        board, stack, source = loaded
        nets = board.nets()
        for n in (net, ground):
            if n not in nets:
                return {"ok": False, "error": f"net {n!r} is not on the board"}
        cap_values, value_note = {}, ""
        if source == "live":
            cap_values, value_note = await _values_from_project(
                im.decaps_without_a_value(board, net, ground, decaps))
        folder, folder_error = None, ""
        if write_files:
            try:
                folder = run_folder(runs_dir, board.name, net)
            except OSError as exc:
                folder_error = f"{type(exc).__name__}: {exc}"
        try:
            res = await asyncio.to_thread(
                im.analyse, board, stack, net, ground, load=load,
                target_ohms=float(target_mohm) * 1e-3, volts=float(volts),
                ripple_pct=float(ripple_pct), step_amps=float(step_amps),
                f_min_hz=f_min_hz, f_max_hz=f_max_hz, points=points,
                decaps=decaps, exclude=exclude, vrm=vrm, mode=mode,
                tand=float(tan_d) or None, out_dir=folder, cap_values=cap_values)
        except (im.PDNError, ValueError, TypeError) as exc:
            _drop_empty(folder)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            _drop_empty(folder)
            return _failed("sim_pdn_impedance", exc)
        if folder_error:
            res["files_error"] = folder_error
        if value_note:
            res["notes"].append(value_note)
        res["ok"] = True
        res["source"] = source
        res["stack_assumed"] = bool(stack.assumed)
        return res
