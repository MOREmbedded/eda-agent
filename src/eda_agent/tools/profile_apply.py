# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""One call that puts everything a profile declares onto what is open.

A profile grew three things a project or board can be held to: the design
rules in ``design_rules.json``, the ERC matrix in ``erc_levels.json``, and
the schematic units and grid in ``document_settings.json``. Each has its
own tool, and running them one at a time is fine until somebody opens a
board a week later and has to remember which ones exist.

Two rules this file exists to keep.

**One failing section must not hide the others.** The design rules need a
board open and the ERC matrix needs a project; a caller with only a
schematic open should still learn what the ERC matrix would do, rather
than getting one bridge error and no answer. Every section is run inside
its own guard and reports its own refusal.

**A section the profile does not define is skipped, not failed.** A
profile declaring only an ERC policy is a complete profile; saying
"design_rules.json is not a file" about it would be noise dressed as a
problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

#: The sections, in the order they are reported and applied: rules before
#: ERC because a board left mid-apply is the more expensive one to find.
SECTIONS = ("design_rules", "erc_levels", "document_settings")


class _Collector:
    """A throwaway registry, used only to reach the managers' own tools."""

    def __init__(self):
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def managed_tools() -> dict[str, Any]:
    """The managers' tool functions, taken from their own registrars.

    They are closures built by ``register_rule_tools`` and
    ``register_erc_tools``, which is where the bridge and profile wiring
    lives. Collecting them through a throwaway registry rather than
    re-implementing their bodies means this can never drift from the tool
    a caller would get by naming it directly.
    """
    from .erc import register_erc_tools
    from .rules import register_rule_tools

    collector = _Collector()
    register_rule_tools(collector)
    register_erc_tools(collector)
    return collector.tools


def _profile_directory(profile_dir: str) -> Optional[Path]:
    if profile_dir:
        return Path(profile_dir)
    from .. import profile as profile_module
    active = profile_module.get_active_profile()
    return active.directory if active else None


def _declares(directory: Optional[Path], filename: str) -> bool:
    return bool(directory) and (directory / filename).is_file()


