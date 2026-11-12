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


def test_impedance_tool_errors(tools, saved):
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "load": {"ref": "U2"}})
    assert not out["ok"] and "target" in out["error"]
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "ground": "AGND",
                                             "load": {"ref": "U2"}, "target_mohm": 10})
    assert not out["ok"] and "AGND" in out["error"]
    out = _call(tools, "sim_pdn_impedance", {**saved, "net": "+1V0", "load": {"ref": "U7"},
                                             "target_mohm": 10})
    assert not out["ok"] and "no pads" in out["error"]
