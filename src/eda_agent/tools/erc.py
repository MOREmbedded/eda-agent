# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Manage a project's ERC matrix from the workspace profile.

Three verbs over one set of ERC levels: ``plan`` says what the profile
asks for (pure Python, no Altium), ``diff`` says how the open project
differs, and ``apply`` closes the gap. A violation type the set does not
name is left exactly as it is and reported, the same way the design-rule
manager reports a rule it did not put there.

A profile may hold one default set in ``erc_levels.json`` and any number
of named ones in ``erc_levels/<name>.json``, each able to ``extends``
another. That is what makes a company base policy and a per-workspace
narrowing of it two files rather than two copies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..design.erc_matrix import (
    DEFAULT_FILENAME,
    UNLISTED_LEAVE,
    read_set,
    reconcile,
    set_names,
    write_items,
)

#: Types per ``project.set_erc_levels`` call. The whole matrix is ~150
#: types, so pinning one takes two calls rather than a single request
#: carrying every description Altium has.
WRITE_CHUNK = 60


def _active_profile():
    from .. import profile as profile_module
    return profile_module.get_active_profile()


def _resolve_set(levels: Optional[dict], unlisted: str, set_name: str,
                 profile_dir: str) -> dict[str, Any]:
    """The levels to aim for: the caller's dict, or a set from the profile.

    An empty dict is a caller, not a missing argument: ``levels={}`` with
    an ``unlisted`` level is how someone asks for the whole matrix at one
    level without writing a file for it.
    """
    if levels is not None:
        return {"ok": True, "levels": dict(levels),
                "unlisted": (unlisted or UNLISTED_LEAVE),
                "chain": [], "source": "levels"}
    directory = Path(profile_dir) if profile_dir else None
    if directory is None:
        active = _active_profile()
        if active is None:
            return {"ok": False, "reason": (
                "no profile is active and no levels were given. Set one with "
                "EDA_AGENT_PROFILE_DIR or `eda-agent profile-use <name>`, or "
                "pass levels= directly. The MCP server is launched from "
                "wherever its client likes, so a project-local "
                ".eda-agent/profile/ usually does NOT reach it; see "
                "docs/PROFILES.md.")}
        directory = active.directory
    got = read_set(directory, set_name)
    if not got.get("ok"):
        named = set_names(directory)
        reason = got.get("reason", "")
        if named:
            reason = f"{reason}. Sets in this profile: {named}"
        elif not set_name:
            reason = (f"{reason}. An ERC set is {DEFAULT_FILENAME} at the "
                      f"profile root, or a named file in erc_levels/")
        return {"ok": False, "reason": reason}
    if unlisted:
        got["unlisted"] = unlisted
    got["source"] = str(directory)
    got["set_name"] = set_name or DEFAULT_FILENAME
    return got


