# SPDX-License-Identifier: Apache-2.0
"""PCB calculator toolkit: field-solver impedance, thermal trace current,
vias, planes, crosstalk, passives, layout and reference calculators.

Importing the package registers every calculator in ``core.REGISTRY``.
The dashboard tab (web/toolkit_view.py) and the MCP tools
(tools/toolkit.py) both read that registry.
"""
from __future__ import annotations

from . import (  # noqa: F401  (imported to register the calculators)
    conductor,
    impedance,
    layout,
    passives,
    plane,
    signal,
    via,
)
from .core import GROUPS, REGISTRY, InputError, Result

__all__ = ["GROUPS", "REGISTRY", "InputError", "Result", "run", "catalog",
           "listing", "describe"]


def catalog() -> dict:
    """Every calculator's declaration, grouped, for a UI or an agent."""
    groups = []
    for key, title in GROUPS:
        items = [c.spec() for c in REGISTRY.values() if c.group == key]
        if items:
            groups.append({"key": key, "title": title, "calculators": items})
    return {"ok": True, "groups": groups, "count": len(REGISTRY)}


def listing() -> dict:
    """Every calculator by group: name, title and summary, no inputs."""
    cat = catalog()
    for g in cat["groups"]:
        g["calculators"] = [{"name": c["name"], "title": c["title"],
                             "summary": c["summary"]} for c in g["calculators"]]
    return cat


def describe(name: str) -> dict:
    """One calculator's declaration: its inputs, defaults and units."""
    calc = REGISTRY.get(name)
    if calc is None:
        return {"ok": False, "reason": f"no calculator named '{name}'",
                "available": sorted(REGISTRY)}
    return {"ok": True, **calc.spec(),
            "hint": "pass inputs (an empty dict for all defaults) to run it"}


def run(name: str, inputs: dict | None = None) -> dict:
    """Run one calculator by name with raw inputs (numbers, "5 mil", ...)."""
    calc = REGISTRY.get(name)
    if calc is None:
        return {"ok": False, "reason": f"no calculator named '{name}'",
                "available": sorted(REGISTRY)}
    return calc.run(inputs or {})
