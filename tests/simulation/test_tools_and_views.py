# SPDX-License-Identifier: Apache-2.0
"""The MCP tools and the dashboard endpoints over features and engines."""
from __future__ import annotations

import asyncio
import json
import time

import numpy as np
import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent import simulation as sim
from eda_agent.emc.touchstone import write_touchstone
from eda_agent.tools.simulation import register_simulation_tools


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path / "engines"))
    monkeypatch.setattr(sim, "_loaded", None)


@pytest.fixture(scope="module")
def tools():
    m = FastMCP("t")
    register_simulation_tools(m)
    return m


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def _s2p(tmp_path):
    f = np.linspace(1e8, 5e9, 21)
    s = [[[0.1, 0.8 * np.exp(-1j * x / 1e9)], [0.8 * np.exp(-1j * x / 1e9), 0.1]] for x in f]
    return write_touchstone(tmp_path / "ch", list(f), s)


def test_app_features_reports_and_narrows(tools):
    out = _call(tools, "app_features", {})
    assert out["ok"] and {f["id"] for f in out["features"]} >= {"core", "openems", "scikit-rf"}
    one = _call(tools, "app_features", {"feature": "openems"})
    assert [f["id"] for f in one["features"]] == ["openems"]
    assert [d["id"] for d in one["dependencies"]] == ["openems"]
    assert not _call(tools, "app_features", {"feature": "nope"})["ok"]


def test_app_install_features_plans_without_confirm(tools, monkeypatch):
    from eda_agent.features import install
    monkeypatch.setattr(install, "execute", lambda *a, **k: pytest.fail("installed without confirm"))
    out = _call(tools, "app_install_features", {"features": ["openems", "pdf"]})
    assert out["ok"] and out["installed"] is False
    kinds = [s["kind"] for s in out["plan"]["steps"]]
    assert "manual" in kinds
    auto = install.Plan(["pdf"], [install.Step("pip", ["pdfplumber"], "pdf", argv=[["py"]])], [])
    monkeypatch.setattr(install, "plan", lambda *a, **k: auto)
    out = _call(tools, "app_install_features", {"features": ["pdf"]})
    assert out["ok"] and out["installed"] is False, "a plan that could run still waits for confirm"
    assert not _call(tools, "app_install_features", {"features": ["nope"]})["ok"]
    assert not _call(tools, "app_install_features", {"features": []})["ok"]


def test_app_install_features_runs_with_confirm(tools, monkeypatch):
    from eda_agent.features import install
    plan = install.Plan(["pdf"], [install.Step("pip", ["pdfplumber"], "pdf", argv=[["py"]])], [])
    monkeypatch.setattr(install, "plan", lambda *a, **k: plan)
    monkeypatch.setattr(install, "execute", lambda p: {"ok": True, "steps": [], "log": "x", "manual": []})
    out = _call(tools, "app_install_features", {"features": ["pdf"], "confirm": True})
    assert out["ok"] and "restart" in out["note"]


def test_sim_tools_run_compare_and_list(tools, tmp_path):
    eng = _call(tools, "sim_engines", {"kind": "touchstone-check"})
    assert {e["id"] for e in eng["engines"]} >= {"eda-agent"}
    assert eng["kinds"][0]["spec"]["tolerance"]["required"] is False
    p = _s2p(tmp_path)
    run = _call(tools, "sim_job_run", {"kind": "touchstone-check", "engines": ["eda-agent", "scikit-rf"],
                                       "inputs": {"touchstone": str(p)}, "title": "channel"})
    assert run["job"] and run["runs"][0]["state"] == "done"
    if run["runs"][1].get("state") == "done":
        assert run["compare"]["agree"]
    st = _call(tools, "sim_job_status", {"job": run["job"]})
    assert st["title"] == "channel" and "eda-agent" in st["runs"]
    cmp = _call(tools, "sim_job_compare", {"job": run["job"]})
    assert cmp["ok"]
    listed = _call(tools, "sim_jobs", {})
    assert listed["jobs"][0]["id"] == run["job"]
    assert not _call(tools, "sim_job_status", {"job": "nope"})["ok"]
    assert not _call(tools, "sim_job_run", {"kind": "touchstone-check", "engines": []})["ok"]
    bad = _call(tools, "sim_job_run", {"kind": "touchstone-check", "engines": ["ghost"],
                                       "inputs": {"touchstone": str(p)}})
    assert not bad["ok"] and "no engine" in bad["runs"][0]["error"]


