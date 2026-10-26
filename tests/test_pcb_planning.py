# SPDX-License-Identifier: Apache-2.0
"""Offline tests for dry-run-first PCB automation plans."""

import pytest

from eda_agent.design.pcb_planning import plan_bga_fanout, plan_return_vias


def test_bga_fanout_is_radial_and_preserves_nets():
    result = plan_bga_fanout(
        [{"x": 90, "y": 100, "net": "A"},
         {"x": 110, "y": 100, "net": "B"}],
        100, 100, 20, 4, 18, 8, "TopLayer",
    )
    assert [v["x"] for v in result["vias"]] == [70, 130]
    assert [v["net"] for v in result["vias"]] == ["A", "B"]
    assert result["tracks"][0]["width"] == 4


def test_return_vias_support_explicit_orientation():
    vias = plan_return_vias(
        [{"x": 100, "y": 200, "angle": 90}], "GND", 40, 30, 14
    )
    assert vias == [{
        "x": 100, "y": 240, "net": "GND", "size": 30, "hole_size": 14,
    }]


def test_invalid_via_geometry_is_rejected():
    with pytest.raises(ValueError, match="smaller"):
        plan_return_vias([{"x": 0, "y": 0}], "GND", 20, 10, 10)


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.mark.asyncio
async def test_bga_tool_dry_run_never_requests_bridge(monkeypatch):
    from eda_agent.tools import pcb

    monkeypatch.setattr(
        pcb, "get_bridge", lambda: (_ for _ in ()).throw(AssertionError("live"))
    )
    mcp = _Mcp()
    pcb.register_pcb_tools(mcp)
    result = await mcp.tools["pcb_plan_bga_fanout"](
        [{"x": 0, "y": 10, "net": "N1"}], 0, 0
    )
    assert result["applied"] is False
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_apply_requires_confirmation_before_bridge(monkeypatch):
    from eda_agent.tools import pcb

    monkeypatch.setattr(
        pcb, "get_bridge", lambda: (_ for _ in ()).throw(AssertionError("live"))
    )
    mcp = _Mcp()
    pcb.register_pcb_tools(mcp)
    with pytest.raises(ValueError, match="confirm=True"):
        await mcp.tools["pcb_plan_return_vias"](
            [{"x": 0, "y": 0}], apply=True, confirm=False
        )
