# SPDX-License-Identifier: Apache-2.0
"""SPICE model library tools: index, find, pin-map and attach vendor models.

The flow, for a part that needs a manufacturer model:

1. ``sim_scan_models`` indexes the model files and LTspice symbols on this
   computer (the workspace's own model folders, EDA_AGENT_SPICE_MODEL_DIRS
   and LTspice's library). The index is kept per design workspace.
2. ``sim_find_schematic_models`` (live) lists, for every part on the
   sheet that needs or has a model, the best candidates by MPN.
   ``sim_find_models`` does the same for a typed query, offline.
3. ``sim_get_pin_map`` (live) shows which symbol pin each model node
   would connect to, matched by NAME. ``sim_set_pin_map`` saves a manual
   map for a model whose names do not match.
4. ``sim_attach_library_model`` (live) links the model to the part.
5. ``eng_ltspice_deck_from_schematic`` then emits the part with the
   model's name, its node order and an ``.include`` of its file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..sim import model_library as lib
from ..sim import pin_map

#: Element letters whose node order depends on the device, so a model's
#: node list or standard pin names are needed to emit them correctly.
MODEL_PREFIXES = frozenset("XDQMJ")


# ---------------------------------------------------------------- index access
def load_index(workspace: Optional[Path] = None) -> dict[str, Any]:
    return lib.load_json(lib.data_dir(workspace) / lib.INDEX_FILE, {})


def load_pin_maps(workspace: Optional[Path] = None) -> dict[str, Any]:
    data = lib.load_json(lib.data_dir(workspace) / lib.PIN_MAPS_FILE, {})
    return data if isinstance(data, dict) else {}


def scan_models(folders: Optional[list[str]] = None,
                workspace: Optional[Path] = None) -> dict[str, Any]:
    roots = [Path(f) for f in folders] if folders else lib.default_roots(workspace)
    missing = [str(r) for r in roots if not Path(r).is_dir()]
    if not any(Path(r).is_dir() for r in roots):
        # Nothing to index: write nothing, so a call made by mistake (or by
        # a test) never leaves an empty index where a real one belongs.
        return {"index_file": "", "roots": [], "missing_folders": missing, "files": 0,
                "models": 0, "symbols": 0,
                "hint": "no model folder was found. Put vendor models in a 'models' "
                        "folder of the workspace, list folders in "
                        "EDA_AGENT_SPICE_MODEL_DIRS, or pass folders."}
    previous = load_index(workspace)
    index = lib.scan(roots, previous)
    target = lib.data_dir(workspace) / lib.INDEX_FILE
    lib.save_json(target, index)
    return {"index_file": str(target), "roots": index["roots"],
            "missing_folders": missing, **index["stats"],
}


def model_nodes(index: dict[str, Any], name: str) -> dict[str, Any]:
    """Node names for a model, preferring an LTspice symbol's pin names
    (they are what a person reads) and falling back to the .SUBCKT header.
    """
    info = lib.resolve(index, name)
    subckts = [d for d in info["definitions"] if d.get("kind") == "subckt"]
    syms = [s for s in info["symbols"] if s.get("nodes")]
    header = subckts[0]["nodes"] if subckts else []
    nodes, source = [], ""
    if syms and (not header or len(syms[0]["nodes"]) == len(header)):
        nodes, source = list(syms[0]["nodes"]), "ltspice_symbol"
    elif header:
        nodes, source = list(header), "subckt_header"
    kind = "subckt" if subckts or syms else (
        info["definitions"][0]["kind"] if info["definitions"] else "")
    return {"name": name, "kind": kind, "nodes": nodes, "node_source": source,
            "file": info["model_file"], "candidate_files": info["candidate_files"],
            "subckt_header": header,
            "device": next((d.get("device", "") for d in info["definitions"]
                            if d.get("kind") == "model"), "")}


def models_from_links(component_models: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Model name and file per designator, from generic.get_component_models.

    Takes the SIM implementation when there is one, and any datafile link
    of kind SimModel (sim_attach_model adds one) when there is not.
    """
    out: dict[str, dict[str, str]] = {}
    for comp in (component_models or {}).get("components") or []:
        des = str(comp.get("designator", ""))
        found: dict[str, str] = {}
        for m in comp.get("models") or []:
            links = m.get("datafile_links") or []
            if str(m.get("model_type", "")).upper() == "SIM":
                found.setdefault("name", str(m.get("model_name", "")))
                for link in links:
                    if link.get("location"):
                        found.setdefault("file", str(link["location"]))
            for link in links:
                if str(link.get("file_kind", "")).lower() == "simmodel":
                    found.setdefault("name", str(link.get("entity_name", "")))
                    if link.get("location"):
                        found.setdefault("file", str(link["location"]))
        if des and found.get("name"):
            out[des] = found
    return out


