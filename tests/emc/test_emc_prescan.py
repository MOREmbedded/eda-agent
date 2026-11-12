# SPDX-License-Identifier: Apache-2.0
"""The pre-scan finds what was built into the boards, and only that."""

from __future__ import annotations

import pytest

from eda_agent.emc.prescan import fast_nets, run_prescan, _is_fast_name
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import Track, Via

from .boards import LAYERS, STACKUP, split_board


@pytest.mark.parametrize("name,fast", [
    ("CLK", True), ("SPI1_SCK", True), ("ETH_RGMII_TXD0", True), ("USB_DP", True),
    ("DDR_DQ3", True), ("SW_NODE", True), ("EN", False), ("RESET_N", False),
    ("LED1", False), ("GND", False), ("VCC", False),
])
def test_fast_net_names(name, fast):
    assert _is_fast_name(name) is fast


def test_fast_nets_skip_returns_and_take_extra():
    b = split_board()
    nets, _ = fast_nets(b)
    assert nets == ["CLK"]
    nets, _ = fast_nets(b, extra=["EN"])
    assert nets == ["CLK", "EN"]


def test_a_track_over_a_split_is_found_where_it_crosses():
    b = split_board(gap=True)
    r = run_prescan(b, stack_from_stackup(STACKUP, LAYERS), checks=["reference_gaps"])
    gaps = r["checks"]["reference_gaps"]
    assert not gaps["pass"]
    row = gaps["crossing_gaps"][0]
    assert row["net"] == "CLK"
    run = row["runs"][0]
    # gap is x = 1000..1050; the run starts at the first sample past 1000
    assert 1000 <= run["from"][0] <= 1010 and 1040 <= run["to"][0] <= 1060
    assert 40 <= row["unreferenced_mils"] <= 60


def test_a_solid_plane_passes():
    b = split_board(gap=False)
    r = run_prescan(b, stack_from_stackup(STACKUP, LAYERS), checks=["reference_gaps"])
    assert r["checks"]["reference_gaps"]["pass"]


def test_seeded_break_a_track_with_no_plane_beside_it():
    b = split_board(gap=False)
    b.tracks.append(Track("BottomLayer", 300, 400, 900, 400, 8.0, "CLK"))
    gaps = run_prescan(b, stack_from_stackup(STACKUP, LAYERS),
                       checks=["reference_gaps"])["checks"]["reference_gaps"]
    assert not gaps["pass"]
    assert gaps["without_plane"] == [{"net": "CLK", "layers": ["BottomLayer"],
                                      "length_mils": 2200.0}]


def test_a_plane_layer_without_split_objects_counts_as_solid():
    b = split_board(gap=False)
    b.regions.clear()
    b.layers[1].kind, b.layers[1].plane_net = "plane", "GND"
    gaps = run_prescan(b, stack_from_stackup(STACKUP, LAYERS),
                       checks=["reference_gaps"])["checks"]["reference_gaps"]
    assert gaps["pass"], gaps


def test_edge_proximity():
    b = split_board(gap=False)
    r = run_prescan(b, checks=["edge_proximity"], edge_mils=100)
    assert r["checks"]["edge_proximity"]["pass"]
    b.tracks.append(Track("TopLayer", 1800, 500, 1950, 500, 8.0, "CLK"))
    found = run_prescan(b, checks=["edge_proximity"], edge_mils=100)["checks"]["edge_proximity"]
    assert not found["pass"]
    assert found["findings"][0]["edge_mils"] == pytest.approx(46.0)


def test_return_vias_use_the_layout_audit():
    b = split_board(gap=False)
    b.vias.append(Via(1000, 500, 20, 10, "TopLayer", "BottomLayer", "CLK"))
    rv = run_prescan(b, checks=["return_vias"])["checks"]["return_vias"]
    assert not rv["pass"] and rv["exceptions"][0]["net"] == "CLK"
    b.vias.append(Via(1030, 500, 20, 10, "TopLayer", "BottomLayer", "GND"))
    assert run_prescan(b, checks=["return_vias"])["checks"]["return_vias"]["pass"]


def test_emission_needs_a_drive_and_scales_with_height():
    b = split_board(gap=False)
    stack = stack_from_stackup(STACKUP, LAYERS)
    r = run_prescan(b, stack, checks=["emission"])
    assert r["checks"]["emission"]["need_drive"] == ["CLK"]
    drives = {"CLK": {"freq_mhz": 50, "rise_ns": 1}}
    em = run_prescan(b, stack, checks=["emission"], drives=drives)["checks"]["emission"]
    row = em["nets"][0]
    # 1600 mil x 8 mil to the GND layer, in mm^2
    assert row["loop_area_mm2"] == pytest.approx(1600 * 8 * 0.0254 ** 2, rel=1e-3)
    assert not row["lower_bound"]
    # same net 6x higher over its plane: 20 log10(6) dB more field
    thick = dict(STACKUP)
    thick["layers"] = [dict(l) for l in STACKUP["layers"]]
    thick["layers"][0]["dielectric_height_mils"] = 48.0
    em2 = run_prescan(b, stack_from_stackup(thick, LAYERS), checks=["emission"],
                      drives=drives)["checks"]["emission"]
    d = em2["nets"][0]["worst"]["e_dbuv_m"] - row["worst"]["e_dbuv_m"]
    assert d == pytest.approx(15.56, abs=0.1)


def test_unknown_check_or_limit_is_refused():
    b = split_board()
    with pytest.raises(ValueError):
        run_prescan(b, checks=["nope"])
    with pytest.raises(ValueError):
        run_prescan(b, limit_name="nope")
