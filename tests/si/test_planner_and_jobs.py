# SPDX-License-Identifier: Apache-2.0
"""The stackup planner, the line-model and stackup-plan jobs, the tools
and the dashboard's file route."""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent import simulation as sim
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.si import jobs as si_jobs
from eda_agent.si import planner
from eda_agent.si.section import MIL

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


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)


@pytest.fixture
def stack_file(tmp_path):
    p = tmp_path / "stackup.json"
    p.write_text(json.dumps(STACKUP), encoding="utf-8")
    return p


# -- planner --------------------------------------------------------------------

def test_layers_get_their_structure_and_references():
    secs = {s["label"]: s for s in planner.layer_sections(stack_from_stackup(STACKUP), planner.Options())}
    assert set(secs) == {"Top Layer", "Sig1", "Bottom Layer"}
    assert secs["Top Layer"]["structure"] == "microstrip" and secs["Top Layer"]["h_mils"] == 4.0
    sig = secs["Sig1"]
    assert sig["structure"] == "stripline" and sig["h_below_mils"] == 6.0 and sig["h_above_mils"] == 5.0
    assert sig["laminate"].er == 4.3 and sig["laminate_above"].er == 4.4
    assert secs["Bottom Layer"]["h_mils"] == 40.0


def test_widths_hit_their_targets():
    info = planner.layer_sections(stack_from_stackup(STACKUP), planner.Options())[0]
    opt = planner.Options()
    se = planner.solve_width(info, opt, 50.0, False)
    assert se["ok"] and se["z0"] == pytest.approx(50.0, rel=0.005)
    # The width is right: solved again on its own, it gives the target.
    z = planner.impedances(planner.build(info, opt, se["width_mils"] * MIL), opt.f_design, 60)
    assert z["z0"] == pytest.approx(50.0, rel=0.005)
    narrower = planner.impedances(planner.build(info, opt, 0.8 * se["width_mils"] * MIL), opt.f_design)
    assert narrower["z0"] > 52
    pair = planner.solve_width(info, opt, 100.0, True)
    assert pair["ok"] and pair["z_diff"] == pytest.approx(100.0, rel=0.005)
    assert "gap = " in pair["gap_rule"]
    fixed = planner.solve_width(info, planner.Options(diff_gap_mils=8.0), 100.0, True)
    assert fixed["ok"] and fixed["gap_mils"] == 8.0


def test_an_unreachable_target_says_what_the_layer_can_do():
    bottom = planner.layer_sections(stack_from_stackup(STACKUP), planner.Options())[-1]
    out = planner.solve_width(bottom, planner.Options(), 50.0, False)
    assert not out["ok"] and "too low" in out["reason"] and "ohm" in out["reason"]


def test_altium_steps_lock_each_layer_width():
    res = {"layers": [{"label": "Top Layer", "single": {"ok": True, "target_ohms": 50.0, "width_mils": 6.3},
                       "pairs": [{"ok": True, "target_ohms": 100.0, "width_mils": 4.9, "gap_mils": 7.4},
                                 {"ok": False, "target_ohms": 90.0}]}]}
    steps = planner.altium_steps(res)
    assert [s["tool"] for s in steps] == ["pcb_impedance_profile", "pcb_impedance_profile_layer",
                                          "pcb_impedance_profile", "pcb_impedance_profile_layer"]
    assert steps[0]["args"] == {"action": "create", "profile": "SE 50R", "target_ohms": 50.0,
                                "tolerance": 10.0, "differential": False}
    assert steps[3]["args"]["gap_mils"] == 7.4 and steps[3]["args"]["width_locked"] is True


def test_comparison_with_altium_profiles():
    stack = stack_from_stackup(STACKUP)
    info = planner.layer_sections(stack, planner.Options())[0]
    se = planner.solve_width(info, planner.Options(), 50.0, False)
    prof = {"profiles": [{"name": "SE 50R", "target_ohms": 50, "differential": False, "layers": [
        {"layer": "Top Layer", "enabled": True, "trace_width_mils": se["width_mils"],
         "calculated_impedance_ohms": 51.0},
        {"layer": "Nowhere", "enabled": True, "trace_width_mils": 5}]}]}
    rows = planner.compare_with_altium(stack, prof)
    assert rows[0]["our_ohms_at_altium_width"] == pytest.approx(50.0, rel=0.005)
    assert rows[0]["difference_pct"] == pytest.approx(100 * (rows[0]["our_ohms_at_altium_width"] / 51.0 - 1), abs=0.01)
    assert rows[0]["our_width_mils"] == pytest.approx(se["width_mils"], rel=0.01)
    assert "not found" in rows[1]["note"]