async def collect_models(bridge, components: list[dict[str, Any]],
                         workspace: Optional[Path] = None) -> tuple[dict[str, dict], list[dict]]:
    """Per designator, the model the deck builder should use.

    Reads the live model links only for parts whose letter makes node
    order matter, then completes each with the index (node names, file)
    and any saved pin map. Returns (models, notes).
    """
    wanted = [c for c in components
              if (str(c.get("spice_prefix") or c.get("suggested_prefix") or "")[:1].upper()
                  in MODEL_PREFIXES)]
    if not wanted:
        return {}, []
    notes: list[dict] = []
    links: dict[str, dict[str, str]] = {}
    try:
        reply = await bridge.send_command_async(
            "generic.get_component_models", {"limit": "5000"})
        links = models_from_links(reply if isinstance(reply, dict) else {})
    except Exception as exc:                            # noqa: BLE001
        notes.append({"note": f"model links could not be read: {exc}"})
    index = load_index(workspace)
    overrides = load_pin_maps(workspace)
    models: dict[str, dict] = {}
    for c in wanted:
        des = str(c.get("designator", ""))
        name = (links.get(des) or {}).get("name") or str(c.get("sim_model") or "")
        if not name or name == "(unnamed)":
            continue
        entry: dict[str, Any] = {"name": name}
        file = (links.get(des) or {}).get("file", "")
        info = model_nodes(index, name) if index else {}
        if info.get("nodes"):
            entry["nodes"] = info["nodes"]
        if file and Path(file).is_file():
            entry["file"] = file
        elif info.get("file"):
            entry["file"] = info["file"]
            if file:
                notes.append({"designator": des,
                              "note": f"linked file {file} not found; using {info['file']} "
                                      "from the model index"})
        elif file:
            notes.append({"designator": des, "note": f"linked model file {file} not found"})
        saved = pin_map.lookup_override(overrides, name, str(c.get("lib_ref", "")))
        if saved:
            entry["override"] = saved
        if not entry.get("nodes") and str(c.get("spice_prefix") or "")[:1].upper() == "X":
            notes.append({"designator": des,
                          "note": f"model {name} is not in the model index, so its node "
                                  "order is unknown; run sim_scan_models"})
        models[des] = entry
    return models, notes


def _numbers(pins: list) -> list[str]:
    return [str(p).strip() for p in pins if str(p).strip()]


