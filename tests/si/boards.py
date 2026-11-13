# SPDX-License-Identifier: Apache-2.0
"""Small boards for the net-extraction and channel tests (mils)."""
from __future__ import annotations

from eda_agent.layout.model import (
    DiffPair, Layer, LayoutBoard, Pad, PadCopper, Track, Via,
)

STACKUP = {"layers": [
    {"layer_id": 1, "name": "Top Layer", "copper_thickness_mils": 1.4,
     "dielectric_height_mils": 4.0, "dielectric_constant": 4.2, "dielectric_type": "Prepreg"},
    {"layer_id": 39, "name": "GND", "copper_thickness_mils": 1.4,
     "dielectric_height_mils": 5.0, "dielectric_constant": 4.4, "dielectric_type": "Core"},
    {"layer_id": 2, "name": "Sig1", "copper_thickness_mils": 0.7,
     "dielectric_height_mils": 6.0, "dielectric_constant": 4.3, "dielectric_type": "Prepreg"},
    {"layer_id": 40, "name": "PWR", "copper_thickness_mils": 1.4,
     "dielectric_height_mils": 40.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
    {"layer_id": 32, "name": "Bottom Layer", "copper_thickness_mils": 1.4}]}

LAYERS = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
          Layer("MidLayer1", "signal", 2), Layer("InternalPlane2", "plane", 3, "VCC"),
          Layer("BottomLayer", "signal", 4)]


def smd(comp: str, name: str, x: float, y: float, net: str, layer: str = "TopLayer") -> Pad:
    return Pad(comp, name, x, y, net=net, copper=[PadCopper(layer, "rect", 20, 20)])


def via(x: float, y: float, net: str, d: float = 20, hole: float = 10) -> Via:
    return Via(x, y, d, hole, "TopLayer", "BottomLayer", net)


def single_net(with_stub: bool = True, with_return_via: bool = True,
               inner: float = 2000) -> LayoutBoard:
    """U1.1 -> 1000 mil top -> via -> ``inner`` mil inner -> via -> 500 mil top
    -> U2.1, with a 300 mil stub to TP1 off the first top run."""
    b = LayoutBoard(name="t.PcbDoc", layers=list(LAYERS))
    b.pads = [smd("U1", "1", 0, 0, "SIG"), smd("U2", "1", 1500, inner, "SIG")]
    b.vias = [via(1000, 0, "SIG"), via(1000, inner, "SIG")]
    b.tracks = [Track("TopLayer", 0, 0, 600, 0, 5, "SIG"),
                Track("TopLayer", 600, 0, 1000, 0, 5, "SIG"),
                Track("MidLayer1", 1000, 0, 1000, inner, 4, "SIG"),
                Track("TopLayer", 1000, inner, 1500, inner, 5, "SIG")]
    if with_stub:
        b.pads.append(smd("TP1", "1", 600, 300, "SIG"))
        b.tracks.append(Track("TopLayer", 600, 0, 600, 300, 5, "SIG"))
    if with_return_via:
        b.vias += [via(1040, 0, "GND"), via(1040, inner, "GND")]
    return b


def pair_board(length: float = 2000, gap: float = 5, w: float = 4, skew: float = 0) -> LayoutBoard:
    """A pair on the inner layer between two parts, breakouts on top, vias down."""
    b = LayoutBoard(name="p.PcbDoc", layers=list(LAYERS))
    pitch = w + gap
    for net, dy in (("D_P", 0.0), ("D_N", pitch)):
        b.pads += [smd("U1", net[-1], -50, dy, net), smd("U2", net[-1], length + 50 + skew * (net == "D_P"), dy, net)]
        b.vias += [via(0, dy + (0 if net == "D_P" else 30), net),
                   via(length + skew * (net == "D_P"), dy + (0 if net == "D_P" else 30), net)]
        vy = dy + (0 if net == "D_P" else 30)
        b.tracks += [Track("TopLayer", -50, dy, 0, vy, 5, net),
                     Track("MidLayer1", 0, vy, 20, dy, w, net),
                     Track("MidLayer1", 20, dy, length - 20 + skew * (net == "D_P"), dy, w, net),
                     Track("MidLayer1", length - 20 + skew * (net == "D_P"), dy,
                           length + skew * (net == "D_P"), vy, w, net),
                     Track("TopLayer", length + skew * (net == "D_P"), vy,
                           length + 50 + skew * (net == "D_P"), dy, 5, net)]
    b.vias += [via(-30, -40, "GND"), via(length + 30, -40, "GND")]
    b.diff_pairs = [DiffPair("D", "D_P", "D_N")]
    return b


def mixed_board() -> LayoutBoard:
    """pair_board() with single_net() (stub and all) 3000 mil above it."""
    b = pair_board(skew=0)
    s = single_net(with_stub=True)
    for p in s.pads:
        p.y += 3000
    for v in s.vias:
        v.y += 3000
    for t in s.tracks:
        t.y1 += 3000
        t.y2 += 3000
    b.pads += s.pads
    b.vias += s.vias
    b.tracks += s.tracks
    b.net_classes = {"FAST": ["SIG"], "PAIRS": ["D_P", "D_N"]}
    return b
