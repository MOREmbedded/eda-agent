# SPDX-License-Identifier: Apache-2.0
"""The on-demand fab / IPC class floor check over a board's design rules.

Kept apart from the profile rule managers (``rules.py``) on purpose: those
are what ``profile_apply_all`` runs, and this check must only ever run when
the user asks for it.
"""

from __future__ import annotations

from typing import Any, Optional


def register_rule_floor_tools(mcp):
    """Register ``pcb_rules_fab_floors``."""

    from .pcb import get_bridge

    @mcp.tool()
    async def pcb_rules_fab_floors(
        fab: str,
        copper_layers: int,
        outer_oz: float = 1.0,
        tier: str = "standard",
        ipc_class: Optional[int] = None,
        apply: bool = False,
        raise_rules: Optional[list[str]] = None,
        tolerance_mils: float = 0.02,
    ) -> dict[str, Any]:
        """Check the board's design rules against a fab's (and IPC class) floors.

        ON DEMAND ONLY: call this when the user asks to check or set the
        board's rules against a fab or an IPC class, never on your own
        initiative. Run it with ``apply=False`` first and show the user the
        rows; write only when they say so.

        The floors are the fab's published minimums for this build (the
        cited fab rules database, as ``fab_rules``), and with ``ipc_class``
        the stricter of each and the class floor from the active profile's
        licensed values (``standards/ipc_class_floors.json``, made by
        ``eda-agent std-floors``; refused when absent, never guessed).

        A floor check, not a rule generator: a board rule already stricter
        than its floor is left alone (nothing is loosened toward a fab
        minimum); a board-wide (scope All) rule below the floor is raised
        to it; a scoped rule below the floor is reported under
        ``to_decide`` because it is often a deliberate exception, and is
        raised only when named in ``raise_rules``. Rules DRC does not
        check, schematic rules and kinds with no writer (annular ring) are
        reported, never written. Nothing is created or deleted. Every write
        is logged in the board's rule log.

        Args:
            fab: Fab key or name, as ``design_list_fabs`` lists them.
            copper_layers: Copper layer count of the build.
            outer_oz: Outer copper weight in oz. Default 1.
            tier: The fab's tier. Default "standard".
            ipc_class: 2 or 3 to add the profile's IPC class floors.
            apply: False (default) reports only. True raises the rules
                whose ``action`` is ``raise``.
            raise_rules: Names of scoped rules to raise as well, after the
                user has seen them in a report.
            tolerance_mils: How far under a floor still counts as at it
                (the round trip through millimetres). Default 0.02 mil.

        Returns:
            ``build``, ``ipc``, ``rows`` (each board rule against its floor:
            ``status`` ok / below_floor / no_value, ``action`` none / raise
            / decide / not_checked / no_writer, the floor's ``source``),
            ``calls``, ``to_raise``, ``to_decide``, ``no_rule_on_board``,
            ``report_only`` (floors with no Altium rule: PTH ring, aspect
            ratio, silkscreen), ``missing_floors`` (what the fab does not
            publish), ``in_step``; after a write also ``written`` and
            ``failures``.
        """
        from ..design.fab_rules import fab_rules
        from ..design.rule_floors import check_rules, floors_from_fab_rules
        from ..export.drc_rules import logged_rule_edit

        got = fab_rules(fab, copper_layers=copper_layers, outer_oz=outer_oz,
                        tier=tier, ipc_class=ipc_class)
        if not got.get("ok"):
            return got
        floors = floors_from_fab_rules(got)
        bridge = get_bridge()
        board = await bridge.send_command_async("pcb.get_design_rules", {})
        if not isinstance(board, dict) or "rules" not in board:
            return {"ok": False, "reason": "could not read the board's rules",
                    "reply": board}
        out = check_rules(floors, board.get("rules") or [],
                          names=raise_rules, tolerance_mils=tolerance_mils)
        out.update({
            "ok": True, "apply": apply, "board_path": board.get("board_path"),
            "build": {"fab": got["vendor"], "tier": got["tier"], **got["build"]},
            "ipc": got.get("ipc"), "fab_source": got.get("source"),
            "fab_data_age": got.get("age"),
            "missing_floors": [m for m in got.get("missing") or []
                               if m["key"] in _FLOOR_KEYS]})
        if not apply or not out["calls"]:
            return out

        async def run() -> dict[str, Any]:
            written: list[str] = []
            failures: list[dict[str, Any]] = []
            for call in out["calls"]:
                reply = await bridge.send_command_async(
                    "pcb.set_rule_properties", {k: str(v) for k, v in call.items()})
                if not isinstance(reply, dict) or reply.get("success") is False \
                        or reply.get("ok") is False:
                    failures.append({"name": call["name"], "reply": reply})
                else:
                    written.append(call["name"])
            return {"written": written, "failures": failures}

        args = {"fab": got["vendor"], "tier": got["tier"], **got["build"],
                "ipc_class": ipc_class, "calls": out["calls"]}
        result = await logged_rule_edit(bridge, "pcb_rules_fab_floors", args, run)
        out.update(result)
        out["ok"] = not result.get("failures")
        return out

    return mcp


#: fab_rules keys this check uses, so ``missing_floors`` names only those.
_FLOOR_KEYS = frozenset({
    "min_gap", "min_track", "min_drill", "min_annular_ring", "min_hole_to_hole",
    "copper_to_edge", "min_mask_sliver", "silk_to_pad", "pth_annular_ring",
    "hole_to_copper_inner", "max_aspect_ratio"})