def register_spice_model_tools(mcp):
    """Register the SPICE model library tools."""
    from ..bridge import get_bridge

    @mcp.tool()
    async def sim_scan_models(folders: Optional[list[str]] = None) -> dict[str, Any]:
        """Index the SPICE models and LTspice symbols on this computer.

        Reads every .lib/.sub/.mod/.cir/.ckt/.mdl/... file (each .SUBCKT
        with its node list, each .MODEL with its device type) and every
        LTspice .asy symbol (model name, model file, pins in SpiceOrder).
        Unchanged files are not read again, so a rescan is quick.

        Args:
            folders: folders to scan. Empty scans the workspace's
                ``models``/``spice``/``spice_models``/``Simulation``
                folders, EDA_AGENT_SPICE_MODEL_DIRS and LTspice's own
                library (sub, cmp, sym).

        Returns:
            index_file (kept in the design workspace, never the repo),
            roots, files, models, symbols, read, reused, skipped.
        """
        try:
            return {"ok": True, **scan_models(folders)}
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": str(exc)}

    @mcp.tool()
    async def sim_find_models(query: str, limit: int = 10) -> dict[str, Any]:
        """Find SPICE models by name or manufacturer part number.

        Matches ignore case, punctuation and package/reel tails, so
        ``LT3080EDD#TRPBF`` finds ``LT3080``. Run sim_scan_models first.

        Returns:
            matches [{score, name, kind, file, nodes, description, ...}],
            best first. kind is subckt, model or symbol (an LTspice symbol
            naming a model, which may be encrypted).
        """
        index = load_index()
        if not index:
            return {"ok": False, "reason": "no model index yet; run sim_scan_models"}
        matches = lib.find(index, query, limit=max(1, min(int(limit), 50)))
        return {"ok": True, "query": query, "matches": matches, "count": len(matches)}

    @mcp.tool()
    async def sim_set_pin_map(model_name: str, pins: list[str], lib_ref: str = "",
                        note: str = "") -> dict[str, Any]:
        """Save which symbol pins connect to a model's nodes, in node order.

        For a model whose node names do not match the symbol's pin names.
        One entry per model node, in the order of the .SUBCKT header:
        ``["3", "1", "2"]`` means node 1 is pin 3. Join pins that share a
        node with '+': ``"4+9"``. Kept per design workspace; with lib_ref
        the map applies to that symbol only.

        Returns:
            saved key, the map, the model's nodes when it is indexed.
        """
        nums = _numbers(pins)
        if not model_name.strip() or not nums:
            return {"ok": False, "reason": "model_name and pins are required"}
        index = load_index()
        info = model_nodes(index, model_name) if index else {}
        if info.get("nodes") and len(info["nodes"]) != len(nums):
            return {"ok": False, "reason": f"{model_name} has {len(info['nodes'])} nodes "
                                           f"({info['nodes']}); {len(nums)} pins given"}
        store = load_pin_maps()
        key = pin_map.override_key(model_name, lib_ref)
        store[key] = {"pins": nums, **({"note": note} if note else {})}
        target = lib.data_dir() / lib.PIN_MAPS_FILE
        lib.save_json(target, store)
        return {"ok": True, "key": key, "pins": nums, "file": str(target),
                "model_nodes": info.get("nodes", [])}

    @mcp.tool()
    async def sim_find_schematic_models(limit_per_part: int = 3) -> dict[str, Any]:
        """For each part on the sheet needing a vendor model, the best candidates.

        Reads sim_get_readiness's ``needs_file`` parts and searches the
        model index by manufacturer part number, then comment, then
        library reference. Nothing is attached.

        Returns:
            parts [{designator, mpn, candidates}], not_found.
        """
        index = load_index()
        if not index:
            return {"ok": False, "reason": "no model index yet; run sim_scan_models"}
        readiness = await get_bridge().send_command_async(
            "generic.get_simulation_readiness", {})
        parts, not_found = [], []
        for c in (readiness or {}).get("needs_file") or []:
            hits: list[dict] = []
            for q in (c.get("manufacturer_part"), c.get("comment"), c.get("lib_ref")):
                if q:
                    hits = lib.find(index, str(q), limit=max(1, min(int(limit_per_part), 10)))
                    if hits:
                        break
            row = {"designator": c.get("designator"), "mpn": c.get("manufacturer_part", ""),
                   "comment": c.get("comment", ""),
                   "candidates": [{k: h.get(k) for k in ("score", "name", "kind", "file",
                                                         "model_file", "nodes", "description")
                                   if h.get(k)} for h in hits]}
            (parts if hits else not_found).append(row)
        return {"ok": True, "parts": parts, "not_found": not_found,
                "next_step": "sim_get_pin_map(designator, model_name) for each choice"}

    async def _pins(designator: str) -> list[dict]:
        conn = await get_bridge().send_command_async(
            "project.get_connectivity_batch", {"designators": designator})
        for c in (conn or {}).get("components") or []:
            if str(c.get("designator", "")) == designator:
                return list(c.get("pins") or [])
        return []

    async def _plan(designator: str, model_name: str, lib_ref: str = "") -> dict[str, Any]:
        index = load_index()
        if not index:
            return {"ok": False, "reason": "no model index yet; run sim_scan_models"}
        info = model_nodes(index, model_name)
        if not info["kind"]:
            return {"ok": False, "reason": f"{model_name} is not in the model index; "
                                           "sim_find_models lists what is"}
        pins = await _pins(designator)
        if not pins:
            return {"ok": False, "reason": f"no pins read for {designator}"}
        if info["kind"] == "model":
            letter = {"D": "D", "NPN": "Q", "PNP": "Q", "NMOS": "M", "PMOS": "M",
                      "VDMOS": "M", "NJF": "J", "PJF": "J"}.get(info["device"], "")
            mapping = pin_map.standard_order(letter, pins) if letter else None
            return {"ok": bool(mapping), "model": info, "primitive": letter or "X",
                    "mapping": mapping or {"reason": "pin names do not say which "
                                           "terminal is which; check the order"},
                    "pins": pins}
        saved = pin_map.lookup_override(load_pin_maps(), model_name, lib_ref)
        mapping = pin_map.propose(info["nodes"], pins, override=saved)
        return {"ok": bool(mapping.get("ok")), "model": info, "primitive": "X",
                "mapping": mapping, "pins": pins}

    @mcp.tool()
    async def sim_get_pin_map(designator: str, model_name: str,
                              lib_ref: str = "") -> dict[str, Any]:
        """Show which of a part's pins each model node would connect to.

        Matches by pin NAME (exact, then a short alias list such as
        VIN=IN, GND=VSS), never silently by pin number; a saved
        sim_set_pin_map entry wins. Read-only.

        Returns:
            ok, model (name, nodes, node_source, file), mapping (method,
            nodes [{index, node, pins, how}], unmatched_nodes,
            unused_pins, needs_review), pins.
        """
        return await _plan(designator, model_name, lib_ref)

    @mcp.tool()
    async def sim_attach_library_model(designator: str, model_name: str,
                                       lib_ref: str = "",
                                       allow_review: bool = False) -> dict[str, Any]:
        """Link an indexed vendor model to a part, after checking its pins.

        Looks the model up in the index (its file, its kind), checks the
        pin map (as sim_get_pin_map) and only then writes SpicePrefix,
        SpiceModel, SimulationFile and a SimModel datafile link to the
        part (as sim_attach_model). Refuses when the pins cannot be
        matched; a map that needed aliases or pin-number order also needs
        ``allow_review=True``, once you have checked it.

        The pin map is what eng_ltspice_deck_from_schematic uses. Altium's
        own simulator keeps its own pin mapping in the model link, which
        this does not set.
        """
        plan = await _plan(designator, model_name, lib_ref)
        if not plan.get("ok"):
            return {**plan, "attached": False}
        mapping = plan["mapping"]
        if mapping.get("needs_review") and not allow_review:
            return {**plan, "ok": False, "attached": False,
                    "reason": f"the pin map used {mapping.get('method')} matching and "
                              "needs a check; confirm it and pass allow_review=True, or "
                              "save the right one with sim_set_pin_map"}
        file = plan["model"].get("file", "")
        if not file:
            return {**plan, "ok": False, "attached": False,
                    "reason": "the model's file is not unambiguous in the index: "
                              f"{plan['model'].get('candidate_files')}"}
        reply = await get_bridge().send_command_async(
            "generic.attach_spice_model",
            {"designator": designator, "file_path": file, "model_name": model_name,
             "primitive": plan["primitive"]})
        ok = isinstance(reply, dict) and bool(reply.get("success"))
        return {"ok": ok, "attached": ok, "designator": designator,
                "model_name": model_name, "file": file, "primitive": plan["primitive"],
                "mapping": mapping, "bridge": reply}
