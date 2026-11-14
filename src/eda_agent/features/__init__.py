# SPDX-License-Identifier: Apache-2.0
"""Features and dependencies: what eda-agent can do, what each part needs,
what is installed here, and how to install the rest.

``catalog``: the built-in list. ``registry``: the list with plugins.
``detect``: status. ``install``: plans and runs installs. ``state``: which
switchable features the user turned off.
"""
from __future__ import annotations

import os
from typing import Any

from . import catalog, detect, install, registry, state

__all__ = ["catalog", "detect", "install", "registry", "state", "report"]


def report(deep: bool = False, include_dev: bool = True) -> dict[str, Any]:
    """Every feature with its state and every dependency with its status,
    grouped as the dashboard and ``eda-agent features`` show them."""
    loaded = registry.load()
    state.apply()
    feats = [f for f in loaded["features"] if include_dev or not f.dev]
    deps = loaded["dependencies"]
    saved = state.saved_paths()
    places = state.path_variables()
    statuses: dict[str, detect.DepStatus] = {}
    for f in feats:
        for d in f.all_deps():
            if d not in statuses:
                statuses[d] = detect.dependency_status(deps[d], deep, f)
    out_feats = []
    for f in feats:
        off = f.switchable and not state.is_on(f.id)
        if off and not f.default_on:
            # Off by default: "not installed" says more until it is.
            probe = detect.feature_status(f, statuses, False)
            off = probe.state in (detect.READY, detect.PARTIAL)
        fs = detect.feature_status(f, statuses, off)
        note = _live_note(f.id)
        out_feats.append({
            "id": f.id, "title": f.title, "group": f.group, "summary": f.summary,
            "offers": list(f.offers), "state": fs.state, "missing": fs.missing,
            "detail": fs.detail, "needs": list(f.needs),
            "any_of": [list(g) for g in f.any_of], "adds": list(f.adds),
            "extra": f.extra, "tools": list(f.tools), "docs": f.docs,
            "core": f.core, "dev": f.dev, "switchable": f.switchable,
            "default_on": f.default_on, "related": list(f.related),
            **({"note": note} if note else {}),
        })
    out_deps = []
    for d_id, st in statuses.items():
        d = deps[d_id]
        out_deps.append({
            "id": d.id, "title": d.title, "kind": d.kind, "license": d.license,
            "url": d.url, "runs_apart": d.runs_apart, "size_mb": d.size_mb,
            "note": d.note, "env": list(d.env), "manual": d.manual,
            "locations": [{"name": n, "value": os.environ.get(n, ""),
                           "source": state.path_source(n),
                           "saved": saved.get(n, "")} for n in d.env if n in places],
            "builds_with_cargo": bool(d.cargo),
            "used_by": [f.id for f in feats if d.id in f.all_deps()],
            **st.to_dict(),
        })
    groups = [g for g in catalog.GROUPS if any(f["group"] == g for f in out_feats)]
    groups += sorted({f["group"] for f in out_feats} - set(groups))
    counts: dict[str, int] = {}
    for f in out_feats:
        counts[f["state"]] = counts.get(f["state"], 0) + 1
    return {"ok": True, "groups": groups, "features": out_feats,
            "dependencies": out_deps, "counts": counts,
            "built_in": sum(1 for f in out_feats if f["core"] and f["state"] == detect.READY),
            "engines_dir": str(detect.engines_dir()),
            "state_file": str(state.state_file()),
            "plugin_errors": loaded["errors"], "deep": deep}


def _live_note(feature_id: str) -> str:
    """One line on how a built-in feature is set up right now."""
    try:
        if feature_id == "layout":
            from ..layout.route.engine import describe
            d = describe()
            where = {"environment": " (set by EDA_AGENT_ROUTER)",
                     "profile": " (set in the profile's router.json)",
                     "fallback": " (the setting could not be used)"}.get(d["source"], "")
            other = "euan" if d["engine"] == "default" else "default"
            text = (f"Routing engine: {d['engine']}{where}. "
                    f"Switch with `eda-agent router-use {other}`.")
            if d.get("problem"):
                text += " " + d["problem"]
            return text
        if feature_id == "compute":
            from .. import compute
            st = compute.status()
            if st.get("error"):
                return st["error"]
            active = "Rust" if st.get("active") == "rust" else "Python"
            return (f"Routing runs in {active} (EDA_AGENT_COMPUTE="
                    f"{st.get('requested') or 'python'}).")
    except Exception:  # noqa: BLE001 - a note is extra
        return ""
    return ""
