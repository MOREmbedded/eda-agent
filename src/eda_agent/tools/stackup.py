# SPDX-License-Identifier: Apache-2.0
"""Bundled fab capabilities, stackup templates and laminate data.

Registered on EVERY backend. Four of the five tools are pure local
reads with no editor involved, and the fifth needs Altium only to
fetch the board's own stackup. Scoping them to one backend is the
mistake the ten calculators made first: they went in beside the
KiCad-only surface and were invisible on Altium until the generated
reference caught it.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["register_stackup_tools"]


def register_stackup_tools(mcp, *, altium: bool = False) -> None:

    @mcp.tool()
    async def design_list_fab_vendors() -> dict[str, Any]:
        """Fab vendors whose capabilities ship with eda-agent.

        Each entry carries the page it was transcribed from, the date,
        how many days old that is, whether it is past the staleness
        threshold, and which figures the vendor does not publish.

        This is a starting point, never an authority. A fab changes its
        capabilities without changing this data, so confirm before
        ordering. A fab that is not bundled goes in your profile's
        `fab_profiles/` directory, where it travels with the project.
        """
        from ..design.stackup_library import STALE_AFTER_DAYS, list_vendors
        vendors = list_vendors()
        return {
            "ok": True,
            "count": len(vendors),
            "vendors": vendors,
            "stale_after_days": STALE_AFTER_DAYS,
            "note": ("transcribed data with its source and date attached. "
                     "Confirm against the fab before ordering."),
        }

    @mcp.tool()
    async def design_fab_vendor(vendor: str) -> dict[str, Any]:
        """One bundled vendor's capabilities and stackup templates.

        Minimums are given in both mm (as the vendor publishes them, so
        each can be checked against the source by eye) and mils (what
        the rest of this project uses). A figure the vendor does not
        publish is **null**, listed in `missing` with the reason: null
        is not zero and not unlimited.

        `warnings` always has content, and it is part of the answer
        rather than an appendix to it.
        """
        from ..design.stackup_library import load_vendor
        return load_vendor(vendor)

    @mcp.tool()
    async def design_fab_vendor_profile(
            vendor: str,
            fill: Optional[dict] = None) -> dict[str, Any]:
        """Turn a bundled vendor into a FabProfile for design_synthesize_rules.

        REFUSES when the vendor does not publish every minimum a fab
        profile requires, naming the ones missing. It will not default
        them: a design rule invented here would arrive looking exactly
        like one the fab published, and rule synthesis downstream
        cannot tell the two apart.

        Pass the missing figures in `fill` (in mils) once you have
        asked the fab. They are recorded in the profile's `source` as
        yours rather than the vendor's, so the provenance survives.
        """
        from ..design.stackup_library import vendor_fab_profile
        return vendor_fab_profile(vendor, fill=fill)

    @mcp.tool()
    async def design_list_laminates() -> dict[str, Any]:
        """Laminate electrical properties, with BOTH dielectric constants.

        Manufacturers publish a PROCESS permittivity (one frequency,
        clamped stripline) and often a DESIGN one (over a band,
        differential phase length). They differ by around 5 percent,
        the design value is the one to size an impedance with, and a
        table carrying a single `er` silently picks one. Both are
        reported, and `er_design` is null where none is published.

        Deliberately short: only entries read off a datasheet are
        here. Add your own to your profile, where they carry your
        source rather than someone else's.
        """
        from ..design.stackup_library import list_laminates
        return list_laminates()

    @mcp.tool()
    async def design_compare_stackup(
            vendor: str, stackup: str,
            live_stackup: dict) -> dict[str, Any]:
        """Compare a bundled stackup template against a board's own.

        `live_stackup` is a `pcb_get_layer_stackup` reply. That reply is
        COPPER-CENTRIC -- one entry per copper layer, each carrying the
        dielectric beneath it -- and a template is a flat ply list; the
        two are reconciled before anything is compared, because pairing
        them raw matches copper against prepreg and reports a correct
        board as entirely wrong.

        A different copper-layer count is reported as ONE finding, not
        one per ply.

        Checks the ply table only. It says nothing about whether the
        fab will press it, what the finished impedance measures, or
        whether the board was ordered at this thickness.
        """
        from ..design.stackup_library import compare_stackup, load_vendor

        loaded = load_vendor(vendor)
        if not loaded.get("ok"):
            return loaded
        match = next((s for s in loaded["stackups"]
                      if s.get("name") == stackup), None)
        if match is None:
            available = [s.get("name") for s in loaded["stackups"]]
            return {"ok": False, "reason":
                    f"{loaded['display_name']} has no bundled stackup "
                    f"{stackup!r}; it has {available or 'none at all'}"}
        result = compare_stackup(match, live_stackup)
        if result.get("ok"):
            result["warnings"] = loaded["warnings"]
        return result

    if altium:
        @mcp.tool()
        async def pcb_check_stackup(vendor: str,
                                    stackup: str) -> dict[str, Any]:
            """Read the open board's stackup and check it against a template.

            The same comparison as design_compare_stackup, with the
            board read for you instead of being passed in.
            """
            from ..bridge import get_bridge
            from ..design.stackup_library import (compare_stackup,
                                                  load_vendor)

            loaded = load_vendor(vendor)
            if not loaded.get("ok"):
                return loaded
            match = next((s for s in loaded["stackups"]
                          if s.get("name") == stackup), None)
            if match is None:
                available = [s.get("name") for s in loaded["stackups"]]
                return {"ok": False, "reason":
                        f"{loaded['display_name']} has no bundled stackup "
                        f"{stackup!r}; it has {available or 'none at all'}"}
            try:
                live = await get_bridge().send_command_async(
                    "pcb.get_layer_stackup", {})
            except Exception as exc:  # noqa: BLE001 - tools refuse, never raise
                return {"ok": False, "reason":
                        f"could not read the board's stackup: {exc}"}
            result = compare_stackup(match, live)
            if result.get("ok"):
                result["warnings"] = loaded["warnings"]
                result["board_name"] = live.get("board_name", "")
            return result

        @mcp.tool()
        async def pcb_apply_stackup_template(
                vendor: str, stackup: str,
                dry_run: bool = True) -> dict[str, Any]:
            """Set the open board's stackup to a bundled template.

            MODIFIES EXISTING LAYERS ONLY. It will not add or remove
            copper layers and refuses when the counts differ: adding
            one means inventing the name Altium's own stack manager
            would have chosen, and guessing wrong restructures a board
            that has routing on it. Set the layer count in the Layer
            Stack Manager first, where you can see what it does.

            Dry run by DEFAULT, matching
            pcb_configure_multilayer_stackup, because a stack change
            can invalidate routing. Pass dry_run=False to apply.

            This sets the ply table. It does not order the board at
            that thickness, and it does not make the fab press it.
            """
            from ..bridge import get_bridge
            from ..design.stackup_library import (load_vendor,
                                                  plan_stackup_operations)

            loaded = load_vendor(vendor)
            if not loaded.get("ok"):
                return loaded
            match = next((s for s in loaded["stackups"]
                          if s.get("name") == stackup), None)
            if match is None:
                available = [s.get("name") for s in loaded["stackups"]]
                return {"ok": False, "reason":
                        f"{loaded['display_name']} has no bundled stackup "
                        f"{stackup!r}; it has {available or 'none at all'}"}

            bridge = get_bridge()
            try:
                live = await bridge.send_command_async(
                    "pcb.get_layer_stackup", {})
            except Exception as exc:  # noqa: BLE001 - tools refuse
                return {"ok": False, "reason":
                        f"could not read the board's stackup: {exc}"}

            plan = plan_stackup_operations(match, live)
            if not plan.get("ok"):
                return plan
            plan["warnings"] = loaded["warnings"]
            plan["board_name"] = live.get("board_name", "")
            if dry_run:
                plan["dry_run"] = True
                return plan

            applied = []
            for op in plan["operations"]:
                params = {k: v for k, v in op.items() if k != "action"}
                try:
                    reply = await bridge.send_command_async(
                        "pcb.modify_layer", params)
                except Exception as exc:  # noqa: BLE001
                    # Report what DID change rather than losing it: a
                    # half-applied stackup is worse than none, and the
                    # reader has to know which half took.
                    return {"ok": False,
                            "reason": f"failed on {op['layer']}: {exc}",
                            "applied": applied,
                            "remaining": [o["layer"] for o
                                          in plan["operations"]
                                          if o["layer"] not in applied],
                            "warning": ("the stackup is PART applied; the "
                                        "layers listed in `applied` were "
                                        "changed and the rest were not")}
                applied.append(op["layer"])
            plan["dry_run"] = False
            plan["applied"] = applied
            return plan
