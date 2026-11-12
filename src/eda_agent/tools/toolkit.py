# SPDX-License-Identifier: Apache-2.0
"""MCP tools for the PCB calculator toolkit (engineering/toolkit).

One tool carries every calculator, so the tool list grows by one and not
by forty-four: it lists the calculators, describes one calculator's
inputs, or runs it. A second, on the Altium backend, reads the open
board's layer stack as ready-made inputs.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


def register_toolkit_tools(mcp, *, altium: bool = False) -> None:

    @mcp.tool()
    async def pcb_calc_toolkit(calculator: str = "",
                               inputs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run a PCB toolkit calculator; with no name, list them.

        The toolkit covers controlled impedance (microstrip, embedded
        microstrip, stripline, dual stripline, coplanar), differential
        pairs (edge-coupled, broadside, coplanar), trace current and
        temperature rise, vias, planes and PDN, crosstalk, timing,
        junction temperature, Ohm's law and passives, padstacks and BGA
        lands, and conversions. Impedance and crosstalk come from a 2D
        field solver; trace and via current from a thermal model of the
        board.

        Three calls, in the order they are normally made:

        - no ``calculator``: every calculator by group, with its name,
          title and one-line summary.
        - a ``calculator`` and no ``inputs``: that calculator's inputs,
          each with its key, kind, default, unit and help. Nothing runs.
        - a ``calculator`` and ``inputs`` (``{}`` for all defaults): the
          result. It states the model used, its status (exact, solver,
          published or estimate), its expected accuracy and, where a
          second model exists, a cross-check against it. Outputs are SI
          values with a formatted ``display`` string.

        Args:
            calculator: a calculator name, for example microstrip,
                edge_coupled_stripline, trace_current, via_properties,
                crosstalk, plane_pair.
            inputs: input values by key. A value is a number in the
                input's default unit, a string with a unit ("5 mil",
                "0.2mm", "2.4 GHz", "1 oz"), or {"value": 5, "unit":
                "mil"}. Inputs left out take their defaults.

        Returns:
            Listing: {ok, count, groups: [{key, title, calculators:
             [{name, title, summary}]}]}.
            Inputs: {ok, calculator, title, summary, inputs: [{key,
             label, kind, default, unit, units?, choices?, help}], hint}.
            Result: {ok, calculator, title, model, status, accuracy,
             references, warnings, outputs: [{key, label, value, kind,
             display, primary}], cross_checks: [{label, model, value,
             difference_percent}]}; or {ok: false, reason}.
        """
        from ..engineering import toolkit
        name = str(calculator or "").strip()
        if not name:
            return toolkit.listing()
        if inputs is None:
            return toolkit.describe(name)
        return await asyncio.to_thread(toolkit.run, name, inputs)

    if not altium:
        return

    @mcp.tool()
    async def pcb_calc_toolkit_board() -> dict[str, Any]:
        """Open board's stackup as toolkit inputs.

        For each copper layer: its copper thickness, the dielectric
        height and Er towards its neighbours, the structure that fits
        (microstrip on an outer layer, stripline inside), and the
        board's thickness and plane count. Pass a layer's values to
        ``pcb_calc_toolkit`` under the matching input keys (w, h, h1,
        h2, t, er, board_thickness).

        Returns:
            {ok, board_name, layer_count, thickness_mils, layers: [{name,
             kind, structure, values: {prefill_key: {value, unit}}}]}
        """
        from ..bridge import get_bridge
        from ..engineering.toolkit.prefill import from_stackup
        try:
            live = await get_bridge().send_command_async("pcb.get_layer_stackup", {})
        except Exception as exc:  # noqa: BLE001 - tools refuse, never raise
            return {"ok": False, "reason": f"could not read the board's stackup: {exc}"}
        return from_stackup(live)
