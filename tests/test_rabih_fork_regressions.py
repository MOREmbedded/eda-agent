# SPDX-License-Identifier: Apache-2.0
"""Regression coverage for multipart SchLib fixes recovered from RabihND."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GENERIC = (ROOT / "scripts/altium/Generic.pas").read_text(encoding="utf-8")
LIBRARY = (ROOT / "scripts/altium/Library.pas").read_text(encoding="utf-8")


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True}


@pytest.mark.asyncio
async def test_set_active_part_wrapper(monkeypatch):
    from eda_agent.tools import library

    bridge = _Bridge()
    monkeypatch.setattr(library, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    library.register_library_tools(mcp)

    await mcp.tools["lib_set_active_part"](3)
    assert bridge.calls[-1] == ("library.set_active_part", {"part_id": 3})


def test_schlib_queries_walk_component_owned_primitives():
    assert "LibComp := GetTargetLibComponent(SchLib);" in GENERIC
    assert "Iterator := LibComp.SchIterator_Create;" in GENERIC
    assert '"_owner_part_id":' in GENERIC


def test_component_create_sets_owner_before_explicit_properties():
    owner = GENERIC.index("SetOwnerPart(NewObj, Component);")
    apply_props = GENERIC.index("ApplySetProperties(NewObj, PropsStr);", owner)
    add = GENERIC.index("Component.AddSchObject(NewObj);", apply_props)
    assert owner < apply_props < add
    assert "Else If PropName = 'OwnerPartId' Then" in GENERIC
    assert "MarkLibDirty(SchLib);" in GENERIC[add : add + 500]


def test_batch_rename_reports_failures_and_rejects_collisions():
    assert "target name already exists" in LIBRARY
    assert "component not found" in LIBRARY
    assert "write raised" in LIBRARY
    assert "EscapeJsonString(Errors)" in LIBRARY


def test_component_details_prefers_live_part_count():
    assert "Try PartCount := Component.PartCount; Except End;" in LIBRARY

