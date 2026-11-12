# SPDX-License-Identifier: Apache-2.0
"""Hand-built boards for the PDN tests, in mils."""

from __future__ import annotations

from eda_agent.layout.model import (
    Component, Layer, LayoutBoard, Pad, PadCopper, Region, Track, Via,
)

#: 4-layer stack: Top / GND (4 mil below) / PWR (40 mil core) / Bottom.
STACKUP = {"board_name": "pdn.PcbDoc", "layers": [
    {"name": "Top", "layer_id": 1, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 4.0, "dielectric_constant": 4.2},
    {"name": "GND", "layer_id": 39, "copper_thickness_mils": 1.4,
     "dielectric_type": "Core", "dielectric_height_mils": 4.0, "dielectric_constant": 4.3},
    {"name": "PWR", "layer_id": 40, "copper_thickness_mils": 1.4,
     "dielectric_type": "PrePreg", "dielectric_height_mils": 50.0, "dielectric_constant": 4.2},
    {"name": "Bottom", "layer_id": 32, "copper_thickness_mils": 1.4},
]}

OUTLINE = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]


def smd(comp, name, x, y, net, w=24.0, h=28.0, layer="TopLayer"):
    return Pad(comp, name, x, y, 0.0, net, [PadCopper(layer, "rect", w, h)])


def via(x, y, net, d=20.0, hole=10.0):
    return Via(x, y, d, hole, "TopLayer", "BottomLayer", net)


def plane_board(n_caps: int = 4, cap_value: str = "100nF") -> LayoutBoard:
    """GND plane on layer 2, +1V0 plane on layer 3; a regulator U1 at the
    left, a load U2 at the right with four power and four ground balls on
    vias, and ``n_caps`` 0402 capacitors beside U2, each on its own via
    pair; one 22 uF 0805 bulk capacitor near U1."""
    b = LayoutBoard(name="pdn.PcbDoc", outline=list(OUTLINE),
                    layers=[Layer("TopLayer", "signal", 0),
                            Layer("InternalPlane1", "plane", 1, plane_net="GND"),
                            Layer("InternalPlane2", "plane", 2, plane_net="+1V0"),
                            Layer("BottomLayer", "signal", 3)])
    # regulator output
    b.components.append(Component("U1", "SOT23-5", 400, 1500, comment="REG"))
    b.pads += [smd("U1", "5", 400, 1500, "+1V0"), smd("U1", "2", 400, 1400, "GND")]
    b.vias += [via(400, 1550, "+1V0"), via(400, 1350, "GND")]
    b.tracks += [Track("TopLayer", 400, 1500, 400, 1550, 20, "+1V0"),
                 Track("TopLayer", 400, 1400, 400, 1350, 20, "GND")]
    # bulk cap
    b.components.append(Component("C1", "C0805", 600, 1500, comment="22uF"))
    b.pads += [smd("C1", "1", 560, 1500, "+1V0", 40, 50), smd("C1", "2", 640, 1500, "GND", 40, 50)]
    b.vias += [via(560, 1560, "+1V0"), via(640, 1560, "GND")]
    b.tracks += [Track("TopLayer", 560, 1500, 560, 1560, 20, "+1V0"),
                 Track("TopLayer", 640, 1500, 640, 1560, 20, "GND")]
    # load: 4 power + 4 ground balls
    b.components.append(Component("U2", "BGA", 3200, 1500, comment="FPGA"))
    k = 0
    for dx in (0, 40):
        for dy in (0, 40):
            k += 1
            x, y = 3180 + dx, 1480 + dy
            b.pads.append(Pad("U2", f"P{k}", x, y, 0.0, "+1V0", [PadCopper("TopLayer", "round", 16, 16)]))
            b.vias.append(via(x, y, "+1V0", 16, 8))
            gx = x + 120
            b.pads.append(Pad("U2", f"G{k}", gx, y, 0.0, "GND", [PadCopper("TopLayer", "round", 16, 16)]))
            b.vias.append(via(gx, y, "GND", 16, 8))
    # decaps beside the load
    for i in range(n_caps):
        ref = f"C{10 + i}"
        x, y = 3000 + 60 * i, 1700
        b.components.append(Component(ref, "C0402", x, y, comment=cap_value))
        b.pads += [smd(ref, "1", x, y - 20, "+1V0", 24, 20), smd(ref, "2", x, y + 20, "GND", 24, 20)]
        b.vias += [via(x, y - 45, "+1V0"), via(x, y + 45, "GND")]
        b.tracks += [Track("TopLayer", x, y - 20, x, y - 45, 12, "+1V0"),
                     Track("TopLayer", x, y + 20, x, y + 45, 12, "GND")]
    return b
