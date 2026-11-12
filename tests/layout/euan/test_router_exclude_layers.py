# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Router(exclude_layers=...) keeps a layer free of routing.

v0 reserves its bottom copper as a solid GND heat-spreader under a machined
plate; the router took every signal layer a pour does not cover, so it
would have routed there.
"""

from __future__ import annotations

from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.route_euan import Router


def _board():
    rules = [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                  "Clearance Constraint (Gap=6mil) (All),(All)"),
             Rule("Width", "1", "All", "All", 1, True, {},
                  "Width Constraint (Min=6mil) (Max=6mil) (Preferred=6mil) (All)")]
    pads = [Pad(c, "1", x, 200, net="A", copper=[PadCopper("TopLayer", "rect", 20, 20)])
            for c, x in (("R1", 100), ("R2", 700))]
    return LayoutBoard(name="x", outline=[(0, 0), (800, 0), (800, 400), (0, 400)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       pads=pads, rules=rules)


def test_both_layers_route_by_default():
    assert set(Router(_board()).grid.layers) == {"TopLayer", "BottomLayer"}


def test_an_excluded_layer_is_not_routed():
    r = Router(_board(), exclude_layers=frozenset({"BottomLayer"}))
    assert list(r.grid.layers) == ["TopLayer"]
