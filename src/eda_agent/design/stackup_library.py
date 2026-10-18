# SPDX-License-Identifier: Apache-2.0
"""Fab capability and stackup templates that ship with eda-agent.

``design/fab_profile.py`` deliberately ships ZERO capability numbers:
its docstring says every value must be transcribed from the fab's own
page and cited. This module is the agreed relaxation of that, not an
abandonment of it. Every number here was transcribed from a named page
on a named date, both travel with the data, and anything the vendor
does not publish is ``null`` with a reason rather than a plausible
guess. The policy existed so that no number could be invented or go
stale unnoticed; a dated, sourced, staleness-checked template keeps
that while saving everyone the transcription.

THREE THINGS THIS WILL NOT DO.

It will not fill in a figure the vendor does not publish. JLCPCB states
no rigid hole-to-hole minimum and no mask sliver; both come back null,
``missing`` lists them, and converting to a :class:`FabProfile` -- which
requires them -- REFUSES and names them. A design rule invented here
would be indistinguishable from one the fab published, which is the
whole hazard.

It will not pretend to be current. Each file carries the date it was
read, ``stale`` goes true past a year, and the warning travels in the
result rather than sitting in a README. Fabs change capabilities
without telling anyone.

It will not treat a fab's number for its own stack as a material
property. JLCPCB's er of 4.6 describes what JLCPCB presses; carrying it
to another fab's FR-4 is not supported and the laminate data says so.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..units import MM_PER_MIL

__all__ = [
    "STALE_AFTER_DAYS",
    "compare_stackup",
    "data_dir",
    "list_laminates",
    "list_vendors",
    "load_vendor",
    "vendor_fab_profile",
]

#: A transcription older than this is reported as stale. A year is
#: arbitrary and deliberately short: the cost of re-checking a
#: capabilities page is minutes, and the cost of a stale minimum track
#: is a scrapped panel.
STALE_AFTER_DAYS = 365

#: FabProfile's required minimums, in the spelling it uses. Named here
#: so the refusal below can list exactly what is missing instead of
#: letting pydantic report it one field at a time.
_REQUIRED_MINIMUMS = (
    "min_track_mils", "min_gap_mils", "min_drill_mils",
    "min_annular_ring_mils", "min_hole_to_hole_mils",
    "min_mask_sliver_mils", "min_silk_width_mils",
)


def data_dir() -> Path:
    """Where the bundled templates live."""
    return Path(__file__).resolve().parent.parent / "data" / "stackups"


def _mm_to_mils(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value) / MM_PER_MIL, 4)


def _age_days(retrieved: Any) -> Optional[int]:
    """Days since the transcription, or None if the date is unreadable.

    None rather than zero: an unreadable date is not a fresh one, and
    the caller reports it as unknown.
    """
    if not isinstance(retrieved, str):
        return None
    try:
        when = datetime.strptime(retrieved.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    return (date.today() - when).days


def list_vendors() -> list[dict[str, Any]]:
    """Every bundled vendor, with its provenance and how old it is."""
    out = []
    for path in sorted(data_dir().glob("*.json")):
        if path.stem == "laminates":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A broken bundled file is a packaging fault, not a reason to
            # hide every other vendor.
            continue
        age = _age_days(raw.get("retrieved"))
        out.append({
            "vendor": raw.get("vendor", path.stem),
            "display_name": raw.get("display_name", path.stem),
            "process": raw.get("process", ""),
            "source": raw.get("source", ""),
            "retrieved": raw.get("retrieved", ""),
            "age_days": age,
            "stale": age is None or age > STALE_AFTER_DAYS,
            "stackups": [s.get("name", "") for s in raw.get("stackups") or []],
            "missing": sorted(raw.get("not_published") or {}),
        })
    return out


def load_vendor(name: str) -> dict[str, Any]:
    """One vendor's capabilities and stackups, with every caveat attached.

    The reply always carries ``warnings``: staleness, the fields the
    vendor does not publish, and the reminder to confirm before
    ordering. They are part of the answer, not an appendix to it.
    """
    key = str(name or "").strip().lower()
    if not key:
        return {"ok": False, "reason": "no vendor named"}
    path = data_dir() / f"{key}.json"
    if not path.is_file():
        available = [v["vendor"] for v in list_vendors()]
        return {"ok": False, "reason":
                f"no bundled data for vendor {name!r}; have {available}. "
                f"A fab that is not here goes in your profile's "
                f"fab_profiles/ directory, where it travels with the "
                f"project."}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"{path.name} is unreadable: {exc}"}

    age = _age_days(raw.get("retrieved"))
    capabilities_mm = raw.get("capabilities") or {}
    capabilities_mils = {
        field.replace("_mm", "_mils"): _mm_to_mils(value)
        for field, value in capabilities_mm.items()
    }
    missing = sorted(raw.get("not_published") or {})

    warnings = [
        f"transcribed from {raw.get('source', 'an unnamed page')} on "
        f"{raw.get('retrieved', 'an unknown date')}; a fab changes its "
        f"capabilities without changing this file, so confirm before "
        f"ordering",
    ]
    if age is None:
        warnings.append("the transcription date is missing or unreadable, "
                        "so this cannot be checked for staleness")
    elif age > STALE_AFTER_DAYS:
        warnings.append(f"STALE: transcribed {age} days ago, past the "
                        f"{STALE_AFTER_DAYS}-day threshold")
    if missing:
        warnings.append(
            "the vendor does not publish " + ", ".join(missing)
            + "; these are null, which is not zero and not unlimited")

    return {
        "ok": True,
        "vendor": raw.get("vendor", key),
        "display_name": raw.get("display_name", key),
        "process": raw.get("process", ""),
        "source": raw.get("source", ""),
        "retrieved": raw.get("retrieved", ""),
        "age_days": age,
        "stale": age is None or age > STALE_AFTER_DAYS,
        "copper_layer_counts": raw.get("copper_layer_counts") or [],
        "board_thickness_mm": raw.get("board_thickness_mm") or [],
        "capabilities_mm": capabilities_mm,
        "capabilities_mils": capabilities_mils,
        "capability_notes": raw.get("capability_notes") or {},
        "not_published": raw.get("not_published") or {},
        "missing": missing,
        "stackups": raw.get("stackups") or [],
        "more_stackups": raw.get("more_stackups") or {},
        "warnings": warnings,
    }


def vendor_fab_profile(name: str,
                       fill: Optional[dict[str, float]] = None
                       ) -> dict[str, Any]:
    """Turn a bundled vendor into a :class:`FabProfile`, or refuse.

    FabProfile requires all seven minimums. Where a vendor does not
    publish one this REFUSES and names it, rather than defaulting:
    a value invented here would arrive looking exactly like one the fab
    published, and rule synthesis downstream cannot tell them apart.

    ``fill`` supplies the missing ones, in MILS, when you have asked the
    fab yourself. They are recorded in the profile's ``source`` string
    as yours rather than the vendor's, so the provenance survives.
    """
    loaded = load_vendor(name)
    if not loaded.get("ok"):
        return loaded

    values = {field: value for field, value in
              loaded["capabilities_mils"].items() if value is not None}
    supplied = {}
    for field, value in (fill or {}).items():
        if field not in _REQUIRED_MINIMUMS:
            return {"ok": False, "reason":
                    f"{field!r} is not one of the minimums a fab profile "
                    f"takes: {list(_REQUIRED_MINIMUMS)}"}
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or value <= 0:
            return {"ok": False, "reason":
                    f"{field} must be a positive number of mils, got "
                    f"{value!r}"}
        values[field] = float(value)
        supplied[field] = float(value)

    absent = [f for f in _REQUIRED_MINIMUMS if f not in values]
    if absent:
        return {
            "ok": False,
            "reason": (
                f"{loaded['display_name']} does not publish "
                f"{', '.join(absent)}, and a fab profile requires every "
                f"minimum. Ask the fab, then pass them in `fill` -- they "
                f"will be recorded as yours rather than theirs. Nothing "
                f"here will guess a design rule."),
            "missing": absent,
            "why": {f: loaded["not_published"].get(f.replace("_mils", "_mm"),
                                                   "not published")
                    for f in absent},
        }

    source = (f"{loaded['source']} (transcribed {loaded['retrieved']})")
    if supplied:
        source += (f"; {', '.join(sorted(supplied))} supplied by the "
                   f"caller, not by the vendor")

    profile: dict[str, Any] = {
        "name": loaded["display_name"],
        "source": source,
        "copper_layer_counts": loaded["copper_layer_counts"],
        **{field: values[field] for field in _REQUIRED_MINIMUMS},
    }
    stackups = [_stackup_to_profile(s) for s in loaded["stackups"]]
    if stackups:
        profile["stackups"] = stackups

    from .fab_profile import load_fab_profile
    validated = load_fab_profile(profile)
    if not validated.get("ok"):
        return {"ok": False, "reason":
                f"the bundled data for {name} does not satisfy FabProfile: "
                f"{validated.get('reason')}"}
    return {"ok": True, "profile": profile,
            "supplied_by_caller": sorted(supplied),
            "warnings": loaded["warnings"]}


def _stackup_to_profile(stackup: dict[str, Any]) -> dict[str, Any]:
    """One bundled stackup in FabProfile's shape (mils, not mm)."""
    layers = []
    for ply in stackup.get("layers") or []:
        out: dict[str, Any] = {
            "name": ply.get("name", ""),
            "kind": ply.get("kind", "core"),
            "thickness_mils": _mm_to_mils(ply.get("thickness_mm")),
        }
        if ply.get("er") is not None:
            out["er"] = ply["er"]
        if ply.get("copper_oz") is not None:
            out["copper_oz"] = ply["copper_oz"]
        layers.append(out)
    return {"name": stackup.get("name", ""), "layers": layers}


