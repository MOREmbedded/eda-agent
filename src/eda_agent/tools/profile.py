# SPDX-License-Identifier: Apache-2.0
"""MCP surface for the per-workspace profile (see eda_agent.profile).

Read-only by design: a profile is owner-written, the same discipline
engineering.rules.py already documents for rulesets ("the owner writes
them; AI clients can list, read and run them but never edit them").
Mutating a profile (init/use/validate/new-project) is a CLI-only action
-- see cli.py's cmd_profile_*.
"""
from __future__ import annotations

import json
from typing import Any

from .. import profile as profile_mod
from ..engineering.rules import RuleStore, validate_ruleset


def _result(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        value = fn(*args, **kwargs)
        return {"ok": True, **value} if isinstance(value, dict) else {"ok": True, "result": value}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": str(exc)}


def _profile_summary() -> dict:
    p = profile_mod.get_active_profile()
    if p is None:
        return {"active": False}
    return {
        "active": True,
        "directory": str(p.directory),
        "meta": p.meta,
        "footprint_defaults": p.footprint_defaults,
        "footprint_policy": p.footprint_policy,
        "document_settings": p.document_settings,
        "has_naming": p.naming_path is not None,
        "has_company_goals": p.company_goals_path is not None,
        "rulesets": p.rulesets(),
        "fab_profiles": p.fab_profile_names(),
        "templates": p.template_names(),
    }


def _profile_list() -> dict:
    root = profile_mod.central_profiles_root()
    names = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    pointer = profile_mod.active_profile_pointer()
    active_name = pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
    return {"profiles": names, "central_active": active_name or None}


def _list_rulesets() -> dict:
    p = profile_mod.get_active_profile()
    store = RuleStore(p.rules_dir if p else None)
    return {"rulesets": store.names()}


def _load_ruleset(name: str) -> dict:
    p = profile_mod.get_active_profile()
    store = RuleStore(p.rules_dir if p else None)
    return {"ruleset": store.load(name)}


def _fab_profile(name: str) -> dict:
    p = profile_mod.get_active_profile()
    if not p or not p.fab_profiles_dir:
        raise ValueError("no active profile defines fab_profiles/")
    path = p.fab_profiles_dir / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"fab profile {name!r} not found in {p.fab_profiles_dir}")
    return {"profile": json.loads(path.read_text(encoding="utf-8"))}


def register_profile_tools(mcp, *, altium: bool = False) -> None:
    @mcp.tool()
    def profile_show() -> dict:
        """The per-workspace profile in effect right now (naming/rules/fab/
        footprint conventions for the owner working here), or {"active": False}
        when none applies. Resolution: EDA_AGENT_PROFILE_DIR env var, then a
        project-local `.eda-agent/profile/` found by walking up from the
        current directory, then the centrally active profile (`profile-use`).
        See docs/PROFILES.md."""
        return _result(_profile_summary)

    @mcp.tool()
    def profile_list() -> dict:
        """Centrally-registered profile names (~/.eda-agent/profiles/) and
        which one, if any, is centrally active. Does not show a per-project
        profile overriding it here -- see profile_show for what actually
        applies right now."""
        return _result(_profile_list)

    @mcp.tool()
    def eng_list_rulesets() -> dict:
        """Ruleset names available in the active profile's rules/ directory,
        ready to pass to eng_load_ruleset then eng_evaluate_rules."""
        return _result(_list_rulesets)

    @mcp.tool()
    def eng_load_ruleset(name: str) -> dict:
        """Load one named ruleset from the active profile's rules/ directory
        (validated the same way eng_validate_ruleset would), ready to pass
        straight to eng_evaluate_rules(ruleset, data)."""
        return _result(_load_ruleset, name)

    @mcp.tool()
    def eng_profile_fab_profile(name: str) -> dict:
        """Read one named fab/stackup profile from the active profile's
        fab_profiles/ directory as a dict, ready to pass straight to
        design_load_fab_profile / design_synthesize_rules."""
        return _result(_fab_profile, name)

    if altium:
        @mcp.tool()
        async def profile_apply_document_settings() -> dict:
            """Apply the active profile's document_settings.json (schematic
            units, snap/visible grid) to the current schematic, via the
            existing sch_set_units / obj_set_grid bridge commands.

            PCB-side units/grid are NOT applied: no bridge command exists
            yet to set a board's display units or default snap grid (see
            docs/PROFILES.md). The profile can still document the intended
            PCB convention; it just isn't auto-applied to a live PCB
            document today.
            """
            from ..bridge import get_bridge

            p = profile_mod.get_active_profile()
            if not p or not p.document_settings:
                return {"ok": False, "reason": "no active profile defines document_settings.json"}
            ds = p.document_settings
            bridge = get_bridge()
            applied: dict[str, Any] = {}
            unit = ds.get("schematic_units")
            if unit:
                applied["units"] = await bridge.send_command_async(
                    "generic.set_sch_units", {"unit": unit})
            snap = ds.get("schematic_snap_grid_mil")
            visible = ds.get("schematic_visible_grid_mil")
            if snap or visible:
                params = {}
                if snap:
                    params["snap_grid"] = str(int(snap))
                if visible:
                    params["visible_grid"] = str(int(visible))
                applied["grid"] = await bridge.send_command_async("generic.set_grid", params)
            if not applied:
                return {"ok": False, "reason": "document_settings.json has no recognised keys"}
            return {"ok": True, **applied}
