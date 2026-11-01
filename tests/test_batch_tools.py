# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tests for the new batch tools (batch_create, batch_delete, place_wires,
place_sch_components_from_library, sch_attach_spice_primitives,
lib_add_pins).

The Python side builds pipe-separated '~~' batch strings; verify the
formatting and parameter wiring against a fake bridge.
"""

from __future__ import annotations

import pytest

from eda_agent.tools.bulk_hints import BulkHintTracker


@pytest.fixture(autouse=True)
def _reset_tracker():
    BulkHintTracker.reset()
    yield
    BulkHintTracker.reset()


class _Sent:
    def __init__(self):
        self.command = None
        self.params = None
        self.timeout = None


def _install_fake_bridge(monkeypatch, module_path: str) -> _Sent:
    sent = _Sent()

    class FakeBridge:
        async def send_command_async(self, command, params=None, timeout=None):
            sent.command = command
            sent.params = params
            sent.timeout = timeout
            return {"ok": True}

    monkeypatch.setattr(f"{module_path}.get_bridge", lambda: FakeBridge())
    return sent


def _capture(module, register_fn_name: str):
    captured = {}

    class DummyMcp:
        def tool(self):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    getattr(module, register_fn_name)(DummyMcp())
    return captured


class TestBulkHintEquivalents:
    def test_every_tracked_singular_has_a_bulk_nudge(self):
        # The tracker nudges the generic-CRUD tools and the expensive read
        # tools whose singular form is still exposed. (The convenience
        # singular wrappers like place_wire / lib_add_pin were removed in
        # favour of their bulk-only variants, so they're no longer tracked.)
        assert "obj_create" in BulkHintTracker.BULK_EQUIVALENTS
        assert "obj_delete" in BulkHintTracker.BULK_EQUIVALENTS
        assert "obj_modify" in BulkHintTracker.BULK_EQUIVALENTS
        assert "proj_get_connectivity" in BulkHintTracker.BULK_EQUIVALENTS
        assert "proj_get_component_info" in BulkHintTracker.BULK_EQUIVALENTS

    def test_bulk_nudge_targets_are_valid(self):
        # Every nudged tool must point at a bulk equivalent whose name
        # shares at least one stem with the singular, so the nudge text
        # makes sense to a reader. Self-references are allowed for tools
        # whose nudge is a usage-pattern change (e.g. get_nets: "call it
        # unfiltered once instead of looping").
        for singular, (bulk, _) in BulkHintTracker.BULK_EQUIVALENTS.items():
            stem = singular.split("_")
            assert any(tok in bulk for tok in stem), (
                f"{singular} -> {bulk} doesn't share a stem"
            )


class TestBatchCreate:
    @pytest.mark.asyncio
    async def test_format_uses_double_tilde_separator(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["obj_batch_create"](operations=[
            {"object_type": "eNetLabel",
             "properties": "Text=VCC|Location.X=100|Location.Y=200"},
            {"object_type": "eNetLabel",
             "properties": "Text=GND|Location.X=100|Location.Y=400"},
        ])
        assert sent.command == "generic.batch_create"
        ops = sent.params["operations"]
        assert "~~" in ops
        assert ops.count("~~") == 1  # 2 ops -> 1 separator
        assert "object_type=eNetLabel" in ops
        assert "Text=VCC|Location.X=100|Location.Y=200" in ops

    @pytest.mark.asyncio
    async def test_empty_operations_returns_error(self, monkeypatch):
        _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        result = await tools["obj_batch_create"](operations=[])
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_object_type_is_refused_not_dropped(self, monkeypatch):
        """It was dropped, and the reply counted only the ops that ran."""
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        out = await tools["obj_batch_create"](operations=[
            {"object_type": "eJunction", "properties": "Location.X=1"},
            {"properties": "Text=orphan"},
        ])
        assert out["ok"] is False and "operations[1]" in out["reason"]
        assert sent.command is None


class TestBatchDelete:
    """Regression cover for the PCB ops that were silently dropped.

    Gen_BatchDelete resolved object_type through ObjectTypeFromString only,
    the SCHEMATIC table, and ran a bare ``Continue`` on the -1 result. A PCB
    op such as ``eTrackObject`` filtered on ``Net=VR_PA_A`` therefore never
    ran, never incremented operations_processed, and never appeared in the
    response: the caller got ``operations_processed: 0`` out of a well-formed
    batch while the singular obj_delete with the identical type and filter
    deleted 6 tracks.
    """

    @pytest.mark.asyncio
    async def test_each_op_carries_scope_type_filter(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        # Both filters are empty, so this really is a delete-all sweep
        # and needs the same confirmation obj_delete demands.
        await tools["obj_batch_delete"](confirm_delete_all=True, operations=[
            {"scope": "active_doc", "object_type": "eNoERC", "filter": ""},
            {"scope": "project", "object_type": "eJunction", "filter": ""},
        ])
        ops = sent.params["operations"].split("~~")
        assert len(ops) == 2
        assert "scope=active_doc" in ops[0]
        assert "object_type=eNoERC" in ops[0]
        assert "scope=project" in ops[1]

    @pytest.mark.asyncio
    async def test_pcb_type_and_filter_reach_the_bridge_intact(
        self, monkeypatch
    ):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["obj_batch_delete"](operations=[
            {"scope": "active_doc", "object_type": "eTrackObject",
             "filter": "Net=VR_PA_A"},
        ])
        assert sent.params["operations"] == (
            "scope=active_doc;object_type=eTrackObject;filter=Net=VR_PA_A"
        )

    @pytest.mark.asyncio
    async def test_pcb_ops_run_instead_of_being_skipped(
        self, altium_sim, e2e_bridge, monkeypatch
    ):
        """The live repro: one PCB track op, addressed purely by net."""
        import asyncio
        from eda_agent.tools import generic as g

        altium_sim.board.tracks = [
            {"x1": 0, "y1": 0, "x2": 100, "y2": 0, "width": 8,
             "layer": "Top Layer", "net": "VR_PA_A"},
            {"x1": 100, "y1": 0, "x2": 100, "y2": 90, "width": 8,
             "layer": "Top Layer", "net": "VR_PA_A"},
            {"x1": 0, "y1": 50, "x2": 60, "y2": 50, "width": 8,
             "layer": "Top Layer", "net": "GND"},
        ]

        monkeypatch.setattr(
            "eda_agent.tools.generic.get_bridge", lambda: e2e_bridge
        )
        tools = _capture(g, "register_generic_tools")
        res = await asyncio.wait_for(
            tools["obj_batch_delete"](operations=[
                {"scope": "active_doc", "object_type": "eTrackObject",
                 "filter": "Net=VR_PA_A"},
            ]),
            timeout=2.0,
        )

        assert res["total"] == 1
        # The op ran. Before the fix this was 0 with no explanation given.
        assert res["operations_processed"] == 1
        assert res["operations_failed"] == 0
        assert res["unresolved"] == []
        assert res["matched"] == 2
        # The GND track is untouched, the filter really was applied.
        assert [t["net"] for t in altium_sim.board.tracks] == ["GND"]

    def test_unresolvable_type_is_named_not_swallowed(
        self, e2e_bridge, monkeypatch
    ):
        import asyncio
        import json

        from mcp.server.fastmcp import FastMCP
        from eda_agent.tools import register_all_tools

        monkeypatch.setattr(
            "eda_agent.tools.generic.get_bridge", lambda: e2e_bridge
        )
        mcp = FastMCP("t")
        register_all_tools(mcp)

        raw = asyncio.run(mcp.call_tool("obj_batch_delete", {
            "confirm_delete_all": True,
            "operations": [
                {"object_type": "eJunction", "filter": ""},
                {"object_type": "eNotAThing", "filter": ""},
            ],
        }))
        content = raw[0] if isinstance(raw, tuple) else raw
        res = json.loads(content[0].text)

        assert res["total"] == 2
        assert res["operations_processed"] == 1
        assert res["operations_failed"] == 1
        # The offending type is named, so "never ran" is distinguishable
        # from "matched nothing".
        assert res["unresolved"] == ["eNotAThing"]
        assert res["failures"] == [
            {"index": 1, "object_type": "eNotAThing", "reason": "INVALID_TYPE"}
        ]


class TestBatchModify:
    """Regression cover for the silent multi-condition drop.

    obj_batch_modify used to serialize each op positionally as
    ``scope;object_type;filter;set`` and join the ops with ``|`` -- the very
    character that separates AND-conditions inside a filter and assignments
    inside a set. An op like
    ``active_doc;eSchComponent;Location.X=500|Location.Y=300;Designator.Text=C1``
    was split at the first pipe, neither half had the three semicolons the
    Pascal parser required, and both were dropped with no error. The call
    returned ``operations_processed: 0`` while the identical filter worked
    through the single-shot obj_modify (whose filter travels in its own JSON
    field). Coordinate-addressed batches -- the only way to reach components
    whose designators are all still ``U?`` -- could therefore never work.
    """

    @pytest.mark.asyncio
    async def test_uses_keyed_double_tilde_encoding(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["obj_batch_modify"](operations=[
            {"scope": "active_doc", "object_type": "eSchComponent",
             "filter": "Location.X=7013|Location.Y=7634",
             "set": "Designator.Text=C1027"},
            {"scope": "active_doc", "object_type": "eSchComponent",
             "filter": "Location.X=7508|Location.Y=6964",
             "set": "Designator.Text=R4"},
        ])
        assert sent.command == "generic.batch_modify"
        ops = sent.params["operations"].split("~~")
        assert len(ops) == 2, "ops must not be split on the filter's '|'"
        assert ops[0] == (
            "scope=active_doc;object_type=eSchComponent;"
            "filter=Location.X=7013|Location.Y=7634;set=Designator.Text=C1027"
        )

    @pytest.mark.asyncio
    async def test_multi_condition_filter_survives_op_split(self, monkeypatch):
        """The op separator must not appear inside filter/set values."""
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["obj_batch_modify"](operations=[
            {"object_type": "ePin", "filter": "Name=S1",
             "set": "Location.X=200|Location.Y=-100|Orientation=2"},
        ])
        ops = sent.params["operations"]
        assert "~~" not in ops  # single op -> no separator
        assert "set=Location.X=200|Location.Y=-100|Orientation=2" in ops

    def test_multi_condition_filter_matches_through_simulator(
        self, e2e_bridge, monkeypatch
    ):
        """End-to-end through the real bridge + the Gen_BatchModify mirror.

        Two seeded components share Location.X=500 and differ only in
        Location.Y, so the filter genuinely needs both conditions.
        """
        import asyncio
        import json

        from mcp.server.fastmcp import FastMCP
        from eda_agent.tools import register_all_tools

        monkeypatch.setattr(
            "eda_agent.tools.generic.get_bridge", lambda: e2e_bridge
        )
        mcp = FastMCP("t")
        register_all_tools(mcp)

        raw = asyncio.run(mcp.call_tool("obj_batch_modify", {"operations": [
            {"scope": "active_doc", "object_type": "eSchComponent",
             "filter": "Location.X=500|Location.Y=300",
             "set": "Designator.Text=C1027"},
            {"scope": "active_doc", "object_type": "eSchComponent",
             "filter": "Location.X=500|Location.Y=100",
             "set": "Designator.Text=R4_N_A"},
        ]}))
        content = raw[0] if isinstance(raw, tuple) else raw
        res = json.loads(content[0].text)

        assert res["operations_processed"] == 2
        assert res["operations_failed"] == 0
        # One component per op, addressed purely by coordinates.
        assert res["matched"] == 2

    def test_unresolvable_op_is_reported_not_swallowed(
        self, e2e_bridge, monkeypatch
    ):
        """A bad object_type must surface in failures[], not vanish."""
        import asyncio
        import json

        from mcp.server.fastmcp import FastMCP
        from eda_agent.tools import register_all_tools

        monkeypatch.setattr(
            "eda_agent.tools.generic.get_bridge", lambda: e2e_bridge
        )
        mcp = FastMCP("t")
        register_all_tools(mcp)

        raw = asyncio.run(mcp.call_tool("obj_batch_modify", {"operations": [
            {"object_type": "eNotAThing", "filter": "Text=VCC",
             "set": "Text=X"},
        ]}))
        content = raw[0] if isinstance(raw, tuple) else raw
        res = json.loads(content[0].text)

        assert res["operations_processed"] == 0
        assert res["operations_failed"] == 1
        assert res["failures"][0]["reason"] == "INVALID_TYPE"


class TestPlaceWires:
    @pytest.mark.asyncio
    async def test_wires_get_coordinate_fields(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["sch_place_wires"](wires=[
            {"x1": 100, "y1": 200, "x2": 300, "y2": 200},
            {"x1": 300, "y1": 200, "x2": 300, "y2": 400},
            {"x1": 300, "y1": 400, "x2": 600, "y2": 400},
        ])
        assert sent.command == "generic.place_wires"
        ops = sent.params["wires"].split("~~")
        assert len(ops) == 3
        assert ops[0] == "x1=100;y1=200;x2=300;y2=200"


class TestPlaceSchComponentsFromLibrary:
    @pytest.mark.asyncio
    async def test_placements_skip_entries_missing_lib_ref(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["sch_place_components"](placements=[
            {"lib_reference": "Res1", "x": 1000, "y": 2000,
             "designator": "R1"},
            {"x": 0, "y": 0},  # no lib_reference → drop
            {"lib_reference": "Cap", "x": 1500, "y": 2000,
             "designator": "C1", "rotation": 90,
             "library_path": "C:\\Lib\\Cap.SchLib"},
        ])
        ops = sent.params["placements"].split("~~")
        assert len(ops) == 2
        assert "lib_reference=Res1" in ops[0]
        assert "rotation=90" in ops[1]
        assert "library_path=C:\\Lib\\Cap.SchLib" in ops[1]


class TestSchAttachSpicePrimitivesBulk:
    @pytest.mark.asyncio
    async def test_bulk_attach_skips_missing_fields(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.generic")
        from eda_agent.tools import generic as g
        tools = _capture(g, "register_generic_tools")
        await tools["sim_attach_primitives"](attachments=[
            {"designator": "R1", "primitive": "R", "value": "10k"},
            {"designator": "C1", "primitive": "C"},
            {"designator": "", "primitive": "R"},  # no designator → drop
            {"designator": "D1"},                   # no primitive → drop
        ])
        assert sent.command == "generic.attach_spice_primitives"
        ops = sent.params["attachments"].split("~~")
        assert len(ops) == 2
        assert "designator=R1" in ops[0]
        assert "value=10k" in ops[0]
        # C1 has no value → omit the field entirely.
        assert "value=" not in ops[1]


class TestLibAddPins:
    @pytest.mark.asyncio
    async def test_bulk_pin_packing(self, monkeypatch):
        sent = _install_fake_bridge(monkeypatch, "eda_agent.tools.library")
        from eda_agent.tools import library as lib
        tools = _capture(lib, "register_library_tools")
        await tools["lib_add_pins"](pins=[
            {"designator": "1", "name": "OUT1", "x": 0, "y": 0,
             "rotation": 180, "electrical_type": "output"},
            {"designator": "2", "name": "IN1-", "x": 0, "y": 100,
             "rotation": 180, "electrical_type": "input"},
            {"designator": "3", "name": "IN1+", "x": 0, "y": 200,
             "rotation": 180, "electrical_type": "input"},
            {"designator": "4", "name": "GND",  "x": 0, "y": 300,
             "electrical_type": "power", "hidden": True},
        ])
        assert sent.command == "library.add_pins"
        ops = sent.params["pins"].split("~~")
        assert len(ops) == 4
        assert "designator=1;name=OUT1" in ops[0]
        assert "electrical_type=output" in ops[0]
        assert "hidden=true" in ops[3]
        # Default length/rotation propagate.
        assert "length=200" in ops[3]
        assert "rotation=0" in ops[3]
