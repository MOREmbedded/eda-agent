# SPDX-License-Identifier: Apache-2.0
"""Every published design rule of one fab build, and the fabs side by side.

``fab_library.fab_profile`` gives the seven minimums a :class:`FabProfile`
carries. A fab publishes far more than that: board-edge clearance, mask
expansion, silk to pad, hole tolerances, aspect ratio. This module reads
the same cited files for ALL of them, for one build (fab, tier, copper
layers, outer copper weight), on the same rules as the library:

- every value keeps the fab's quote, page and reading date;
- a figure the fab does not publish stays missing, with its reason;
- nothing is guessed, and nothing is merged across tiers or fabs.

IPC class. ``ipc_class=2`` or ``3`` asks for the class floors on top of
the fab's minimums: each rule becomes the stricter of the two and says
which one won. IPC values are licensed, so none ship in this repository:
the floors are read from the active profile
(``<profile dir>/standards/ipc_class_floors.json``), each citing its
clause. Without that file a class is REFUSED: a class 3 rule set built
from fab minimums alone would look like a class 3 rule set and not be
one. Note that IPC classes mostly set rings, hole wall, hole to copper
and aspect ratio; track, gap, mask and silkscreen stay the fab's.

All lengths at the API are MILS. Pure offline.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Optional

from . import fab_library as _fl

#: Rule key -> what it is, and which way is stricter.
#: kind "min": a floor (larger is stricter); "max": a ceiling (smaller is
#: stricter); "info": a text or range figure carried as published.
RULE_KEYS: dict[str, dict[str, str]] = {
    "min_track": {"kind": "min", "label": "Minimum track width",
                  "altium": "Width"},
    "min_gap": {"kind": "min", "label": "Minimum copper to copper gap",
                "altium": "Clearance"},
    "min_drill": {"kind": "min", "label": "Minimum mechanical drill",
                  "altium": "HoleSize"},
    "min_laser_drill": {"kind": "min", "label": "Minimum laser drill (microvia)",
                        "altium": "HoleSize"},
    "min_npth_drill": {"kind": "min", "label": "Minimum non-plated hole",
                       "altium": "HoleSize"},
    "min_annular_ring": {"kind": "min", "label": "Minimum via annular ring",
                         "altium": "RoutingVias"},
    "pth_annular_ring": {"kind": "min", "label": "Minimum component (PTH) annular ring",
                         "altium": "PadRing"},
    "min_hole_to_hole": {"kind": "min", "label": "Minimum hole to hole",
                         "altium": "HoleToHoleClearance"},
    "hole_to_copper_inner": {"kind": "min", "label": "Inner layer hole to copper",
                             "altium": "Clearance"},
    "inner_via_hole_to_copper": {"kind": "min", "label": "Inner layer via hole to copper",
                                 "altium": "Clearance"},
    "min_mask_sliver": {"kind": "min", "label": "Minimum solder mask dam",
                        "altium": "MinimumSolderMaskSliver"},
    "mask_expansion": {"kind": "info", "label": "Solder mask expansion",
                       "altium": "SolderMaskExpansion"},
    "min_silk_width": {"kind": "min", "label": "Minimum silkscreen line width",
                       "altium": "SilkscreenLineWidth"},
    "min_silk_height": {"kind": "min", "label": "Minimum silkscreen text height",
                        "altium": "SilkscreenTextHeight"},
    "silk_to_pad": {"kind": "min", "label": "Silkscreen to pad clearance",
                    "altium": "SilkToSolderMaskClearance"},
    "copper_to_edge": {"kind": "min", "label": "Copper to board edge",
                       "altium": "BoardOutlineClearance"},
    "copper_to_vcut": {"kind": "min", "label": "Copper to V-score line",
                       "altium": "BoardOutlineClearance"},
    "max_aspect_ratio": {"kind": "max", "label": "Maximum drill aspect ratio"},
    "min_bga_pitch": {"kind": "min", "label": "Minimum BGA pitch"},
    "board_thickness": {"kind": "info", "label": "Board thickness range"},
    "copper_weights": {"kind": "info", "label": "Copper weights"},
    "pth_hole_tolerance": {"kind": "info", "label": "Plated hole tolerance"},
    "npth_hole_tolerance": {"kind": "info", "label": "Non-plated hole tolerance"},
    "outline_tolerance": {"kind": "info", "label": "Outline tolerance"},
    "max_board_size": {"kind": "info", "label": "Maximum board size"},
    "via_in_pad": {"kind": "info", "label": "Via in pad"},
    "impedance_tolerance": {"kind": "info", "label": "Impedance tolerance"},
    "castellated_holes": {"kind": "info", "label": "Castellated holes"},
    "surface_finishes": {"kind": "info", "label": "Surface finishes"},
    "ipc_class": {"kind": "info", "label": "IPC class built to"},
    "copper_layer_counts": {"kind": "info", "label": "Copper layer counts"},
}

IPC_FLOORS_FILE = Path("standards") / "ipc_class_floors.json"


def _ipc_catalogue() -> Optional[Path]:
    """The active profile's class-floor file, if there is a profile."""
    from ..profile import get_active_profile
    active = get_active_profile()
    return None if active is None else Path(active.directory) / IPC_FLOORS_FILE


