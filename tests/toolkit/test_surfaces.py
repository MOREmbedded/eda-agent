# SPDX-License-Identifier: Apache-2.0
"""The toolkit's two surfaces: the MCP tools and the dashboard tab."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from eda_agent.engineering import toolkit
from eda_agent.tools.toolkit import register_toolkit_tools
from eda_agent.web import toolkit_view

from test_calculators import STACK

PAGE = (Path(toolkit_view.__file__).parent / "dashboard_static" / "workbench.html").read_text(
    encoding="utf-8")


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools(altium):
    m = _Mcp()
    register_toolkit_tools(m, altium=altium)
    return m.tools


def test_two_tools_everywhere_and_the_board_reader_only_with_altium():
    assert set(_tools(False)) == {"pcb_calc_toolkit_list", "pcb_calc_toolkit"}
    assert set(_tools(True)) == {"pcb_calc_toolkit_list", "pcb_calc_toolkit",
                                 "pcb_calc_toolkit_board"}


def test_the_tools_are_registered_on_the_server_surface():
    import eda_agent.tools as tools
    src = Path(tools.__file__).read_text(encoding="utf-8")
    assert "register_toolkit_tools(mcp, altium=" in src


def test_list_tool_filters_by_group():
    t = _tools(False)
    everything = asyncio.run(t["pcb_calc_toolkit_list"]())
    assert everything["count"] == len(toolkit.REGISTRY)
    pairs = asyncio.run(t["pcb_calc_toolkit_list"]("pairs"))
    assert [g["key"] for g in pairs["groups"]] == ["pairs"]
    assert not asyncio.run(t["pcb_calc_toolkit_list"]("nope"))["ok"]


def test_run_tool_matches_the_engine_and_refuses_cleanly():
    t = _tools(False)
    got = asyncio.run(t["pcb_calc_toolkit"]("attenuator", {"db": 10, "z0": 75}))
    assert got == toolkit.run("attenuator", {"db": 10, "z0": 75})
    assert got["ok"] and got["status"] == "exact"
    assert not asyncio.run(t["pcb_calc_toolkit"]("attenuator", {"db": "loud"}))["ok"]
    assert not asyncio.run(t["pcb_calc_toolkit"]("no_such"))["ok"]
    assert asyncio.run(t["pcb_calc_toolkit"]("ppm"))["ok"]       # inputs may be omitted


def test_board_tool_reads_the_stack_through_the_bridge(monkeypatch):
    import eda_agent.bridge as bridge

    class Fake:
        async def send_command_async(self, cmd, params):
            assert cmd == "pcb.get_layer_stackup"
            return STACK
    monkeypatch.setattr(bridge, "get_bridge", lambda: Fake())
    got = asyncio.run(_tools(True)["pcb_calc_toolkit_board"]())
    assert got["ok"] and [l["name"] for l in got["layers"]] == ["Top", "GND", "PWR", "Bottom"]

    class Down:
        async def send_command_async(self, cmd, params):
            raise RuntimeError("Altium is not running")
    monkeypatch.setattr(bridge, "get_bridge", lambda: Down())
    got = asyncio.run(_tools(True)["pcb_calc_toolkit_board"]())
    assert not got["ok"] and "Altium is not running" in got["reason"]


# ------------------------------------------------------------ dashboard

@pytest.fixture()
def client(tmp_path):
    from eda_agent.web.dashboard import create_app
    app = create_app(tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


def test_catalog_route(client):
    got = client.get("/api/wb/toolkit/catalog").get_json()
    assert got["ok"] and got["count"] == len(toolkit.REGISTRY)
    micro = next(c for g in got["groups"] for c in g["calculators"] if c["name"] == "microstrip")
    w = next(i for i in micro["inputs"] if i["key"] == "w")
    assert w["unit"] == "mil" and "mm" in w["units"] and w["prefill"] == "trace.width"


def test_run_route(client):
    got = client.post("/api/wb/toolkit/run", json={
        "name": "voltage_divider",
        "inputs": {"vin": {"value": "12", "unit": "V"}, "r1": {"value": "9", "unit": "kohm"},
                   "r2": {"value": "3", "unit": "kohm"}}}).get_json()
    assert got["ok"] and got["outputs"][0]["display"] == "3.000 V"
    bad = client.post("/api/wb/toolkit/run", json={"name": "voltage_divider",
                                                    "inputs": {"vin": {"value": "x", "unit": "V"}}})
    assert bad.get_json()["ok"] is False
    assert client.post("/api/wb/toolkit/run", json={}).get_json()["ok"] is False
    assert client.post("/api/wb/toolkit/run", data="not json",
                       content_type="application/json").get_json()["ok"] is False


def test_board_route_without_a_board(client):
    got = client.get("/api/wb/toolkit/board").get_json()
    assert got["ok"] is False and got["reason"]


def test_board_values_reads_and_reports_failure():
    got = toolkit_view.board_values("D:/x/demo.PcbDoc", read=lambda b: STACK)
    assert got["ok"] and got["board"] == "D:/x/demo.PcbDoc" and len(got["layers"]) == 4

    def boom(_b):
        raise RuntimeError("no answer")
    got = toolkit_view.board_values("D:/x/demo.PcbDoc", read=boom)
    assert not got["ok"] and "no answer" in got["reason"]
    assert not toolkit_view.board_values("")["ok"]
    assert not toolkit_view.board_values("D:/x/demo.PcbDoc", read=lambda b: None)["ok"]


def test_the_page_has_the_calculators_mode():
    assert '<button data-mode="calc"' in PAGE
    modes = re.search(r"const MODES = \[(.*?)\];", PAGE).group(1)
    panes = re.search(r"const PANE_MODES = \[(.*?)\];", PAGE).group(1)
    assert '"calc"' in modes and '"calc"' in panes
    assert 'if (S.mode === "calc") return paintCalcPane();' in PAGE
    assert 'else if (k === "0") setMode("calc");' in PAGE
    for route in ("/api/wb/toolkit/catalog", "/api/wb/toolkit/run", "/api/wb/toolkit/board"):
        assert route in PAGE
    # The earlier modes keep their number keys.
    assert modes.replace('"', "").replace(" ", "").split(",")[:9] == [
        "place", "route", "review", "library", "assemble", "engines", "tools", "workspace", "usage"]


def test_the_page_escapes_what_the_server_sends():
    # Calculator titles, labels and reasons come from the server; each is
    # put on the page through esc().
    block = PAGE[PAGE.index("/* ================================================================ calculators */"):
                 PAGE.index('$("#boardbtn").addEventListener')]
    for raw in re.findall(r"\$\{((?:c|i|o|r|k|g|L|l|B)\.[a-z_.]+)\}", block):
        raise AssertionError(f"unescaped ${{{raw}}} in the calculators block")
