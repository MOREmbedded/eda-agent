# SPDX-License-Identifier: Apache-2.0
"""Small boards built by hand for the thermal tests, in mils."""

from __future__ import annotations

from eda_agent.emc.stack import Stack, stack_from_stackup
from eda_agent.layout.model import (
    Component, Layer, LayoutBoard, Pad, PadCopper, Region, Via,
)


def rect(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def two_layer(w: float, h: float, core_mils: float = 60.0, copper_mils: float = 1.4,
              pours: bool = True, name: str = "two.PcbDoc") -> tuple[LayoutBoard, Stack]:
    """A w x h board, two copper layers; ``pours`` covers both with copper."""
    layers = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
    regions = []
    if pours:
        regions = [Region("TopLayer", rect(0, 0, w, h), net="GND", kind="pour"),
                   Region("BottomLayer", rect(0, 0, w, h), net="GND", kind="pour")]
    board = LayoutBoard(name=name, outline=rect(0, 0, w, h), layers=layers, regions=regions)
    stack = stack_from_stackup({"layers": [
        {"layer_id": 1, "copper_thickness_mils": copper_mils, "dielectric_type": "Core",
         "dielectric_height_mils": core_mils, "dielectric_constant": 4.5},
        {"layer_id": 32, "copper_thickness_mils": copper_mils}]}, layers)
    return board, stack


FOUR_LAYER_STACKUP = {"layers": [
    {"name": "Top", "layer_id": 1, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 8.0, "dielectric_constant": 4.2},
    {"name": "GND", "layer_id": 39, "copper_thickness_mils": 1.4,
     "dielectric_type": "Core", "dielectric_height_mils": 44.0, "dielectric_constant": 4.6},
    {"name": "PWR", "layer_id": 40, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 8.0, "dielectric_constant": 4.2},
    {"name": "Bottom", "layer_id": 32, "copper_thickness_mils": 1.4},
]}


def four_layer(w: float, h: float, name: str = "four.PcbDoc") -> tuple[LayoutBoard, Stack]:
    """A w x h board: signal, GND plane, 3V3 plane, signal with a GND pour."""
    layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
              Layer("InternalPlane2", "plane", 2, "3V3"), Layer("BottomLayer", "signal", 3)]
    board = LayoutBoard(name=name, outline=rect(0, 0, w, h), layers=layers,
                        regions=[Region("BottomLayer", rect(0, 0, w, h), net="GND", kind="pour")])
    return board, stack_from_stackup(FOUR_LAYER_STACKUP, layers)


def add_qfn(board: LayoutBoard, ref: str, x: float, y: float, ep: float = 180.0,
            body: float = 280.0, vias: int = 3, via_pitch: float = 60.0,
            side: str = "top", net: str = "GND") -> None:
    """A part with an exposed pad and a ``vias`` x ``vias`` field of
    thermal vias under it."""
    layer = "BottomLayer" if side == "bottom" else "TopLayer"
    half = body / 2
    board.components.append(Component(ref, x=x, y=y, side=side, height=35.0,
                                      courtyard=rect(x - half, y - half, x + half, y + half)))
    board.pads.append(Pad(ref, "EP", x, y, net=net,
                          copper=[PadCopper(layer, "rect", ep, ep)]))
    for i in range(vias):
        for j in range(vias):
            board.vias.append(Via(x + (i - (vias - 1) / 2) * via_pitch,
                                  y + (j - (vias - 1) / 2) * via_pitch,
                                  20.0, 10.0, "TopLayer", "BottomLayer", net))


def add_chip(board: LayoutBoard, ref: str, x: float, y: float, side: str = "top") -> None:
    """A two-pad chip part (1206 size), pads on nets A and B."""
    layer = "BottomLayer" if side == "bottom" else "TopLayer"
    board.components.append(Component(ref, x=x, y=y, side=side, height=20.0,
                                      courtyard=rect(x - 80, y - 45, x + 80, y + 45)))
    for name, dx, net in (("1", -55.0, "A"), ("2", 55.0, "B")):
        board.pads.append(Pad(ref, name, x + dx, y, net=net,
                              copper=[PadCopper(layer, "rect", 45.0, 65.0)]))


def demo_board() -> tuple[LayoutBoard, Stack]:
    """A 3 x 2 inch four-layer board with two ICs on thermal vias and
    three chip resistors, the board the report and tool tests run."""
    board, stack = four_layer(3000.0, 2000.0, name="demo.PcbDoc")
    add_qfn(board, "U1", 800.0, 1000.0, ep=120.0, body=200.0)
    add_qfn(board, "U2", 2100.0, 1300.0)
    board.regions.append(Region("TopLayer", rect(600, 800, 1000, 1200), net="GND", kind="pour"))
    for i, (x, y) in enumerate([(1400, 500), (1500, 1500), (500, 400)], start=1):
        add_chip(board, f"R{i}", x, y)
    add_chip(board, "C5", 1050.0, 1300.0)
    return board, stack