def _pick(options: list[dict], copper_layers: int, outer_oz: float) -> tuple[
        Optional[dict], Optional[str]]:
    """The first option that fits the build, or why none does.

    An option for inner layers only is skipped when an outer (or
    unqualified) one fits: the board's rule is set by its outer layers
    unless the key is about the inner ones."""
    fitting = [f for f in options
               if _fl._matches(f.get("when", {}), copper_layers, outer_oz)]
    side_ok = [f for f in fitting if f.get("when", {}).get("layer_side") != "inner"]
    fitting = side_ok or fitting
    for f in fitting:
        if "unpublished" not in f:
            return f, None
    why = next((f["unpublished"] for f in fitting if "unpublished" in f), None)
    return None, why or f"not published for {copper_layers} layers at {outer_oz:g} oz"


def _value(f: dict) -> dict:
    mils = _fl._mils(f)
    if mils is not None:
        return {"mils": mils}
    for k in ("counts", "value"):
        if k in f:
            return {k: f[k]}
    return {}


def fab_rules(name: str, *, copper_layers: int, outer_oz: float = 1.0,
              tier: str = "standard", ipc_class: Optional[int] = None,
              ipc_path: Optional[Path] = None,
              today: Optional[_dt.date] = None) -> dict[str, Any]:
    """All published rules for one build at one fab, each cited.

    Returns ``{"ok": True, "vendor", "tier", "build", "rules": {key:
    {label, kind, mils|value|counts, quote, url}}, "missing": [{key,
    reason}], "age"}``. With ``ipc_class`` each rule also carries the
    class floor and which of the two governs (see the module note).
    """
    got = _fl.vendor(name, today)
    if not got["ok"]:
        return got
    if tier not in got["tiers"]:
        return {"ok": False, "reason": f"{got['vendor']} has no tier {tier!r}",
                "tiers": sorted(got["tiers"])}
    figs = got["tiers"][tier]
    counts = (figs.get("copper_layer_counts") or [{}])[0].get("counts")
    if counts and copper_layers not in counts:
        return {"ok": False, "reason": f"{got['vendor']} {tier} does not list "
                f"{copper_layers} copper layers", "copper_layer_counts": counts}

    ipc = None
    if ipc_class is not None:
        ipc = ipc_floors(ipc_class, ipc_path)
        if not ipc["ok"]:
            return ipc

    url = got["source"]["url"]
    rules: dict[str, dict] = {}
    missing: list[dict] = []
    for key in sorted(set(figs) | set(RULE_KEYS)):
        meta = RULE_KEYS.get(key, {"kind": "info", "label": key})
        options = figs.get(key)
        if not options:
            missing.append({"key": key, "reason": "not in the stored data"})
            continue
        f, why = _pick(options, copper_layers, outer_oz)
        if f is None:
            missing.append({"key": key, "reason": why})
            continue
        entry = {"label": meta["label"], "kind": meta["kind"], **_value(f),
                 "quote": f.get("quote", ""), "url": f.get("url", url)}
        for extra in ("note", "derived", "when"):
            if extra in f:
                entry[extra] = f[extra]
        if "altium" in meta:
            entry["altium_rule"] = meta["altium"]
        if ipc is not None:
            _apply_floor(entry, ipc["floors"].get(key))
        rules[key] = entry
    out = {"ok": True, "vendor": got["vendor"], "tier": tier,
           "build": {"copper_layers": copper_layers, "outer_oz": outer_oz},
           "rules": rules, "missing": missing, "age": got["age"],
           "source": got["source"]}
    if ipc is not None:
        out["ipc"] = {"class": ipc_class, "source": ipc["source"]}
    return out


