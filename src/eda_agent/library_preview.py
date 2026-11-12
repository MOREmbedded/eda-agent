# SPDX-License-Identifier: Apache-2.0
"""A part's symbol and footprint, drawn before the part is placed.

One read in Altium per part: ``library.get_symbol_geometry`` loads the
symbol the way placement would (a Workspace part by its Design Item ID, a
file part by library and name), answers its graphics, pins and models,
and leaves nothing on a sheet. The footprint is then read from the
library its current footprint model names, with the same reads
``lib_render_footprint`` makes, and both are drawn here.

Used by the dashboard's part preview and by ``lib_preview_part``. The
search beside the preview reads the profile's scanned Altium 365 library
offline, so finding a part costs Altium nothing; only the preview asks.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

#: send(command, params, timeout_s) -> reply dict; raises on failure.
Send = Callable[[str, dict, float], Any]

_FOOTPRINT_TYPES = ("PCBLIB", "FOOTPRINT", "PCB3DLIB")


def footprint_link(models: list[dict]) -> dict[str, str]:
    """The current footprint model: {name, library, model}. ``library`` is
    the file its datafile link names, empty when it names none."""
    fps = [m for m in models or []
           if str(m.get("model_type", "")).upper() in ("PCBLIB", "FOOTPRINT")]
    cur = next((m for m in fps if m.get("is_current")), fps[0] if fps else None)
    if cur is None:
        return {"name": "", "library": "", "model": ""}
    links = cur.get("datafile_links") or []
    link = next((d for d in links if str(d.get("location", "")).strip()),
                links[0] if links else {})
    return {"name": str(link.get("entity_name") or cur.get("model_name") or ""),
            "library": str(link.get("location") or "").strip(),
            "model": str(cur.get("model_name") or "")}


def _is_file_library(path: str) -> bool:
    return bool(re.search(r"\.pcblib$", path.strip(), re.I))


def read_symbol(send: Send, *, design_item_id: str = "", vault: str = "",
                library_path: str = "", component_name: str = "",
                part: int = 1) -> dict[str, Any]:
    """The symbol read (``library.get_symbol_geometry``)."""
    params: dict[str, str] = {"part": str(max(1, int(part or 1)))}
    if design_item_id:
        params["design_item_id"] = design_item_id
        if vault:
            params["vault"] = vault
    else:
        params["component_name"] = component_name
        params["library_path"] = library_path
    return send("library.get_symbol_geometry", params, 60.0) or {}


def read_footprint(send: Send, name: str, library_path: str) -> tuple[Any, list[str]]:
    """(FootprintModel, notes) for one footprint, read from its library."""
    from .design.landpattern.live import model_from_live

    args = {"footprint_name": name, "library_path": library_path}
    pads = send("library.get_pad_geometry", args, 90.0) or {}
    prims = send("library.get_footprint_primitives", args, 90.0) or {}
    notes: list[str] = []
    mech = None
    try:
        mech = (send("library.get_mech_layers",
                     {"library_path": str(prims.get("library_path") or library_path)},
                     30.0) or {}).get("layers") or []
    except Exception as exc:                              # noqa: BLE001
        notes.append(f"layer kinds not read ({exc}); courtyard and assembly "
                     f"come from layer names only")
    model = model_from_live(pads, prims, mech)
    if not model.pads and not model.graphics:
        notes.append(f"{name} is empty: no pads and no primitives")
    return model, notes


def _pin_summary(read: dict) -> dict[str, Any]:
    pins = read.get("pins") or []
    kinds: dict[str, int] = {}
    for p in pins:
        kinds[p.get("electrical") or "passive"] = kinds.get(p.get("electrical") or "passive", 0) + 1
    return {"pins": len(pins), "hidden_pins": sum(1 for p in pins if p.get("hidden")),
            "by_type": kinds}


def preview_part(send: Send, *, design_item_id: str = "", vault: str = "",
                 library_path: str = "", component_name: str = "",
                 part: int = 1, footprint: bool = True) -> dict[str, Any]:
    """Both drawings of one part, and what was found on the way.

    Returns {ok, lib_ref, description, comment, source, part, part_count,
    symbol: {svg, pins, hidden_pins, by_type}, footprint: {ok, svg, name,
    library, pads, notes} or {ok: False, reason}, notes}. A symbol that
    does not read is {ok: False, reason}.
    """
    from .render.symbol_svg import render_symbol_svg

    try:
        sym = read_symbol(send, design_item_id=design_item_id, vault=vault,
                          library_path=library_path, component_name=component_name,
                          part=part)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "reason": f"the symbol did not read: {exc}"}
    out: dict[str, Any] = {
        "ok": True, "lib_ref": sym.get("lib_ref", ""), "comment": sym.get("comment", ""),
        "description": sym.get("description", ""), "source": sym.get("source", ""),
        "design_item_id": design_item_id, "part": sym.get("part", part),
        "part_count": sym.get("part_count", 1), "notes": [],
        "symbol": {"svg": render_symbol_svg(sym), **_pin_summary(sym),
                   "graphics": len(sym.get("primitives") or [])},
        "models": sym.get("models") or [],
    }
    if not (sym.get("primitives") or sym.get("pins")):
        out["notes"].append("the symbol read back with no graphics and no pins")
    if not footprint:
        return out
    link = footprint_link(sym.get("models") or [])
    fp: dict[str, Any] = {"ok": False, "name": link["name"], "library": link["library"]}
    if not link["name"]:
        fp["reason"] = "the part has no footprint model"
    elif not _is_file_library(link["library"]):
        fp["reason"] = (f"the footprint {link['name']} names no library file Altium can "
                        f"open here ({link['library'] or 'no location'})")
    else:
        try:
            from .design.landpattern.svg import render_svg
            model, notes = read_footprint(send, link["name"], link["library"])
            fp.update(ok=True, pads=len(model.pads), notes=notes,
                      svg=render_svg(model, notes=[f"{len(model.pads)} pads"]))
        except Exception as exc:                          # noqa: BLE001
            fp["reason"] = f"the footprint did not read: {exc}"
    out["footprint"] = fp
    return out


def preview_key(spec: dict) -> str:
    """One cache key per part asked for."""
    if spec.get("design_item_id"):
        return f"ws:{spec.get('vault', '')}:{spec['design_item_id']}:{spec.get('part', 1)}"
    return (f"lib:{str(spec.get('library_path', '')).lower()}:"
            f"{spec.get('component_name', '')}:{spec.get('part', 1)}")


def find_parts(query: str, *, limit: int = 40,
               databases: Optional[list[Path]] = None) -> dict[str, Any]:
    """Parts in the profile's scanned Altium 365 libraries matching
    ``query`` (name, comment, description, MPN), read offline."""
    from .altium365_library import base_item, known_databases, search_components

    query = (query or "").strip()
    dbs = known_databases() if databases is None else databases
    if not dbs:
        return {"ok": True, "query": query, "results": [], "databases": 0,
                "note": "no Altium 365 library has been scanned for this profile"}
    if len(query) < 2:
        return {"ok": True, "query": query, "results": [], "databases": len(dbs)}
    hits = search_components(dbs, query, limit=limit)
    rows = []
    for h in hits.get("results") or []:
        rows.append({k: h.get(k, "") for k in ("name", "comment", "description",
                                               "folder", "lifecycle")}
                    | {"item": base_item(h.get("name", "")),
                       "symbols": h.get("symbols") or [],
                       "footprints": h.get("footprints") or [],
                       "mpns": [m.get("mpn", "") for m in h.get("manufacturer_parts") or []
                                if m.get("mpn")],
                       "workspace": h.get("library_path", "")})
    return {"ok": True, "query": query, "results": rows, "databases": len(dbs),
            "matches": hits.get("matches", len(rows)),
            "truncated": bool(hits.get("truncated"))}


class PreviewView:
    """Previews read one at a time in the background, the last few kept."""

    KEEP = 24

    def __init__(self, send: Optional[Send] = None) -> None:
        self._send = send
        self._lock = threading.Lock()
        self._done: "OrderedDict[str, dict]" = OrderedDict()
        self.state: dict[str, Any] = {"state": "empty"}

    def send(self, command: str, params: dict, timeout: float) -> Any:
        if self._send is not None:
            return self._send(command, params, timeout)
        from .bridge.altium_bridge import following_front, get_bridge
        with following_front(background=False):
            return get_bridge().send_command(command, params, timeout=timeout)

    def ask(self, spec: dict) -> dict[str, Any]:
        """Start reading ``spec`` (design_item_id [+ vault] or library_path
        + component_name, part), or answer it from what is kept."""
        again = bool(spec.get("again"))
        spec = {k: str(spec.get(k) or "").strip() for k in
                ("design_item_id", "vault", "library_path", "component_name")} | {
            "part": max(1, int(spec.get("part") or 1))}
        if not spec["design_item_id"] and not spec["component_name"]:
            return {"ok": False, "reason": "name a part: design_item_id, or "
                                           "component_name with library_path"}
        key = preview_key(spec)
        with self._lock:
            if key in self._done and not again:
                self._done.move_to_end(key)
                self.state = self._done[key]
                return {"ok": True, "key": key, "cached": True}
            if self.state.get("state") == "reading":
                return {"ok": False, "reason": "already reading a part"}
            self.state = {"state": "reading", "key": key, "spec": spec,
                          "started": time.time()}
        threading.Thread(target=self._read, args=(key, spec), name="part-preview",
                         daemon=True).start()
        return {"ok": True, "key": key}

    def _read(self, key: str, spec: dict) -> None:
        started = time.time()
        try:
            got = preview_part(self.send, **spec)
        except Exception as exc:                          # noqa: BLE001
            got = {"ok": False, "reason": str(exc)}
        got.update(state="ready" if got.get("ok") else "failed", key=key, spec=spec,
                   seconds=round(time.time() - started, 1), read_at=time.time())
        with self._lock:
            if got.get("ok"):
                self._done[key] = got
                while len(self._done) > self.KEEP:
                    self._done.popitem(last=False)
            self.state = got

    def snapshot(self) -> dict[str, Any]:
        return self.state


_VIEW: Optional[PreviewView] = None


def get_preview_view() -> PreviewView:
    global _VIEW
    if _VIEW is None:
        _VIEW = PreviewView()
    return _VIEW
