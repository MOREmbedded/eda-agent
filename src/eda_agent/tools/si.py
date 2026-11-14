# SPDX-License-Identifier: Apache-2.0
"""Signal integrity: transmission-line models and the stackup planner.

``si_line_model`` gives a trace's or a pair's wideband model (impedances,
delay, loss, crosstalk, Touchstone file) from its numbers or from a layer
of the board's stackup. ``si_stackup_plan`` gives every signal layer's
width for a single-ended target and pair geometry for differential
targets, can compare them with Altium's impedance profiles and, when
asked, write them as profiles. Both run as simulation jobs, so the
answers are kept and show on the dashboard's Simulation page.

``si_board_snapshot`` reads the open board and its stackup once and keeps
them; ``si_net_channel`` models a routed net or differential pair of a
snapshot end to end (every trace section, via and stub) and works out the
design rules that hold it to its targets.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Optional

__all__ = ["register_si_tools"]


def _failed(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _save_input(name: str, data: dict[str, Any]) -> str:
    """Keep a reply from Altium as a job input file (content-named)."""
    from ..simulation.jobs import jobs_dir
    text = json.dumps(data, sort_keys=True, indent=1, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    folder = jobs_dir() / "_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{digest}.json"
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return str(path)


def _bridge_params(args: dict[str, Any]) -> dict[str, str]:
    """Tool arguments as the bridge takes them (text)."""
    out = {}
    for k, v in args.items():
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[k] = repr(float(v))
        else:
            out[k] = str(v)
    return out


def _run(kind: str, engines: list[str], spec: dict, inputs: dict, title: str, force: bool):
    from .. import simulation as sim
    runs = []
    for e in engines:
        try:
            runs.append(sim.submit(kind, e, spec, inputs, title, True, force))
        except KeyError as exc:
            runs.append({"ok": False, "engine": e, "error": str(exc.args[0])})
    job = next((r.get("job") for r in runs if r.get("job")), None)
    out: dict[str, Any] = {"ok": any(r.get("state") == "done" for r in runs), "job": job,
                           "runs": runs}
    if job:
        st = sim.refresh(job)
        out["folder"] = st.get("folder")
        out["results"] = {name: r.get("results") for name, r in st["runs"].items()
                          if r.get("state") == "done"}
        if len(out["results"]) >= 2:
            out["compare"] = sim.compare(job)
    errors = [f"{r.get('engine')}: {r.get('error')}" for r in runs if r.get("error")]
    if errors:
        out["errors"] = errors
    return out


def register_si_tools(mcp) -> None:
    _register_si_net_tools(mcp)
    _register_si_time_tools(mcp)
    _ibis_models_tool(mcp)
    _register_si_board_tools(mcp)

    @mcp.tool()
    async def si_line_model(spec: dict, engines: Optional[list[str]] = None, stackup_file: str = "",
                            title: str = "", force: bool = False) -> dict[str, Any]:
        """Wideband model of a trace, or of coupled traces, on a PCB.

        Solves the cross-section for R, L, G and C at every frequency:
        R(f) and L(f) from a 2D eddy-current solve (skin and proximity
        effect, the reference planes included), C(f) and G(f) from the 2D
        field solve with causal (Djordjevic-Sarkar) dielectrics, and copper
        roughness. Gives the impedances (lossless, as a fab quotes them,
        and lossy), delay, loss per inch, crosstalk for a pair, and the
        S-parameters of a length of line as a Touchstone file.

        Args:
            spec: the line, lengths in mils. Either ``structure``
                ("microstrip" with ``h_mils``; "stripline" with
                ``h_below_mils`` and ``h_above_mils``) or ``layer`` with a
                stackup. Always ``width_mils``; for a pair ``traces``: 2
                and ``gap_mils``. Optional: ``copper_mils``, ``er``,
                ``tand``, ``laminate`` (fr4, fr4-mid-loss, low-loss,
                ultra-low-loss, ro4350b), ``f_spec_ghz``, ``er_above``,
                ``mask_mils``, ``etch_mils``, ``foil`` (smooth, std, rtf,
                vlp, hvlp), ``length_mm`` (100), ``f_min_ghz`` (0.01),
                ``f_max_ghz`` (20), ``points`` (201), ``z_ref`` (50),
                ``f_report_ghz`` (1). sim_engines lists every key.
            engines: default ["eda-agent"]; add "scikit-rf" for a
                closed-form second opinion on a single microstrip.
            stackup_file: a saved pcb_get_layer_stackup reply, used with
                ``spec.layer``. Empty with a layer: read from the open board.
            title: a name for the job list.
            force: solve again even when results exist.

        Returns:
            ``results`` per engine (``metrics``, ``verdicts``, ``files`` in
            the job folder: Touchstone, rlgc.json, plot), ``compare`` when
            two engines ran, ``job`` and ``folder``.
        """
        spec = dict(spec or {})
        inputs: dict[str, str] = {}
        try:
            if spec.get("layer"):
                if stackup_file:
                    inputs["stackup"] = stackup_file
                else:
                    from ..bridge import get_bridge
                    st = await read_live_stackup(get_bridge())
                    if not isinstance(st, dict) or not st.get("layers"):
                        return {"ok": False, "error": "could not read the board's stackup; "
                                "pass stackup_file or give the cross-section by numbers"}
                    inputs["stackup"] = _save_input("stackup", st)
            return await asyncio.to_thread(_run, "line-model", list(engines or ["eda-agent"]),
                                           spec, inputs, title or "line model", bool(force))
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @mcp.tool()
    async def si_stackup_plan(se_ohms: float = 50.0, diff_ohms: Optional[list[float]] = None,
                              layers: Optional[list[str]] = None, stackup_file: str = "",
                              options: Optional[dict] = None, compare_altium: bool = False,
                              apply: bool = False, force: bool = False) -> dict[str, Any]:
        """Trace widths and pair geometry for target impedances, per layer.

        For every signal layer of the stackup the real cross-section is
        solved (copper thickness, the dielectric below and above, solder
        mask on outer layers, the nearest copper as references) for the
        width that gives ``se_ohms`` and the width and gap that give each
        of ``diff_ohms``.

        Args:
            se_ohms: single-ended target; 0 for none.
            diff_ohms: differential targets (default [100]).
            layers: only these layers (names or stack labels).
            stackup_file: a saved pcb_get_layer_stackup reply; empty reads
                the open board's stackup.
            options: tand, f_design_ghz, mask_mils, mask_er, etch_mils,
                foil, min_width_mils, max_width_mils, diff_gap_mils,
                diff_gap_ratio, loss_ghz (list: also loss per inch, slower).
            compare_altium: also read the board's impedance profiles and
                give our impedance at Altium's widths, a second opinion on
                the Layer Stack Manager's solver.
            apply: write the plan as impedance profiles in the Layer Stack
                Manager (one profile per target, widths and gaps locked).
                Changes the board; only when the user asked for it.
            force: solve again even when results exist.

        Returns:
            ``plan`` (per layer: structure, references, heights, Er, and
            per target width, gap, impedances, loss), ``altium_steps`` (the
            pcb_impedance_profile calls that write it), ``altium_comparison``
            when asked, ``applied`` when written, ``job`` and ``folder``.
        """
        spec: dict[str, Any] = dict(options or {})
        spec["se_ohms"] = float(se_ohms or 0)
        spec["diff_ohms"] = [float(x) for x in (diff_ohms if diff_ohms is not None else [100.0])]
        if layers:
            spec["layers"] = [str(x) for x in layers]
        try:
            bridge = None
            if stackup_file:
                path = stackup_file
            else:
                from ..bridge import get_bridge
                bridge = get_bridge()
                st = await read_live_stackup(bridge)
                if not isinstance(st, dict) or not st.get("layers"):
                    return {"ok": False, "error": "could not read the board's stackup; "
                            "open a board or pass stackup_file"}
                path = _save_input("stackup", st)
            if compare_altium:
                from ..bridge import get_bridge
                bridge = bridge or get_bridge()
                prof = await bridge.send_command_async("pcb.get_impedance_profiles", {})
                if isinstance(prof, dict) and prof.get("profiles"):
                    spec["altium_profiles"] = prof
            out = await asyncio.to_thread(_run, "stackup-plan", ["eda-agent"], spec,
                                          {"stackup": path}, "stackup plan", bool(force))
            res = (out.get("results") or {}).get("eda-agent") or {}
            plan = res.get("plan") or {}
            reply = {"ok": bool(plan), "job": out.get("job"), "folder": out.get("folder"),
                     "plan": plan.get("layers", []), "altium_steps": plan.get("altium_steps", []),
                     "verdicts": res.get("verdicts", []), "notes": res.get("notes", [])}
            if "altium_comparison" in plan:
                reply["altium_comparison"] = plan["altium_comparison"]
            if out.get("errors"):
                reply["errors"] = out["errors"]
            if apply and plan:
                from ..bridge import get_bridge
                bridge = bridge or get_bridge()
                done = []
                for step in plan.get("altium_steps", []):
                    cmd = ("pcb.impedance_profile" if step["tool"] == "pcb_impedance_profile"
                           else "pcb.impedance_profile_layer")
                    r = await bridge.send_command_async(cmd, _bridge_params(step["args"]))
                    done.append({"step": step, "reply": r})
                reply["applied"] = done
            return reply
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


async def _snapshot_from_args(snapshot: str, expect_file: str, board_json_path: str,
                              stackup_json_path: str) -> dict[str, Any]:
    """A snapshot record from an id, a live read, or saved files."""
    from ..si import snapshot as snap
    if snapshot:
        rec = snap.load(snapshot)
        return {**rec, **snap.status(rec)}
    if board_json_path:
        from ..layout.model import LayoutBoard
        board = await asyncio.to_thread(LayoutBoard.load, board_json_path)
        stackup = None
        if stackup_json_path:
            stackup = json.loads(open(stackup_json_path, encoding="utf-8").read())
        rec = await asyncio.to_thread(snap.save, board, stackup, board.source)
        return {**rec, **snap.status(rec)}
    if not expect_file:
        raise ValueError("pass snapshot (an id from si_board_snapshot), expect_file (the "
                         "focused board's full path, read live) or board_json_path")
    from ..bridge import get_bridge
    from ..layout.read_altium import read_live_board
    board = await asyncio.to_thread(read_live_board, expect_file)
    stackup = None
    if stackup_json_path:
        stackup = json.loads(open(stackup_json_path, encoding="utf-8").read())
    else:
        reply = await read_live_stackup(get_bridge())
        if isinstance(reply, dict) and reply.get("layers"):
            stackup = reply
    rec = await asyncio.to_thread(snap.save, board, stackup, expect_file)
    return {**rec, **snap.status(rec)}


def _register_si_net_tools(mcp) -> None:

    @mcp.tool()
    async def si_board_snapshot(expect_file: str = "", board_json_path: str = "",
                                stackup_json_path: str = "", list_only: bool = False
                                ) -> dict[str, Any]:
        """Read the open board and its stackup once, for the SI tools.

        The copper (pads, tracks, arcs, vias, regions, net classes, pairs,
        rules) and the stackup are kept in a folder named by their content
        hash. Pass its ``id`` as ``snapshot`` to si_net_channel: every job
        from one snapshot is about the same board, and the board is not
        read again. A snapshot is reported ``stale`` once the board file is
        saved after it.

        Args:
            expect_file: the focused board's full path; the read refuses a
                different board. Reads the whole board: on a large board
                that takes a while.
            board_json_path: a saved LayoutBoard instead of a live read.
            stackup_json_path: a saved pcb_get_layer_stackup reply; empty
                with a live read asks Altium.
            list_only: only list the snapshots kept so far.

        Returns:
            ``id``, ``counts``, ``diff_pairs``, ``stale`` and the files; or
            ``snapshots`` with list_only.
        """
        try:
            if list_only:
                from ..si import snapshot as snap
                return {"ok": True, "snapshots": await asyncio.to_thread(snap.listing)}
            rec = await _snapshot_from_args("", expect_file, board_json_path, stackup_json_path)
            from ..layout.model import LayoutBoard
            board = await asyncio.to_thread(LayoutBoard.load, rec["board"])
            rec["diff_pairs"] = [{"name": d.name, "positive": d.positive, "negative": d.negative}
                                 for d in board.diff_pairs]
            if not rec.get("has_stackup"):
                rec["warning"] = ("no stackup was read; si_net_channel needs one "
                                  "(pass stackup_json_path)")
            return {"ok": True, **rec}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @mcp.tool()
    async def si_net_channel(net: str = "", pair: str = "", snapshot: str = "",
                             expect_file: str = "", board_json_path: str = "",
                             stackup_json_path: str = "", options: Optional[dict] = None,
                             apply_rules: bool = False, title: str = "",
                             force: bool = False) -> dict[str, Any]:
        """Model a routed net or differential pair of the board, end to end.

        Finds the net's routed path between its pins and models every part
        of it: each trace section from its own cross-section (layer, width,
        and for a pair the gap; eddy-current R and L, causal dielectrics),
        each via (barrel, stubs, pads, the return-via loop when the
        reference plane changes), and each stub off the path. Gives the
        channel's S-parameters (Touchstone), TDR, delay, insertion and
        return loss at Nyquist, and for a pair mode conversion and skew;
        finds the other nets routed beside it, and over or under it on
        a facing signal layer, and their near-end and
        far-end crosstalk (over the limit, with the gap that brings
        the worst of them within it), and the places where the route crosses a split
        or a gap in its reference plane; checks them against limits; and works out the Altium rules (width,
        pair gap, uncoupled length, length from the loss budget) that hold
        the net to its targets.

        Args:
            net: the net to model; or ``pair``: a differential pair (or
                one of its nets).
            snapshot: an id from si_board_snapshot (preferred). Else
                expect_file reads the open board live, or board_json_path
                and stackup_json_path name saved files.
            options: from_pin, to_pin (DESIGNATOR.PIN), data_rate_gbps,
                f_max_ghz (20), points (400), rise_ps (35), z_ref (50),
                z_target, z_tol_pct (10), il_max_db, rl_min_db,
                skew_max_ps, tand (0.02), foil, mask_mils, etch_mils,
                rules (true); crosstalk from the copper beside the net:
                xtalk (true), xtalk_max_pct (5, of the aggressors' swing),
                xtalk_reach_mils, xtalk_quiet (name globs of nets that do
                not switch), xtalk_aggressors ([{match, rise_ps,
                swing_mv}]: neighbours with their own edge and swing),
                xtalk_swing_mv (the swing of the rest), xtalk_max_mv
                (limit on crosstalk in mV on the net); gaps in the reference copper under the
                route: return_path (true), return_gap_min_mils (8).
                sim_engines lists every key.
            apply_rules: create the rules in Altium (pcb_create_design_rule).
                Changes the board; only when the user asked for it.
            title: a name for the job list.
            force: solve again even when results exist.

        Returns:
            ``metrics``, ``verdicts``, ``path`` (sections, vias, stubs),
            ``crosstalk`` (each aggressor: nets, layer, gap, shared length,
            near and far end, where), ``return_path`` (each gap in the
            reference copper under the route: layer, length, where),
            ``altium_steps`` (the rules), ``applied`` when written,
            ``snapshot`` with ``stale``, ``job`` and ``folder``.
        """
        if not net and not pair:
            return {"ok": False, "error": "give net or pair"}
        try:
            rec = await _snapshot_from_args(snapshot, expect_file, board_json_path,
                                            stackup_json_path)
            if not rec.get("has_stackup"):
                return {"ok": False, "error": "the snapshot has no stackup; take it again "
                        "with stackup_json_path or with the board open in Altium",
                        "snapshot": rec["id"]}
            spec: dict[str, Any] = dict(options or {})
            if pair:
                spec["pair"] = pair
            else:
                spec["net"] = net
            out = await asyncio.to_thread(
                _run, "net-channel", ["eda-agent"], spec,
                {"board": rec["board"], "stackup": rec["stackup"]},
                title or f"channel {pair or net}", bool(force))
            res = (out.get("results") or {}).get("eda-agent") or {}
            reply: dict[str, Any] = {
                "ok": bool(res), "job": out.get("job"), "folder": out.get("folder"),
                "snapshot": {"id": rec["id"], "stale": rec.get("stale"), "why": rec.get("why")},
                "metrics": res.get("metrics", {}), "verdicts": res.get("verdicts", []),
                "files": res.get("files", {}), "notes": res.get("notes", []),
                "altium_steps": res.get("altium_steps", []),
                "path": (res.get("figures") or {}).get("path")}
            for key in ("crosstalk", "return_path"):
                if key in (res.get("figures") or {}):
                    reply[key] = res["figures"][key]
            if out.get("errors"):
                reply["errors"] = out["errors"]
            if apply_rules and reply["altium_steps"]:
                if rec.get("stale"):
                    reply["applied"] = []
                    reply["error"] = "not applied: the board changed since the snapshot"
                    return reply
                from .pcb import _rule_edit, create_rule_params
                done = []
                for step in reply["altium_steps"]:
                    params = create_rule_params(**step["args"])
                    if "name" not in params:
                        done.append({"step": step, "reply": params})
                        continue
                    r = await _rule_edit("pcb_create_design_rule", step["args"],
                                         "pcb.create_design_rule", params)
                    done.append({"step": step, "reply": r})
                reply["applied"] = done
            return reply
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


def _channel_file(touchstone_file: str, net_job: str) -> str:
    """The .sNp of a channel: a file, or the one a net-channel job wrote."""
    if touchstone_file:
        return touchstone_file
    if not net_job:
        raise ValueError("pass touchstone_file or net_job (a net-channel job id)")
    from ..simulation import jobs as _jobs
    try:
        job = _jobs.get(net_job)
    except (KeyError, ValueError, OSError):
        job = None
    if job is None or job.kind != "net-channel":
        raise ValueError(f"{net_job} is not a net-channel job")
    folder = job.work_folder("eda-agent")
    found = sorted(folder.glob("channel.s*p"))
    if not found:
        raise ValueError(f"job {net_job} has no channel file yet; run it first")
    return str(found[0])


def _register_si_time_tools(mcp) -> None:

    @mcp.tool()
    async def si_channel_eye(data_rate_gbps: float, touchstone_file: str = "", net_job: str = "",
                             engines: Optional[list[str]] = None, options: Optional[dict] = None,
                             title: str = "", force: bool = False) -> dict[str, Any]:
        """Eye diagram of a channel, by convolution and in SPICE.

        The channel is a 2-port line or a 4-port pair (ports 1, 2 near P
        and N; 3, 4 far), from a file or from a si_net_channel job. The
        ``eda-agent`` engine convolves its pulse response with a PRBS
        pattern and adds the worst-case eye (peak distortion analysis:
        no pattern can close it further). The ``spice`` engine fits the
        channel as a passive rational macromodel, keeps it as
        ``channel.sub`` with an LTspice symbol ``channel.asy``, and runs
        the same pattern in ngspice or LTspice; it also reports how far
        the circuit simulator's waveform is from the convolution's.

        Args:
            data_rate_gbps: NRZ data rate.
            touchstone_file: the channel's .s2p or .s4p; or ``net_job``: a
                net-channel job id (its channel file is used).
            engines: default ["eda-agent", "spice"] (spice when ngspice or
                LTspice is installed).
            options: rise_ps (0.3 UI), amplitude_mv (800, differential
                for a pair), noise_mv (0; peak noise at the receiver, the
                eye loses twice this), prbs (7, 9, 11, 15), repeats (2),
                overshoot_max_pct, ringback_max_pct,
                edge_reversal_max_pct (limits on one edge at the receiver,
                percent of its settled level; no check if absent),
                tx_ffe (transmitter taps one UI apart, e.g.
                [-0.1, 0.7, -0.2]; scaled to a peak of 1), ctle_db
                (receiver CTLE boost over DC, 0-30), ctle_peak_ghz (its
                first pole, default Nyquist), dfe_taps (receiver DFE taps,
                0-32), dfe_max (most one tap takes off, part of the main
                cursor, 0.5); equalisation is in the eda-agent engine only,
                samples_per_ui (32), spice_oversample (4), simulator (auto,
                ngspice, ltspice), fit_tol (0.002); the ends: tx_ibis and
                tx_model (an IBIS driver: its output resistance, C_comp,
                edge and swing), rx_ibis and rx_model (an IBIS receiver:
                its C_comp, high impedance), or tx_r_ohms, tx_c_pf,
                rx_r_ohms (a number or "open"), rx_c_pf. Both ends are the
                reference impedance by default.
            title: a name for the job list.
            force: run again even when results exist.

        Returns:
            ``results`` per engine (eye height and width, worst-case eye,
            files: eye.png, waveform.csv, channel.sub, channel.asy),
            ``compare`` of the two engines, ``job`` and ``folder``.
        """
        try:
            path = _channel_file(touchstone_file, net_job)
            spec: dict[str, Any] = dict(options or {})
            spec["data_rate_gbps"] = float(data_rate_gbps)
            if engines is None:
                from .. import simulation as sim
                engines = ["eda-agent"]
                try:
                    if sim.engine_status(sim.get_engine("spice")).get("available"):
                        engines.append("spice")
                except KeyError:
                    pass
            return await asyncio.to_thread(_run, "channel-eye", list(engines), spec,
                                           {"touchstone": path},
                                           title or f"eye {data_rate_gbps:g} Gb/s", bool(force))
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)

    @mcp.tool()
    async def si_spice_model(touchstone_file: str = "", net_job: str = "", name: str = "channel",
                             out_folder: str = "", fit_tol: float = 2e-3) -> dict[str, Any]:
        """A Touchstone channel as a passive SPICE subcircuit for LTspice or ngspice.

        Fits the S-parameters with common poles (relaxed vector fitting),
        enforces passivity, and writes ``<name>.sub`` (resistors,
        capacitors and linear controlled sources only, so every SPICE
        runs it) and an LTspice symbol ``<name>.asy``. Place the symbol in
        an LTspice schematic with real driver and receiver models to see
        the channel as the board has it.

        Args:
            touchstone_file: a .sNp; or ``net_job``: a net-channel job id.
            name: the subcircuit's name.
            out_folder: where to write; default beside the Touchstone file.
            fit_tol: RMS error allowed for the fit.

        Returns:
            ``files``, ``poles``, ``rms_error``, ``passivity`` (largest
            singular value before and after enforcement), ``ports`` (the
            subcircuit's pin order).
        """
        try:
            import re
            from pathlib import Path
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", name or ""):
                return {"ok": False, "error": "name is a SPICE identifier (letters, digits, _)"}
            path = Path(_channel_file(touchstone_file, net_job))
            from ..si import eye_jobs, vectfit

            def work():
                f, s, z0 = eye_jobs.load_channel(path) if path.suffix.lower() in (".s2p", ".s4p") \
                    else _any_ports(path)
                keep = f > 0
                model = vectfit.macromodel(f[keep], s[keep], fit_tol)
                folder = Path(out_folder) if out_folder else path.parent
                folder.mkdir(parents=True, exist_ok=True)
                files = eye_jobs.write_model(model, folder, name, z0)
                return model, {k: str(folder / v) for k, v in files.items()}

            model, files = await asyncio.to_thread(work)
            ports = (["p_near", "n_near", "p_far", "n_far"] if model.ports == 4
                     else [f"port{i + 1}" for i in range(model.ports)])
            return {"ok": True, "files": files, "poles": len(model.poles),
                    "rms_error": model.info["rms_error_after_enforcement"],
                    "passivity": {"before": model.info["passivity_before"],
                                  "after": model.info["passivity_after"]},
                    "ports": ports + ["ref"]}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


def _ibis_models_tool(mcp) -> None:

    @mcp.tool()
    async def si_ibis_models(ibis_file: str) -> dict[str, Any]:
        """The buffer models of an IBIS file, reduced for a channel eye.

        For each [Model]: its type, C_comp (typ, min, max), supply, and
        for a driver the output resistance (slope of the pull-down and
        pull-up V-I tables at 0 V), the 10-90 % edge and the swing from
        [Ramp]. Pass a model's name as tx_model or rx_model to
        si_channel_eye with the same file as tx_ibis or rx_ibis.

        Args:
            ibis_file: an .ibs file.

        Returns:
            ``models``: name, model_type, c_comp_f, voltage, and
            ``driver`` figures where the model has V-I tables or a ramp.
        """
        try:
            from ..si import ibis

            def work():
                out = []
                for m in ibis.read(ibis_file).values():
                    row = m.to_dict()
                    if m.model_type.lower() not in ibis.NOT_DRIVERS:
                        d = ibis.driver(m)
                        if len(d) > 1 and (m.pulldown or m.pullup or m.ramp_r or m.ramp_f):
                            row["driver"] = d
                    r = ibis.receiver(m)
                    if "r_term_ohms" in r:
                        row["termination_ohms"] = round(r["r_term_ohms"], 2)
                    out.append(row)
                return out

            models = await asyncio.to_thread(work)
            if not models:
                return {"ok": False, "error": "no [Model] in the file"}
            return {"ok": True, "file": ibis_file, "models": models}
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


#: What every marker text starts with: "SI F3 impedance". A marking run
#: removes the free texts that start so before it writes its own.
MARKER_PREFIX = "SI F"


async def unsaved_edit(rec: dict[str, Any]) -> str:
    """Why the focused board is not the snapshot's board as it was read,
    or "". The snapshot's own staleness looks at the saved file, and an
    edit that is not saved yet leaves the file as it was: so the board is
    read again and compared before anything is written on it."""
    from ..layout.read_altium import read_live_board
    try:
        live = await asyncio.to_thread(read_live_board, rec["source_file"])
    except Exception as exc:  # noqa: BLE001
        return f"the board could not be read again ({exc})"
    now = json.dumps(live.to_dict(), sort_keys=True, separators=(",", ":"))
    try:
        then = open(rec["board"], encoding="utf-8").read()
    except OSError as exc:
        return f"the snapshot's board is not readable ({exc})"
    return "" if now == then else "the board was edited since the snapshot (not saved yet)"


async def read_live_stackup(bridge) -> Any:
    """The focused board's stackup, with the dielectric taken from its
    physical stack where the stackup reply carries none."""
    from ..emc.stack import fill_dielectrics, stack_from_stackup
    st = await bridge.send_command_async("pcb.get_layer_stackup", {})
    if not isinstance(st, dict) or not st.get("layers"):
        return st
    try:
        bare = any(a.z_mils == b.z_mils for a, b in
                   zip(stack_from_stackup(st).layers, stack_from_stackup(st).layers[1:]))
    except Exception:  # noqa: BLE001
        bare = True
    if not bare:
        return st
    phys = await bridge.send_command_async("pcb.physical_stack", {})
    if isinstance(phys, dict) and phys.get("layers"):
        return fill_dielectrics(st, phys)
    return st


async def mark_findings(findings: list[dict[str, Any]], board_nets: dict[str, list[str]],
                        mode: str, layer: str, height: float = 40.0) -> dict[str, Any]:
    """Show board-SI findings in Altium: highlight the failing nets, and
    with ``mode="text"`` put each finding's id and check as a text on
    ``layer`` where the finding is. Only errors are marked."""
    from ..bridge import get_bridge
    bridge = get_bridge()
    errs = [f for f in findings if f.get("severity") == "error"]
    nets: list[str] = []
    for f in errs:
        for n in board_nets.get(f"{f['kind']} {f['item']}", [f["item"]]):
            if n not in nets:
                nets.append(n)
    out: dict[str, Any] = {"highlighted": [], "texts": [], "errors": []}
    for i, n in enumerate(nets):
        r = await bridge.send_command_async(
            "generic.highlight_net", {"net_name": n, "clear_existing": "true" if i == 0 else "false",
                                      "context": "pcb"})
        (out["highlighted"] if not (isinstance(r, dict) and r.get("error")) else out["errors"]
         ).append(n if not (isinstance(r, dict) and r.get("error")) else f"{n}: {r.get('error')}")
    if mode == "text":
        # the markers of an earlier run go first, or each run adds a set
        from ..scope import to_wire
        r = await bridge.send_command_async("generic.delete_objects", {
            "scope": to_wire("active_doc"), "object_type": "eTextObject",
            "filter": f"Text~{MARKER_PREFIX}* *|InComponent=false"})
        if isinstance(r, dict) and r.get("error"):
            out["errors"].append(f"earlier markers not removed: {r.get('error')}")
        else:
            out["removed"] = int((r or {}).get("matched", 0) or 0) if isinstance(r, dict) else 0
        for f in errs:
            at = f.get("at_mils")
            if not at:
                continue
            label = f"SI {f['id']} {f['check']}"
            r = await bridge.send_command_async("pcb.place_text", {
                "text": label, "x": str(at[0]), "y": str(at[1]), "layer": layer,
                "height": str(height), "rotation": "0"})
            if isinstance(r, dict) and r.get("error"):
                out["errors"].append(f"{f['id']}: {r.get('error')}")
            else:
                out["texts"].append({"id": f["id"], "at_mils": at, "text": label})
    return out


def _register_si_board_tools(mcp) -> None:

    @mcp.tool()
    async def si_board_check(snapshot: str = "", expect_file: str = "",
                             board_json_path: str = "", stackup_json_path: str = "",
                             targets: Optional[list[dict]] = None,
                             options: Optional[dict] = None, mark: str = "none",
                             marker_layer: str = "Mechanical13", title: str = "",
                             force: bool = False) -> dict[str, Any]:
        """Check every high-speed net and pair of a board, and place each fault on it.

        Each net or pair the targets name is modelled end to end (as
        si_net_channel does) and checked against its target's limits:
        impedance, insertion and return loss, skew, stub resonance, and
        optionally the eye. A failure becomes a finding placed where it is:
        an impedance fault where the TDR sees it (walked along the routed
        path at each section's own delay), a stub where it leaves the path,
        a via stub at its via, crosstalk at the stretch beside (or on a
        facing signal layer, across from) the worst neighbour, each gap in the reference plane where the route crosses
        it, other faults at the driving pin. Writes
        report.html (self-contained), findings.json and summary.csv.

        Args:
            snapshot: an id from si_board_snapshot (preferred); or
                expect_file (read the open board live) or board_json_path
                and stackup_json_path.
            targets: a list of {match: net or pair name glob, or class: an
                Altium net class; kind: "pair" or "net"; data_rate_gbps,
                z_target, z_tol_pct, il_max_db, rl_min_db, skew_max_ps,
                stub_margin, delay_match_ps (each of the target's nets may
                be this far ahead of the slowest of them),
                xtalk_max_pct (crosstalk from neighbours, 5),
                xtalk_swing_mv (the neighbours' swing: crosstalk then
                closes the eye; default the eye's amplitude),
                xtalk_aggressors ([{match, rise_ps, swing_mv}]: neighbours
                with their own edge and swing), xtalk_max_mv (limit in mV);
                eye: true to add the eye, eye_min_mv,
                overshoot_max_pct, ringback_max_pct, edge_reversal_max_pct
                (one edge at the receiver, placed at the receiving pin),
                eye_min_ui, the eye's tx_ibis/rx_ibis options and its
                equalisation (tx_ffe, ctle_db, ctle_peak_ghz, dfe_taps,
                dfe_max)}. A net
                is taken by the first target that matches it. Default: every
                differential pair.
            options: data_rate_gbps (for targets without one), eye,
                max_items (40), workers (4), f_max_ghz, points (200),
                temperature_c or thermal_npz (a thermal analysis'
                result.npz: hot copper loses more), tand, foil, mask_mils,
                etch_mils, xtalk (true), xtalk_max_pct, xtalk_reach_mils,
                xtalk_quiet, xtalk_aggressors, xtalk_swing_mv, xtalk_max_mv,
                return_path (true), return_gap_min_mils.
            mark: "none"; "highlight" highlights the failing nets in
                Altium; "text" also writes each error's id and check as
                text on ``marker_layer`` at its place (changes the board;
                only when the user asked for it, and never from a stale
                snapshot).
            marker_layer: the layer for "text" markers.
            title: a name for the job list.
            force: solve again even when results exist.

        Returns:
            ``metrics`` (items checked and failing, errors, warnings),
            ``findings`` (id, severity, item, check, detail, at_mils, where),
            ``report`` (the HTML file), ``marked`` when marking ran,
            ``snapshot`` with ``stale``, ``job`` and ``folder``.
        """
        if mark not in ("none", "highlight", "text"):
            return {"ok": False, "error": "mark is none, highlight or text"}
        try:
            rec = await _snapshot_from_args(snapshot, expect_file, board_json_path,
                                            stackup_json_path)
            if not rec.get("has_stackup"):
                return {"ok": False, "error": "the snapshot has no stackup; take it again "
                        "with stackup_json_path or with the board open in Altium",
                        "snapshot": rec["id"]}
            spec: dict[str, Any] = dict(options or {})
            if targets:
                spec["targets"] = list(targets)
            out = await asyncio.to_thread(
                _run, "board-si", ["eda-agent"], spec,
                {"board": rec["board"], "stackup": rec["stackup"]},
                title or "board SI", bool(force))
            res = (out.get("results") or {}).get("eda-agent") or {}
            findings = (res.get("figures") or {}).get("findings", [])
            folder = out.get("folder") or ""
            reply: dict[str, Any] = {
                "ok": bool(res), "job": out.get("job"), "folder": folder,
                "snapshot": {"id": rec["id"], "stale": rec.get("stale"), "why": rec.get("why")},
                "metrics": res.get("metrics", {}), "verdicts": res.get("verdicts", []),
                "findings": findings, "notes": res.get("notes", []),
                "files": res.get("files", {})}
            if out.get("errors"):
                reply["errors"] = out["errors"]
            if res:
                from ..simulation import jobs as _jobs
                work = _jobs.get(out["job"]).work_folder("eda-agent")
                reply["report"] = str(work / "report.html")
            if mark != "none" and findings:
                if mark == "text" and rec.get("stale"):
                    reply["marked"] = {"error": "no markers written: the board changed since "
                                                "the snapshot"}
                    return reply
                if mark == "text" and rec.get("stale") is False:
                    why = await unsaved_edit(rec)
                    if why:
                        reply["marked"] = {"error": f"no markers written: {why}; take a new "
                                                    "snapshot and run it again"}
                        return reply
                from ..layout.model import LayoutBoard
                board = await asyncio.to_thread(LayoutBoard.load, rec["board"])
                nets = {f"pair {d.name}": [d.positive, d.negative] for d in board.diff_pairs}
                reply["marked"] = await mark_findings(findings, nets, mark, marker_layer)
            return reply
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)


def _any_ports(path):
    import numpy as np
    from ..emc.touchstone import read_touchstone
    data = read_touchstone(path)
    f = np.asarray(data["freq_hz"], dtype=float)
    s = np.asarray(data["s"], dtype=complex).reshape(len(f), data["ports"], data["ports"])
    return f, s, float(data["z0"])