# --- comparing a template against the board in front of you -----------

def _plies_from_live(live: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a live stackup read into an alternating ply list.

    ``pcb.get_layer_stackup`` is COPPER-CENTRIC: one entry per copper
    layer, each carrying the dielectric beneath it. A template is a flat
    list of plies. Comparing them without reconciling the two shapes
    compares a copper layer against a prepreg and reports every row as
    wrong, which is how a correct board looks broken.
    """
    plies: list[dict[str, Any]] = []
    for layer in live.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        copper = layer.get("copper_thickness_mils")
        if isinstance(copper, (int, float)) and copper > 0:
            plies.append({"name": str(layer.get("name", "")),
                          "kind": "copper",
                          "thickness_mils": float(copper)})
        height = layer.get("dielectric_height_mils")
        if isinstance(height, (int, float)) and height > 0:
            kind = str(layer.get("dielectric_type", "")).strip().lower()
            plies.append({
                "name": f"{layer.get('name', '')} dielectric",
                # Altium names the material freely; anything that is not
                # recognisably a prepreg is filed as core, which is what
                # the template calls the thick middle ply.
                "kind": "prepreg" if "prepreg" in kind else "core",
                "thickness_mils": float(height),
                "er": layer.get("dielectric_constant"),
            })
    return plies


def compare_stackup(template: dict[str, Any], live: dict[str, Any],
                    *, thickness_tol_mils: float = 0.5,
                    er_tol: float = 0.1) -> dict[str, Any]:
    """Compare a bundled stackup against the board actually open.

    ``template`` is one entry from ``load_vendor(...)["stackups"]``;
    ``live`` is a ``pcb_get_layer_stackup`` reply.

    Reports per ply, and refuses to align two stacks of different
    heights rather than pairing them off and calling the overhang a
    difference: a 4-layer template against a 6-layer board is one
    finding, not six.
    """
    if not isinstance(template, dict) or not isinstance(live, dict):
        return {"ok": False, "reason":
                "template and live must both be dicts"}
    wanted = [_normalise_template_ply(p) for p in template.get("layers") or []]
    actual = _plies_from_live(live)
    if not wanted:
        return {"ok": False, "reason":
                "the template has no layers to compare against"}
    if not actual:
        return {"ok": False, "reason":
                "the live stackup reply carried no usable layers; "
                "pcb_get_layer_stackup returns copper_thickness_mils and "
                "dielectric_height_mils per copper layer"}

    findings: list[dict[str, Any]] = []
    wanted_copper = sum(1 for p in wanted if p["kind"] == "copper")
    actual_copper = sum(1 for p in actual if p["kind"] == "copper")
    if wanted_copper != actual_copper:
        findings.append({
            "check": "copper_layer_count", "severity": "error",
            "expected": wanted_copper, "actual": actual_copper,
            "message": (f"the board has {actual_copper} copper layers and "
                        f"the template has {wanted_copper}; the stacks are "
                        f"not comparable ply by ply"),
        })
        return _verdict(template, findings, wanted, actual)

    if len(wanted) != len(actual):
        findings.append({
            "check": "ply_count", "severity": "warning",
            "expected": len(wanted), "actual": len(actual),
            "message": ("same copper count but a different number of plies; "
                        "comparing the first "
                        f"{min(len(wanted), len(actual))}"),
        })

    for index, (want, got) in enumerate(zip(wanted, actual)):
        if want["kind"] != got["kind"]:
            findings.append({
                "check": "ply_kind", "severity": "error", "ply": index,
                "expected": want["kind"], "actual": got["kind"],
                "message": f"ply {index} should be {want['kind']}",
            })
            continue
        delta = abs((want["thickness_mils"] or 0.0)
                    - (got["thickness_mils"] or 0.0))
        if delta > thickness_tol_mils:
            findings.append({
                "check": "thickness", "severity": "error", "ply": index,
                "expected": want["thickness_mils"],
                "actual": got["thickness_mils"],
                "message": (f"ply {index} ({want['name']}) is "
                            f"{delta:.3f} mil from the template"),
            })
        if want.get("er") is not None and got.get("er") is not None:
            if abs(float(want["er"]) - float(got["er"])) > er_tol:
                findings.append({
                    "check": "dielectric_constant", "severity": "warning",
                    "ply": index, "expected": want["er"],
                    "actual": got["er"],
                    "message": (f"ply {index} ({want['name']}) has a "
                                f"different er, which changes every "
                                f"impedance on the board"),
                })
    return _verdict(template, findings, wanted, actual)


def _normalise_template_ply(ply: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": ply.get("name", ""),
        "kind": ply.get("kind", "core"),
        "thickness_mils": _mm_to_mils(ply.get("thickness_mm"))
        if ply.get("thickness_mm") is not None
        else ply.get("thickness_mils"),
        "er": ply.get("er"),
    }


def _verdict(template, findings, wanted, actual) -> dict[str, Any]:
    errors = [f for f in findings if f["severity"] == "error"]
    total_wanted = sum(p["thickness_mils"] or 0.0 for p in wanted)
    total_actual = sum(p["thickness_mils"] or 0.0 for p in actual)
    return {
        "ok": True,
        "matches": not errors,
        "template": template.get("name", ""),
        "findings": findings,
        "error_count": len(errors),
        "warning_count": len(findings) - len(errors),
        "template_thickness_mils": round(total_wanted, 4),
        "board_thickness_mils": round(total_actual, 4),
        "template_thickness_mm": round(total_wanted * MM_PER_MIL, 4),
        "board_thickness_mm": round(total_actual * MM_PER_MIL, 4),
        "note": ("compares the ply table only. It says nothing about "
                 "whether the fab will press it, what the finished "
                 "impedance measures, or whether the board was ordered "
                 "at this thickness."),
    }


# --- laminates --------------------------------------------------------

def list_laminates() -> dict[str, Any]:
    """Laminate electrical properties, with both dielectric constants.

    Manufacturers publish a PROCESS er (one frequency, clamped
    stripline) and often a DESIGN er (over a band, differential phase
    length). They differ by around 5 percent, the design value is the
    one to size an impedance with, and a table carrying a single ``er``
    silently picks one. Both are reported, and ``er_design`` is null
    where the manufacturer publishes none.
    """
    path = data_dir() / "laminates.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"laminates.json is unreadable: {exc}"}
    laminates = raw.get("laminates") or []
    return {
        "ok": True,
        "count": len(laminates),
        "laminates": laminates,
        "conventions": raw.get("conventions") or [],
        "note": ("deliberately short: only entries read off a datasheet "
                 "are here. Add your own to the profile's laminates, "
                 "where they carry your source rather than someone "
                 "else's."),
    }


def plan_stackup_operations(template: dict[str, Any],
                            live: dict[str, Any]) -> dict[str, Any]:
    """The ``pcb_configure_multilayer_stackup`` operations that would make
    the open board match a template.

    MODIFY ONLY. It will not add or remove copper layers, and refuses
    when the counts differ. Adding one means inventing a layer name
    that Altium's own stack manager would have chosen, and guessing it
    wrong restructures a board that has routing on it. Changing the
    layer count is a decision to take in the editor, with the routing
    in front of you; this sets the thicknesses and materials once the
    shape is right.

    The template's dielectric plies are mapped back onto the
    copper-centric shape the writer expects: each copper layer carries
    the dielectric BENEATH it, which is how ``pcb.get_layer_stackup``
    reports it and how ``pcb_modify_layer`` takes it.
    """
    if not isinstance(template, dict) or not isinstance(live, dict):
        return {"ok": False, "reason": "template and live must both be dicts"}

    plies = [_normalise_template_ply(p) for p in template.get("layers") or []]
    if not plies:
        return {"ok": False, "reason": "the template has no layers"}

    live_layers = [layer for layer in live.get("layers") or []
                   if isinstance(layer, dict)
                   and isinstance(layer.get("copper_thickness_mils"),
                                  (int, float))]
    wanted_copper = [p for p in plies if p["kind"] == "copper"]
    if len(wanted_copper) != len(live_layers):
        return {
            "ok": False,
            "reason": (
                f"the board has {len(live_layers)} copper layers and the "
                f"template has {len(wanted_copper)}. This only modifies "
                f"existing layers; set the layer count in Altium's Layer "
                f"Stack Manager first, where you can see what the change "
                f"does to the routing."),
            "board_copper_layers": len(live_layers),
            "template_copper_layers": len(wanted_copper),
        }

    # Walk the template as (copper, dielectric-beneath) pairs. The last
    # copper ply has nothing beneath it, which is the bottom of the board.
    operations: list[dict[str, Any]] = []
    index = 0
    for position, layer in enumerate(live_layers):
        copper = plies[index]
        beneath = plies[index + 1] if index + 1 < len(plies) else None
        if beneath is not None and beneath["kind"] == "copper":
            beneath = None
        op: dict[str, Any] = {
            "action": "modify",
            "layer": str(layer.get("name", "")),
            "copper_thickness_mils": round(copper["thickness_mils"] or 0.0, 4),
        }
        if beneath is not None:
            op["dielectric_type"] = beneath["kind"]
            op["dielectric_height_mils"] = round(
                beneath["thickness_mils"] or 0.0, 4)
            if beneath.get("er") is not None:
                op["dielectric_constant"] = float(beneath["er"])
            index += 2
        else:
            index += 1
        operations.append(op)

    return {
        "ok": True,
        "template": template.get("name", ""),
        "operations": operations,
        "count": len(operations),
        "warning": ("stack changes can invalidate existing routing; this "
                    "is why the tool that applies them defaults to a dry "
                    "run"),
        "not_changed": ("layer count, layer names, and anything the "
                        "template does not state"),
    }
