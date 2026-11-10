# SPDX-License-Identifier: Apache-2.0
"""The dashboard's project stages: each tracked .PrjPcb on its way from
Feasibility to Production, and the switch that turns the gates on.

Run against a temp HOME and cwd, isolated from any real ~/.eda-agent, the
same discipline as test_workbench_workspace.py.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timedelta

import pytest

from eda_agent import profile as profile_mod
from eda_agent.design import phase_gates as pg
from eda_agent.design import project_stages as ps
from eda_agent.web.dashboard import create_app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("EDA_AGENT_PROFILE_DIR", "EDA_AGENT_IPC_DIR", "ALTIUM_AI_IPC_DIR",
                "EDA_AGENT_PHASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")


def _profile(name: str = "acme") -> pathlib.Path:
    directory = profile_mod.central_profiles_root() / name
    directory.mkdir(parents=True)
    (directory / "profile.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    profile_mod.use_central_profile(name)
    return directory


def _project(tmp_path, name: str = "Widget", folder: str = "boards") -> str:
    path = tmp_path / folder / f"{name}.PrjPcb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Design]\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def ws(tmp_path):
    return tmp_path / "ws"


@pytest.fixture
def client(ws):
    app = create_app(workspace_dir=ws)
    app.config["TESTING"] = True
    return app.test_client()


def _meet(ledger: pg.Ledger, stage: str, revision: str = "") -> None:
    for check in pg.checks_of(stage):
        ledger.add(check.key, pg.MET, source="test", detail="done", revision=revision)


# -- the stages -----------------------------------------------------------------

def test_research_comes_between_feasibility_and_prototype():
    assert pg.STAGES == ("feasibility", "research", "prototype", "evt", "dvt", "pvt")
    assert pg.stage_from("Proof of concept") == "research"
    assert pg.stage_from("R&D") == "research"
    assert pg.checks_of("research"), "a stage with no checks would clear on nothing"
    assert pg.parse_stages("all") == pg.STAGES


# -- off by default, and the switch ---------------------------------------------

def test_off_by_default_shows_no_pipeline(client):
    _profile()
    got = client.get("/api/wb/stages").get_json()
    assert got["ok"] and got["enabled"] is False and got["projects"] == []
    assert got["can_switch"] is True
    assert [s["short"] for s in got["all"]] == ["Feasibility", "Research", "Prototype",
                                                "EVT", "DVT", "PVT"]


def test_the_switch_writes_the_setting_the_phase_tools_read(client):
    directory = _profile()
    (directory / "phase_gates.json").write_text(
        json.dumps({"_comment": "kept", "stages": []}), encoding="utf-8")
    got = client.post("/api/wb/stages/switch", json={"on": True}).get_json()
    assert got["ok"] and got["stages"] == list(pg.STAGES)
    assert profile_mod.get_phase_stages() == pg.STAGES
    data = json.loads((directory / "phase_gates.json").read_text(encoding="utf-8"))
    assert data["_comment"] == "kept"
    assert client.get("/api/wb/stages").get_json()["enabled"] is True
    got = client.post("/api/wb/stages/switch", json={"on": False}).get_json()
    assert got["ok"] and profile_mod.get_phase_stages() == ()


def test_the_switch_needs_a_profile_and_yields_to_the_environment(client, monkeypatch):
    got = client.post("/api/wb/stages/switch", json={"on": True}).get_json()
    assert not got["ok"] and "no profile" in got["reason"]
    _profile()
    monkeypatch.setenv("EDA_AGENT_PHASES", "evt")
    assert client.get("/api/wb/stages").get_json()["can_switch"] is False
    got = client.post("/api/wb/stages/switch", json={"on": False}).get_json()
    assert not got["ok"] and "EDA_AGENT_PHASES" in got["reason"]


def test_an_unreadable_setting_is_left_alone(client):
    directory = _profile()
    (directory / "phase_gates.json").write_text("{not json", encoding="utf-8")
    got = client.post("/api/wb/stages/switch", json={"on": True}).get_json()
    assert not got["ok"]
    assert (directory / "phase_gates.json").read_text(encoding="utf-8") == "{not json"


def test_the_switch_needs_a_same_origin_request(client):
    _profile()
    got = client.post("/api/wb/stages/switch", json={"on": True},
                      headers={"Origin": "http://evil.example"})
    assert got.status_code == 403
    assert profile_mod.get_phase_stages() == ()


# -- which projects are tracked --------------------------------------------------

def test_tracking_lives_in_the_workspace_and_removal_keeps_the_ledger(tmp_path, ws):
    path = _project(tmp_path)
    assert ps.add(ws, path) == {"ok": True, "product": "Widget"}
    assert ps.add(ws, path)["already"] is True
    assert (ws / "phase_gates" / "projects.json").is_file()
    ps.answer(ws, "Widget", "feasibility:cost-target", pg.MET, "under target")
    assert ps.remove(ws, "Widget")["ok"]
    assert ps.overview(ws, pg.STAGES) == []
    assert (ws / "phase_gates" / "Widget.jsonl").is_file()
    ps.add(ws, path)
    assert ps.overview(ws, pg.STAGES)[0]["answers"] == 1


@pytest.mark.parametrize("given, why", [("", "give the path"),
                                        ("C:/nowhere/Gone.PrjPcb", "no file"),
                                        ("notes.txt", "not a .PrjPcb")])
def test_tracking_refuses_what_is_not_a_project(ws, given, why):
    got = ps.add(ws, given)
    assert not got["ok"] and why in got["reason"]


def test_two_projects_with_one_name_would_share_a_ledger_so_the_second_is_refused(tmp_path, ws):
    assert ps.add(ws, _project(tmp_path, folder="a"))["ok"]
    got = ps.add(ws, _project(tmp_path, folder="b"))
    assert not got["ok"] and "share one ledger" in got["reason"]


def test_the_dashboard_and_the_phase_tools_share_one_ledger(tmp_path, ws, monkeypatch):
    """design_phase_answer(product=<file name>) lands where the dashboard reads."""
    from eda_agent.config import get_config
    from eda_agent.tools import register_backend
    from eda_agent.tools.registry import ToolRegistry

    monkeypatch.setenv("EDA_AGENT_PHASES", "all")
    monkeypatch.setattr(get_config(), "workspace_dir", ws, raising=False)
    tools = ToolRegistry()
    register_backend(tools, "altium")
    ps.add(ws, _project(tmp_path))
    reply = asyncio.run(tools.get("design_phase_answer").fn(
        "research:architecture", "met", detail="block diagram B2", product="Widget"))
    assert reply["ok"], reply
    lines = ps.stage_checks(ws, "Widget", "research")["checks"]
    row = next(r for r in lines if r["key"] == "research:architecture")
    assert row["outcome"] == "met" and row["detail"] == "block diagram B2"


# -- the pipeline ----------------------------------------------------------------

def test_the_pipeline_marks_passed_current_ahead_and_not_run(tmp_path, ws):
    ps.add(ws, _project(tmp_path))
    ledger = pg.Ledger(ws / "phase_gates" / "Widget.jsonl")
    _meet(ledger, "feasibility")
    _meet(ledger, "evt")          # met, but ahead of an open stage
    running = ("feasibility", "research", "prototype", "evt", "dvt")
    row = ps.overview(ws, running)[0]
    states = {s["stage"]: s["state"] for s in row["stages"]}
    assert states == {"feasibility": "cleared", "research": "current", "prototype": "ahead",
                      "evt": "ahead", "dvt": "ahead", "pvt": "off"}
    assert row["current"] == "research" and not row["in_production"]
    feas = row["stages"][0]
    assert feas["done"] == feas["required"] == len(pg.checks_of("feasibility"))


def test_clearing_every_stage_in_force_reaches_production(tmp_path, ws):
    ps.add(ws, _project(tmp_path))
    ledger = pg.Ledger(ws / "phase_gates" / "Widget.jsonl")
    for stage in ("feasibility", "research"):
        _meet(ledger, stage)
    row = ps.overview(ws, ("feasibility", "research"))[0]
    assert row["in_production"] and row["current"] == pg.DONE


def test_a_new_revision_reopens_the_gate_and_says_why(tmp_path, ws):
    ps.add(ws, _project(tmp_path))
    ps.set_revision(ws, "Widget", "A")
    for check in pg.checks_of("feasibility"):
        assert ps.answer(ws, "Widget", check.key, pg.MET, "ok")["ok"]
    assert ps.overview(ws, pg.STAGES)[0]["stages"][0]["state"] == "cleared"
    ps.set_revision(ws, "Widget", "B")
    row = ps.overview(ws, pg.STAGES)[0]
    assert row["current"] == "feasibility" and row["stages"][0]["stale"] > 0
    check = ps.stage_checks(ws, "Widget", "feasibility")["checks"][0]
    assert check["outcome"] == "open" and "revision 'A'" in check["ignored_because"]


def test_an_old_answer_stops_counting(tmp_path, ws):
    ps.add(ws, _project(tmp_path))
    ledger = pg.Ledger(ws / "phase_gates" / "Widget.jsonl")
    ledger.add("research:risks-retired", pg.MET, detail="eval kit",
               when=datetime.now() - timedelta(days=90))
    row = next(r for r in ps.stage_checks(ws, "Widget", "research")["checks"]
               if r["key"] == "research:risks-retired")
    assert row["outcome"] == "open" and "days ago" in row["ignored_because"]


# -- recording an answer ----------------------------------------------------------

def test_an_answer_needs_a_running_stage_and_a_waiver_a_reason(client, tmp_path, ws):
    directory = _profile()
    (directory / "phase_gates.json").write_text(json.dumps({"stages": ["evt"]}),
                                                encoding="utf-8")
    path = _project(tmp_path)
    assert client.post("/api/wb/stages/project", json={"action": "add", "path": path}).get_json()["ok"]
    post = lambda **b: client.post("/api/wb/stages/answer",  # noqa: E731
                                   json={"product": "Widget", **b}).get_json()
    got = post(key="feasibility:cost-target", outcome="met")
    assert not got["ok"] and "not a stage this profile runs" in got["reason"]
    got = post(key="evt:heat", outcome="waived")
    assert not got["ok"] and "waiver needs a detail" in got["reason"]
    assert post(key="evt:heat", outcome="waived", detail="no hot parts")["ok"]
    assert not post(key="evt:nothing", outcome="met")["ok"]
    assert not post(key="evt:heat", outcome="done")["ok"]
    rows = client.get("/api/wb/stages/checks?product=Widget&stage=EVT").get_json()["checks"]
    heat = next(r for r in rows if r["key"] == "evt:heat")
    assert heat["outcome"] == "waived" and heat["source"] == ps.SOURCE


def test_answers_are_refused_while_the_gates_are_off(client, tmp_path):
    _profile()
    client.post("/api/wb/stages/project", json={"action": "add", "path": _project(tmp_path)})
    got = client.post("/api/wb/stages/answer", json={
        "product": "Widget", "key": "evt:heat", "outcome": "met"}).get_json()
    assert not got["ok"] and "off" in got["reason"]


def test_the_overview_through_the_dashboard(client, tmp_path):
    directory = _profile()
    (directory / "phase_gates.json").write_text(json.dumps({"stages": "all"}), encoding="utf-8")
    path = _project(tmp_path)
    client.post("/api/wb/stages/project", json={"action": "add", "path": path})
    got = client.post("/api/wb/stages/project",
                      json={"action": "revision", "product": "Widget", "revision": "B2"}).get_json()
    assert got == {"ok": True, "product": "Widget", "revision": "B2"}
    row = client.get("/api/wb/stages").get_json()["projects"][0]
    assert row["path"] == path and row["revision"] == "B2" and row["exists"]
    assert row["current"] == "feasibility" and row["stages"][0]["state"] == "current"
    assert not client.post("/api/wb/stages/project", json={"action": "rename"}).get_json()["ok"]


def test_the_page_draws_the_pipeline_and_wires_the_switch():
    page = (pathlib.Path(ps.__file__).parents[1] / "web" / "dashboard_static"
            / "workbench.html").read_text(encoding="utf-8")
    assert "${stagesCard()}" in page and "stagesRow(w.stages, from)" in page
    assert '"/api/wb/stages/switch"' in page and '"/api/wb/stages/answer"' in page
    assert "else if (e.target.dataset.wsstages != null) stagesSwitch(e.target);" in page
