# SPDX-License-Identifier: Apache-2.0
"""Board SI: selection, located findings, the report, the thermal link,
the job graph and the Altium marks."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent import simulation as sim
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.layout.model import LayoutBoard, Track
from eda_agent.si import board_jobs, extract, net_model
from eda_agent.si import snapshot as snap
from eda_agent.si import thermal_link as th
from eda_agent.simulation import jobs as _jobs


from .boards import LAYERS, STACKUP, mixed_board, pair_board, single_net, smd
from .test_nets import _rlgc


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


# -- thermal link -------------------------------------------------------------------------

def test_hot_copper_scales_dc_fully_and_skin_by_the_square_root():
    f = np.concatenate([[1e3], np.geomspace(1e6, 1e11, 40)])
    r = _rlgc(f)
    # make R(f) a real skin-effect curve: R_dc below, sqrt(f) far above
    R = np.array([3.0 * math.sqrt(1 + (x / 5e6)) for x in f])
    r.R[:, 0, 0] = R
    hot = th.scale_r(r, 120.0)
    s = 1 + th.ALPHA_CU * 100
    assert hot.R[0, 0, 0] / R[0] == pytest.approx(s, rel=1e-4)
    assert hot.R[-1, 0, 0] / R[-1] == pytest.approx(math.sqrt(s), rel=5e-3)
    assert th.scale_r(r, 20.0) is r
    assert np.array_equal(hot.L, r.L) and np.array_equal(hot.G, r.G)
    with pytest.raises(ValueError):
        th.scale_r(r, -300)


def _npz(tmp_path, hot_c, cold_c=25.0):
    """A temperature map: the top layer hot left of x = 800, cool elsewhere."""
    x0, y0, cell = -100.0, -100.0, 10.0
    nx, ny = 300, 60
    top = np.full((ny, nx), cold_c, dtype=np.float32)
    top[:, : int((800 - x0) / cell)] = hot_c
    path = tmp_path / "result.npz"
    np.savez_compressed(path, x0=x0, y0=y0, cell_mils=cell,
                        layers=np.array(["TopLayer", "MidLayer1"]), ambient_c=25.0,
                        T_TopLayer=top, T_MidLayer1=np.full((ny, nx), cold_c, np.float32))
    return str(path)


def test_temperature_map_is_sampled_along_each_section(tmp_path):
    t = th.Temperatures(npz=_npz(tmp_path, 100.0))
    assert t.along("TopLayer", [(0, 0), (600, 0)]) == pytest.approx(100.0)
    assert t.along("TopLayer", [(1000, 0), (1500, 0)]) == pytest.approx(25.0)
    mixed = t.along("TopLayer", [(600, 0), (1000, 0)])
    assert 25 < mixed < 100
    assert t.along("Nowhere", [(0, 0), (10, 0)]) is None
    assert th.Temperatures(uniform_c=50).along("Nowhere", []) == 50


def test_a_hot_board_loses_more_and_only_where_it_is_hot(tmp_path):
    b = single_net(with_stub=False)
    stack = stack_from_stackup(STACKUP, b.layers)
    p = extract.net_path(b, "SIG")
    f = np.linspace(0.1e9, 10e9, 100)
    cold = net_model.single(b, stack, p, f)
    same = net_model.single(b, stack, p, f, temps=th.Temperatures(uniform_c=20.0))
    assert np.allclose(same.s, cold.s, atol=1e-12)
    hot = net_model.single(b, stack, p, f, temps=th.Temperatures(npz=_npz(tmp_path, 105.0, 20.0)))
    k = 60
    gain = hot.line_loss_db[k] - cold.line_loss_db[k]
    assert gain > 0
    # only the first 1000 mil of top layer (600 hot, 400 half) heated: compare with
    # every trace at 105 degC, which must lose more
    all_hot = net_model.single(b, stack, p, f, temps=th.Temperatures(uniform_c=105.0))
    assert all_hot.line_loss_db[k] - cold.line_loss_db[k] > 1.5 * gain
    assert any("copper at" in n for n in hot.notes)
    assert abs(hot.s[k, 1, 0]) < abs(cold.s[k, 1, 0])


# -- selection ----------------------------------------------------------------------------

def test_targets_select_by_pattern_class_and_kind():
    b = mixed_board()
    items = board_jobs.select(b, {})
    assert [(i["kind"], i["name"]) for i in items] == [("pair", "D")]
    items = board_jobs.select(b, {"targets": [{"class": "fast", "data_rate_gbps": 5},
                                              {"match": "*", "data_rate_gbps": 1}]})
    got = {(i["kind"], i["name"]): i["target"] for i in items}
    assert got[("net", "SIG")] == 1                      # the first target wins
    assert got[("pair", "D")] == 2
    assert ("net", "D_P") not in got                     # a pair's nets are the pair
    only = board_jobs.select(b, {"targets": [{"match": "*", "kind": "net"}]})
    assert {i["kind"] for i in only} == {"net"}
    with pytest.raises(ValueError, match="no net class"):
        board_jobs.select(b, {"targets": [{"class": "NOPE"}]})
    with pytest.raises(ValueError, match="unknown keys"):
        board_jobs.select(b, {"targets": [{"match": "*", "il_max": 3}]})
    with pytest.raises(ValueError, match="no differential pairs"):
        board_jobs.select(single_net(), {})


# -- the job ---------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def board_run(tmp_path_factory):
    folder = tmp_path_factory.mktemp("board")
    spec = {"targets": [{"match": "D", "kind": "pair", "data_rate_gbps": 8, "eye": True,
                         "eye_min_mv": 900},
                        {"class": "FAST", "data_rate_gbps": 6, "il_max_db": 20}],
            "points": 120, "f_max_ghz": 12, "workers": 2}
    res = board_jobs.run_board_si(spec, mixed_board().to_dict(), STACKUP, folder,
                                  snapshot="test")
    return res, folder


def test_findings_are_placed_where_the_faults_are(board_run):
    res, _ = board_run
    fs = res["figures"]["findings"]
    by: dict = {}
    for f in fs:
        by.setdefault((f["item"], f["check"]), f)
    tee = by[("SIG", "impedance")]
    # the 300 mil test-point stub leaves the top run 600 mil from U1.1 (at y = 3000)
    assert tee["at_mils"][1] == pytest.approx(3000, abs=1)
    assert tee["at_mils"][0] == pytest.approx(600, abs=45)
    assert tee["layer"] == "TopLayer"
    stub = by[("SIG", "stub resonance")]
    assert stub["at_mils"] == pytest.approx([600, 3000], abs=0.5) and "TP1.1" in stub["where"]
    skew = by[("D", "skew")]
    assert skew["at_mils"] == pytest.approx([-50, 0], abs=0.5) and skew["where"] == "at U1.P"
    eye = by[("D", "eye")]
    assert eye["severity"] == "error" and "900 mV needed" in eye["detail"]
    assert [f["id"] for f in fs] == [f"F{i + 1}" for i in range(len(fs))]
    m = {k: v["value"] for k, v in res["metrics"].items()}
    assert m["items_checked"] == 2 and m["items_failed"] == 2
    assert m["errors"] == sum(f["severity"] == "error" for f in fs)
    assert 0 < m["smallest_eye_mv"] < 900


def test_report_and_files(board_run):
    res, folder = board_run
    data = json.loads((folder / "findings.json").read_text())
    assert data["snapshot"] == "test" and len(data["items"]) == 2
    page = (folder / "report.html").read_text()
    for f in res["figures"]["findings"]:
        assert f">{f['id']}<" in page
    assert page.count("data:image/png;base64,") >= 2 and "<script" not in page
    rows = (folder / "summary.csv").read_text().splitlines()
    assert rows[0].startswith("kind,name,errors,warnings") and len(rows) == 3


def test_a_net_that_cannot_be_modelled_is_a_warning(tmp_path):
    b = mixed_board()
    b.tracks = [t for t in b.tracks if t.net != "SIG"]          # pads only: nothing routed
    res = board_jobs.run_board_si({"targets": [{"match": "SIG", "data_rate_gbps": 5}],
                                   "points": 60, "f_max_ghz": 8}, b.to_dict(), STACKUP, tmp_path)
    fs = res["figures"]["findings"]
    assert len(fs) == 1 and fs[0]["severity"] == "warning" and fs[0]["check"] == "modelled"
    from eda_agent.layout.model import LayoutBoard
    with pytest.raises(ValueError, match="no nets with copper"):
        board_jobs.select(LayoutBoard.from_dict({**b.to_dict(), "pads": [], "tracks": [],
                                                 "vias": [], "arcs": [], "diff_pairs": []}), {})
    with pytest.raises(ValueError, match="no data rate"):
        board_jobs.run_board_si({"targets": [{"match": "SIG"}]}, b.to_dict(), STACKUP, tmp_path)


def test_a_slow_net_with_an_eye_gets_a_sweep_that_holds_its_bit():
    item = {"kind": "net", "name": "SIG", "settings": {"eye": True, "data_rate_gbps": 0.2}}
    # 10 GHz in 200 points covers 20 ns, four of this net's 5 ns bits
    s = board_jobs._item_spec(item, {}, 10.0, 200)
    assert s["points"] == 400                           # 8 bits: a 25 MHz step
    fast = {"kind": "net", "name": "SIG", "settings": {"eye": True, "data_rate_gbps": 10}}
    assert board_jobs._item_spec(fast, {}, 15.0, 200)["points"] == 200
    no_eye = {"kind": "net", "name": "SIG", "settings": {"data_rate_gbps": 0.2}}
    assert board_jobs._item_spec(no_eye, {}, 10.0, 200)["points"] == 200
    crawl = {"kind": "net", "name": "SIG", "settings": {"eye": True, "data_rate_gbps": 0.001}}
    assert board_jobs._item_spec(crawl, {}, 10.0, 200)["points"] == board_jobs.MAX_EYE_POINTS


def test_a_wide_section_in_an_off_target_line_gets_its_own_finding(tmp_path):
    from eda_agent.layout.model import Track
    b = single_net(with_stub=False, with_return_via=False, inner=0)
    b.vias = []
    b.pads[1].x, b.pads[1].y = 2400, 0
    b.tracks = [Track("TopLayer", 0, 0, 1000, 0, 4, "SIG"),
                Track("TopLayer", 1000, 0, 1400, 0, 20, "SIG"),
                Track("TopLayer", 1400, 0, 2400, 0, 4, "SIG")]
    res = board_jobs.run_board_si({"targets": [{"match": "SIG", "data_rate_gbps": 10,
                                                "z_target": 50, "z_tol_pct": 10}],
                                   "points": 200, "f_max_ghz": 20}, b.to_dict(), STACKUP,
                                  tmp_path)
    imp = [f for f in res["figures"]["findings"] if f["check"] == "impedance"]
    zs = [float(f["where"].split("TDR ")[1].split(" ohm")[0]) for f in imp]
    assert len(imp) >= 2 and min(zs) < 45 < 55 < max(zs)
    low = min(imp, key=lambda f: float(f["where"].split("TDR ")[1].split(" ohm")[0]))
    # the 20 mil section starts 1000 mil from U1.1
    assert low["at_mils"][0] == pytest.approx(1000, abs=60) and low["at_mils"][1] == 0
    assert len({tuple(f["at_mils"]) for f in imp}) == len(imp)
    assert [f["id"] for f in res["figures"]["findings"]] == \
        [f"F{i + 1}" for i in range(len(res["figures"]["findings"]))]


# -- the job graph -----------------------------------------------------------------------------

def test_lineage_links_snapshot_channel_and_eye(tmp_path):
    pcb = tmp_path / "t.PcbDoc"
    pcb.write_text("x")
    rec = snap.save(single_net(with_stub=False), STACKUP, str(pcb))
    r = sim.submit("net-channel", "eda-agent", {"net": "SIG", "data_rate_gbps": 6,
                                               "f_max_ghz": 10, "points": 100},
                   {"board": rec["board"], "stackup": rec["stackup"]}, "chan", True)
    ts = sorted(_jobs.get(r["job"]).work_folder("eda-agent").glob("channel.s*p"))[0]
    e = sim.submit("channel-eye", "eda-agent", {"data_rate_gbps": 6, "repeats": 1},
                   {"touchstone": str(ts)}, "eye", True)
    up = _jobs.lineage(r["job"])
    assert [s["snapshot"] for s in up["snapshots"]] == [rec["id"]]
    assert up["snapshots"][0]["stale"] is False and not up["stale"]
    assert [c["job"] for c in up["children"]] == [e["job"]]
    down = _jobs.lineage(e["job"])
    assert [p["job"] for p in down["parents"]] == [r["job"]] and not down["snapshots"]
    # a board job names its snapshot by the id the graph shows
    b = sim.submit("board-si", "eda-agent", {"targets": [{"match": "SIG", "data_rate_gbps": 6}],
                                            "f_max_ghz": 10, "points": 100},
                   {"board": rec["board"], "stackup": rec["stackup"]}, "board", True)
    found = _jobs.get(b["job"]).work_folder("eda-agent") / "findings.json"
    assert json.loads(found.read_text(encoding="utf-8"))["snapshot"] == rec["id"]
    os.utime(pcb, (time.time() + 10, time.time() + 10))
    assert _jobs.lineage(r["job"])["stale"] is True
    with pytest.raises(KeyError):
        _jobs.lineage("nope")


# -- the tool and Altium -----------------------------------------------------------------------

class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, command, params, timeout=None):
        self.sent.append((command, dict(params)))
        return {"success": True}


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_tool_marks_failing_nets_in_altium(tmp_path, monkeypatch):
    import eda_agent.bridge as bridge_mod
    from eda_agent.tools.si import register_si_tools
    br = _Bridge()
    monkeypatch.setattr(bridge_mod, "get_bridge", lambda: br)
    m = FastMCP("t")
    register_si_tools(m)
    bp, sp = tmp_path / "b.json", tmp_path / "s.json"
    pair_board(skew=0).save(bp)
    sp.write_text(json.dumps(STACKUP))
    out = _call(m, "si_board_check", {"board_json_path": str(bp), "stackup_json_path": str(sp),
                                      "options": {"data_rate_gbps": 8, "points": 80,
                                                  "f_max_ghz": 8},
                                      "mark": "text", "marker_layer": "Mechanical13"})
    assert out["ok"], out
    assert out["report"].endswith("report.html") and os.path.isfile(out["report"])
    hl = [p for c, p in br.sent if c == "generic.highlight_net"]
    assert [p["net_name"] for p in hl] == ["D_P", "D_N"]
    assert hl[0]["clear_existing"] == "true" and hl[1]["clear_existing"] == "false"
    texts = [p for c, p in br.sent if c == "pcb.place_text"]
    errs = [f for f in out["findings"] if f["severity"] == "error"]
    assert len(texts) == len(errs) and all(p["layer"] == "Mechanical13" for p in texts)
    assert texts[0]["text"].startswith("SI F1 ")
    # the markers of an earlier run are removed first, and only those
    cmds = [c for c, _ in br.sent]
    assert cmds.index("generic.delete_objects") < cmds.index("pcb.place_text")
    gone = next(p for c, p in br.sent if c == "generic.delete_objects")
    assert gone["object_type"] == "eTextObject"
    assert gone["filter"] == "Text~SI F* *|InComponent=false"
    # a stale snapshot: results, but no text written on the board
    pcb = tmp_path / "b.PcbDoc"
    pcb.write_text("x")
    from eda_agent.layout.model import LayoutBoard
    rec = snap.save(LayoutBoard.load(str(bp)), STACKUP, str(pcb))
    os.utime(pcb, (time.time() + 10, time.time() + 10))
    br.sent.clear()
    stale = _call(m, "si_board_check", {"snapshot": rec["id"], "mark": "text",
                                        "options": {"data_rate_gbps": 8, "points": 80,
                                                    "f_max_ghz": 8}})
    assert stale["snapshot"]["stale"] is True and "changed" in stale["marked"]["error"]
    assert not br.sent
    assert _call(m, "si_board_check", {"mark": "paint"})["ok"] is False
    # an edit that is not saved leaves the file as it was: the board is read again
    import eda_agent.layout.read_altium as ra
    rec = snap.save(LayoutBoard.load(str(bp)), STACKUP, str(pcb))
    args = {"snapshot": rec["id"], "mark": "text",
            "options": {"data_rate_gbps": 8, "points": 80, "f_max_ghz": 8}}
    edited = LayoutBoard.load(str(bp))
    edited.tracks[0].x2 += 10
    for live, written in ((edited, False), (LayoutBoard.load(str(bp)), True)):
        monkeypatch.setattr(ra, "read_live_board", lambda path, live=live: live)
        br.sent.clear()
        out = _call(m, "si_board_check", args)
        assert out["snapshot"]["stale"] is False
        assert bool([c for c, _ in br.sent if c == "pcb.place_text"]) is written
        assert ("not saved yet" in out["marked"].get("error", "")) is not written

    def gone_board(path):
        raise RuntimeError("another document is in front")
    monkeypatch.setattr(ra, "read_live_board", gone_board)
    br.sent.clear()
    out = _call(m, "si_board_check", args)
    assert "another document is in front" in out["marked"]["error"] and not br.sent
    # highlighting writes nothing on the board and needs no second read
    out = _call(m, "si_board_check", {**args, "mark": "highlight"})
    assert out["marked"]["highlighted"] == ["D_P", "D_N"]


def test_dielectric_layers_of_the_physical_stack_fill_a_bare_stackup(monkeypatch):
    from eda_agent.emc.stack import fill_dielectrics
    from eda_agent.si import planner
    from eda_agent.tools.si import read_live_stackup
    bare = {"layers": [
        {"layer_id": 1, "name": "Top Layer", "copper_thickness_mils": 1.4,
         "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0},
        {"layer_id": 32, "name": "Bottom Layer", "copper_thickness_mils": 1.4,
         "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0}]}
    phys = {"layers": [
        {"name": "Top Solder", "kind": "dielectric", "thickness_mils": 0.4, "constant": 3.5,
         "dielectric_type": 3},
        {"name": "Top Layer", "kind": "copper", "thickness_mils": 1.4},
        {"name": "Dielectric 1", "kind": "dielectric", "thickness_mils": 6.0, "constant": 4.8,
         "material": "FR-4", "dielectric_type": 1},
        {"name": "Dielectric 2", "kind": "dielectric", "thickness_mils": 2.0, "constant": 4.0,
         "material": "PP", "dielectric_type": 2},
        {"name": "Bottom Layer", "kind": "copper", "thickness_mils": 1.4},
        {"name": "Bottom Solder", "kind": "dielectric", "thickness_mils": 0.4, "constant": 3.5,
         "dielectric_type": 3}]}
    out = fill_dielectrics(bare, phys)
    top = out["layers"][0]
    assert top["dielectric_height_mils"] == 8.0 and top["dielectric_type"] == "Core"
    assert top["dielectric_constant"] == pytest.approx((6 * 4.8 + 2 * 4.0) / 8)
    assert out["dielectric_from_physical_stack"] == ["Top Layer"]
    assert bare["layers"][0]["dielectric_height_mils"] == 0          # a copy
    info = planner.layer_sections(stack_from_stackup(out), planner.Options())[0]
    assert info["h_mils"] == pytest.approx(8.0)
    # a stackup that has its dielectric is left alone, and so is a mismatch
    assert fill_dielectrics(STACKUP, phys) is STACKUP
    assert fill_dielectrics(bare, {"layers": phys["layers"][:3]}) is bare

    class B:
        def __init__(self, st):
            self.st, self.sent = st, []

        async def send_command_async(self, command, params, timeout=None):
            self.sent.append(command)
            return self.st if command == "pcb.get_layer_stackup" else phys
    b = B(bare)
    got = asyncio.run(read_live_stackup(b))
    assert got["layers"][0]["dielectric_height_mils"] == 8.0
    assert b.sent == ["pcb.get_layer_stackup", "pcb.physical_stack"]
    b = B(STACKUP)
    assert asyncio.run(read_live_stackup(b)) is STACKUP and b.sent == ["pcb.get_layer_stackup"]


def _bus(lengths):
    """Nets B0, B1, ... side by side on the top layer, 200 mil apart, each
    from UA to UB over its own length."""
    b = LayoutBoard(name="bus.PcbDoc", layers=list(LAYERS))
    for i, length in enumerate(lengths):
        y = 200.0 * i
        net = f"B{i}"
        b.pads += [smd("UA", str(i + 1), 0, y, net), smd("UB", str(i + 1), length, y, net)]
        b.tracks.append(Track("TopLayer", 0, y, length, y, 5, net))
    b.net_classes = {"BUS": [f"B{i}" for i in range(len(lengths))]}
    return b


def test_nets_of_a_target_are_matched_in_delay_to_the_slowest(tmp_path):
    b = _bus([2000, 1990, 1700, 2000])
    spec = {"targets": [{"class": "BUS", "data_rate_gbps": 2, "z_tol_pct": 60,
                         "delay_match_ps": 10}], "points": 80, "f_max_ghz": 10}
    res = board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / "a", snapshot="t")
    items = {r["name"]: r for r in json.loads((tmp_path / "a" / "findings.json").read_text())["items"]}
    d = {k: r["metrics"]["delay_ps"] for k, r in items.items()}
    per_mil = d["B0"] / 2000
    assert items["B0"]["metrics"]["delay_lead_ps"] == pytest.approx(0, abs=0.01)
    assert items["B2"]["metrics"]["delay_lead_ps"] == pytest.approx(300 * per_mil, rel=0.02)
    fs = [f for f in res["figures"]["findings"] if f["check"] == "delay match"]
    # 10 mil short is inside 10 ps; 300 mil short is not
    assert [f["item"] for f in fs] == ["B2"] and fs[0]["severity"] == "error"
    assert fs[0]["at_mils"] == [0.0, 400.0] and fs[0]["where"] == "at UA.3"
    lead = items["B2"]["metrics"]["delay_lead_ps"]
    assert f"{lead:.1f} ps ahead of net B" in fs[0]["detail"] and "10 ps allowed" in fs[0]["detail"]
    # the trace to add is the missing length
    add = float(fs[0]["detail"].split("about ")[1].split(" mil")[0])
    assert add == pytest.approx(300, abs=8)
    v = {k: next(x for x in r["verdicts"] if x["check"] == "delay match") for k, r in items.items()}
    assert [v[k]["pass"] for k in ("B0", "B1", "B2", "B3")] == [True, True, False, True]
    assert sum("the slowest of the 4 matched to it" in x["detail"] for x in v.values()) == 1
    assert "delay_lead_ps" in (tmp_path / "a" / "summary.csv").read_text().splitlines()[0]
    # a wider window passes; without the key nothing is matched; one net alone has no group
    res = board_jobs.run_board_si({**spec, "targets": [{**spec["targets"][0], "delay_match_ps": 80}]},
                                  b.to_dict(), STACKUP, tmp_path / "b", snapshot="t")
    assert not [f for f in res["figures"]["findings"] if f["check"] == "delay match"]
    plain = {k: v for k, v in spec["targets"][0].items() if k != "delay_match_ps"}
    res = board_jobs.run_board_si({**spec, "targets": [plain]}, b.to_dict(), STACKUP,
                                  tmp_path / "c", snapshot="t")
    items = json.loads((tmp_path / "c" / "findings.json").read_text())["items"]
    assert all("delay_lead_ps" not in r["metrics"] for r in items)
    assert not any(x["check"] == "delay match" for r in items for x in r["verdicts"])
    # each target is its own group: B2 alone in a second target is matched to nothing
    two = [{"match": "B2", "data_rate_gbps": 2, "z_tol_pct": 60, "delay_match_ps": 10},
           spec["targets"][0]]
    res = board_jobs.run_board_si({**spec, "targets": two}, b.to_dict(), STACKUP,
                                  tmp_path / "d", snapshot="t")
    assert not [f for f in res["figures"]["findings"] if f["check"] == "delay match"]
    alone = next(r for r in json.loads((tmp_path / "d" / "findings.json").read_text())["items"]
                 if r["name"] == "B2")
    assert "delay_lead_ps" not in alone["metrics"]
    assert not any(x["check"] == "delay match" for x in alone["verdicts"])
    with pytest.raises(ValueError, match="delay_match_ps"):
        board_jobs.run_board_si({**spec, "targets": [{**plain, "delay_match_ps": -1}]},
                                b.to_dict(), STACKUP, tmp_path / "e", snapshot="t")


def test_the_edge_checks_of_the_eye_are_placed_at_the_receiving_pin(tmp_path):
    b = _bus([3000])
    target = {"match": "B0", "kind": "net", "data_rate_gbps": 1, "z_tol_pct": 60, "eye": True,
              "tx_r_ohms": 15, "rx_r_ohms": "open", "rise_ps": 100,
              "overshoot_max_pct": 20, "ringback_max_pct": 60, "edge_reversal_max_pct": 5}
    spec = {"targets": [target], "f_max_ghz": 10}
    res = board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / "a", snapshot="t")
    item = json.loads((tmp_path / "a" / "findings.json").read_text())["items"][0]
    m = item["metrics"]
    assert 30 < m["eye_overshoot_pct"] < 70 and m["eye_ringback_pct"] > 10
    assert m["eye_edge_reversal_pct"] < 5 and m["eye_settling_ps"] > 0
    checks = {v["check"]: v["pass"] for v in item["verdicts"]}
    assert checks["overshoot"] is False and checks["ringback"] and checks["edge reversal"]
    f, = [x for x in res["figures"]["findings"] if x["check"] in board_jobs.EDGE_CHECKS]
    assert f["check"] == "overshoot" and f["severity"] == "error"
    assert f["at_mils"] == [3000.0, 0.0] and f["where"] == "at UB.1"
    r = m["eye_series_r_ohms"]
    assert 10 < r < 40 and f["detail"] == (
        f"{m['eye_overshoot_pct']:.1f} % of the settled level, 20 % allowed; {r:.0f} ohm in "
        "series at the driver brings it within 20 %")
    # with no limits the figures are there and nothing is judged
    for k in ("overshoot_max_pct", "ringback_max_pct", "edge_reversal_max_pct"):
        target.pop(k)
    res = board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / "b", snapshot="t")
    item = json.loads((tmp_path / "b" / "findings.json").read_text())["items"][0]
    assert item["metrics"]["eye_overshoot_pct"] == m["eye_overshoot_pct"]
    assert not any(v["check"] in board_jobs.EDGE_CHECKS for v in item["verdicts"])


def test_a_target_carries_its_equaliser_to_the_eye(tmp_path):
    b = _bus([8000])
    target = {"match": "B0", "kind": "net", "data_rate_gbps": 10, "z_tol_pct": 60, "eye": True}
    spec = {"targets": [target], "f_max_ghz": 30}

    def eye_of(name, **more):
        target.update(more)
        board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / name, snapshot="t")
        return json.loads((tmp_path / name / "findings.json").read_text())["items"][0]["metrics"]
    plain = eye_of("a")
    eq = eye_of("b", tx_ffe=[0.8, -0.2], dfe_taps=2)
    assert "eye_dfe_tap1_mv" in eq and "eye_dfe_tap1_mv" not in plain
    assert eq["eye_isi_mv"] < plain["eye_isi_mv"]
    assert eq["eye_main_cursor_mv"] < plain["eye_main_cursor_mv"]     # the FFE's price


def test_a_target_carries_its_bit_error_rate_to_the_eye(tmp_path):
    b = _bus([8000])
    target = {"match": "B0", "kind": "net", "data_rate_gbps": 5, "z_tol_pct": 60, "eye": True}
    spec = {"targets": [target], "f_max_ghz": 30}

    def item_of(name, **more):
        target.update(more)
        board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / name, snapshot="t")
        return json.loads((tmp_path / name / "findings.json").read_text())["items"][0]
    plain = item_of("a")
    assert "eye_height_at_ber_mv" not in plain["metrics"]
    it = item_of("b", ber=1e-12, noise_rms_mv=5, rj_rms_ps=2)
    m = it["metrics"]
    assert 0 < m["eye_height_at_ber_mv"] < m["eye_height_mv"] - 2 * 3 * 5
    assert 0 < m["eye_width_at_ber_ui"] < m["eye_width_ui"] and "bit_error_rate" in m
    assert not [f for f in it["findings"] if f["check"] == "eye at the bit error rate"]
    # a limit it does not meet is a finding of the net
    it = item_of("c", eye_at_ber_min_mv=m["eye_height_at_ber_mv"] + 5)
    f = [f for f in it["findings"] if f["check"] == "eye at the bit error rate"]
    assert len(f) == 1 and f[0]["severity"] == "error"
    # where the signal is judged: the far pin of the 8000 mil route, as the eye itself
    assert f[0]["at_mils"] == [8000.0, 0.0] and f[0]["where"].startswith("at ")
    it = item_of("d", eye_min_mv=5000)
    e = [x for x in it["findings"] if x["check"] == "eye"]
    assert len(e) == 1 and e[0]["at_mils"] == f[0]["at_mils"] and e[0]["where"] == f[0]["where"]
    assert f[0]["detail"].endswith(f", {m['eye_height_at_ber_mv'] + 5:g} mV needed")


def test_a_target_has_its_equaliser_chosen(tmp_path):
    b = _bus([8000])
    target = {"match": "B0", "kind": "net", "data_rate_gbps": 10, "z_tol_pct": 60, "eye": True}
    spec = {"targets": [target], "f_max_ghz": 30}

    def eye_of(name, **more):
        target.update(more)
        board_jobs.run_board_si(spec, b.to_dict(), STACKUP, tmp_path / name, snapshot="t")
        return json.loads((tmp_path / name / "findings.json").read_text())["items"][0]["metrics"]
    plain = eye_of("a")
    auto = eye_of("b", tx_ffe="auto", tx_ffe_post=2, ctle_db="auto", ctle_max_db=3)
    assert "eye_ffe_main_tap" in auto and 0 <= auto["eye_ctle_db"] <= 3
    assert "eye_ffe_main_tap" not in plain and "eye_ctle_db" not in plain
    assert auto["worst_case_eye_height_mv"] >= plain["worst_case_eye_height_mv"]
