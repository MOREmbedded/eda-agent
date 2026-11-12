# SPDX-License-Identifier: Apache-2.0
"""The MCP tools over a saved board: no Altium, no openEMS."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP

from eda_agent.tools.emc import register_emc_tools

from .boards import STACKUP, microstrip_board, split_board


@pytest.fixture(scope="module")
def tools():
    m = FastMCP("t")
    register_emc_tools(m)
    return m


def _call(m, name, args):
    out = asyncio.run(m.call_tool(name, args))
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_prescan_tool_on_a_saved_board(tools, tmp_path):
    path = tmp_path / "b.json"
    split_board(gap=True).save(path)
    st = tmp_path / "stack.json"
    st.write_text(json.dumps(STACKUP))
    out = _call(tools, "sim_emc_prescan", {"board_json_path": str(path),
                                           "stackup_json_path": str(st),
                                           "drives": {"CLK": {"freq_mhz": 25}}})
    assert out["ok"] and out["source"] == "file" and not out["pass"]
    assert not out["summary"]["reference_gaps"]["pass"]
    assert out["checks"]["emission"]["nets"][0]["net"] == "CLK"
    assert out["stack"]["assumed"] is False


def test_prescan_tool_without_a_board_says_what_to_pass(tools):
    out = _call(tools, "sim_emc_prescan", {})
    assert not out["ok"] and "expect_file" in out["error"]


def test_build_model_tool_writes_a_folder(tools, tmp_path):
    path = tmp_path / "line.json"
    microstrip_board().save(path)
    out = _call(tools, "sim_em_build_model", {"nets": ["RF"], "board_json_path": str(path),
                                              "f_max_mhz": 3000, "runs_dir": str(tmp_path)})
    assert out["ok"], out
    assert [p["name"] for p in out["ports"]] == ["J1.1", "J2.1"]
    assert out["stack_assumed"] is True
    from pathlib import Path
    assert (Path(out["folder"]) / "model.json").is_file()
    assert (Path(out["folder"]) / "openems_runner.py").is_file()
    st = _call(tools, "sim_em_results", {"folder": out["folder"]})
    assert st["state"] == "not_started"


def test_read_touchstone_tool(tools, tmp_path):
    p = tmp_path / "a.s1p"
    p.write_text("# MHz S DB R 50\n2400 -20 0\n2450 -30 0\n2500 -10 0\n")
    out = _call(tools, "sim_read_touchstone", {"path": str(p), "at_mhz": [2450]})
    assert out["ok"] and out["ports_detail"][0]["best_at_mhz"] == 2450
    bad = _call(tools, "sim_read_touchstone", {"path": str(tmp_path / "none.s1p")})
    assert not bad["ok"]
