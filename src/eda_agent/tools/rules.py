# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Manage a board's design rules against the workspace profile's.

Three verbs over one rule set: ``plan`` works out what the profile asks
for (pure Python, no Altium), ``diff`` says how the board differs, and
``apply`` closes the gap. Apply never deletes: a rule the profile does
not define is reported so the drift is visible and the decision stays
with the owner.

The profile's ``design_rules.json`` holds the TARGETS, not the rules:
``{"fab_profile": <name or inline dict>, "net_classes": {...},
"options": {...}}``. ``rule_synthesis`` turns those into rules and
asserts that every number traces to a profile field or a verified
calculator, so a stored number could only drift away from the
calculator that justified it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..design.fab_profile import load_fab_profile
from ..design.rule_manager import reconcile, update_call
from ..design.rule_synthesis import synthesize_rules
from . import profile as _profile_tools  # noqa: F401  (registration order)


def _active_profile():
    from .. import profile as profile_module
    return profile_module.get_active_profile()


def _resolve_targets(targets: Optional[dict],
                     profile_dir: str) -> tuple[Optional[dict], Optional[dict]]:
    """``(targets, refusal)``: the caller's dict, or the profile's file."""
    if targets:
        return dict(targets), None
    if profile_dir:
        path = Path(profile_dir) / "design_rules.json"
        if not path.is_file():
            return None, {"ok": False, "reason": f"{path} is not a file"}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig")), None
        except (OSError, ValueError) as exc:
            return None, {"ok": False, "reason": f"{path}: {exc}"}
    active = _active_profile()
    if active is None:
        return None, {"ok": False, "reason": (
            "no profile is active and no targets were given. Set one with "
            "EDA_AGENT_PROFILE_DIR or `eda-agent profile-use <name>`, or "
            "pass targets= directly. The MCP server is launched from "
            "wherever its client likes, so a project-local "
            ".eda-agent/profile/ usually does NOT reach it; see "
            "docs/PROFILES.md.")}
    if not active.design_rules:
        return None, {"ok": False, "profile_dir": str(active.directory),
                      "reason": ("the active profile has no design_rules.json. "
                                 "It holds the synthesis targets: fab_profile, "
                                 "net_classes and options.")}
    return dict(active.design_rules), None


def _fab_profile_of(targets: dict, profile_dir: Optional[Path]) -> dict:
    """The fab profile the targets name, inline or by file name."""
    named = targets.get("fab_profile")
    if isinstance(named, dict):
        return load_fab_profile(named)
    if not isinstance(named, str) or not named.strip():
        return {"ok": False, "reason": (
            "design_rules.json needs a fab_profile: a name matching a file "
            "in the profile's fab_profiles/, or the profile inline")}
    if profile_dir is None:
        return {"ok": False, "reason": (
            f"fab_profile {named!r} names a file, but no profile directory "
            "is active to look in")}
    stem = named if named.lower().endswith(".json") else f"{named}.json"
    path = Path(profile_dir) / "fab_profiles" / stem
    if not path.is_file():
        return {"ok": False, "reason": f"{path} is not a file"}
    return load_fab_profile(path)


def plan_rules(targets: Optional[dict] = None, profile_dir: str = "") -> dict[str, Any]:
    """pcb_rules_plan's body, callable without the MCP server: the
    dashboard's Workspace view shows the same plan the tool returns."""
    targets, refusal = _resolve_targets(targets, profile_dir)
    if refusal:
        return refusal
    active = _active_profile()
    directory = Path(profile_dir) if profile_dir else (
        active.directory if active else None)
    loaded = _fab_profile_of(targets, directory)
    if not loaded.get("ok"):
        return {"ok": False, "reason": loaded.get("reason")}
    # net_class_map is required and refuses None: a profile that names
    # no net classes asks for the fab-minimum rules alone, which is an
    # empty map, not a missing argument.
    got = synthesize_rules(
        loaded["profile"],
        net_class_map=targets.get("net_classes") or {},
        options=targets.get("options") or {})
    if isinstance(got, dict):
        got.setdefault("source", str(directory) if directory else "targets")
    return got