def _apply_floor(entry: dict, floor: Optional[dict]) -> None:
    if floor is None or "mils" not in entry or "mils" not in floor:
        return
    fab, cls = entry["mils"], floor["mils"]
    stricter_is_cls = cls > fab if entry["kind"] == "min" else cls < fab
    entry["ipc_floor"] = floor
    entry["governs"] = "ipc" if stricter_is_cls else "fab"
    entry["design_mils"] = cls if stricter_is_cls else fab


def ipc_floors(ipc_class: int, path: Optional[Path] = None) -> dict[str, Any]:
    """The IPC class floors per rule key, each citing its clause.

    Read from ``path`` or the active profile; refused when there is no
    such file: the numbers must come from the standard, not memory."""
    if ipc_class not in (1, 2, 3):
        return {"ok": False, "reason": f"IPC class must be 1, 2 or 3, not {ipc_class!r}"}
    path = path or _ipc_catalogue()
    if path is None or not path.is_file():
        return {"ok": False, "reason": (
            "no IPC class floors are installed for the active profile "
            f"({path or '<no active profile>/' + str(IPC_FLOORS_FILE)}); "
            "class floors are refused rather than guessed. Use fab minimums "
            "alone (ipc_class=None) and check the class requirements against "
            "the standard.")}
    data = json.loads(path.read_text(encoding="utf-8"))
    floors = data.get("classes", {}).get(str(ipc_class))
    if not floors:
        return {"ok": False, "reason": f"the IPC catalogue has no class {ipc_class}"}
    return {"ok": True, "floors": floors, "source": data.get("source", {})}


def compare(keys: Optional[list[str]] = None, *, copper_layers: int = 2,
            outer_oz: float = 1.0, today: Optional[_dt.date] = None) -> dict:
    """Every fab and tier side by side for one build.

    Rows are vendor/tier pairs that list the copper count; cells are the
    published value in mils (or as published) with the quote, or the
    reason it is missing."""
    keys = keys or ["min_track", "min_gap", "min_drill", "min_annular_ring",
                    "min_hole_to_hole", "min_mask_sliver", "copper_to_edge"]
    unknown = [k for k in keys if k not in RULE_KEYS]
    if unknown:
        return {"ok": False, "reason": f"unknown rule keys {unknown}",
                "keys": sorted(RULE_KEYS)}
    rows = []
    for v in _fl.list_vendors(today):
        for tier in v["tiers"]:
            got = fab_rules(v["vendor"], copper_layers=copper_layers,
                            outer_oz=outer_oz, tier=tier, today=today)
            if not got["ok"]:
                continue
            cells = {}
            for k in keys:
                if k in got["rules"]:
                    r = got["rules"][k]
                    cells[k] = {x: r[x] for x in ("mils", "value", "counts", "quote")
                                if x in r}
                else:
                    cells[k] = {"missing": next(
                        (m["reason"] for m in got["missing"] if m["key"] == k), "")}
            rows.append({"vendor": v["vendor"], "tier": tier,
                         "stale": v["stale"], "retrieved": v["retrieved"],
                         "cells": cells})
    return {"ok": True, "build": {"copper_layers": copper_layers,
                                  "outer_oz": outer_oz},
            "keys": keys, "rows": rows}


__all__ = ["RULE_KEYS", "fab_rules", "ipc_floors", "compare"]