def register_profile_apply_tools(mcp):
    """Register the whole-profile diff and apply tools."""

    @mcp.tool()
    async def profile_diff_all(
        profile_dir: str = "",
        erc_set: str = "",
    ) -> dict[str, Any]:
        """How what is open differs from everything the profile declares.

        Runs each section's own diff: ``pcb_rules_diff`` over the board's
        design rules and ``proj_erc_diff`` over the project's ERC matrix.
        A section the profile does not define is reported as skipped; a
        section that cannot run, because the document it needs is not
        open, reports its own refusal and does not stop the others.

        ``document_settings`` has no reader to compare against, so it is
        reported as apply-only rather than silently left out.

        Args:
            profile_dir: Read the profile from this directory instead of
                the active one.
            erc_set: A named set in the profile's ``erc_levels/``. Omit
                for its ``erc_levels.json``.

        Returns:
            ``sections`` keyed by name, ``in_step`` (every section that
            could be compared is in step), ``profile_dir`` and
            ``not_in_step`` naming the sections that are not.
        """
        return await _run(profile_dir, erc_set, apply=False, dry_run=True)

    @mcp.tool()
    async def profile_apply_all(
        profile_dir: str = "",
        erc_set: str = "",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Bring what is open in step with everything the profile declares.

        The same sections as ``profile_diff_all``, then applied. Both
        managers are idempotent, so running this on a board and project
        already in step writes nothing, and neither ever deletes: a design
        rule the profile does not define and a violation type its ERC set
        does not name are both reported and left exactly as they are.

        A section that cannot run does not stop the others, so a partial
        apply is a real outcome: ``applied`` names the sections that were
        written and ``failed`` the ones that were not.

        Args:
            profile_dir: Read the profile from this directory instead.
            erc_set: A named set in the profile's ``erc_levels/``.
            dry_run: True (the default) reports what would change and
                writes nothing. Pass False to write.

        Returns:
            ``sections``, ``in_step``, ``applied``, ``failed`` and
            ``dry_run``.
        """
        return await _run(profile_dir, erc_set, apply=True, dry_run=dry_run)

    async def _run(profile_dir: str, erc_set: str, *, apply: bool,
                   dry_run: bool) -> dict[str, Any]:
        directory = _profile_directory(profile_dir)
        tools = managed_tools()
        sections: dict[str, Any] = {}
        # Which sections actually ran, tracked here rather than inferred
        # from the reply: a rules diff carries its own "skipped" list (the
        # rules the schematic owns), so reading the section's keys to find
        # out whether the section ran mistook one for the other.
        ran: list[str] = []

        async def section(name: str, filename: str, call):
            if not _declares(directory, filename):
                sections[name] = {"skipped": (
                    f"the profile does not define {filename}")}
                return
            ran.append(name)
            try:
                sections[name] = await call()
            except Exception as exc:                          # noqa: BLE001
                # A bridge timeout or a refusal from the editor. Caught per
                # section on purpose: the point of this tool is that the
                # caller still learns what the other sections say.
                sections[name] = {"ok": False, "reason": str(exc)}

        async def rules():
            if apply:
                return await tools["pcb_rules_apply"](
                    profile_dir=profile_dir, dry_run=dry_run)
            return await tools["pcb_rules_diff"](profile_dir=profile_dir)

        async def erc():
            if apply:
                return await tools["proj_erc_apply"](
                    profile_dir=profile_dir, set_name=erc_set,
                    dry_run=dry_run)
            return await tools["proj_erc_diff"](
                profile_dir=profile_dir, set_name=erc_set)

        await section("design_rules", "design_rules.json", rules)
        await section("erc_levels", "erc_levels.json", erc)
        await _document_settings(sections, ran, directory, apply=apply,
                                 dry_run=dry_run)

        # in_step counts only sections that actually compared something: a
        # skipped section is not a disagreement, and a section that could
        # not run has not got an answer to disagree with.
        compared = [n for n, s in sections.items()
                    if isinstance(s, dict) and "in_step" in s]
        not_in_step = [n for n in compared if not sections[n]["in_step"]]
        out: dict[str, Any] = {
            "ok": True,
            "profile_dir": str(directory) if directory else "",
            "sections": sections,
            "compared": compared,
            "not_in_step": not_in_step,
            "in_step": bool(compared) and not not_in_step,
        }
        if directory is None:
            out["ok"] = False
            out["reason"] = (
                "no profile is active and no profile_dir was given. Set one "
                "with EDA_AGENT_PROFILE_DIR or `eda-agent profile-use "
                "<name>`; see docs/PROFILES.md.")
        if apply:
            out["dry_run"] = dry_run
            out["applied"] = [n for n in ran
                              if sections[n].get("ok") is not False]
            out["failed"] = [n for n in ran
                             if sections[n].get("ok") is False]
        return out

    async def _document_settings(sections: dict[str, Any], ran: list,
                                 directory: Optional[Path], *,
                                 apply: bool, dry_run: bool) -> None:
        """Apply-only: nothing reads a schematic's units back, so there is
        no comparison to make. Reported rather than left out, so a profile
        that sets it does not look ignored."""
        if not _declares(directory, "document_settings.json"):
            sections["document_settings"] = {"skipped": (
                "the profile does not define document_settings.json")}
            return
        if not apply or dry_run:
            sections["document_settings"] = {
                "apply_only": True,
                "reason": ("nothing reads a schematic's units and grid back, "
                           "so there is nothing to compare; "
                           "profile_apply_all(dry_run=False) writes them")}
            return
        # profile_apply_document_settings reads the ACTIVE profile and takes
        # no directory, so applying it while profile_dir points somewhere
        # else would write a different profile's units and report them as
        # this one's. Refused by name instead.
        ran.append("document_settings")
        from .. import profile as profile_module
        active = profile_module.get_active_profile()
        if active is None or Path(active.directory) != Path(directory):
            sections["document_settings"] = {"ok": False, "reason": (
                "profile_apply_document_settings reads the active profile "
                "and takes no directory, and the active profile is "
                f"{active.directory if active else 'none'}, not {directory}. "
                "Activate it, or call that tool directly.")}
            return
        try:
            from .profile import register_profile_tools
            collector = _Collector()
            register_profile_tools(collector, altium=True)
            sections["document_settings"] = await collector.tools[
                "profile_apply_document_settings"]()
        except Exception as exc:                              # noqa: BLE001
            sections["document_settings"] = {"ok": False, "reason": str(exc)}

    return mcp
