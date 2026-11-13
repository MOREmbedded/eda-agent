# SPDX-License-Identifier: Apache-2.0
"""Signal integrity: transmission-line models and the stackup planner.

``si_line_model`` gives a trace's or a pair's wideband model (impedances,
delay, loss, crosstalk, Touchstone file) from its numbers or from a layer
of the board's stackup. ``si_stackup_plan`` gives every signal layer's
width for a single-ended target and pair geometry for differential
targets, can compare them with Altium's impedance profiles and, when
asked, write them as profiles. Both run as simulation jobs, so the
answers are kept and show on the dashboard's Simulation page.
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
                    st = await get_bridge().send_command_async("pcb.get_layer_stackup", {})
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
                st = await bridge.send_command_async("pcb.get_layer_stackup", {})
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
