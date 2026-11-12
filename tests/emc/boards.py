# SPDX-License-Identifier: Apache-2.0
"""Small boards built by hand for the EMC tests, in mils."""

from __future__ import annotations

from eda_agent.layout.model import (
    Layer, LayoutBoard, Pad, PadCopper, Region, Track, Via,
)

#: 4-layer stackup reply as pcb_get_layer_stackup gives it, top first.
STACKUP = {"board_name": "emc.PcbDoc", "layers": [
    {"name": "Top", "layer_id": 1, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 8.0, "dielectric_constant": 4.2},
    {"name": "GND", "layer_id": 2, "copper_thickness_mils": 1.4,
     "dielectric_type": "Core", "dielectric_height_mils": 40.0, "dielectric_constant": 4.6},
    {"name": "PWR", "layer_id": 3, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 8.0, "dielectric_constant": 4.2},
    {"name": "Bottom", "layer_id": 32, "copper_thickness_mils": 1.4},
]}

LAYERS = [Layer("TopLayer", "signal", 0), Layer("MidLayer1", "signal", 1),
          Layer("MidLayer2", "signal", 2), Layer("BottomLayer", "signal", 3)]


def layers() -> list[Layer]:
    """Fresh copies: a test may turn one into a plane."""
    return [Layer(l.name, l.kind, l.order) for l in LAYERS]

OUTLINE = [(0, 0), (2000, 0), (2000, 1000), (0, 1000)]


def smd(comp, name, x, y, net, layer="TopLayer", w=40.0, h=40.0):
    return Pad(comp, name, x, y, 0.0, net, [PadCopper(layer, "rect", w, h)])


def split_board(gap: bool = True) -> LayoutBoard:
    """GND on layer 2, split at x = 1000..1050 when ``gap``; a clock
    track on top crossing it, a slow net beside it."""
    b = LayoutBoard(name="emc.PcbDoc", outline=list(OUTLINE), layers=layers())
    if gap:
        b.regions += [Region("MidLayer1", [(0, 0), (1000, 0), (1000, 1000), (0, 1000)], [], "GND", "pour"),
                      Region("MidLayer1", [(1050, 0), (2000, 0), (2000, 1000), (1050, 1000)], [], "GND", "pour")]
    else:
        b.regions.append(Region("MidLayer1", list(OUTLINE), [], "GND", "pour"))
    b.pads += [smd("U1", "1", 200, 500, "CLK"), smd("U2", "1", 1800, 500, "CLK"),
               smd("U1", "2", 200, 300, "EN"), smd("U2", "2", 1800, 300, "EN"),
               smd("U1", "3", 200, 700, "GND"), smd("U2", "3", 1800, 700, "GND")]
    b.tracks += [Track("TopLayer", 200, 500, 1800, 500, 8.0, "CLK"),
                 Track("TopLayer", 200, 300, 1800, 300, 8.0, "EN")]
    b.vias += [Via(200, 700, 20, 10, "TopLayer", "BottomLayer", "GND"),
               Via(1800, 700, 20, 10, "TopLayer", "BottomLayer", "GND")]
    return b


def microstrip_board(length=1000.0, width=14.0) -> LayoutBoard:
    """A straight microstrip on top over a solid GND layer 2, pads at
    both ends: the openEMS reference case."""
    b = LayoutBoard(name="line.PcbDoc", outline=[(0, 0), (length + 400, 0),
                                                 (length + 400, 600), (0, 600)],
                    layers=layers())
    b.regions.append(Region("MidLayer1", list(b.outline), [], "GND", "pour"))
    x0, x1 = 200.0, 200.0 + length
    b.pads += [smd("J1", "1", x0, 300, "RF", w=width, h=width),
               smd("J2", "1", x1, 300, "RF", w=width, h=width)]
    b.tracks.append(Track("TopLayer", x0, 300, x1, 300, width, "RF"))
    return b
