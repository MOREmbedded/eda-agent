# SPDX-License-Identifier: Apache-2.0
"""The reference copper under a routed net: gaps, splits and missing
copper found from the board's plane and pour shapes, the net job's check
and the board job's placed findings."""
from __future__ import annotations

import json

import numpy as np
import pytest

from eda_agent import simulation as sim
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import Layer, Region
from eda_agent.si import board_jobs, extract, net_jobs, planner, return_path
from eda_agent.si.jobs import plan_options

from .boards import LAYERS, STACKUP, pair_board, single_net

BIG = [(-500, -500), (2500, -500), (2500, 3000), (-500, 3000)]


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


@pytest.fixture(scope="module")
def info():
    stack = stack_from_stackup(STACKUP, LAYERS)
    return {i["layer"]: i for i in planner.layer_sections(stack, plan_options({}))}


def rect(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def plane(layer, outline, net="GND", holes=()):
    return Region(layer=layer, outline=list(outline), holes=[list(h) for h in holes], net=net,
                  kind="plane", source="split_plane")


def board(*regions):
    """SIG: 1000 mil on top along y = 0 (over InternalPlane1), 2000 mil on the
    inner layer along x = 1000 (between InternalPlane1 and 2), 500 mil on top."""
    b = single_net(with_stub=False)
    b.regions = list(regions)
    return b


def gaps(b, info, **kw):
    return return_path.of_path(b, extract.net_path(b, "SIG"), info, **kw)


def test_whole_planes_and_planes_never_read_give_no_gap(info):
    res = gaps(board(), info)
    assert res["gaps"] == [] and res["worst_mils"] == 0
    assert any("InternalPlane1 has no copper shapes" in n and "solid" in n for n in res["notes"])
    res = gaps(board(plane("InternalPlane1", BIG), plane("InternalPlane2", BIG, "VCC")), info)
    assert res["gaps"] == [] and res["notes"] == []


def test_a_slot_under_a_microstrip_is_found_with_its_length_and_place(info):
    # a 30 mil slot across the first top run, 400 mil from U1.1
    b = board(plane("InternalPlane1", BIG, holes=[rect(400, -100, 430, 100)]))
    res = gaps(b, info)
    assert len(res["gaps"]) == 1
    g = res["gaps"][0]
    assert g["layer"] == "TopLayer" and g["reference"] == "InternalPlane1"
    assert g["length_mils"] == pytest.approx(30, abs=2.1)
    assert g["at_mils"] == pytest.approx([400, 0], abs=2.1)
    assert g["distance_mils"] == pytest.approx(400, abs=2.1)
    assert g["net_before"] == g["net_after"] == "GND" and not g["other_reference_solid"]
    assert g["text"] == f"crosses a gap of {g['length_mils']:.0f} mil in InternalPlane1"
    assert res["worst_mils"] == g["length_mils"] == res["uncovered_mils"]
    # a slot beside the trace is nothing, and neither is a cutout elsewhere
    b = board(plane("InternalPlane1", BIG, holes=[rect(400, 20, 430, 100)]),
              Region(layer="InternalPlane1", outline=rect(1500, 500, 1600, 600), kind="cutout"))
    assert gaps(b, info)["gaps"] == []
    # a board cutout drawn as its own region counts as a hole
    b = board(plane("InternalPlane1", BIG),
              Region(layer="InternalPlane1", outline=rect(700, -50, 760, 50), kind="cutout"))
    g = gaps(b, info)["gaps"][0]
    assert g["length_mils"] == pytest.approx(60, abs=2.1) and g["at_mils"][0] == pytest.approx(700, abs=2.1)


def test_a_split_between_two_nets_is_named_even_when_it_is_narrow(info):
    # GND to x = 500, +3V3 from x = 505: a 5 mil split, under the minimum gap
    b = board(plane("InternalPlane1", rect(-500, -500, 500, 3000)),
              plane("InternalPlane1", rect(505, -500, 2500, 3000), "+3V3"))
    res = gaps(b, info)
    tops = [g for g in res["gaps"] if g["layer"] == "TopLayer"]
    assert len(tops) == 1
    g = tops[0]
    assert (g["net_before"], g["net_after"]) == ("GND", "+3V3")
    assert g["text"].startswith("crosses the split between GND and +3V3 (")
    assert g["at_mils"][0] == pytest.approx(500, abs=2.1)
    # the same 5 mil as a slot in one net is under the minimum, until the minimum is lowered
    b = board(plane("InternalPlane1", BIG, holes=[rect(500, -100, 505, 100)]))
    assert gaps(b, info)["gaps"] == []
    assert len(gaps(b, info, min_gap=3)["gaps"]) == 1


def test_the_antipad_of_the_nets_own_via_is_not_a_gap_but_a_long_run_out_is(info):
    # a 30 mil antipad round the via at (1000, 0), where the top run ends
    hole = rect(970, -30, 1030, 30)
    b = board(plane("InternalPlane1", BIG, holes=[hole]), plane("InternalPlane2", BIG, "VCC"))
    assert gaps(b, info)["gaps"] == []
    # the plane stops 200 mil before the via: that is a gap
    b = board(plane("InternalPlane1", rect(-500, -500, 800, 3000)))
    res = gaps(b, info)
    g = next(x for x in res["gaps"] if x["layer"] == "TopLayer" and x["at_mils"][1] == 0)
    assert g["length_mils"] == pytest.approx(200, abs=2.1) and g["net_before"] == "GND"
    assert g["net_after"] == ""


def test_a_stripline_with_one_plane_whole_is_the_milder_case_and_sorts_last(info):
    # the inner run (x = 1000, y 0 to 2000) lies between InternalPlane1 and 2
    b = board(plane("InternalPlane1", BIG, holes=[rect(300, -100, 320, 100)]),
              plane("InternalPlane2", BIG, "VCC", holes=[rect(900, 1000, 1100, 1100)]))
    res = gaps(b, info)
    assert [(g["layer"], g["reference"], g["other_reference_solid"]) for g in res["gaps"]] == [
        ("TopLayer", "InternalPlane1", False), ("MidLayer1", "InternalPlane2", True)]
    inner = res["gaps"][1]
    assert inner["length_mils"] == pytest.approx(100, abs=2.1)
    assert inner["distance_mils"] == pytest.approx(1000 + 1000, abs=2.1)
    assert inner["at_mils"] == pytest.approx([1000, 1000], abs=2.1)
    # both planes open at the same place: no longer mild
    b = board(plane("InternalPlane1", BIG, holes=[rect(900, 1000, 1100, 1100)]),
              plane("InternalPlane2", BIG, "VCC", holes=[rect(900, 1000, 1100, 1100)]))
    assert [g["other_reference_solid"] for g in gaps(b, info)["gaps"]] == [False, False]


def test_a_signal_layer_taken_as_reference_must_carry_copper():
    layers = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
    stackup = {"layers": [
        {"layer_id": 1, "name": "Top Layer", "copper_thickness_mils": 1.4,
         "dielectric_height_mils": 8.0, "dielectric_constant": 4.3, "dielectric_type": "Core"},
        {"layer_id": 32, "name": "Bottom Layer", "copper_thickness_mils": 1.4}]}
    b = single_net(with_stub=False, inner=0)
    b.layers = layers
    b.vias = []
    b.pads[1].x, b.pads[1].y = 1000, 0
    b.tracks = b.tracks[:2]
    info = {i["layer"]: i for i in planner.layer_sections(stack_from_stackup(stackup, layers),
                                                          plan_options({}))}
    path = extract.net_path(b, "SIG")
    res = return_path.of_path(b, path, info)
    assert len(res["gaps"]) == 1 and res["gaps"][0]["length_mils"] == pytest.approx(1000, abs=1)
    assert res["gaps"][0]["text"] == "crosses no copper in BottomLayer"
    # a ground pour under it, and the route is covered
    b.regions = [Region(layer="BottomLayer", outline=rect(-100, -100, 1100, 100), net="GND",
                        kind="pour", source="polygon:GND")]
    assert return_path.of_path(b, path, info)["gaps"] == []
    # the polygon's outline alone is not copper
    b.regions[0].kind = "pour_boundary"
    assert len(return_path.of_path(b, path, info)["gaps"]) == 1


def test_a_pair_is_looked_at_on_both_sides(info):
    b = pair_board()     # inner layer, D_P along y = 0 and D_N along y = 9, x 20 to 1980
    # a slot that only D_N crosses
    b.regions = [plane("InternalPlane1", BIG, holes=[rect(800, 5, 840, 60)]),
                 plane("InternalPlane2", BIG, "VCC", holes=[rect(800, 5, 840, 60)])]
    res = return_path.of_path(b, extract.pair_path(b, "D", "D_P", "D_N"), info)
    assert len(res["gaps"]) == 2 and {g["reference"] for g in res["gaps"]} == {
        "InternalPlane1", "InternalPlane2"}
    assert all(g["at_mils"] == pytest.approx([800, 9], abs=2.1) for g in res["gaps"])


def test_the_net_job_and_the_board_check_report_and_place_the_gaps(tmp_path):
    b = board(plane("InternalPlane1", BIG, holes=[rect(400, -100, 430, 100),
                                                  rect(700, -100, 715, 100)]),
              plane("InternalPlane2", BIG, "VCC", holes=[rect(900, 1000, 1100, 1100)]))
    spec = {"net": "SIG", "points": 80, "rules": False}
    res = net_jobs.run_net_channel(spec, b.to_dict(), STACKUP, tmp_path / "a")
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["return_gaps"] == 3 and m["return_gap_mils"] == pytest.approx(100, abs=2.1)
    v = next(x for x in res["verdicts"] if x["check"] == "return path")
    assert not v["pass"] and "severity" not in v
    assert v["detail"].startswith("crosses a gap of") and "400 mil along the route on TopLayer" \
        in v["detail"] and v["detail"].endswith("; 2 more")
    saved = json.loads((tmp_path / "a" / "return_path.json").read_text())
    assert saved["gaps"] == res["figures"]["return_path"] and len(saved["gaps"]) == 3
    # only the stripline's one-sided gap left: a warning
    only = board(plane("InternalPlane1", BIG),
                 plane("InternalPlane2", BIG, "VCC", holes=[rect(900, 1000, 1100, 1100)]))
    res = net_jobs.run_net_channel(spec, only.to_dict(), STACKUP, tmp_path / "b")
    v = next(x for x in res["verdicts"] if x["check"] == "return path")
    assert not v["pass"] and v["severity"] == "warning"
    assert "the other reference plane is whole there" in v["detail"]
    # whole planes pass; the check can be left out; a bad minimum is refused
    whole = board(plane("InternalPlane1", BIG), plane("InternalPlane2", BIG, "VCC"))
    res = net_jobs.run_net_channel(spec, whole.to_dict(), STACKUP, tmp_path / "c")
    v = next(x for x in res["verdicts"] if x["check"] == "return path")
    assert v["pass"] and v["detail"] == "reference copper under the whole route"
    res = net_jobs.run_net_channel({**spec, "return_path": False}, b.to_dict(), STACKUP,
                                   tmp_path / "d")
    assert not any(x["check"] == "return path" for x in res["verdicts"])
    assert "return_gaps" not in res["metrics"]
    res = net_jobs.run_net_channel({**spec, "return_gap_min_mils": 20}, b.to_dict(), STACKUP,
                                   tmp_path / "f")
    assert res["metrics"]["return_gaps"]["value"] == 2       # the 15 mil slot no longer counts
    with pytest.raises(ValueError, match="return_gap_min_mils"):
        net_jobs.run_net_channel({**spec, "return_gap_min_mils": -1}, b.to_dict(), STACKUP,
                                 tmp_path / "e")
    # the board check: one finding per gap, each at its place, the mild one a warning
    bs = {"targets": [{"match": "SIG", "kind": "net", "z_tol_pct": 60, "data_rate_gbps": 2}],
          "points": 80, "f_max_ghz": 10}
    out = board_jobs.run_board_si(bs, b.to_dict(), STACKUP, tmp_path / "x", snapshot="t")
    fs = [f for f in out["figures"]["findings"] if f["check"] == "return path"]
    assert [(f["severity"], f["layer"]) for f in fs] == [
        ("error", "TopLayer"), ("error", "TopLayer"), ("warning", "MidLayer1")]
    assert fs[0]["at_mils"] == pytest.approx([400, 0], abs=2.1)
    assert fs[1]["at_mils"] == pytest.approx([700, 0], abs=2.1)
    assert fs[2]["at_mils"] == pytest.approx([1000, 1000], abs=2.1)
    assert "mil along the route, crosses a gap of" in fs[0]["where"]
    head = (tmp_path / "x" / "summary.csv").read_text().splitlines()[0]
    assert "return_gaps" in head and "return_gap_mils" in head
    out = board_jobs.run_board_si({**bs, "return_path": False}, b.to_dict(), STACKUP,
                                  tmp_path / "y", snapshot="t")
    assert not [f for f in out["figures"]["findings"] if f["check"] == "return path"]


def test_points_are_sampled_evenly_and_rings_are_tested_as_drawn():
    pts, s = return_path._sample([(0, 0), (100, 0), (100, 50)], 2.0)
    assert len(pts) == 75 and s[0] == pytest.approx(1.0) and s[-1] == pytest.approx(149.0)
    assert pts[49] == pytest.approx([99, 0]) and pts[50] == pytest.approx([100, 1])
    x = np.array([5.0, 15.0, 5.0, -1.0])
    y = np.array([5.0, 5.0, 15.0, 5.0])
    # an L: the notch at the top right is outside
    ring = [(0, 0), (20, 0), (20, 10), (10, 10), (10, 20), (0, 20)]
    assert return_path._in_ring(x, y, ring).tolist() == [True, True, True, False]
    assert return_path._in_ring(np.array([15.0]), np.array([15.0]), ring).tolist() == [False]