# -- jobs -------------------------------------------------------------------------

MS = {"structure": "microstrip", "width_mils": 6.3, "h_mils": 4, "er": 4.2, "tand": 0.02,
      "length_mm": 50, "f_max_ghz": 10, "points": 21}


def test_line_model_job_and_scikit_rf_agree():
    a = sim.submit("line-model", "eda-agent", MS)
    assert a["state"] == "done", a
    res = sim.refresh(a["job"])["runs"]["eda-agent"]["results"]
    assert {"z0_ohms", "z0_lossy_ohms", "er_eff", "loss_db_per_in", "s21_db_at_f_max"} <= set(res["metrics"])
    assert all(v["pass"] for v in res["verdicts"])
    folder = sim._jobs.get(a["job"]).work_folder("eda-agent")
    assert (folder / res["files"]["touchstone"]).is_file() and (folder / "rlgc.json").is_file()
    pytest.importorskip("skrf")
    b = sim.submit("line-model", "scikit-rf", MS)
    assert b["state"] == "done", b
    rows = {r["metric"]: r for r in sim.compare(a["job"])["metrics"]}
    assert rows["z0_ohms"]["agree"] and rows["er_eff"]["agree"]
    assert rows["z0_ohms"]["spread"] < 0.005


def test_scikit_rf_refuses_what_it_cannot_model():
    from eda_agent.simulation.builtin import ScikitRfEngine
    e = ScikitRfEngine()
    assert e.check("line-model", MS, {}) == []
    assert e.check("line-model", {**MS, "traces": 2, "gap_mils": 5}, {})
    assert e.check("line-model", {**MS, "mask_mils": 0.5}, {})
    assert e.check("line-model", {**MS, "structure": "stripline"}, {})


def test_line_model_spec_errors_are_said_in_words():
    for spec, needle in (({"structure": "microstrip", "width_mils": 5}, "h_mils"),
                         ({"structure": "coax", "width_mils": 5}, "structure"),
                         ({"structure": "stripline", "width_mils": 5, "h_below_mils": 5}, "h_above_mils"),
                         ({"structure": "microstrip", "width_mils": 5, "h_mils": 4, "traces": 2}, "gap_mils"),
                         ({"layer": "Sig1", "width_mils": 5}, "stackup")):
        r = sim.submit("line-model", "eda-agent", spec)
        assert not r["ok"] and needle in r["error"], (spec, r)


def test_a_stackup_layer_sets_the_line(stack_file):
    spec = {"layer": "Sig1", "width_mils": 3.67, "f_max_ghz": 5, "points": 6}
    r = sim.submit("line-model", "eda-agent", spec, {"stackup": str(stack_file)})
    assert r["state"] == "done", r
    sec = json.loads((sim._jobs.get(r["job"]).work_folder("eda-agent") / "section.json").read_text())
    assert sec["kind"] == "stripline" and sec["plane_above_m"] is not None
    z = sim.refresh(r["job"])["runs"]["eda-agent"]["results"]["metrics"]["z0_ohms"]["value"]
    assert 45 < z < 55


def test_stackup_plan_job(stack_file):
    r = sim.submit("stackup-plan", "eda-agent", {"diff_ohms": [100], "layers": ["Top Layer"]},
                   {"stackup": str(stack_file)})
    assert r["state"] == "done", r
    res = sim.refresh(r["job"])["runs"]["eda-agent"]["results"]
    assert res["metrics"]["TopLayer.se_width_mils"]["value"] > 0
    assert res["metrics"]["TopLayer.diff100_gap_mils"]["value"] > 0
    assert len(res["plan"]["layers"]) == 1 and res["plan"]["altium_steps"]


# -- tools ---------------------------------------------------------------------------

def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


class FakeBridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, cmd, params):
        self.sent.append((cmd, params))
        if cmd == "pcb.get_layer_stackup":
            return STACKUP
        if cmd == "pcb.get_impedance_profiles":
            return {"profiles": []}
        return {"ok": True}


@pytest.fixture
def tools(monkeypatch):
    from eda_agent.tools.si import register_si_tools
    m = FastMCP("t")
    register_si_tools(m)
    fake = FakeBridge()
    import eda_agent.bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "get_bridge", lambda: fake)
    return m, fake


