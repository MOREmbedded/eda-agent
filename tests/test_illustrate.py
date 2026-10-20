# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The placement illustrator must emit valid, non-trivial PNGs."""

from __future__ import annotations

from eda_agent.design import illustrate as ill
from eda_agent.design.pcb_placement import (
    ConstructOptions,
    DesignRules,
    construct_placement,
)
from eda_agent.placement import PlaceComp, PlaceNet, PlacePin


def _board():
    u1 = PlaceComp(ref="U1", w=200, h=200, cx=0, cy=0, rotatable=True,
                   pins=(PlacePin(-90, 0, "A"), PlacePin(90, 0, "B")))
    r1 = PlaceComp(ref="R1", w=40, h=40, cx=600, cy=600, rotatable=True,
                   pins=(PlacePin(-20, 0, "A"), PlacePin(20, 0, "C")))
    r2 = PlaceComp(ref="R2", w=40, h=40, cx=-600, cy=600, rotatable=True,
                   pins=(PlacePin(-20, 0, "B"), PlacePin(20, 0, "C")))
    comps = [u1, r1, r2]
    nets = [PlaceNet(refs=("U1", "R1"), name="A"),
            PlaceNet(refs=("U1", "R2"), name="B"),
            PlaceNet(refs=("R1", "R2"), name="C")]
    return comps, nets


def _valid_png(path) -> tuple[int, int]:
    from PIL import Image
    assert path.exists() and path.stat().st_size > 1000
    with Image.open(path) as im:
        im.verify()
    with Image.open(path) as im:
        return im.size


def test_placement_png_renders(tmp_path):
    comps, nets = _board()
    res = construct_placement(comps, nets, DesignRules(layers=2),
                              ConstructOptions(seed=3))
    pos = {r: (c[0], c[1]) for r, c in res.centroids.items()}
    out = tmp_path / "pcb.png"
    ill.placement_png(comps, pos, res.region, nets, str(out),
                      title="tiny board", rotations=res.rotations,
                      sides=res.sides, report=res.report)
    w, h = _valid_png(out)
    assert w > 200 and h > 200


def test_placement_png_handles_missing_report(tmp_path):
    comps, nets = _board()
    res = construct_placement(comps, nets, DesignRules(layers=2),
                              ConstructOptions(seed=1))
    pos = {r: (c[0], c[1]) for r, c in res.centroids.items()}
    out = tmp_path / "pcb_noreport.png"
    # report=None must still render (title without the metric line).
    ill.placement_png(comps, pos, res.region, nets, str(out))
    _valid_png(out)
