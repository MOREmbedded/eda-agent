# SPDX-License-Identifier: Apache-2.0
"""Schematic symbol geometry generator.

Produces a rectangle body with pins on a 100 mil grid. Pins with side=auto are
placed by rule: inputs and power-in on the left, outputs on the right, ground
pins on the bottom-left group, preserving datasheet order. All coordinates are
integer mils relative to the symbol origin (body lower-left corner)."""
from __future__ import annotations

from .pins import POWER_NAMES, Pin

GRID = 100
PIN_LEN = 200
ORIENT = {"left": 180, "right": 0, "top": 90, "bottom": 270}


def _auto_side(p: Pin) -> str:
    if p.side != "auto":
        return p.side
    if p.type in ("output", "open_collector", "open_emitter"):
        return "right"
    if p.name.upper().startswith(("GND", "VSS", "AGND", "DGND", "PGND")):
        return "left"
    if p.type == "power" and POWER_NAMES.match(p.name):
        return "left"
    if p.type == "io":
        return "right"
    return "left"


def generate_symbol(pins: list[Pin], lib_ref: str, description: str, designator_prefix: str) -> dict:
    sides = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        sides[_auto_side(p)].append(p)
    rows = max(len(sides["left"]), len(sides["right"]), 1)
    cols = max(len(sides["top"]), len(sides["bottom"]), 0)
    longest = max([len(p.name) for p in pins] + [4])
    width = max(GRID * 6, ((longest * 60 * 2 + GRID * 4) // GRID) * GRID, (cols + 1) * GRID)
    height = (rows + 1) * GRID
    out_pins = []
    for i, p in enumerate(sides["left"]):
        y = height - (i + 1) * GRID
        out_pins.append({"number": p.number, "name": p.name, "electrical": p.type,
                         "x": -PIN_LEN, "y": y, "orientation": ORIENT["left"], "length": PIN_LEN})
    for i, p in enumerate(sides["right"]):
        y = height - (i + 1) * GRID
        out_pins.append({"number": p.number, "name": p.name, "electrical": p.type,
                         "x": width + PIN_LEN, "y": y, "orientation": ORIENT["right"], "length": PIN_LEN})
    for i, p in enumerate(sides["top"]):
        x = (i + 1) * GRID
        out_pins.append({"number": p.number, "name": p.name, "electrical": p.type,
                         "x": x, "y": height + PIN_LEN, "orientation": ORIENT["top"], "length": PIN_LEN})
    for i, p in enumerate(sides["bottom"]):
        x = (i + 1) * GRID
        out_pins.append({"number": p.number, "name": p.name, "electrical": p.type,
                         "x": x, "y": -PIN_LEN, "orientation": ORIENT["bottom"], "length": PIN_LEN})
    # x/y above are the outer (electrical hot-spot) ends. body_x/body_y are the ends that touch the body.
    # Which of the two Altium's ISch_Pin.Location expects is confirmed by validation V-06; the worker
    # selects one according to WorkerPolicy.pin_location_convention.
    for p in out_pins:
        dx = {0: -PIN_LEN, 180: PIN_LEN, 90: 0, 270: 0}[p["orientation"]]
        dy = {90: -PIN_LEN, 270: PIN_LEN, 0: 0, 180: 0}[p["orientation"]]
        p["body_x"], p["body_y"] = p["x"] + dx, p["y"] + dy
    return {"lib_ref": lib_ref, "description": description, "designator_prefix": designator_prefix,
            "body": {"x1": 0, "y1": 0, "x2": width, "y2": height}, "pins": out_pins, "grid_mils": GRID}


def check_symbol(sym: dict) -> list[str]:
    problems = []
    for p in sym["pins"]:
        if p["x"] % GRID or p["y"] % GRID:
            problems.append(f"pin {p['number']} off {GRID} mil grid")
    locs = [(p["x"], p["y"]) for p in sym["pins"]]
    if len(set(locs)) != len(locs):
        problems.append("two pins share a hot-spot location")
    return problems
