# SPDX-License-Identifier: Apache-2.0
"""drawing_dims places a land outline's cut corner and an ordinate datum.

Qorvo QPA9903's exposed pad prints "0.14 x 45 deg" on its pin-1 corner,
built as a custom_pads outline, and labels the ordinate datum 0. Neither
had a form, so the footprint was refused with every land correct.
"""
from __future__ import annotations

from types import SimpleNamespace as P

from eda_agent.design.landpattern.drawing_dims import (
    check_dims, drawing_refusal, read_dims, read_numbers, unplaced_numbers)
from eda_agent.design.landpattern.spec import CustomPad



def _pad(outline):
    cp = CustomPad.from_any({"number": "17", "x": 0, "y": 0, "width": 1.6,
                             "height": 1.6, "outline": outline})
    built = [P(number="17", x=0.0, y=0.0, width=1.6, height=1.6, rotation=0)]
    return built, [cp]


CUT = [(-0.8, 0.66), (-0.66, 0.8), (0.8, 0.8), (0.8, -0.8), (-0.8, -0.8)]


def test_a_cut_corner_is_measured_along_its_leg():
    built, custom = _pad(CUT)
    dims = read_dims([{"pad": "17#1", "chamfer": 0.14}])
    row = check_dims(dims, built, custom)[0]
    assert row["ok"] and row["built_mm"] == 0.14 and row["between"] == "chamfer"
    wrong = check_dims(read_dims([{"pad": "17", "chamfer": 0.2}]), built, custom)[0]
    assert not wrong["ok"] and "0.140" in wrong["why"]


def test_a_land_without_a_cut_corner_is_refused():
    built, custom = _pad([(-0.8, 0.8), (0.8, 0.8), (0.8, -0.8), (-0.8, -0.8)])
    row = check_dims(read_dims([{"pad": "17", "chamfer": 0.14}]), built, custom)[0]
    assert not row["ok"] and "no cut corner" in row["why"]


def test_the_chamfer_places_its_number_and_0_needs_no_dimension():
    dims = read_dims([{"pad": "17", "chamfer": 0.14}])
    numbers = read_numbers([0.14, 0, "0.0"])
    assert unplaced_numbers(numbers, dims) == []
    assert drawing_refusal(True, dims, numbers) is None
