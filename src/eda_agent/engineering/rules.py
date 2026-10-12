"""Owner design-rule engine.

Rulesets are JSON files in policy.rules_dir (one file per ruleset). The owner writes
them (or converts company guidelines into them); AI clients can list, read and run
them but never edit them. Every rule has: id, type, severity (error|warning|info),
description. Machine-checkable types are evaluated deterministically against data
read from Altium; `advisory` rules carry free text that is given to the AI model as
review context and NEVER produce pass/fail results on their own.

Data contract (produced by design_review from bridge read-backs):
  components      [{designator, lib_ref, x_mils, y_mils, unique_id}]
  parameters      {designator: {param_name: value}}
  nets            [{name, nodes: ["U1.8", ...]}]
  pcb_components  [{designator, footprint, x_um, y_um, layer: "T"|"B", rotation}]
  pcb_rules       [{name, kind, enabled, scope1, scope2, descriptor}]
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import NotFound, ValidationFailed

SEVERITIES = ("error", "warning", "info")
RULE_TYPES = {
    # type: (required fields, data needed)
    "component_parameter_required": (("parameters",), {"components", "parameters"}),
    "parameter_pattern": (("parameter", "pattern"), {"components", "parameters"}),
    "parameter_forbidden_values": (("parameter", "values"), {"components", "parameters"}),
    "designator_prefix_by_libref": (("map",), {"components"}),
    "net_name_pattern": (("pattern",), {"nets"}),
    "single_node_net": ((), {"nets"}),
    "net_requires_component": (("net_pattern", "designator_prefix"), {"nets"}),
    "pcb_rule_required": (("kind_contains",), {"pcb_rules"}),
    "pcb_component_layer": (("designator_prefix", "layer"), {"pcb_components"}),
    "advisory": (("text",), set()),
}
_UNIT = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(mm|mil|um)\s*$", re.I)
_DESC_KV = re.compile(r"(\w+)\s*=\s*([0-9]*\.?[0-9]+)\s*(mm|mil|um)", re.I)
AUTO_NET = re.compile(r"^(Net|NetC|N\$)\w*$")


def to_mm(v: str) -> float:
    m = _UNIT.match(v)
    if not m:
        raise ValidationFailed(f"length {v!r} must be a number followed by mm, mil or um")
    x, u = float(m.group(1)), m.group(2).lower()
    return x if u == "mm" else x * 0.0254 if u == "mil" else x / 1000


# ---------------------------------------------------------------- loading and validation
def validate_ruleset(rs: dict) -> dict:
    errors = []
    if not isinstance(rs, dict) or not isinstance(rs.get("rules"), list):
        raise ValidationFailed("ruleset must be an object with a 'rules' list")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", str(rs.get("ruleset", ""))):
        errors.append("ruleset name missing or invalid")
    ids = set()
    for i, r in enumerate(rs["rules"]):
        where = f"rules[{i}]"
        rid, typ = r.get("id"), r.get("type")
        if not rid or not re.fullmatch(r"[A-Za-z0-9_\-.]{1,40}", str(rid)):
            errors.append(f"{where}: invalid id")
        elif rid in ids:
            errors.append(f"{where}: duplicate id {rid}")
        ids.add(rid)
        if typ not in RULE_TYPES:
            errors.append(f"{where} ({rid}): unknown type {typ!r}; known: {sorted(RULE_TYPES)}")
            continue
        for f in RULE_TYPES[typ][0]:
            if f not in r:
                errors.append(f"{where} ({rid}): missing field {f}")
        if r.get("severity", "warning") not in SEVERITIES:
            errors.append(f"{where} ({rid}): severity must be one of {SEVERITIES}")
        for key in ("pattern", "net_pattern"):
            if key in r:
                try:
                    re.compile(r[key])
                except re.error as e:
                    errors.append(f"{where} ({rid}): bad regex in {key}: {e}")
        for m in r.get("map", []) if typ == "designator_prefix_by_libref" else []:
            try:
                re.compile(m["libref_pattern"])
            except (re.error, KeyError) as e:
                errors.append(f"{where} ({rid}): bad map entry: {e}")
        for k, v in (r.get("descriptor_min") or {}).items():
            try:
                to_mm(v)
            except ValidationFailed as e:
                errors.append(f"{where} ({rid}): {k}: {e.message}")
    if errors:
        raise ValidationFailed("ruleset invalid", details={"errors": errors})
    return {"ruleset": rs["ruleset"], "rules": len(rs["rules"]), "data_needed": sorted(data_needed(rs))}


def data_needed(rs: dict) -> set[str]:
    need: set[str] = set()
    for r in rs["rules"]:
        if r.get("enabled", True):
            need |= RULE_TYPES[r["type"]][1]
    return need


class RuleStore:
    def __init__(self, directory: Path | None):
        self.dir = directory

    def names(self) -> list[str]:
        if not self.dir or not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def load(self, name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name) or not self.dir:
            raise ValidationFailed("invalid ruleset name")
        p = self.dir / f"{name}.json"
        if not p.exists():
            raise NotFound(f"ruleset {name} not found")
        rs = json.loads(p.read_text(encoding="utf-8"))
        validate_ruleset(rs)
        return rs


# ---------------------------------------------------------------- evaluation
def _prefix(designator: str) -> str:
    m = re.match(r"[A-Z]+", designator)
    return m.group(0) if m else ""


def _in_scope(designator: str, scope: dict | None) -> bool:
    if not scope:
        return True
    pre = scope.get("designator_prefix")
    if pre and _prefix(designator) not in pre:
        return False
    pat = scope.get("designator_pattern")
    if pat and not re.fullmatch(pat, designator):
        return False
    return True


def evaluate(rs: dict, data: dict) -> dict:
    findings, advisories, skipped = [], [], []

    def add(r, obj, msg):
        findings.append({"rule": r["id"], "severity": r.get("severity", "warning"), "object": obj, "message": msg})

    comps = data.get("components") or []
    params = data.get("parameters") or {}
    nets = data.get("nets") or []
    for r in rs["rules"]:
        if not r.get("enabled", True):
            continue
        t = r["type"]
        missing = [k for k in RULE_TYPES[t][1] if k not in data]
        if missing:
            skipped.append({"rule": r["id"], "reason": f"data not available: {missing}"})
            continue
        if t == "advisory":
            advisories.append({"rule": r["id"], "text": r["text"], "severity": r.get("severity", "info")})
        elif t == "component_parameter_required":
            for c in comps:
                d = c["designator"]
                if _in_scope(d, r.get("scope")):
                    have = params.get(d, {})
                    for pname in r["parameters"]:
                        if not str(have.get(pname, "")).strip():
                            add(r, d, f"missing parameter '{pname}'")
        elif t == "parameter_pattern":
            for c in comps:
                d = c["designator"]
                v = params.get(d, {}).get(r["parameter"])
                if v is not None and _in_scope(d, r.get("scope")) and not re.fullmatch(r["pattern"], v):
                    add(r, d, f"parameter '{r['parameter']}' value {v!r} does not match {r['pattern']}")
        elif t == "parameter_forbidden_values":
            bad = {x.lower() for x in r["values"]}
            for c in comps:
                d = c["designator"]
                v = params.get(d, {}).get(r["parameter"])
                if v is not None and v.lower() in bad and _in_scope(d, r.get("scope")):
                    add(r, d, f"parameter '{r['parameter']}' has forbidden value {v!r}")
        elif t == "designator_prefix_by_libref":
            for c in comps:
                for m in r["map"]:
                    if re.search(m["libref_pattern"], c["lib_ref"]):
                        if _prefix(c["designator"]) not in m["prefixes"]:
                            add(r, c["designator"], f"library reference {c['lib_ref']} requires prefix {m['prefixes']}")
                        break
        elif t == "net_name_pattern":
            for n in nets:
                if r.get("exclude_auto_named", True) and AUTO_NET.match(n["name"]):
                    continue
                if not re.fullmatch(r["pattern"], n["name"]):
                    add(r, n["name"], f"net name does not match {r['pattern']}")
        elif t == "single_node_net":
            allow = [re.compile(a) for a in r.get("allow", [])]
            for n in nets:
                if len(n["nodes"]) < 2 and not any(a.fullmatch(n["name"]) for a in allow):
                    add(r, n["name"], f"net has {len(n['nodes'])} node(s)")
        elif t == "net_requires_component":
            pat = re.compile(r["net_pattern"])
            need = int(r.get("min_count", 1))
            for n in nets:
                if pat.search(n["name"]):
                    cnt = len({x.split(".")[0] for x in n["nodes"] if _prefix(x.split(".")[0]) == r["designator_prefix"]})
                    if cnt < need:
                        add(r, n["name"], f"needs >= {need} component(s) with prefix {r['designator_prefix']}, found {cnt}")
        elif t == "pcb_rule_required":
            kc = r["kind_contains"].lower()
            cands = [x for x in data["pcb_rules"] if (kc in str(x.get("kind", "")).lower()
                                                      or kc in str(x.get("descriptor", "")).lower())
                     and (not r.get("name") or x.get("name") == r["name"]) and str(x.get("enabled", "1")) in ("1", "True", "TRUE", "true")]
            if not cands:
                add(r, r["kind_contains"], "no enabled PCB rule of this kind")
            for key, minv in (r.get("descriptor_min") or {}).items():
                lim = to_mm(minv)
                for x in cands:
                    vals = {k.lower(): to_mm(f"{v}{u}") for k, v, u in _DESC_KV.findall(x.get("descriptor", ""))}
                    if key.lower() not in vals:
                        add(r, x.get("name"), f"descriptor has no {key}= value (cannot check)")
                    elif vals[key.lower()] + 1e-9 < lim:
                        add(r, x.get("name"), f"{key}={vals[key.lower()]:.4f} mm is below required {lim:.4f} mm")
        elif t == "pcb_component_layer":
            for pc in data["pcb_components"]:
                if _prefix(pc["designator"]) in r["designator_prefix"] and pc.get("layer") != r["layer"]:
                    add(r, pc["designator"], f"must be on layer {r['layer']}, is on {pc.get('layer')}")
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    return {"ruleset": rs["ruleset"], "passed": counts["error"] == 0, "counts": counts, "findings": findings,
            "advisories": advisories, "skipped": skipped}


# ---------------------------------------------------------------- Altium .RUL import
def parse_rul(text: str) -> list[dict]:
    """Parse an Altium PCB rules export (.RUL). Each non-empty line is one rule record of
    '|'-separated KEY=VALUE fields. Field names vary by rule kind and Altium version;
    the parser keeps them verbatim. Format: UNVERIFIED-AD26 (procedure R-01)."""
    rules = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or "=" not in ln:
            continue
        rec = {}
        for part in ln.strip("|").split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                rec[k.strip().upper()] = v.strip()
        if rec:
            rules.append(rec)
    return rules