# -- dashboard ---------------------------------------------------------------

@pytest.fixture
def client():
    from flask import Flask
    from eda_agent.web import features_view
    app = Flask("t")
    features_view.register(app)
    return app.test_client()


def test_features_endpoints(client):
    r = client.get("/api/wb/features").get_json()
    assert r["ok"] and r["install"]["running"] is False
    assert any(e["id"] == "openems" for e in r["engines"])
    r = client.post("/api/wb/features/switch", json={"id": "openems", "on": False}).get_json()
    assert r["ok"] and r["off"] == ["openems"]
    r = client.post("/api/wb/features/switch", json={"id": "render", "on": False}).get_json()
    assert not r["ok"]
    r = client.post("/api/wb/features/plan", json={"features": ["openems"]}).get_json()
    assert r["ok"] and r["plan"]["steps"][0]["kind"] == "manual"
    r = client.post("/api/wb/features/plan", json={"features": ["ghost"]}).get_json()
    assert not r["ok"]


def test_dashboard_install_runs_once_at_a_time(client, monkeypatch):
    from eda_agent.features import install
    from eda_agent.web import features_view
    plan = install.Plan(["pdf"], [install.Step("pip", ["pdfplumber"], "pdf", argv=[["py"]])], [])
    monkeypatch.setattr(install, "plan", lambda *a, **k: plan)
    gate = {"go": False}

    def slow(p, on_line=None):
        on_line("working")
        while not gate["go"]:
            time.sleep(0.01)
        return {"ok": True, "steps": [{"title": "pdf", "ok": True, "seconds": 0}], "log": "l", "manual": []}
    monkeypatch.setattr(install, "execute", slow)
    monkeypatch.setattr(features_view, "INSTALLER", features_view._Installer())
    r = client.post("/api/wb/features/install", json={"features": ["pdf"]}).get_json()
    assert r["ok"] and r["started"]
    again = client.post("/api/wb/features/install", json={"features": ["pdf"]}).get_json()
    assert not again["ok"] and "already" in again["error"]
    gate["go"] = True
    for _ in range(200):
        st = client.get("/api/wb/features/install").get_json()
        if not st["running"]:
            break
        time.sleep(0.01)
    assert st["result"]["ok"] and "working" in st["lines"]


def test_simulation_endpoints(client, tmp_path):
    eng = client.get("/api/wb/sim/engines").get_json()
    assert eng["ok"] and any(k["id"] == "touchstone-check" for k in eng["kinds"])
    p = _s2p(tmp_path)
    r = client.post("/api/wb/sim/run", json={"kind": "touchstone-check", "engine": "eda-agent",
                                             "inputs": {"touchstone": str(p)}, "title": "x"}).get_json()
    assert r["ok"] and r["job"]
    for _ in range(200):
        j = client.get(f"/api/wb/sim/job/{r['job']}").get_json()
        if j["runs"]["eda-agent"]["state"] == "done":
            break
        time.sleep(0.02)
    assert j["runs"]["eda-agent"]["state"] == "done" and "compare" in j
    jobs = client.get("/api/wb/sim/jobs").get_json()
    assert jobs["jobs"][0]["id"] == r["job"]
    assert not client.get("/api/wb/sim/job/nope").get_json()["ok"]
    bad = client.post("/api/wb/sim/run", json={"kind": "touchstone-check", "engine": "ghost"}).get_json()
    assert not bad["ok"]
