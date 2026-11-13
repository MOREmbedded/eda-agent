# SPDX-License-Identifier: Apache-2.0
"""The PDN MCP tools over a saved board: no Altium."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent.tools.pdn import register_pdn_tools

from .boards import STACKUP, plane_board


@pytest.fixture(scope="module")
def tools():
    m = FastMCP("t")
    register_pdn_tools(m)
    return m


@pytest.fixture(scope="module")
def saved(tmp_path_factory):
    d = tmp_path_factory.mktemp("pdnboard")
    plane_board().save(d / "b.json")
    (d / "stack.json").write_text(json.dumps(STACKUP))
    return {"board_json_path": str(d / "b.json"), "stackup_json_path": str(d / "stack.json"),
            "runs_dir": str(d / "runs")}


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_ir_drop_tool(tools, saved):
    out = _call(tools, "sim_pdn_ir_drop", {
        **saved, "net": "+1V0", "return_net": "GND", "cell_mils": 8,
        "sources": [{"ref": "U1", "volts": 1.0}], "sinks": [{"ref": "U2", "amps": 5.0}],
        "max_drop_pct": 1.0})
    assert out["ok"] and out["source"] == "file" and not out["stack_assumed"]
    assert out["supply"]["worst"]["sink"] == "U2"
    assert 5 < out["supply"]["worst"]["drop_mv"] < 20
    assert out["rails"][0]["total_drop_pct"] == out["worst_drop_pct"]
    assert out["pass"] is False and out["limit_pct"] == 1.0
    assert out["supply"]["vias_over_capacity"] >= 1
    assert "convergence" in out["supply"]
    assert any(f.endswith(".npz") for f in out["files"])
    assert all(os.path.isfile(f) for f in out["files"])
    assert "_maps" not in out["supply"]


def test_ir_drop_tool_errors(tools, saved):
    out = _call(tools, "sim_pdn_ir_drop", {**saved, "net": "NOPE", "sources": [], "sinks": []})
    assert not out["ok"] and "not on the board" in out["error"]
    out = _call(tools, "sim_pdn_ir_drop", {**saved, "net": "+1V0",
                                           "sources": [{"ref": "U9", "volts": 1}],
                                           "sinks": [{"ref": "U2", "amps": 1}]})
    assert not out["ok"] and "no pads" in out["error"]
    out = _call(tools, "sim_pdn_ir_drop", {"net": "+1V0", "sources": [], "sinks": []})
    assert not out["ok"] and "expect_file" in out["error"]


def test_impedance_tool(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {
        **saved, "net": "+1V0", "load": {"ref": "U2"}, "volts": 1.0, "ripple_pct": 3,
        "step_amps": 2, "points": 121})
    assert out["ok"] and out["model"] == "cavity" and out["pass"] is False
    assert out["target"]["target_mohm"] == 15.0
    assert len(out["decaps"]) == 5 and out["violations"]
    assert any(f.endswith(".csv") for f in out["files"])
    rows = open([f for f in out["files"] if f.endswith(".csv")][0]).read().splitlines()
    assert rows[0].startswith("freq_hz,") and len(rows) == 122


def test_impedance_tool_takes_the_target_in_milliohms(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {
        **saved, "net": "+1V0", "load": {"ref": "U2"}, "target_mohm": 15.0,
        "mode": "lumped", "points": 61, "write_files": False})
    assert out["ok"] and out["target"] == {"target_mohm": 15.0, "from": "given"}
    assert out["model"] == "lumped" and "files" not in out
    assert out["worst"]["over_target_x"] == pytest.approx(out["worst"]["mohm"] / 15.0, rel=1e-3)


def test_impedance_tool_errors(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "load": {"ref": "U2"}})
    assert not out["ok"] and "target" in out["error"]
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "ground": "AGND",
                                             "load": {"ref": "U2"}, "target_mohm": 10})
    assert not out["ok"] and "AGND" in out["error"]
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "load": {"ref": "U7"},
                                             "target_mohm": 10})
    assert not out["ok"] and "no pads" in out["error"]


# ---- inputs that must not pass quietly ---------------------------------------

@pytest.mark.parametrize("sources,sinks,word", [
    ([{"ref": "U1"}], [{"ref": "U2", "amps": 5.0}], "volts"),
    ([{"ref": "U1", "volts": 1.0}], [{"ref": "U2"}], "amps"),
    ([{"ref": "U1", "voltage": 1.0}], [{"ref": "U2", "amps": 5.0}], "voltage"),
    ([{"ref": "U1", "volts": 1.0}], [{"ref": "U2", "current": 5.0}], "current"),
    ([{"ref": "U1", "volts": 1.0}], [], "sink"),
    ([{"ref": "U1", "volts": 0}], [{"ref": "U2", "amps": 5.0}], "above 0"),
])
def test_ir_drop_without_a_voltage_or_a_current_is_an_error_not_a_pass(tools, saved, sources,
                                                                      sinks, word):
    out = _call(tools, "sim_pdn_ir_drop", {**saved, "net": "+1V0", "cell_mils": 10,
                                           "sources": sources, "sinks": sinks,
                                           "max_drop_pct": 5.0, "write_maps": False})
    assert out["ok"] is False and word in out["error"]
    assert "pass" not in out and "worst_drop_pct" not in out


def test_a_missing_percentage_is_never_counted_as_zero():
    from eda_agent.tools.pdn import ir_verdict
    good = {"supply": {"sinks": [{"drop_pct": 0.8}, {"drop_pct": 2.5}]}}
    assert ir_verdict(good, 2.0) == {"limit_pct": 2.0, "worst_drop_pct": 2.5, "pass": False}
    assert ir_verdict(good, 3.0)["pass"] is True
    assert ir_verdict(good, 0.0) == {"worst_drop_pct": 2.5}
    holed = {"supply": {"sinks": [{"drop_pct": 0.8}, {"drop_pct": None}]}}
    v = ir_verdict(holed, 2.0)
    assert v["worst_drop_pct"] is None and v["pass"] is None and "no pass" in v["verdict_note"]
    rails = {"supply": {"sinks": [{"drop_pct": 0.1}]},
             "rails": [{"total_drop_pct": 1.5}, {"total_drop_pct": None}]}
    assert ir_verdict(rails, 2.0)["pass"] is None
    assert ir_verdict({"supply": {"sinks": []}}, 2.0)["pass"] is None


def test_an_error_nobody_foresaw_is_a_reply_and_leaves_no_folder(tools, saved, monkeypatch, tmp_path):
    from eda_agent.pdn import impedance as im
    from eda_agent.pdn import ir_drop as ir

    def boom(*a, **k):
        raise RuntimeError("the solver fell over")
    monkeypatch.setattr(im, "analyse", boom)
    monkeypatch.setattr(ir, "analyse", boom)
    runs = tmp_path / "runs"
    out = _call(tools, "sim_pdn_impedance", {**saved, "runs_dir": str(runs), "net": "+1V0",
                                             "load": {"ref": "U2"}, "target_mohm": 10})
    assert out == {"ok": False, "error": "RuntimeError: the solver fell over"}
    assert not runs.exists() or not any(runs.iterdir())
    out = _call(tools, "sim_pdn_ir_drop", {**saved, "runs_dir": str(runs), "net": "+1V0",
                                           "sources": [{"ref": "U1", "volts": 1.0}],
                                           "sinks": [{"ref": "U2", "amps": 1.0}]})
    assert out == {"ok": False, "error": "RuntimeError: the solver fell over"}


def test_a_folder_that_cannot_be_made_costs_the_files_not_the_answer(tools, saved, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    out = _call(tools, "sim_pdn_impedance", {
        **saved, "runs_dir": str(blocker), "net": "+1V0", "load": {"ref": "U2"},
        "target_mohm": 15.0, "mode": "lumped", "points": 61})
    assert out["ok"] and out["worst"]["mohm"] > 0
    assert out["files_error"] and not out.get("files")
    out = _call(tools, "sim_pdn_ir_drop", {
        **saved, "runs_dir": str(blocker), "net": "+1V0", "cell_mils": 10,
        "sources": [{"ref": "U1", "volts": 1.0}], "sinks": [{"ref": "U2", "amps": 5.0}]})
    assert out["ok"] and out["worst_drop_pct"] > 0
    assert out["files_error"] and out["files"] == []


def test_more_points_than_the_most_is_said(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {
        **saved, "net": "+1V0", "load": {"ref": "U2"}, "target_mohm": 15.0,
        "mode": "lumped", "points": 50000, "write_files": False})
    assert out["ok"] and any("points 50000 taken as 2001" in n for n in out["notes"])


# ---- run folders -------------------------------------------------------------

def test_two_runs_in_the_same_instant_get_a_folder_each(tmp_path, monkeypatch):
    import time

    from eda_agent.pdn import run_folder
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.123)
    a = run_folder(str(tmp_path), "b.PcbDoc", "+1V0")
    b = run_folder(str(tmp_path), "b.PcbDoc", "+1V0")
    c = run_folder(str(tmp_path), "b.PcbDoc", "+1V0")
    assert a != b != c and a.is_dir() and b.is_dir() and c.is_dir()
    assert a.name.endswith("-123") and b.name == a.name + "_2" and c.name == a.name + "_3"


def test_run_folder_names_are_ones_windows_accepts(tmp_path):
    from eda_agent.pdn import run_folder
    f = run_folder(str(tmp_path), 'my:board?.PcbDoc', 'VDD/CORE*<1>|"A"\\B ')
    assert f.is_dir() and f.parent == tmp_path
    assert not set(f.name) & set('\\/:*?"<>| ')
    assert not f.name.endswith((".", " "))


def test_runs_at_once_do_not_spoil_each_others_files(tools, saved, tmp_path):
    pytest.importorskip("matplotlib")
    from concurrent.futures import ThreadPoolExecutor

    def run(k):
        return _call(tools, "sim_pdn_impedance", {
            **saved, "runs_dir": str(tmp_path), "net": "+1V0", "load": {"ref": "U2"},
            "target_mohm": 10.0 + k, "mode": "lumped", "points": 61})
    with ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(run, range(4)))
    folders = set()
    for out in outs:
        assert out["ok"] and "files_error" not in out
        png = [f for f in out["files"] if f.endswith(".png")]
        assert len(png) == 1 and os.path.getsize(png[0]) > 5000
        folders.add(os.path.dirname(png[0]))
    assert len(folders) == 4


def test_capacitor_kinds_reach_the_reply_and_the_csv(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {
        **saved, "net": "+1V0", "load": {"ref": "U2"}, "target_mohm": 15.0, "points": 61,
        "mode": "lumped", "decaps": [{"ref": "C1", "kind": "polymer"}]})
    by = {d["ref"]: d for d in out["decaps"]}
    assert by["C1"]["kind"] == "polymer" and by["C1"]["esr_mohm"] == 50.0
    assert by["C10"]["kind"] == "mlcc" and by["C10"]["esr_mohm"] == 30.0
    rows = open([f for f in out["files"] if f.endswith("_decaps.csv")][0]).read().splitlines()
    head = rows[0].split(",")
    c1 = next(r for r in rows if r.startswith("C1,")).split(",")
    assert c1[head.index("kind")] == "polymer"