def register_rule_tools(mcp):
    """Register the design-rule manager tools."""

    from .pcb import get_bridge

    @mcp.tool()
    async def pcb_rules_plan(
        targets: Optional[dict] = None,
        profile_dir: str = "",
    ) -> dict[str, Any]:
        """The design rules the workspace profile asks a board to carry.

        Pure Python: reads the profile, runs the synthesis and returns the
        rules without touching Altium. Every value traces to a fab-profile
        field or a verified calculator (IPC-2221 width, IPC-2141
        impedance); a rule missing an input is skipped with a note rather
        than guessed.

        Args:
            targets: The synthesis targets inline, in the shape of the
                profile's ``design_rules.json``: ``fab_profile`` (a name
                under the profile's ``fab_profiles/``, or the profile
                itself as a dict), optional ``net_classes``
                ({net: class}) and ``options`` (``class_current_a``,
                ``diff_pair_target_ohms``, ``stackup``, ...). Omit to
                read the active profile's file.
            profile_dir: Read the targets from this directory instead of
                the active profile. Useful where the server cannot see a
                project-local profile.

        Returns:
            ``rules`` (``pcb_create_design_rule`` parameter dicts),
            ``stackup_ops``, ``notes`` saying how each number was reached,
            ``skipped`` and the ``source`` the targets came from.
        """
        return plan_rules(targets, profile_dir)

    @mcp.tool()
    async def pcb_rules_diff(
        targets: Optional[dict] = None,
        profile_dir: str = "",
        tolerance_mils: float = 0.02,
    ) -> dict[str, Any]:
        """How the active board's design rules differ from the profile's.

        Reads the board and compares by rule name. A rule the profile does
        not define is reported under ``extra`` and never removed; a rule
        the schematic owns is reported under ``skipped``, because the next
        update from the schematic rewrites it.

        Most rule kinds report their values only inside Altium's own
        ``descriptor`` prose, so the comparison parses it. ``tolerance_mils``
        absorbs the round trip through the millimetre descriptor: 6 mil
        comes back as 0.152mm, which is 5.984 mil and is not a difference.

        Args:
            targets: Synthesis targets inline; omit for the active profile.
            profile_dir: Read the targets from this directory instead.
            tolerance_mils: How far a value may be off and still count as
                matching. Default 0.02 mil.

        Returns:
            ``in_step``, ``create``, ``update`` (each with the differing
            fields), ``same``, ``skipped``, ``extra`` and ``duplicates``
            (rules already sharing a name on the board).
        """
        plan = await pcb_rules_plan(targets=targets, profile_dir=profile_dir)
        if not plan.get("ok", True) or plan.get("ok") is False:
            return plan
        board = await get_bridge().send_command_async("pcb.get_design_rules", {})
        got = reconcile(plan.get("rules") or [], board.get("rules") or [],
                        tolerance_mils=tolerance_mils)
        got["board_rule_count"] = board.get("count")
        got["planned_rule_count"] = len(plan.get("rules") or [])
        return got

    @mcp.tool()
    async def pcb_rules_apply(
        targets: Optional[dict] = None,
        profile_dir: str = "",
        dry_run: bool = True,
        tolerance_mils: float = 0.02,
    ) -> dict[str, Any]:
        """Bring the active board's design rules in step with the profile.

        Idempotent: a rule already on the board is UPDATED in place, never
        added a second time. Altium's own create sets the name and adds
        with no check, so applying a set twice without this leaves two
        rules sharing a name and Altium's priority order picks the winner.

        Nothing is ever deleted. A rule the profile does not define comes
        back under ``extra``; a rule the schematic owns is left alone.

        Args:
            targets: Synthesis targets inline; omit for the active profile.
            profile_dir: Read the targets from this directory instead.
            dry_run: True (the default) reports what would change and
                writes nothing. Pass False to write.
            tolerance_mils: As for ``pcb_rules_diff``.

        Returns:
            The diff, plus ``created``, ``updated``, ``drc_toggled`` and
            ``unwritable`` (rules whose kind has no in-place writer, which
            are reported rather than deleted and recreated, since that
            would lose the rule's priority and any hand-edited scope).
        """
        diff = await pcb_rules_diff(targets=targets, profile_dir=profile_dir,
                                    tolerance_mils=tolerance_mils)
        if diff.get("ok") is False:
            return diff
        diff["dry_run"] = dry_run
        created: list[str] = []
        updated: list[str] = []
        toggled: list[dict[str, Any]] = []
        unwritable: list[dict[str, Any]] = []

        for entry in diff.get("update") or []:
            call = update_call(entry)
            if call is None:
                unwritable.append({"name": entry["name"],
                                   "rule_type": entry.get("rule_type"),
                                   "differences": entry["differences"],
                                   "reason": ("no in-place writer for this rule "
                                              "kind; change it in Altium, or "
                                              "delete it and apply again")})
                continue
            drc = call.pop("_drc_enabled", None)
            if drc is not None:
                toggled.append({"name": call["name"], "drc_enabled": drc})
            if len(call) > 1:
                updated.append(call["name"])

        for rule in diff.get("create") or []:
            created.append(rule.get("name"))

        diff.update({"created": created, "updated": updated,
                     "drc_toggled": toggled, "unwritable": unwritable})
        if dry_run:
            return diff

        bridge = get_bridge()
        failures: list[dict[str, Any]] = []
        for rule in diff.get("create") or []:
            params = {k: v for k, v in rule.items() if v is not None}
            reply = await bridge.send_command_async(
                "pcb.create_design_rule", {k: str(v) for k, v in params.items()})
            if isinstance(reply, dict) and reply.get("success") is False:
                failures.append({"name": rule.get("name"), "reply": reply})
        for entry in diff.get("update") or []:
            call = update_call(entry)
            if call is None:
                continue
            drc = call.pop("_drc_enabled", None)
            if drc is not None:
                await bridge.send_command_async(
                    "pcb.set_rules_enabled",
                    {"names": call["name"], "enabled": "true" if drc else "false",
                     "match": "name"})
            if len(call) > 1:
                reply = await bridge.send_command_async(
                    "pcb.set_rule_properties",
                    {k: str(v) for k, v in call.items()})
                if isinstance(reply, dict) and reply.get("success") is False:
                    failures.append({"name": call["name"], "reply": reply})
        diff["failures"] = failures
        diff["ok"] = not failures
        return diff

    return mcp