def test_si_stackup_plan_reads_the_board_and_applies_only_when_asked(tools):
    m, fake = tools
    out = _call(m, "si_stackup_plan", {"layers": ["Top Layer"], "diff_ohms": []})
    assert out["ok"] and out["plan"][0]["single"]["ok"]
    assert [c for c, _ in fake.sent] == ["pcb.get_layer_stackup"]
    out = _call(m, "si_stackup_plan", {"layers": ["Top Layer"], "diff_ohms": [], "apply": True})
    cmds = [c for c, _ in fake.sent]
    assert cmds[-2:] == ["pcb.impedance_profile", "pcb.impedance_profile_layer"]
    params = fake.sent[-1][1]
    assert params["width_locked"] == "true" and float(params["width_mils"]) > 0
    assert len(out["applied"]) == 2


def test_si_line_model_tool(tools, stack_file):
    m, fake = tools
    out = _call(m, "si_line_model", {"spec": {**MS, "points": 5}})
    assert out["ok"] and "z0_ohms" in out["results"]["eda-agent"]["metrics"]
    out = _call(m, "si_line_model", {"spec": {"layer": "Top Layer", "width_mils": 6, "points": 3,
                                              "f_max_ghz": 2}})
    assert out["ok"], out
    assert fake.sent[-1][0] == "pcb.get_layer_stackup"


# -- dashboard ---------------------------------------------------------------------------

def test_dashboard_serves_job_files_and_offers_only_engines_that_fit():
    from flask import Flask
    from eda_agent.web import features_view
    app = Flask("t")
    features_view.register(app)
    c = app.test_client()
    pair = {**MS, "traces": 2, "gap_mils": 6, "points": 5}
    r = sim.submit("line-model", "eda-agent", pair)
    j = c.get(f"/api/wb/sim/job/{r['job']}").get_json()
    assert "scikit-rf" not in j["runnable"] and "eda-agent" in j["runnable"]
    res = j["runs"]["eda-agent"]["results"]
    assert res["metric_order"][0] == "z_diff_ohms"
    got = c.get(f"/api/wb/sim/file/{r['job']}/eda-agent/{res['files']['touchstone']}")
    assert got.status_code == 200 and b"# Hz S RI" in got.data
    assert c.get(f"/api/wb/sim/file/{r['job']}/eda-agent/..").status_code == 404
    assert c.get(f"/api/wb/sim/file/{r['job']}/eda-agent/nothing.png").status_code == 404
    assert c.get("/api/wb/sim/file/nope/eda-agent/line.s4p").status_code == 404


def test_signal_layer_references_are_named_and_warned():
    st = {"layers": [
        {"layer_id": 1, "name": "Top", "copper_thickness_mils": 1.4,
         "dielectric_height_mils": 4.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
        {"layer_id": 39, "name": "GND", "copper_thickness_mils": 1.4,
         "dielectric_height_mils": 5.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
        {"layer_id": 2, "name": "In1", "copper_thickness_mils": 0.7,
         "dielectric_height_mils": 30.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
        {"layer_id": 3, "name": "In2", "copper_thickness_mils": 0.7,
         "dielectric_height_mils": 5.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
        {"layer_id": 40, "name": "PWR", "copper_thickness_mils": 1.4,
         "dielectric_height_mils": 4.0, "dielectric_constant": 4.2, "dielectric_type": "Core"},
        {"layer_id": 32, "name": "Bottom", "copper_thickness_mils": 1.4}]}
    secs = {s["label"]: s for s in planner.layer_sections(stack_from_stackup(st), planner.Options())}
    assert secs["Top"]["reference_label"] == "GND" and "note" not in secs["Top"]
    assert secs["In1"]["reference_label"] == ["GND", "In2"]
    assert "In2" in secs["In1"]["note"] and "In1" in secs["In2"]["note"]
    res = si_jobs.run_stackup_plan({"se_ohms": 50, "diff_ohms": [], "layers": ["In1"]}, st,
                                   __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    assert any(n.startswith("In1: signal layer In2") for n in res["notes"])


def test_a_stackup_with_no_dielectric_names_the_layers():
    # an empty two-layer board from Altium: copper, and nothing between
    bare = {"layers": [
        {"layer": "TopLayer", "name": "Top Layer", "copper_thickness_mils": 1.4,
         "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0},
        {"layer": "BottomLayer", "name": "Bottom Layer", "copper_thickness_mils": 1.4,
         "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0}]}
    with pytest.raises(ValueError, match="no dielectric thickness between Top Layer and "
                                         "Bottom Layer.*Layer Stack Manager"):
        planner.layer_sections(stack_from_stackup(bare), planner.Options())
