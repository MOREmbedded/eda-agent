# SPDX-License-Identifier: Apache-2.0
"""Offline tests for checkpoint-backed command transactions."""

from __future__ import annotations

import pytest

from eda_agent.bridge.change_transaction import validate_change_plan


def test_plan_validation_blocks_lifecycle_and_destructive_commands():
    errors = validate_change_plan(
        [
            {"command": "application.stop_server", "params": {}},
            {"command": "pcb.delete_tracks", "params": {}},
        ],
        [{"command": "pcb.place_via", "params": {}}],
        allow_destructive=False,
    )
    assert any("lifecycle" in e for e in errors)
    assert any("allow_destructive" in e for e in errors)
    assert any("validation command" in e for e in errors)


def test_valid_plan_accepts_mutation_plus_audit():
    assert validate_change_plan(
        [{"command": "pcb.place_track", "params": {"net": "N"}}],
        [{"command": "pcb.run_drc", "params": {}}],
        allow_destructive=False,
    ) == []


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Info:
    def summary(self):
        return {"id": "cp1", "label": "test", "file_count": 2}


class _Store:
    def __init__(self, _path):
        pass

    def create(self, *_args, **_kwargs):
        return _Info()

    def restore(self, checkpoint_id, project_dir, prune_added=False):
        return {"restored": 2, "checkpoint_id": checkpoint_id,
                "prune_added": prune_added}


class _Bridge:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "project.get_project_path":
            return {"project_dir": "D:/project", "project_name": "x.PrjPcb"}
        if command == "application.get_open_documents":
            return [{"file_path": "D:/project/Main.SchDoc", "loaded": True}]
        if command == "pcb.place_track" and self.fail:
            raise RuntimeError("placement failed")
        return {"success": True}


def _registered(monkeypatch, bridge):
    from eda_agent import checkpoint
    from eda_agent.tools import application
    monkeypatch.setattr(checkpoint, "CheckpointStore", _Store)
    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    application.register_application_tools(mcp)
    return mcp.tools


@pytest.mark.asyncio
async def test_transaction_is_dry_run_by_default(monkeypatch):
    bridge = _Bridge()
    tools = _registered(monkeypatch, bridge)
    result = await tools["app_change_transaction"](
        [{"command": "pcb.place_track", "params": {}}],
        [{"command": "pcb.run_drc", "params": {}}],
    )
    assert result["dry_run"] is True
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_transaction_rolls_back_and_reloads_on_failure(monkeypatch):
    bridge = _Bridge(fail=True)
    tools = _registered(monkeypatch, bridge)
    result = await tools["app_change_transaction"](
        [{"command": "pcb.place_track", "params": {}}],
        dry_run=False,
        confirm=True,
    )
    assert result["success"] is False
    assert result["rollback"]["restored"] == 2
    assert any(command == "application.reload_document" for command, _ in bridge.calls)

