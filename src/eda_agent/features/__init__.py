# SPDX-License-Identifier: Apache-2.0
"""Features and dependencies: what eda-agent can do, what each part needs,
what is installed here, and how to install the rest.

``catalog``: the built-in list. ``registry``: the list with plugins.
``detect``: status. ``install``: plans and runs installs. ``state``: which
switchable features the user turned off.
"""
from __future__ import annotations

from typing import Any

from . import catalog, detect, install, registry, state

__all__ = ["catalog", "detect", "install", "registry", "state", "report"]


def report(deep: bool = False, include_dev: bool = True) -> dict[str, Any]:
    """Every feature with its state and every dependency with its status,
    grouped as the dashboard and ``eda-agent features`` show them."""
    loaded = registry.load()
    feats = [f for f in loaded["features"] if include_dev or not f.dev]
    deps = loaded["dependencies"]
    off = state.switched_off()
    statuses: dict[str, detect.DepStatus] = {}
    for f in feats:
        for d in f.all_deps():
            if d not in statuses:
                statuses[d] = detect.dependency_status(deps[d], deep, f)
    out_feats = []
    for f in feats:
        fs = detect.feature_status(f, statuses, f.switchable and f.id in off)
        out_feats.append({
            "id": f.id, "title": f.title, "group": f.group, "summary": f.summary,
            "offers": list(f.offers), "state": fs.state, "missing": fs.missing,
            "detail": fs.detail, "needs": list(f.needs),
            "any_of": [list(g) for g in f.any_of], "adds": list(f.adds),
            "extra": f.extra, "tools": list(f.tools), "docs": f.docs,
            "core": f.core, "dev": f.dev, "switchable": f.switchable,
        })
    out_deps = []
    for d_id, st in statuses.items():
        d = deps[d_id]
        out_deps.append({
            "id": d.id, "title": d.title, "kind": d.kind, "license": d.license,
            "url": d.url, "runs_apart": d.runs_apart, "size_mb": d.size_mb,
            "note": d.note, "env": list(d.env), "manual": d.manual,
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
            "engines_dir": str(detect.engines_dir()),
            "state_file": str(state.state_file()),
            "plugin_errors": loaded["errors"], "deep": deep}
