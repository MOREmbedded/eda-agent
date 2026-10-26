# SPDX-License-Identifier: Apache-2.0
"""Pre-ECO schematic vs. PCB comparison (components, footprints, nets).

Complements core/eco_verify.py rather than overlapping it: eco_verify
compares compiled-schematic pins against board PAD NETS after an ECO has
already run, to catch Altium's "ECO reported success but left pads
unbound" failure mode. This module runs BEFORE an ECO, at a coarser
grain it cannot see: components added/removed, footprint changes, and
whole nets added/removed - the things "Design > Update PCB Document"
itself would act on. Ported from Altium Booster 3.2's eco.py; its
EcoDialogDriver (presses the ECO dialog's buttons via Windows UI
Automation) and bridge trigger are out of scope here - they belong to
Booster's job-queue/governance layer, which this integration does not
port.
"""
from __future__ import annotations


def diff(sch: dict, pcb: dict) -> dict:
    """sch: {components: {des: footprint}, pins: {"U1.1": net}}; pcb: same shape from the board."""
    sc, pc = sch["components"], pcb["components"]
    add = sorted(set(sc) - set(pc))
    remove = sorted(set(pc) - set(sc))
    fp_changes = sorted(d for d in set(sc) & set(pc) if sc[d] and pc[d] and sc[d] != pc[d])
    sp, pp = sch["pins"], pcb["pins"]
    net_changes = []
    for pin, net in sorted(sp.items()):
        des = pin.split(".")[0]
        if des in pc and pp.get(pin, "") != net:
            net_changes.append({"pin": pin, "schematic": net, "pcb": pp.get(pin, "")})
    stray = sorted(pin for pin, net in pp.items() if net and pin not in sp and pin.split(".")[0] in sc)
    sch_nets, pcb_nets = set(sp.values()) - {""}, set(pp.values()) - {""}
    out = {"components_to_add": add, "components_to_remove": remove, "footprint_changes": fp_changes,
           "pin_net_changes": net_changes[:500], "pins_connected_only_on_pcb": stray[:500],
           "nets_to_add": sorted(sch_nets - pcb_nets), "nets_to_remove": sorted(pcb_nets - sch_nets)}
    out["in_sync"] = not any(out[k] for k in out if k != "in_sync")
    out["summary"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    return out


def sch_view(snapshot: dict) -> dict:
    """Build diff()'s schematic-side shape from a design-intent-style snapshot
    ({"components": [{"ref"/"designator", "footprint"}], "nets": [{"name", "nodes": ["des.pin", ...]}]})."""
    comps = {c.get("designator") or c.get("ref"): c.get("footprint", "") for c in snapshot["components"]}
    pins = {}
    for n in snapshot.get("nets", []):
        for node in n["nodes"]:
            pins[node] = n["name"]
    return {"components": comps, "pins": pins}


def pcb_view(pcb_components: list[dict], pcb_pins: dict) -> dict:
    """Build diff()'s PCB-side shape from a component list and an existing (designator.pad -> net) map."""
    return {"components": {c["designator"]: c.get("footprint", "") for c in pcb_components}, "pins": pcb_pins}
