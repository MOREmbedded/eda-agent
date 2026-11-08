# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Workspace view: which owner profile this server is working
under, why that one, and everything it declares.

A profile decides naming, design-rule targets, the ERC matrix, fab limits and
templates for every tool the AI calls (see eda_agent.profile). Until now the
only way to see which one applied was ``eda-agent profile-show`` in a shell,
run from the right directory, and that is not always the answer the server
gives: the server resolves from its own working directory and environment.
This view answers from inside the server process, so what it shows is what
the tools use.

It writes two things. Switching the centrally active profile, the same
write ``eda-agent profile-use`` makes (profile.use_central_profile). And the
profile's ``preferences.json`` from the Settings card: the value format, the
safety switches and the default design mode this profile wants
(preferences.save_profile_preferences). Every other profile file is
owner-written and never edited from here.

The Settings card also shows what the Config tab on the Altium panel chose
(the bridge folder's preferences.json) and which of the two is in effect;
that file belongs to the panel and is only read here, because the panel
rewrites it whole from what it holds in memory on every click.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from .. import profile as profile_mod

#: A file larger than this is described, not sent: profile files are small
#: hand-written JSON, and a template board can be megabytes of binary.
MAX_FILE_BYTES = 256 * 1024

_TEXT_SUFFIXES = {".json", ".txt", ".md", ".csv", ".ini", ".cfg", ".yaml", ".yml", ".toml"}


def _json_or_none(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _rulesets(p: profile_mod.ActiveProfile) -> list[dict]:
    """Each ruleset with its rule count and severities, for the list."""
    out = []
    if not p.rules_dir:
        return out
    for path in sorted(p.rules_dir.glob("*.json")):
        data = _json_or_none(path)
        rules = data.get("rules") if isinstance(data, dict) else None
        rules = rules if isinstance(rules, list) else []
        sev: dict[str, int] = {}
        for r in rules:
            if isinstance(r, dict):
                s = str(r.get("severity") or "?")
                sev[s] = sev.get(s, 0) + 1
        out.append({"name": path.stem, "file": f"rules/{path.name}",
                    "description": str(data.get("description") or "") if isinstance(data, dict) else "",
                    "rules": len(rules), "severities": sev,
                    "readable": isinstance(data, dict)})
    return out


def _erc(p: profile_mod.ActiveProfile) -> dict:
    """The default ERC set and the named ones, each resolved."""
    from ..design.erc_matrix import DEFAULT_FILENAME, SETS_DIRNAME, read_set

    sets = []
    names = ([""] if (p.directory / DEFAULT_FILENAME).is_file() else []) + p.erc_sets()
    for name in names:
        got = read_set(p.directory, name)
        levels = (got.get("levels") or {}) if got.get("ok") else {}
        by_level: dict[str, int] = {}
        for lv in levels.values():
            by_level[str(lv)] = by_level.get(str(lv), 0) + 1
        sets.append({"name": name or "(default)", "default": not name,
                     "file": DEFAULT_FILENAME if not name else f"{SETS_DIRNAME}/{name}.json",
                     "ok": bool(got.get("ok")), "reason": got.get("reason", ""),
                     "types": len(levels), "by_level": by_level,
                     "unlisted": got.get("unlisted", ""),
                     "chain": [Path(c).name for c in got.get("chain") or []]})
    return {"sets": sets}


def _fab_profiles(p: profile_mod.ActiveProfile) -> list[dict]:
    """Each fab profile's headline limits, the numbers a layout is held to."""
    keys = ("min_track_mils", "min_gap_mils", "min_drill_mils", "min_annular_ring_mils",
            "min_hole_to_hole_mils", "min_mask_sliver_mils", "min_silk_width_mils")
    out = []
    if not p.fab_profiles_dir:
        return out
    for path in sorted(p.fab_profiles_dir.glob("*.json")):
        data = _json_or_none(path)
        d = data if isinstance(data, dict) else {}
        out.append({"name": path.stem, "file": f"fab_profiles/{path.name}",
                    "title": str(d.get("name") or path.stem), "source": str(d.get("source") or ""),
                    "layers": d.get("copper_layer_counts") or [],
                    "limits": {k: d[k] for k in keys if k in d},
                    "readable": isinstance(data, dict)})
    return out


def _templates(p: profile_mod.ActiveProfile) -> dict:
    projects = []
    for name in p.template_names():
        root = p.templates_dir / name
        files = sorted(str(f.relative_to(root)).replace("\\", "/")
                       for f in root.rglob("*") if f.is_file())
        projects.append({"name": name, "files": len(files), "sample": files[:8]})
    documents = [{"name": t["name"], "file": f"templates/documents/{t['file']}",
                  "extension": t["extension"]} for t in p.document_templates()]
    return {"projects": projects, "documents": documents}


def _restart_note(before: Optional[profile_mod.ActiveProfile],
                  after: Optional[profile_mod.ActiveProfile]) -> str:
    """What a switch inside a running server does not reach.

    Which phase tools are advertised is decided when the server builds its
    tool list; every other setting is read on each call.
    """
    a = (before.phase_gates.get("stages") if before else None) or []
    b = (after.phase_gates.get("stages") if after else None) or []
    if a != b:
        return ("The two profiles run different NPI phase gates. The phase tools the AI "
                "sees change when the MCP server restarts.")
    return ""


def _mode_names(profile_dir: Optional[Path]) -> tuple[dict, list[str]]:
    from ..design.modes import load_modes
    return load_modes(profile_dir)


def settings(workspace: Optional[Path] = None,
             start: Optional[Path] = None) -> dict[str, Any]:
    """The Settings card: each preference, what the panel and the profile say,
    which one is in effect, and the design mode a run starts in."""
    from .. import preferences as prefs
    from ..design import modes as modes_mod

    directory = profile_mod.resolve_profile_dir(start)
    modes, _ = _mode_names(directory)
    stored = prefs.read_profile_file(directory)
    pointer = modes_mod.active_mode_pointer()
    try:
        machine_mode = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        machine_mode = ""
    selected, source = modes_mod.selected_mode_name(directory)
    panel_file = Path(workspace) / prefs.PREFERENCES_FILE if workspace else None
    return {
        "can_save": directory is not None,
        "profile_file": prefs.PROFILE_FILE if (directory and (directory / prefs.PROFILE_FILE).is_file()) else "",
        "panel_file": str(panel_file) if panel_file else "",
        "items": prefs.resolved(workspace),
        "choices": {k: list(v) for k, v in prefs.CHOICES.items()},
        "strict": dict(prefs.STRICT),
        "problems": prefs.check_profile_preferences(stored, set(modes)) if stored else [],
        "mode": {"selected": selected, "source": source,
                 "exists": selected in modes,
                 "profile": modes_mod.profile_default_mode(directory) if directory else "",
                 "machine": machine_mode,
                 "environment": os.environ.get(modes_mod.MODE_ENV, "").strip(),
                 "modes": [{"name": m.name, "title": m.title, "source": m.source}
                           for m in modes.values()]},
    }


def save_settings(changes: Any, start: Optional[Path] = None) -> dict[str, Any]:
    """Write CHANGES into the profile's preferences.json (None clears a key)."""
    from .. import preferences as prefs

    directory = profile_mod.resolve_profile_dir(start)
    if directory is None:
        return {"ok": False, "reason": "no profile is in effect, so there is nowhere to "
                                       "keep a per-profile setting; choose or make one first"}
    if not isinstance(changes, dict):
        return {"ok": False, "reason": "changes must be an object of setting: value"}
    modes, _ = _mode_names(directory)
    return prefs.save_profile_preferences(directory, changes, set(modes))


def describe(start: Optional[Path] = None,
             workspace: Optional[Path] = None) -> dict[str, Any]:
    """Everything the Workspace view shows, in one read."""
    where = profile_mod.resolution(start)
    central = profile_mod.central_profile_names()
    profiles = []
    for name in central:
        meta = _json_or_none(profile_mod.central_profiles_root() / name / "profile.json")
        meta = meta if isinstance(meta, dict) else {}
        profiles.append({"name": name, "title": str(meta.get("name") or name),
                         "description": str(meta.get("description") or ""),
                         "central_active": name == where["central"]["name"],
                         "in_effect": where["source"] == "central"
                         and name == where["central"]["name"]})
    out: dict[str, Any] = {"ok": True, "where": where, "profiles": profiles,
                           "ipc_dir": os.environ.get("EDA_AGENT_IPC_DIR", ""),
                           "profile": None,
                           "settings": settings(workspace, start)}
    p = profile_mod.get_active_profile(start)
    if p is None:
        return out
    out["profile"] = {
        "name": str(p.meta.get("name") or p.directory.name),
        "description": str(p.meta.get("description") or ""),
        "directory": str(p.directory),
        "summary": p.summary(),
        "problems": profile_mod.validate_profile(p.directory),
        "design_rules": p.design_rules,
        "rulesets": _rulesets(p),
        "erc": _erc(p),
        "fab_profiles": _fab_profiles(p),
        "templates": _templates(p),
        "naming": "naming.json" if p.naming_path else "",
        "company_goals": "company_goals.json" if p.company_goals_path else "",
        "footprint_defaults": p.footprint_defaults,
        "footprint_policy": p.footprint_policy,
        "document_settings": p.document_settings,
        "phases": profile_mod.describe_phase_stages(start),
    }
    return out


def read_file(rel: str, start: Optional[Path] = None) -> dict[str, Any]:
    """One file of the profile in effect, by its path inside the profile.

    Confined to the profile's directory: a path that leaves it, by ``..``
    or by a link, is refused rather than read.
    """
    p = profile_mod.get_active_profile(start)
    if p is None:
        return {"ok": False, "reason": "no profile is in effect"}
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return {"ok": False, "reason": "path is required"}
    root = p.directory.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return {"ok": False, "reason": f"{rel} is outside the profile"}
    if not target.is_file():
        return {"ok": False, "reason": f"{rel} is not a file in the profile"}
    size = target.stat().st_size
    out: dict[str, Any] = {"ok": True, "path": rel, "full_path": str(target), "bytes": size}
    if size > MAX_FILE_BYTES or target.suffix.lower() not in _TEXT_SUFFIXES:
        out["binary"] = True
        return out
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "reason": f"{rel}: {exc}"}
    out["text"] = text
    if target.suffix.lower() == ".json":
        try:
            out["json"] = json.loads(text)
        except ValueError as exc:
            out["json_error"] = str(exc)
    return out


def use(name: str, start: Optional[Path] = None) -> dict[str, Any]:
    """Switch the centrally active profile, and say whether that changed
    what is in effect here (a project profile or the environment win)."""
    before = profile_mod.get_active_profile(start)
    try:
        directory = profile_mod.use_central_profile(name)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    after = profile_mod.get_active_profile(start)
    moved = profile_mod.follow_env(before.directory if before else None, after)
    where = profile_mod.resolution(start)
    out: dict[str, Any] = {"ok": True, "name": name, "directory": str(directory),
                           "in_effect": where["source"] == "central",
                           "source": where["source"], "env_moved": moved,
                           "note": _restart_note(before, after)}
    if where["source"] == "environment":
        out["reason"] = (f"{where['environment']['variable']} is set, so it still decides; "
                         f"the central choice applies once it is unset.")
    elif where["source"] == "project":
        out["reason"] = (f"a project profile at {where['project']} wins here; "
                         f"the central choice applies outside that project.")
    return out


def rules_plan(start: Optional[Path] = None) -> dict[str, Any]:
    """The design rules the profile in effect asks a board to carry, as
    pcb_rules_plan returns them. Pure Python: Altium is not asked."""
    p = profile_mod.get_active_profile(start)
    if p is None:
        return {"ok": False, "reason": "no profile is in effect"}
    if not p.design_rules:
        return {"ok": False, "reason": "the profile has no design_rules.json"}
    from ..tools.rules import plan_rules
    try:
        return plan_rules(None, str(p.directory))
    except Exception as exc:                              # noqa: BLE001 - shown, not raised
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def register(app: Flask) -> None:
    @app.route("/api/wb/workspace")
    def wb_workspace() -> Response:
        out = describe(workspace=Path(app.config["WORKSPACE_DIR"])
                       if app.config.get("WORKSPACE_DIR") else None)
        out["workspace_dir"] = str(app.config.get("WORKSPACE_DIR") or "")
        return jsonify(out)

    @app.route("/api/wb/workspace/file")
    def wb_workspace_file() -> Response:
        return jsonify(read_file(request.args.get("path", "")))

    @app.route("/api/wb/workspace/rules-plan")
    def wb_workspace_rules_plan() -> Response:
        return jsonify(rules_plan())

    @app.route("/api/wb/workspace/use", methods=["POST"])
    def wb_workspace_use() -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(use(str(body.get("name") or "")))

    @app.route("/api/wb/workspace/settings", methods=["POST"])
    def wb_workspace_settings() -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(save_settings(body.get("changes")))
