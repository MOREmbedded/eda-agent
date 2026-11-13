# SPDX-License-Identifier: Apache-2.0
"""The report, the heat maps, the run layer and the MCP tool, on boards
built by hand: no Altium."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent.layout.copper_raster import rasterize
from eda_agent.thermal import physics as ph
from eda_agent.thermal import run as tr
from eda_agent.thermal.analysis import (
    HeatMap, analyze, auto_cell_mils, board_area_mils2, component_temps, find_hotspots,
    parse_packages,
)
from eda_agent.thermal.render import palette, render_all, render_layer, save_npz
from eda_agent.thermal.solver import HeatArea, Package, Setup, solve
from eda_agent.tools.thermal import register_thermal_tools

from .boards import FOUR_LAYER_STACKUP, add_chip, demo_board, rect, two_layer

POWERS = {"U1": 1.0, "U2": 1.5, "R*": 0.1}
PACKAGES = {"U1": {"theta_jb": 12.0, "tj_max_c": 125.0},
            "U2": {"psi_jb": 8.0, "theta_jc": 20.0, "tj_max_c": 80.0}}


@pytest.fixture(scope="module")
def demo():
    board, stack = demo_board()
    report, result = analyze(board, stack, powers=POWERS, packages=parse_packages(PACKAGES),
                             cell_mils=25.0, limit_c=60.0)
    return board, stack, report, result


# -- the report ---------------------------------------------------------------

def test_report_has_what_a_designer_reads(demo):
    board, _, rep, res = demo
    assert rep["board"] == "demo.PcbDoc" and rep["ambient_c"] == 25.0
    assert rep["power_w"] == pytest.approx(2.8)
    assert rep["t_max_c"] == pytest.approx(res.t_max_c, abs=0.01)
    assert rep["rise_max_k"] == pytest.approx(rep["t_max_c"] - 25.0, abs=0.01)
    assert rep["t_max_layer"] == "TopLayer"
    assert [l["name"] for l in rep["layers"]] == ["TopLayer", "InternalPlane1",
                                                  "InternalPlane2", "BottomLayer"]
    top = rep["layers"][0]
    assert top["t_min_c"] < top["t_mean_c"] < top["t_max_c"]
    assert rep["layers"][1]["copper_fraction"] > 0.98          # a solid plane
    assert rep["balance"]["error_pct"] < 1e-4
    assert rep["grid"]["cell_mils"] == 25.0 and rep["grid"]["auto"] is False
    assert rep["grid"]["converged"] and rep["grid"]["unknowns"] > rep["grid"]["cells"]
    assert rep["stackup"] == {"assumed": False, "thickness_mils": pytest.approx(60.0, abs=0.1),
                              "layers": 4}
    assert rep["setup"]["orientation"] == "horizontal" and "h_top" not in rep["setup"]
    assert set(rep["convection"]) == {"top", "bottom"}
    json.dumps(rep)                                            # plain JSON all the way down


def test_parts_are_judged_against_their_limits(demo):
    _, _, rep, _ = demo
    parts = {p["ref"]: p for p in rep["parts"]}
    assert set(parts) == {"U1", "U2", "R1", "R2", "R3"}
    u1, u2 = parts["U1"], parts["U2"]
    assert u1["t_junction_c"] - u1["t_pads_c"] == pytest.approx(12.0, abs=0.01)
    assert u1["margin_k"] == pytest.approx(125.0 - u1["t_junction_c"], abs=0.01)
    assert u2["t_pads_c"] < u2["t_case_c"] < u2["t_junction_c"]
    # the hottest junction first
    temps = [p.get("t_junction_c", p["t_pads_c"]) for p in rep["parts"]]
    assert temps == sorted(temps, reverse=True)
    # U2 runs over its 80 C limit, U1 stays under 125 C
    assert u2["margin_k"] < 0 < u1["margin_k"]
    assert rep["over_tj_max"] == ["U2"] and rep["pass"] is False
    assert "t_junction_c" not in parts["R1"]                   # no theta_jb given


def test_unpowered_parts_and_the_board_limit(demo):
    _, _, rep, _ = demo
    hottest = rep["hottest_components"]
    assert [c["ref"] for c in hottest] == ["C5"]               # the only unpowered part
    assert 30.0 < hottest[0]["t_c"] < rep["t_max_c"]
    assert rep["limit_c"] == 60.0
    assert "U2" in rep["over_limit"] and "R3" not in rep["over_limit"]


def test_hotspots_sit_on_the_powered_parts(demo):
    board, _, rep, res = demo
    spots = rep["hotspots"]
    assert spots and spots[0]["t_c"] == pytest.approx(rep["t_max_c"], abs=0.01)
    assert spots[0]["nearest"] == "U2" and spots[0]["distance_mils"] < 100
    assert {"U1", "U2"} <= {s["nearest"] for s in spots}
    assert [s["t_c"] for s in spots] == sorted((s["t_c"] for s in spots), reverse=True)
    assert all(s["rise_k"] >= 0.25 * rep["rise_max_k"] - 0.01 for s in spots)
    assert find_hotspots(res, board, limit=1) == spots[:1]
    by_ref = {c["ref"]: c for c in component_temps(res, board)}
    assert by_ref["U2"]["t_c"] > by_ref["U1"]["t_c"] > by_ref["R3"]["t_c"]


def test_a_pass_needs_every_limit_met():
    board, stack = demo_board()
    rep, _ = analyze(board, stack, powers={"U1": 0.2},
                     packages=parse_packages({"U1": {"theta_jb": 12, "tj_max_c": 125}}),
                     cell_mils=50.0, limit_c=100.0)
    assert rep["pass"] is True and rep["over_tj_max"] == [] and rep["over_limit"] == []
    assert not any("no theta_jb" in n for n in rep["notes"])
    rep, _ = analyze(board, stack, powers={"U1": 0.2}, cell_mils=50.0)
    assert any("no theta_jb" in n for n in rep["notes"])
    # Nothing to judge against is not a pass.
    assert rep["pass"] is None and any("no verdict" in n for n in rep["notes"])


def test_nothing_to_dissipate_is_an_error():
    board, stack = demo_board()
    with pytest.raises(ValueError, match="nothing dissipates"):
        analyze(board, stack)
    with pytest.raises(ValueError, match="nothing dissipates"):
        analyze(board, stack, powers={"U1": 0.0})


# -- grid -------------------------------------------------------------------------

def test_automatic_cell_size_follows_the_board():
    board, _ = demo_board()                       # 3 x 2 in
    assert board_area_mils2(board) == 6.0e6
    assert auto_cell_mils(board) == 12.5          # 38,400 cells
    small, _ = two_layer(500, 400)
    assert auto_cell_mils(small) == 5.0
    big, _ = two_layer(12000, 9000)
    assert auto_cell_mils(big) == 100.0
    holed, _ = two_layer(1000, 1000)
    holed.cutouts.append(rect(0, 0, 500, 1000))
    assert board_area_mils2(holed) == 0.5e6


def test_refine_reports_how_far_the_numbers_moved():
    board, stack = demo_board()
    pk = parse_packages(PACKAGES)
    coarse, _ = analyze(board, stack, powers=POWERS, packages=pk, cell_mils=50.0)
    rep, res = analyze(board, stack, powers=POWERS, packages=pk, cell_mils=50.0, refine=True)
    ref = rep["refinement"]
    assert ref["cells_mils"] == [50.0, 25.0] and rep["grid"]["cell_mils"] == 25.0
    assert res.raster.cell_mils == 25.0
    assert ref["t_max_c"]["coarse_c"] == pytest.approx(coarse["t_max_c"], abs=0.01)
    assert ref["t_max_c"]["fine_c"] == pytest.approx(rep["t_max_c"], abs=0.01)
    rows = {r["ref"]: r for r in ref["parts"]}
    u1 = rows["U1"]
    assert u1["quantity"] == "t_junction_c"
    assert u1["delta_k"] == pytest.approx(u1["fine_c"] - u1["coarse_c"], abs=2e-3)
    assert u1["extrapolated_c"] == pytest.approx(u1["fine_c"] + u1["delta_k"], abs=0.01)
    assert rows["R1"]["quantity"] == "t_pads_c"
    worst = max([abs(r["delta_k"]) for r in ref["parts"]] + [abs(ref["t_max_c"]["delta_k"])])
    assert ref["estimated_error_k"] == pytest.approx(worst, abs=0.01)
    # the ICs on thermal vias are settled to a fraction of a kelvin already
    assert abs(rows["U1"]["delta_k"]) < 1.0 and abs(rows["U2"]["delta_k"]) < 1.0


def test_refine_is_refused_for_heat_tied_to_one_grid():
    board, stack = demo_board()
    r = rasterize(board, cell_mils=100)
    with pytest.raises(ValueError, match="refine"):
        analyze(board, stack, extra_heat_w={"TopLayer": np.ones((r.ny, r.nx)) * 1e-3},
                cell_mils=100.0, refine=True)


# -- heat maps from another grid ------------------------------------------------------

def test_heat_map_moves_to_the_thermal_grid_and_keeps_every_watt(tmp_path):
    board, stack = two_layer(1000, 600)
    w = np.zeros((6, 10))                          # a 100 mil grid
    w[2:4, 1:3] = 0.25                             # 1 W over x 100..300, y 200..400
    hm = HeatMap(0.0, 0.0, 100.0, {"TopLayer": w})
    assert hm.power_w == pytest.approx(1.0)
    for cell in (40.0, 25.0, 130.0):
        moved, lost = hm.on(rasterize(board, cell_mils=cell))
        assert moved["TopLayer"].sum() == pytest.approx(1.0, rel=1e-12) and lost == pytest.approx(0)
    moved, _ = hm.on(rasterize(board, cell_mils=50.0))
    assert moved["TopLayer"][4:8, 2:6] == pytest.approx(1.0 / 16)
    assert moved["TopLayer"].sum() - moved["TopLayer"][4:8, 2:6].sum() == pytest.approx(0)
    # the same heat as a rectangle gives the same field
    a, _ = analyze(board, stack, heat_maps=[hm], cell_mils=50.0)
    b, _ = analyze(board, stack, heat_areas=[HeatArea("TopLayer", 100, 200, 300, 400, 1.0)],
                   cell_mils=50.0)
    assert a["t_max_c"] == pytest.approx(b["t_max_c"], abs=0.01)
    assert a["power_w"] == pytest.approx(1.0)
    # a map hanging off the board loses that part, and says so
    off = HeatMap(900.0, 0.0, 100.0, {"TopLayer": np.full((6, 2), 0.1)})
    rep, _ = analyze(board, stack, heat_maps=[off], cell_mils=50.0)
    assert rep["power_w"] == pytest.approx(0.6)
    assert any("outside the board" in n for n in rep["notes"])
    # refine works with a heat map (it is put on each grid in turn)
    rep, _ = analyze(board, stack, heat_maps=[hm], cell_mils=50.0, refine=True)
    assert rep["refinement"]["cells_mils"] == [50.0, 25.0]

    path = tmp_path / "ir.npz"
    np.savez_compressed(path, x0=0.0, y0=0.0, cell_mils=100.0, layers=np.array(["TopLayer"]),
                        W_TopLayer=w, V_TopLayer=w * 0)
    back = HeatMap.load(path)
    assert back.cell_mils == 100.0 and list(back.layers) == ["TopLayer"]
    assert back.power_w == pytest.approx(1.0)
    np.savez_compressed(tmp_path / "bad.npz", x0=0.0, y0=0.0, cell_mils=100.0)
    with pytest.raises(ValueError, match="W_<layer>"):
        HeatMap.load(tmp_path / "bad.npz")
    with pytest.raises(ValueError, match="not a copper layer"):
        analyze(board, stack, heat_maps=[HeatMap(0, 0, 100, {"Mid9": w})], cell_mils=50.0)


# -- packages ----------------------------------------------------------------------

def test_parse_packages():
    got = parse_packages({"U1": {"psi_jb": 7, "tj_max_c": "125"}, "U2": {"theta_jc": None}})
    assert got["U1"] == Package(theta_jb=7.0, tj_max_c=125.0) and got["U2"] == Package()
    assert parse_packages(None) == {}
    with pytest.raises(ValueError, match="unknown keys"):
        parse_packages({"U1": {"theta_ja": 40}})
    with pytest.raises(ValueError, match="must be a number"):
        parse_packages({"U1": {"theta_jb": "hot"}})
    with pytest.raises(ValueError, match="must be an object"):
        parse_packages({"U1": 5})


# -- pictures and files --------------------------------------------------------------

def test_palette_runs_dark_to_bright():
    pal = palette()
    assert pal.shape == (256, 3) and pal.dtype == np.uint8
    luma = pal @ np.array([0.299, 0.587, 0.114])
    assert np.all(np.diff(luma) > -0.5) and luma[-1] > luma[0] + 150


def test_heat_maps_are_written_per_layer_with_one_scale(demo, tmp_path):
    from PIL import Image

    board, _, _, res = demo
    files = render_all(res, board, tmp_path)
    assert list(files) == res.raster.layers
    sizes = set()
    for layer, path in files.items():
        with Image.open(path) as im:
            assert im.format == "PNG" and im.mode == "RGB"
            sizes.add(im.size)
            px = np.asarray(im)
        assert px.std() > 10                                    # not a blank sheet
    assert len(sizes) == 1 and next(iter(sizes))[0] >= 1300
    one = render_layer(res, board, "TopLayer", tmp_path / "sub" / "top.png",
                       width_px=600, title="Top", labels=["U1"])
    assert one.is_file()


def test_the_hottest_spot_is_the_brightest_part_of_the_map(demo, tmp_path):
    from PIL import Image

    board, _, _, res = demo
    path = render_layer(res, board, "InternalPlane1", tmp_path / "p.png", width_px=800)
    with Image.open(path) as im:
        px = np.asarray(im).astype(float)
    t = res.temps["InternalPlane1"]
    iy, ix = np.unravel_index(np.nanargmax(t), t.shape)
    scale = (800 - 150 - 2 * 24) / t.shape[1]
    # a patch a few cells off the peak marker, and one in a cold corner
    hot = px[int(64 + (t.shape[0] - iy - 3) * scale), int(24 + (ix + 3) * scale)]
    cold = px[int(64 + (t.shape[0] - 2) * scale), int(24 + (t.shape[1] - 2) * scale)]
    assert hot.sum() > cold.sum() + 150


def test_npz_holds_the_fields_and_their_grid(demo, tmp_path):
    _, _, _, res = demo
    path = save_npz(res, tmp_path / "out" / "result.npz")
    with np.load(path) as z:
        assert float(z["cell_mils"]) == 25.0 and float(z["ambient_c"]) == 25.0
        assert list(z["layers"]) == res.raster.layers
        t = z["T_TopLayer"]
        assert t.shape == (res.raster.ny, res.raster.nx)
        assert np.nanmax(t) == pytest.approx(np.nanmax(res.temps["TopLayer"]), abs=1e-3)


# -- the run layer -------------------------------------------------------------------

def test_setup_from_tool_arguments():
    s = tr.make_setup(40.0, "vertical", 1.5, 90.0, 0.0,
                      {"emissivity": 0.8, "via_fill": "copper", "radiation": False, "max_iter": 30})
    assert (s.ambient_c, s.orientation, s.airflow_ms, s.airflow_deg) == (40.0, "vertical", 1.5, 90.0)
    assert (s.emissivity, s.via_fill, s.radiation, s.max_iter) == (0.8, "copper", False, 30)
    high = tr.make_setup(altitude_m=3000.0)
    assert high.pressure_pa == pytest.approx(70_109, rel=2e-3)   # ISO 2533 at 3000 m
    assert ph.pressure_at_altitude(0.0) == ph.P_ATM
    for bad in ({"colour": "red"}, {"radiation": "yes"}, {"emissivity": "shiny"},
                {"dielectric": "cheese"}):
        with pytest.raises(ValueError):
            tr.make_setup(options=bad)
    with pytest.raises(ValueError, match="not both"):
        tr.make_setup(altitude_m=1000.0, options={"pressure_pa": 9e4})
    with pytest.raises(ValueError, match="orientation"):
        tr.make_setup(orientation="sideways")


def test_run_merges_an_inputs_file_with_arguments(tmp_path):
    board, stack = demo_board()
    inputs = tmp_path / "in.json"
    inputs.write_text(json.dumps({
        "powers": {"U1": 0.5, "U2": 0.5}, "packages": {"U1": {"theta_jb": 10}},
        "heat_areas": [{"layer": "BottomLayer", "x0": 100, "y0": 100, "x1": 300, "y1": 300,
                        "power_w": 0.25}],
        "options": {"emissivity": 0.5}}))
    rep, res, used = tr.run(board, stack, inputs_json_path=str(inputs), powers={"U2": 2.0},
                            options={"via_fill": "copper"}, cell_mils=50.0, ambient_c=30.0)
    assert used["powers"] == {"U1": 0.5, "U2": 2.0}             # the argument wins
    assert rep["power_w"] == pytest.approx(2.75)
    assert rep["setup"]["emissivity"] == 0.5 and rep["setup"]["via_fill"] == "copper"
    assert rep["ambient_c"] == 30.0 and used["cell_mils"] == 50.0
    assert {p["ref"] for p in rep["parts"]} == {"U1", "U2"}

    files = tr.write_files(rep, res, board, tmp_path / "run", used)
    names = sorted(p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in files)
    assert names == ["inputs.json", "report.json", "result.npz",
                     "thermal_BottomLayer.png", "thermal_InternalPlane1.png",
                     "thermal_InternalPlane2.png", "thermal_TopLayer.png"]
    assert json.loads((tmp_path / "run" / "report.json").read_text())["t_max_c"] == rep["t_max_c"]
    again = json.loads((tmp_path / "run" / "inputs.json").read_text())
    rep2, _, _ = tr.run(board, stack, **{k: again[k] for k in (
        "powers", "packages", "heat_areas", "clamps", "ambient_c", "options", "cell_mils")})
    assert rep2["t_max_c"] == rep["t_max_c"]                    # a run can be repeated from it

    (tmp_path / "bad.json").write_text("{\"power\": {}}")
    with pytest.raises(ValueError, match="unknown keys"):
        tr.run(board, stack, inputs_json_path=str(tmp_path / "bad.json"))
    with pytest.raises(ValueError, match="could not read"):
        tr.run(board, stack, inputs_json_path=str(tmp_path / "none.json"))
    with pytest.raises(ValueError, match="watts"):
        tr.run(board, stack, powers={"U1": "lots"})
    with pytest.raises(ValueError, match="missing"):
        tr.run(board, stack, powers={"U1": 1}, heat_areas=[{"layer": "TopLayer"}])
    with pytest.raises(ValueError, match="missing"):
        tr.run(board, stack, powers={"U1": 1}, clamps=[{"x0": 0}])


def test_runs_folder_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("EDA_AGENT_THERMAL_RUNS", raising=False)
    assert tr.runs_dir().parts[-2:] == ("EDA Agent", "thermal")
    monkeypatch.setenv("EDA_AGENT_THERMAL_RUNS", str(tmp_path / "t"))
    assert tr.runs_dir() == tmp_path / "t"
    assert tr.runs_dir(str(tmp_path / "named")) == tmp_path / "named"
    a = tr.run_folder(tmp_path, "My Board (rev B).PcbDoc")
    assert a.name.startswith("My_Board_rev_B_")
    a.mkdir()
    assert tr.run_folder(tmp_path, "My Board (rev B).PcbDoc") != a


# -- the MCP tool --------------------------------------------------------------------

@pytest.fixture(scope="module")
def tools():
    m = FastMCP("t")
    register_thermal_tools(m)
    return m


def _call(m, args):
    out = asyncio.run(m.call_tool("sim_thermal_analysis", args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_tool_on_a_saved_board(tools, tmp_path):
    board, _ = demo_board()
    path = tmp_path / "b.json"
    board.save(path)
    st = tmp_path / "stack.json"
    st.write_text(json.dumps(FOUR_LAYER_STACKUP))
    out = _call(tools, {"board_json_path": str(path), "stackup_json_path": str(st),
                        "powers": POWERS, "packages": PACKAGES, "cell_mils": 50,
                        "runs_dir": str(tmp_path / "runs"), "limit": 3})
    assert out["ok"] and out["source"] == "file"
    assert out["stackup"]["assumed"] is False and out["pass"] is False
    assert out["over_tj_max"] == ["U2"] and len(out["hotspots"]) <= 3
    assert out["run_dir"].startswith(str(tmp_path / "runs"))
    assert len(out["files"]) == 7 and all(__import__("os").path.isfile(f) for f in out["files"])
    assert any("steady state" in a for a in out["assumptions"])

    # no stackup: a 1.6 mm FR-4 board is assumed and the reply says so
    out = _call(tools, {"board_json_path": str(path), "powers": {"U1": 1.0}, "cell_mils": 100,
                        "write_maps": False})
    assert out["ok"] and out["stackup"]["assumed"] is True and "files" not in out


def test_tool_errors_are_replies_not_exceptions(tools, tmp_path):
    out = _call(tools, {"powers": {"U1": 1.0}})
    assert not out["ok"] and "expect_file" in out["error"]
    board, _ = demo_board()
    path = tmp_path / "b.json"
    board.save(path)
    base = {"board_json_path": str(path), "write_maps": False, "cell_mils": 100}
    assert "nothing dissipates" in _call(tools, base)["error"]
    assert "no component 'U9'" in _call(tools, {**base, "powers": {"U9": 1.0}})["error"]
    assert "orientation" in _call(tools, {**base, "powers": {"U1": 1}, "orientation": "up"})["error"]
    assert "unknown options" in _call(tools, {**base, "powers": {"U1": 1},
                                              "options": {"fan": True}})["error"]
    assert "too fine" in _call(tools, {**base, "powers": {"U1": 1}, "cell_mils": 0.5})["error"]


def test_solver_result_lookup(demo):
    _, _, _, res = demo
    assert res.at("TopLayer", 2100, 1300) > res.at("TopLayer", 100, 100) > 25.0
    assert res.t_min_c > 25.0 and res.t_max_c >= float(np.nanmax(res.temps["TopLayer"]))


# -- a stackup that was never set up ---------------------------------------------------

def test_a_stackup_with_no_dielectric_is_filled_in_and_said_so():
    """Altium reports a board whose layer stack was never edited with no
    dielectric between the copper layers. Solved as read it is a board a
    few mils thick; the missing heights come from a 1.6 mm board."""
    from eda_agent.emc.stack import stack_from_stackup
    from eda_agent.thermal.analysis import usable_stack

    board, good = demo_board()
    bare = stack_from_stackup({"layers": [
        {"layer_id": 1, "copper_thickness_mils": 1.4}, {"layer_id": 39, "copper_thickness_mils": 0},
        {"layer_id": 40, "copper_thickness_mils": 1.4}, {"layer_id": 32, "copper_thickness_mils": 1.4},
    ]}, board.layers)
    assert bare.thickness_mils == 0.0
    fixed = usable_stack(bare)
    assert fixed.thickness_mils == pytest.approx(63.0, abs=0.1) and fixed.assumed is True
    assert [l.name for l in fixed.layers] == [l.name for l in bare.layers]
    assert [round(d.height_mils, 1) for d in fixed.dielectrics] == [8.0, 47.0, 8.0]
    assert fixed.get("InternalPlane1").copper_mils == 1.4
    assert any("no dielectric between TopLayer/InternalPlane1 8 mil" in n for n in fixed.notes)
    assert any("no copper thickness for InternalPlane1" in n for n in fixed.notes)
    assert usable_stack(good) is good                           # a real stackup is left alone

    rep, _ = analyze(board, bare, powers={"U1": 1.0}, cell_mils=50.0)
    ref, _ = analyze(board, usable_stack(bare), powers={"U1": 1.0}, cell_mils=50.0)
    assert rep["t_max_c"] == ref["t_max_c"] and rep["stackup"]["assumed"] is True
    assert rep["stackup"]["thickness_mils"] == pytest.approx(63.0, abs=0.1)
    assert any("Set the layer stack" in n for n in rep["notes"])

    # one height missing: only that one is assumed, the stack stays the board's own
    part = stack_from_stackup({"layers": [
        {"layer_id": 1, "copper_thickness_mils": 1.4, "dielectric_type": "PrePreg",
         "dielectric_height_mils": 5.0, "dielectric_constant": 4.0},
        {"layer_id": 39, "copper_thickness_mils": 1.4},
        {"layer_id": 40, "copper_thickness_mils": 1.4, "dielectric_type": "PrePreg",
         "dielectric_height_mils": 5.0, "dielectric_constant": 4.0},
        {"layer_id": 32, "copper_thickness_mils": 1.4}]}, board.layers)
    fixed = usable_stack(part)
    assert [round(d.height_mils, 1) for d in fixed.dielectrics] == [5.0, 47.0, 5.0]
    assert fixed.assumed is False and fixed.dielectrics[0].er == 4.0


def test_a_board_as_the_altium_reader_gives_it_is_solved():
    """The reader's own output, from the bridge replies its tests use: a
    plane layer, a pour, a keep-out fill, a board cutout, a mounting hole
    and a part with one pad. Nothing in it is built by hand for the
    solver."""
    from eda_agent.emc.stack import assumed_stack
    from eda_agent.layout.read_altium import read_board
    from tests.layout.test_read_altium import _replies

    send, _ = _replies()
    board = read_board(send)
    rep, res = analyze(board, assumed_stack(board.layers), powers={"U1": 0.3}, cell_mils=10.0)
    frac = {row["name"]: row["copper_fraction"] for row in rep["layers"]}
    # The plane is solid but for the mounting hole's antipad and the via's;
    # the top layer holds one pad and one track, and the keep-out fill is
    # not copper.
    assert 0.98 < frac["InternalPlane1"] < 1.0
    assert 0.004 < frac["TopLayer"] < 0.01
    assert rep["balance"]["error_pct"] < 0.01
    assert rep["t_max_layer"] == "TopLayer"
    assert rep["t_max_at_mils"] == pytest.approx([100.0, 200.0], abs=10.0)
    assert np.isfinite(res.t_max_c) and rep["t_max_c"] > rep["layers"][1]["t_max_c"] + 50
    assert rep["stackup"]["assumed"] is True
