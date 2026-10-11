# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Check that the board's pads carry the nets the schematic gives their pins.

Update PCB can report success and still leave pads with no net: one part
came back with 10 of its 13 pads unbound while its twin was complete, and
nothing in the reply said so. It showed up later as unrouted nets.

This compares the two sides offline. The schematic side is the
``components`` list from proj_get_connectivity_many (``pins`` with
``pin_number`` and ``net``); the board side is the ``pads`` list from
pcb_get_pad_properties (``component``, ``name`` and ``net``). Pins and
pads are matched on designator and pin number, ignoring case and
surrounding spaces, since pad names like "a1" and "A1" both occur.

A pin with no net is unconnected on the schematic and is only counted.
A pad with no component (a fiducial, a mounting hole) has no pin to
match and is left out. A pad name can repeat on one part, a thermal pad
split in several pieces for example, and each copy is checked.
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_LIMIT = 200


def _key(designator: Any, pin: Any) -> tuple[str, str]:
    return (str(designator or "").strip().upper(),
            str(pin or "").strip().upper())


def schematic_pin_nets(components: Iterable[Any]) -> dict[tuple[str, str], str]:
    """``(designator, pin) -> net`` from proj_get_connectivity_many."""
    nets: dict[tuple[str, str], str] = {}
    for comp in components or []:
        if not isinstance(comp, dict):
            continue
        for pin in comp.get("pins") or []:
            if not isinstance(pin, dict):
                continue
            key = _key(comp.get("designator"), pin.get("pin_number"))
            if all(key):
                nets[key] = str(pin.get("net") or "").strip()
    return nets


def board_pad_nets(pads: Iterable[Any]) -> dict[tuple[str, str], list[str]]:
    """``(designator, pad) -> [net of each pad of that name]``."""
    nets: dict[tuple[str, str], list[str]] = {}
    for pad in pads or []:
        if not isinstance(pad, dict):
            continue
        key = _key(pad.get("component"), pad.get("name"))
        if all(key):
            nets.setdefault(key, []).append(str(pad.get("net") or "").strip())
    return nets


def _listed(out: dict[str, Any], name: str, rows: list[dict], limit: int):
    out[name] = rows[:limit]
    out[f"{name}_count"] = len(rows)
    out[f"{name}_truncated"] = len(rows) > limit


def verify_eco(components: Iterable[Any], pads: Iterable[Any], *,
               limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Compare schematic pin nets with board pad nets.

    Returns ``complete`` (true only when both sides were read and every
    pin with a net has pads carrying that net) and four lists, each
    with a ``_count`` and a ``_truncated`` flag past ``limit``:

    - ``unbound``: the pin has a net and a pad of that name has none.
      This is what an unfinished ECO leaves.
    - ``mismatched``: the pad carries a different net.
    - ``pins_without_pad``: a pin with a net has no pad on the board.
    - ``pads_without_pin``: a part's pad has no pin on the schematic.

    ``unbound_by_component`` counts the unbound pads per part, which is
    how a pcb_bind_pad_nets repair is laid out. ``nothing_compared`` is
    true when either side came back empty, from the wrong project, an
    uncompiled one, or no board; that is never reported as complete.
    """
    pins = schematic_pin_nets(components)
    board = board_pad_nets(pads)

    unbound: list[dict] = []
    mismatched: list[dict] = []
    without_pad: list[dict] = []
    unconnected = 0
    for (designator, pin), net in sorted(pins.items()):
        if not net:
            unconnected += 1
            continue
        if (designator, pin) not in board:
            without_pad.append(
                {"designator": designator, "pin": pin, "net": net})
            continue
        for pad_net in board[(designator, pin)]:
            if not pad_net:
                unbound.append(
                    {"designator": designator, "pin": pin, "net": net})
            elif pad_net != net:
                mismatched.append({"designator": designator, "pin": pin,
                                   "schematic_net": net, "pad_net": pad_net})
    without_pin = [
        {"designator": designator, "pin": pin, "net": pad_net}
        for (designator, pin), pad_nets in sorted(board.items())
        if (designator, pin) not in pins
        for pad_net in pad_nets
    ]

    by_component: dict[str, int] = {}
    for row in unbound:
        by_component[row["designator"]] = by_component.get(
            row["designator"], 0) + 1

    nothing_compared = not pins or not board
    out: dict[str, Any] = {
        "complete": not (nothing_compared or unbound or mismatched
                         or without_pad),
        "nothing_compared": nothing_compared,
        "pins_checked": len(pins),
        "pads_checked": sum(len(v) for v in board.values()),
        "pins_unconnected": unconnected,
        "unbound_by_component": dict(sorted(by_component.items())),
    }
    _listed(out, "unbound", unbound, limit)
    _listed(out, "mismatched", mismatched, limit)
    _listed(out, "pins_without_pad", without_pad, limit)
    _listed(out, "pads_without_pin", without_pin, limit)
    return out
