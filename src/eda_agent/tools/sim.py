# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""SPICE simulation tools.

The simulation flow is three steps:

1. ``sim_get_readiness()`` audits every component on the
   active schematic and returns three buckets:

     - ``ready``          , has SpicePrefix; nothing to do.
     - ``needs_primitive``, R/L/C/V/I where just setting the prefix +
                             value makes it simulatable (no model file).
     - ``needs_file``     , an IC or active part that requires a
                             vendor SPICE model (.mdl / .ckt / .lib).

   The response also carries ``_spice_guidance`` with the hard rule:
   **vendor datasheets + vendor-published SPICE models only**. LLM-
   generated SPICE models are not trustworthy and must not be used.

2. ``sim_attach_primitives()``, for ``needs_primitive`` entries.
   One bulk call attaches the whole list; no file needed.

3. For ``needs_file`` entries: fetch the vendor model, save it locally,
   call ``sim_attach_model()`` pointing at that file.

Then ``sim_setup_analysis()`` sets the transient, AC and DC values,
``sim_run()`` runs Altium's mixed-signal simulator and returns each
wave's figures, and ``sim_get_results()`` returns the traces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge import get_bridge
from ..sim.sdf import SdfError, latest_charts, read_sdf, summarise
from ..sim.simcfg import (DEFAULT_NAME, SimCfgError, analysis_changes, apply,
                          new_simcfg, read_simcfg, write_simcfg)
from .bulk_hints import BulkHintTracker

#: The results document the last sim_run produced, so sim_get_results can
#: be called without repeating its path.
_last_results: dict[str, str] = {}


def _results(charts, waves=None, max_points=0, as_list=False):
    """Charts as plain data, optionally filtered to some waves."""
    keep = {w.strip().lower() for w in (waves or []) if w.strip()}
    out = []
    for chart in charts:
        rows = []
        for wave in chart.waves:
            if keep and wave.name.lower() not in keep:
                continue
            summary = summarise(wave, max_points=max_points)
            if max_points <= 0:
                summary.pop("samples", None)
                summary.pop("sample_columns", None)
            rows.append(summary)
        out.append({"analysis": chart.name, "waves": rows})
    if as_list:
        return out
    return {entry["analysis"]: entry["waves"] for entry in out}


SPICE_MODEL_RULES = [
    "SPICE models for ICs and active parts must come from the "
    "manufacturer's own product page or a trusted aggregator "
    "(SnapEDA, Ultra Librarian, Digi-Key model archive). Do NOT "
    "generate a .mdl or .ckt file from datasheet reasoning, the "
    "parameters that matter for convergence and small-signal "
    "behavior are not in any datasheet summary.",
    "Prefer the manufacturer page first. Most vendors host the model "
    "under '<product page> -> Design Resources -> Models' or a linked "
    "ZIP archive. Typical search: "
    "'<manufacturer> <part_number> SPICE model filetype:lib OR "
    "filetype:mdl OR filetype:cir'.",
    "A model file usually contains a .SUBCKT <name> declaration. The "
    "<name> is what goes into sim_attach_model's model_name "
    "parameter, not the file name.",
    "If a vendor SPICE model cannot be located after a real search, "
    "tell the user. Do not fabricate a substitute; running the sim "
    "with a made-up model gives confidently-wrong results.",
    "For passives (R, L, C) and ideal sources (V, I), no file is "
    "needed, sim_attach_primitives sets SpicePrefix + Value "
    "directly.",
]


def _suggest_vendor_search(entry: dict[str, Any]) -> dict[str, Any]:
    """Build a suggested-search hint from a needs_file entry."""
    mfr = str(entry.get("manufacturer") or "").strip()
    part = str(entry.get("manufacturer_part") or "").strip()
    comment = str(entry.get("comment") or "").strip()
    lib_ref = str(entry.get("lib_ref") or "").strip()

    search_part = part or comment or lib_ref
    search_mfr = mfr

    hints: dict[str, Any] = {
        "designator": entry.get("designator"),
        "search_part": search_part,
        "manufacturer": search_mfr,
    }
    if search_part:
        query_terms = [search_mfr, search_part] if search_mfr else [search_part]
        query = " ".join(query_terms).strip()
        hints["web_search_query"] = (
            f"{query} SPICE model filetype:lib OR filetype:mdl "
            f"OR filetype:cir OR filetype:sub"
        )
        hints["datasheet_query"] = f"{query} datasheet filetype:pdf"
    return hints


