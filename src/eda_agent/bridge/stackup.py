# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The board's layer stack-up as ``pcb.get_layer_stackup`` gives it, with
each copper layer's dielectric filled from the physical stack where that
reply has none.

On Altium 26 the layer stack reply read a dielectric of 0 mil and constant
0 under every layer of a board whose Layer Stack Manager has FR-4 12.6 mil
at 4.8, so every reader of it (the stack-up CSV for the fab, the
comparison with a fab profile, the 3D render, the KiCad conversion,
impedance) worked from nothing. Everything reads the stack through here.
"""

from __future__ import annotations

from typing import Any, Optional


async def layer_stackup(bridge, timeout: Optional[float] = None) -> Any:
    """``pcb.get_layer_stackup``, its dielectrics filled (see the module)."""
    kw = {} if timeout is None else {"timeout": timeout}
    result = await bridge.send_command_async("pcb.get_layer_stackup", {}, **kw)
    return await fill_dielectrics(bridge, result)


async def fill_dielectrics(bridge, result: Any) -> Any:
    """Each copper layer's dielectric (the one beneath it) from
    ``pcb.physical_stack``, when no layer of ``result`` has one; marked with
    ``dielectrics_from``. The reply as it was when that fails or the copper
    layers do not match."""
    layers = (result or {}).get("layers") if isinstance(result, dict) else None
    if not layers or any(float(l.get("dielectric_height_mils") or 0) > 0 for l in layers):
        return result
    try:
        phys = await bridge.send_command_async("pcb.physical_stack", {})
    except Exception:  # noqa: BLE001 - the reply stands as read
        return result
    stack = [l for l in (phys or {}).get("layers") or []
             if l.get("kind") in ("copper", "dielectric")]
    copper = [i for i, l in enumerate(stack) if l["kind"] == "copper"]
    if len(copper) != len(layers):
        return result
    for layer, i in zip(sorted(layers, key=lambda l: l.get("order", 0)), copper):
        # Every dielectric down to the next copper: a core and a prepreg
        # between two coppers add up, with the thickness weighted constant.
        below = []
        for row in stack[i + 1:]:
            if row["kind"] != "dielectric":
                break
            below.append(row)
        if not below or i == copper[-1]:
            continue
        h = sum(float(r.get("thickness_mils") or 0) for r in below)
        if h <= 0:
            continue
        layer["dielectric_type"] = below[0].get("material") or "dielectric"
        layer["dielectric_height_mils"] = round(h, 4)
        layer["dielectric_constant"] = round(
            sum(float(r.get("thickness_mils") or 0) * float(r.get("constant") or 0)
                for r in below) / h, 4)
    result["dielectrics_from"] = "pcb_physical_stack"
    return result


__all__ = ["layer_stackup", "fill_dielectrics"]
