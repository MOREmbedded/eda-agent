# SPDX-License-Identifier: Apache-2.0
"""Shared engineering design-intent schema (model independent).

Both ChatGPT and Claude read and write the SAME document through the
submit_design_intent / get_design_intent tools. Concurrency is optimistic:
every submission names the revision it was based on; a stale base revision is
rejected with CONFLICT, and the client must re-read, merge, and resubmit.

Document shape (schema id altium-ai/design-intent/v1):
{
  "schema": "altium-ai/design-intent/v1",
  "title": str,
  "requirements": [{"id": "REQ-1", "text": str}],
  "components": [{"ref": "U1", "component_id": "NE555_SOIC8", "value": str, "notes": str}],
  "nets": [{"name": "VCC", "class": "power|signal|ground", "nodes": ["U1.8", "C1.1"]}],
  "constraints": [{"id": "CON-1", "text": str}],
  "open_questions": [str]
}
"""
from __future__ import annotations

import re

from .errors import NotFound, ValidationFailed

SCHEMA_ID = "altium-ai/design-intent/v1"
REF = re.compile(r"[A-Z]{1,4}[0-9]{1,5}")
NET = re.compile(r"[A-Za-z0-9_+\-/.]{1,64}")
NODE = re.compile(r"([A-Z]{1,4}[0-9]{1,5})\.([A-Z]{0,2}[0-9]{1,4}|EP|TAB)")
NET_CLASSES = ("power", "ground", "signal")
PASSIVE_PINS = {"R": ["1", "2"], "C": ["1", "2"], "L": ["1", "2"], "D": ["1", "2"]}


def validate_intent(doc: dict, component_store=None) -> dict:
    """Return {'warnings': [...], 'unconnected_pins': [...]} or raise ValidationFailed."""
    errors, warnings = [], []
    if doc.get("schema") != SCHEMA_ID:
        errors.append(f"schema must be {SCHEMA_ID}")
    for key in ("title", "components", "nets"):
        if key not in doc:
            errors.append(f"missing {key}")
    if errors:
        raise ValidationFailed("design intent invalid", details={"errors": errors})
    pins_by_ref: dict[str, set[str]] = {}
    for c in doc["components"]:
        ref = c.get("ref", "")
        if not REF.fullmatch(ref):
            errors.append(f"invalid ref {ref!r}")
            continue
        if ref in pins_by_ref:
            errors.append(f"duplicate ref {ref}")
        pins: set[str] | None = None
        cid = c.get("component_id")
        if cid and component_store is not None:
            try:
                pins = {p["number"] for p in component_store.get(cid)["pins"]}
            except NotFound:
                errors.append(f"{ref}: component_id {cid} not in component store")
        if pins is None:
            prefix = re.match(r"[A-Z]+", ref).group(0)
            pins = set(PASSIVE_PINS.get(prefix, []))
            if not pins:
                warnings.append(f"{ref}: pins unknown (no component package); node pins not checked")
        pins_by_ref[ref] = pins
    node_owner: dict[str, str] = {}
    net_names = set()
    for n in doc["nets"]:
        name = n.get("name", "")
        if not NET.fullmatch(name):
            errors.append(f"invalid net name {name!r}")
        if name in net_names:
            errors.append(f"duplicate net {name}")
        net_names.add(name)
        if n.get("class", "signal") not in NET_CLASSES:
            errors.append(f"net {name}: class must be one of {NET_CLASSES}")
        nodes = n.get("nodes", [])
        if len(nodes) < 2:
            warnings.append(f"net {name} has fewer than two nodes")
        for node in nodes:
            m = NODE.fullmatch(node)
            if not m:
                errors.append(f"net {name}: invalid node {node!r} (expected REF.PIN)")
                continue
            ref, pin = m.groups()
            if ref not in pins_by_ref:
                errors.append(f"net {name}: node {node} references unknown component {ref}")
            elif pins_by_ref[ref] and pin not in pins_by_ref[ref]:
                errors.append(f"net {name}: {ref} has no pin {pin}")
            if node in node_owner:
                errors.append(f"node {node} is in both {node_owner[node]} and {name} (short)")
            node_owner[node] = name
    if errors:
        raise ValidationFailed("design intent invalid", details={"errors": errors})
    unconnected = sorted(f"{r}.{p}" for r, ps in pins_by_ref.items() for p in ps if f"{r}.{p}" not in node_owner)
    if unconnected:
        warnings.append(f"{len(unconnected)} pins unconnected (review against datasheet NC requirements)")
    return {"warnings": warnings, "unconnected_pins": unconnected}


def diff_intent(old: dict | None, new: dict) -> dict:
    old = old or {"components": [], "nets": []}
    oc = {c["ref"]: c for c in old["components"]}
    nc = {c["ref"]: c for c in new["components"]}
    on = {n["name"]: set(n.get("nodes", [])) for n in old["nets"]}
    nn = {n["name"]: set(n.get("nodes", [])) for n in new["nets"]}
    return {
        "components_added": sorted(set(nc) - set(oc)),
        "components_removed": sorted(set(oc) - set(nc)),
        "components_changed": sorted(r for r in set(oc) & set(nc) if oc[r] != nc[r]),
        "nets_added": sorted(set(nn) - set(on)),
        "nets_removed": sorted(set(on) - set(nn)),
        "nets_changed": sorted(n for n in set(on) & set(nn) if on[n] != nn[n]),
    }