def register_erc_tools(mcp):
    """Register the ERC-matrix manager tools."""

    from ..bridge import get_bridge

    @mcp.tool()
    async def proj_erc_plan(
        set_name: str = "",
        profile_dir: str = "",
        levels: Optional[dict] = None,
        unlisted: str = "",
    ) -> dict[str, Any]:
        """The ERC report levels the workspace profile asks a project to carry.

        Pure Python: reads the profile and returns the resolved set without
        touching Altium. ``extends`` is followed, so a set built on a
        company base comes back merged, with ``chain`` naming the files it
        came from, base first.

        Args:
            set_name: A named set in the profile's ``erc_levels/``. Omit for
                the profile's own ``erc_levels.json``.
            profile_dir: Read the set from this directory instead of the
                active profile. Useful where the server cannot see a
                project-local profile.
            levels: The levels inline, ``{violation type: level}``, instead
                of reading any file. A type is its exact description as
                ``proj_get_erc_levels`` lists it, or its kind number; a
                level is no_report, warning, error or fatal.
            unlisted: Override the set's own ``unlisted``: "leave" (the
                default) touches nothing the set does not name, a level
                name pins every other type to it.

        Returns:
            ``levels``, ``unlisted``, ``chain``, ``set_name`` and the
            ``source`` they came from.
        """
        got = _resolve_set(levels, unlisted, set_name, profile_dir)
        if not got.get("ok"):
            return got
        got["count"] = len(got.get("levels") or {})
        return got

    @mcp.tool()
    async def proj_erc_diff(
        set_name: str = "",
        profile_dir: str = "",
        levels: Optional[dict] = None,
        unlisted: str = "",
        project_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """How the project's ERC matrix differs from the profile's.

        Reads Project Options > Error Reporting and compares type by type,
        matching the way the writer does: by kind number, else by an exact
        description match ignoring case. A type the set does not name is
        reported under ``unmanaged`` and never written.

        Args:
            set_name: A named set in the profile's ``erc_levels/``.
            profile_dir: Read the set from this directory instead.
            levels: The levels inline instead of reading any file.
            unlisted: Override the set's own ``unlisted``.
            project_path: The .PrjPcb; default the focused project.

        Returns:
            ``in_step``, ``change`` (each with the kind a write would use),
            ``same``, ``unknown`` (named by the set, absent from this
            Altium), ``invalid`` and ``unmanaged``.
        """
        plan = await proj_erc_plan(set_name=set_name, profile_dir=profile_dir,
                                   levels=levels, unlisted=unlisted)
        if not plan.get("ok"):
            return plan
        params: dict[str, Any] = {"match": ""}
        if project_path:
            params["project_path"] = project_path
        board = await get_bridge().send_command_async(
            "project.get_erc_levels", params, timeout=30.0)
        if isinstance(board, dict) and board.get("error"):
            return {"ok": False, "reason": board["error"]}
        got = reconcile(plan["levels"], (board or {}).get("levels") or [],
                        unlisted=plan.get("unlisted", UNLISTED_LEAVE))
        got["ok"] = True
        got["set_name"] = plan.get("set_name", "")
        got["chain"] = plan.get("chain", [])
        got["unlisted"] = plan.get("unlisted", UNLISTED_LEAVE)
        got["project"] = (board or {}).get("project", "")
        got["type_count"] = (board or {}).get("count")
        return got

    @mcp.tool()
    async def proj_erc_apply(
        set_name: str = "",
        profile_dir: str = "",
        levels: Optional[dict] = None,
        unlisted: str = "",
        project_path: Optional[str] = None,
        dry_run: bool = True,
        save: bool = True,
    ) -> dict[str, Any]:
        """Bring the project's ERC matrix in step with the profile's.

        Idempotent: only the types that differ are written, so a second
        apply writes nothing. Every write is read back by Altium, and a
        level that did not take comes back with a ``problem`` rather than
        being reported as set.

        Nothing is switched off that the set does not ask to be switched
        off: a type the set does not name keeps whatever level it has.

        Args:
            set_name: A named set in the profile's ``erc_levels/``.
            profile_dir: Read the set from this directory instead.
            levels: The levels inline instead of reading any file.
            unlisted: Override the set's own ``unlisted``.
            project_path: The .PrjPcb; default the focused project.
            dry_run: True (the default) reports what would change and
                writes nothing. Pass False to write.
            save: Save the project afterwards, so the matrix persists. The
                levels live in the .PrjPcb, so an unsaved change is lost
                when the project closes.

        Returns:
            The diff, plus ``written``, ``failed``, ``results`` (Altium's
            read-back per type) and ``saved``.
        """
        diff = await proj_erc_diff(set_name=set_name, profile_dir=profile_dir,
                                   levels=levels, unlisted=unlisted,
                                   project_path=project_path)
        if not diff.get("ok"):
            return diff
        diff["dry_run"] = dry_run
        diff["saved"] = False
        change = diff.get("change") or []
        items = write_items(change)
        # A change the write cannot carry, because the read gave it no kind
        # number. The real bridge always gives one, so this is empty in
        # practice; reported rather than assumed so a change can never be
        # dropped between the diff and the count of what was written.
        diff["unwritable"] = [e for e in change
                              if not isinstance(e.get("kind"), int)
                              or isinstance(e.get("kind"), bool)]
        blocked = bool(diff.get("unknown") or diff.get("invalid")
                       or diff["unwritable"])
        if dry_run or not items:
            diff["written"] = 0
            diff["failed"] = 0
            diff["results"] = []
            diff["ok"] = not blocked
            return diff

        bridge = get_bridge()
        written = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for start in range(0, len(items), WRITE_CHUNK):
            params: dict[str, Any] = {
                "levels": "~~".join(items[start:start + WRITE_CHUNK])}
            if project_path:
                params["project_path"] = project_path
            batch = items[start:start + WRITE_CHUNK]
            reply = await bridge.send_command_async(
                "project.set_erc_levels", params, timeout=60.0)
            # A refusal is a dict with an error and no counts, so adding
            # up set and failed alone would report a whole batch that
            # never landed as nothing having gone wrong.
            if not isinstance(reply, dict) or reply.get("error"):
                problem = reply.get("error") if isinstance(reply, dict) else None
                failed += len(batch)
                results.append({"problem": problem or str(reply),
                                "types": len(batch)})
                continue
            written += int(reply.get("set") or 0)
            failed += int(reply.get("failed") or 0)
            results.extend(reply.get("results") or [])
        diff["written"] = written
        diff["failed"] = failed
        diff["results"] = results

        if save and written:
            target = project_path or diff.get("project") or ""
            try:
                reply = await bridge.send_command_async(
                    "application.save_document", {"file_path": target},
                    timeout=60.0)
                diff["saved"] = bool(isinstance(reply, dict)
                                     and reply.get("saved"))
            except Exception as exc:                          # noqa: BLE001
                diff["save_problem"] = str(exc)
        diff["ok"] = not (failed or blocked)
        return diff

    return mcp