def _guidance_block(
    needs_file: list[dict[str, Any]],
    needs_primitive: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "spice_model_rules": SPICE_MODEL_RULES,
        "action_required": (
            "Pass all needs_primitive entries to "
            "sim_attach_primitives in one bulk call. For every entry in "
            "needs_file, fetch the vendor SPICE model (WebFetch / "
            "WebSearch using the suggested query below), save it to "
            "a local file, then call sim_attach_model with the "
            ".SUBCKT name from inside the file. Do NOT fabricate a "
            "SPICE model from datasheet reasoning, if the vendor "
            "doesn't publish one, flag it to the user and stop."
        ),
        "search_hints": [
            _suggest_vendor_search(e) for e in needs_file
        ],
        "primitive_hints": [
            {
                "designator": e.get("designator"),
                "suggested_prefix": e.get("suggested_prefix"),
                "suggested_value": e.get("suggested_value"),
            }
            for e in needs_primitive
        ],
    }


def register_sim_tools(mcp):
    """Register SPICE / simulation tools with the MCP server."""

    @mcp.tool()
    async def sim_get_readiness() -> dict[str, Any]:
        """Audit every component on the active schematic for SPICE readiness.

        Use this as step 1 of any simulation workflow. The response
        classifies each component into one of three buckets:

          - ``ready``          , SpicePrefix already set; nothing to do.
          - ``needs_primitive``, a passive (R/L/C) or source (V/I/D/Q)
                                  that just needs SpicePrefix + Value.
                                  Pass them all to
                                  sim_attach_primitives in one call.
          - ``needs_file``     , an IC or active part that requires a
                                  manufacturer-supplied .mdl / .ckt /
                                  .lib file. Call sim_attach_model
                                  after fetching the file from the
                                  vendor.

        CRITICAL, vendor SPICE models only:
        Never fabricate a SPICE model from datasheet reasoning. The
        parameters that matter for convergence, bias point, and
        small-signal behavior are not in any datasheet summary; a
        plausible-looking hand-rolled model produces confidently-wrong
        simulation results. The response's ``_spice_guidance`` block
        makes this rule explicit and gives you per-part search queries.

        Returns:
            Dict with:
              - ready, ready_count
              - needs_primitive, needs_primitive_count
                (each entry has designator, comment, suggested_prefix,
                 suggested_value)
              - needs_file, needs_file_count
                (each entry has designator, comment, lib_ref,
                 manufacturer, manufacturer_part)
              - _spice_guidance (rules + structured search_hints +
                primitive_hints)
        """
        bridge = get_bridge()
        result = await bridge.send_command_async(
            "generic.get_simulation_readiness", {}
        )
        if isinstance(result, dict):
            result["_spice_guidance"] = _guidance_block(
                result.get("needs_file") or [],
                result.get("needs_primitive") or [],
            )
        return result

    @mcp.tool()
    async def sim_attach_model(
        designator: str,
        file_path: str,
        model_name: str,
        primitive: str = "X",
    ) -> dict[str, Any]:
        """Attach an external SPICE model file (.mdl / .ckt / .lib) to a component.

        Use this after fetching a vendor SPICE model file from the
        manufacturer and saving it locally. The model_name parameter
        must be the exact ``.SUBCKT <name>`` declaration from inside
        the file, not the file name.

        Args:
            designator: Component reference (e.g. "U1").
            file_path: Full path to the locally-saved .mdl / .ckt / .lib.
            model_name: Subcircuit / model name declared inside the
                file. Open the file and look for ``.SUBCKT <name>``
                or ``.MODEL <name>``.
            primitive: SPICE prefix letter. Default "X" (subcircuit).
                Use "D"/"Q"/"M" when the file declares a .MODEL of
                that kind instead of a subcircuit.

        Returns:
            Dict with success, designator, file_path, model_name.
        """
        bridge = get_bridge()
        return await bridge.send_command_async(
            "generic.attach_spice_model",
            {
                "designator": designator,
                "file_path": file_path,
                "model_name": model_name,
                "primitive": primitive,
            },
        )

    @mcp.tool()
    async def sim_run(analysis: str = "") -> dict[str, Any]:
        """Run Altium's mixed-signal simulation of the focused project.

        Runs the analyses set up for the project: with sim_setup_analysis,
        or in Altium's Simulation Dashboard. A run started from a script
        performs an operating point, DC sweep, transient and AC together
        (MEASURED: the dashboard's on/off switches are not honoured).

        It succeeds only when the run produced its results document (a
        .sdf, opened in its own tab), and fails with SIM_NOT_RUN when
        nothing appeared. The results are saved and read back: the reply
        carries each analysis's waves with min, max, first and last, and
        sim_get_results gives the traces.

        Typical workflow:
            1. Activate one of the project's schematic sheets.
            2. Use sim_get_readiness + sim_attach_* to make sure every
               component has a SPICE model.
            3. Place probes on measurement nodes with sch_place_probe.
            4. Set the analyses with sim_setup_analysis.
            5. Call sim_run, then sim_get_results for the traces.

        Args:
            analysis: Free-form label echoed back in the response.
                Does not drive Altium, the project's setup does.

        Returns:
            Dict with success, ran, results_document (the .sdf path),
            results_saved, results (per analysis, per wave: unit, points,
            min, max, first, last; magnitude for an AC wave), analysis
            and note.
        """
        bridge = get_bridge()
        params: dict[str, Any] = {}
        if analysis:
            params["analysis"] = analysis
        reply = await bridge.send_command_async(
            "generic.run_simulation", params, timeout=600.0
        )
        if not (isinstance(reply, dict) and reply.get("ran")):
            return reply
        path = reply.get("results_document") or ""
        if path:
            _last_results["path"] = path
        if not reply.get("results_saved"):
            reply["results_note"] = (
                "the results document could not be saved, so its values "
                "were not read; they are open in Altium")
            return reply
        try:
            charts = latest_charts(read_sdf(path))
        except (OSError, SdfError) as exc:
            reply["results_note"] = f"the results file could not be read: {exc}"
            return reply
        reply["results"] = _results(charts, waves=None, max_points=0)
        return reply

    @mcp.tool()
    async def sim_setup_analysis(
        transient: dict | None = None,
        ac: dict | None = None,
        dc: dict | None = None,
        outputs: list[str] | None = None,
        project_path: str = "",
    ) -> dict[str, Any]:
        """Set the analyses sim_run will use, in the project's .simcfg.

        Values are SPICE values: a number in SI units, or a string with a
        SPICE suffix ("5u", "10n", "1k", "100meg").

        Args:
            transient: {"stop", "step", "start" (default 0),
                "max_step" (default step)}.
            ac: {"start", "stop", "points", "sweep": "dec" | "oct" | "lin"}.
                Points are per decade or octave, or in total for lin.
            dc: {"source" (a source designator such as "V1", or "Temp"),
                "start", "stop", "step"}.
            outputs: expressions to plot in each analysis set here, e.g.
                ["v(out)", "i(v1)"].
            project_path: the project; empty uses the focused one.

        WHAT A RUN DOES, MEASURED: a run started from a script performs
        an operating point, a DC sweep, a transient and an AC analysis
        together, whatever is switched off in the dashboard. The values
        set here are what those analyses use. An analysis you do not need
        still runs on its last settings; sim_get_results(analysis=...)
        returns just the one you want.

        Altium's own settings are saved first, so changes made in the
        Simulation Dashboard are kept, then the file is changed and
        reloaded into Altium. A project without a .simcfg gets one.

        Returns:
            Dict with simcfg (the file), set (the keys written), reloaded,
            and created when the file is new.
        """
        try:
            changes = analysis_changes(transient=transient, ac=ac, dc=dc,
                                       outputs=outputs)
        except SimCfgError as exc:
            return {"success": False, "error": "BAD_SETTINGS", "message": str(exc)}

        bridge = get_bridge()
        if not project_path:
            focused = await bridge.send_command_async("project.get_focused", {})
            project_path = (focused or {}).get("project_path", "") \
                if isinstance(focused, dict) else ""
        if not project_path or not project_path.lower().endswith(".prjpcb"):
            return {"success": False, "error": "NO_PROJECT",
                    "message": "no PCB project is focused; pass project_path"}

        docs = await bridge.send_command_async(
            "project.get_documents", {"project_path": project_path})
        if isinstance(docs, dict):
            docs = docs.get("result") or docs.get("documents") or []
        simcfg = next((d.get("file_path") for d in docs or []
                       if str(d.get("document_kind", "")).lower() == "simcfg"
                       or str(d.get("file_path", "")).lower().endswith(".simcfg")), "")

        created = False
        if simcfg:
            saved = await bridge.send_command_async(
                "generic.simcfg_document", {"action": "save", "file_path": simcfg})
            if isinstance(saved, dict) and saved.get("success") is False:
                return {"success": False, "error": "SIMCFG_NOT_SAVED",
                        "message": "Altium's settings could not be saved first, "
                                   "so changing the file would lose them",
                        "detail": saved}
            cfg = read_simcfg(simcfg)
        else:
            simcfg = str(Path(project_path).parent / DEFAULT_NAME)
            cfg = read_simcfg(simcfg) if Path(simcfg).exists() else new_simcfg()
            created = not Path(simcfg).exists()

        try:
            write_simcfg(apply(cfg, changes), simcfg)
        except OSError as exc:
            return {"success": False, "error": "NOT_WRITTEN",
                    "message": f"cannot write {simcfg}: {exc}"}

        if created or not any(str(d.get("file_path", "")).lower() == simcfg.lower()
                              for d in docs or []):
            added = await bridge.send_command_async(
                "project.add_document",
                {"document_path": simcfg, "project_path": project_path})
            if isinstance(added, dict) and added.get("success") is False:
                return {"success": False, "error": "NOT_ADDED", "simcfg": simcfg,
                        "message": "the settings file was written but could not "
                                   "be added to the project", "detail": added}
        reload = await bridge.send_command_async(
            "generic.simcfg_document", {"action": "reload", "file_path": simcfg})
        reloaded = bool(isinstance(reload, dict) and reload.get("done"))
        if isinstance(reload, dict) and reload.get("success") is False:
            return {"success": False, "error": "NOT_RELOADED", "simcfg": simcfg,
                    "message": "the file was changed but Altium did not reload "
                               "it, so the next run would use its old settings",
                    "detail": reload}
        return {"success": True, "simcfg": simcfg, "created": created,
                "reloaded": reloaded, "set": changes}

    @mcp.tool()
    async def sim_get_results(
        results_document: str = "",
        analysis: str = "",
        waves: list[str] | None = None,
        max_points: int = 50,
        all_runs: bool = False,
    ) -> dict[str, Any]:
        """Read a simulation's results: every wave, with sampled points.

        Reads the .sdf that sim_run saved (the last one by default). Each
        wave comes with its unit, point count, min/max/first/last over
        every point, and up to max_points samples evenly spaced from first
        to last. An AC wave reports magnitude and phase in degrees against
        frequency. The sweep variable (time, frequency, the swept source)
        is marked independent.

        Args:
            results_document: path of a .sdf; empty uses the last sim_run.
            analysis: one analysis by name ("Transient Analysis", "AC
                Analysis", "Operating Point", "DC Sweep", ...); empty
                returns all.
            waves: wave names to keep, e.g. ["v(out)", "i(v1)"]; empty
                keeps all.
            max_points: samples per wave; 0 returns statistics only.
            all_runs: a document run more than once holds every run; by
                default only the latest run of each analysis is returned.

        Returns:
            Dict with results_document and analyses: a list of
            {analysis, waves: [...]}, or an error naming what is missing.
        """
        path = results_document or _last_results.get("path", "")
        if not path:
            return {"success": False, "error": "NO_RESULTS",
                    "message": "no results_document given and no sim_run "
                               "has produced one in this session"}
        try:
            charts = read_sdf(path)
        except OSError as exc:
            return {"success": False, "error": "NOT_FOUND",
                    "message": f"cannot read {path}: {exc}"}
        except SdfError as exc:
            return {"success": False, "error": "NOT_RESULTS", "message": str(exc)}
        if not all_runs:
            charts = latest_charts(charts)
        if analysis:
            want = analysis.strip().lower()
            charts = [c for c in charts if c.name.lower() == want]
            if not charts:
                return {"success": False, "error": "NO_SUCH_ANALYSIS",
                        "message": f"no {analysis!r} in {path}",
                        "analyses": sorted({c.name for c in read_sdf(path)})}
        return {"success": True, "results_document": path,
                "analyses": _results(charts, waves=waves, max_points=max_points,
                                     as_list=True)}
