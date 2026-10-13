"""Altium 365 write tools (eng_a365_publish_model, eng_a365_release_component,
eng_a365_update_parameters): Phase 4 of the Booster integration.

Every write tool refuses unless confirm=True, and update_parameters'
replace_existing additionally requires confirm_replace set to the exact
string Booster 3.2's own guard uses ("REPLACE ALL PARAMETERS"). These
exercise the tool-level glue (confirm gates, read-back verification,
naming checks) against a fake NexarClient - the underlying client
methods (create_model, release_component, update_parameters) are
already tested in test_a365_client.py.
"""
from __future__ import annotations

import pytest

from eda_agent.tools import engineering as eng_module
from eda_agent.tools.engineering import register_engineering_tools
from eda_agent.tools.registry import ToolRegistry


def _tool(name):
    registry = ToolRegistry()
    register_engineering_tools(registry, altium=True)
    return registry.get(name).fn


class _FakeNexar:
    def __init__(self, component_params: dict | None = None):
        self.calls: list[tuple] = []
        self._component_params = component_params or {"MPN": "ABC123"}

    def update_parameters(self, component_id, parameters, release_note, replace_existing):
        self.calls.append(("update_parameters", component_id, parameters,
                           release_note, replace_existing))
        return {"new_component_id": "C2"}

    def component(self, component_id):
        self.calls.append(("component", component_id))
        return {"id": component_id, "details": {"parameters": [
            {"name": k, "value": v} for k, v in self._component_params.items()]}}

    def create_model(self, kind, workspace_url, folder_id, name, file_path):
        self.calls.append(("create_model", kind, workspace_url, folder_id, name, str(file_path)))
        return {"id": "SYM1", "file_id": "F1"}

    def release_component(self, input_obj):
        self.calls.append(("release_component", input_obj))
        return {"released": input_obj["componentItemName"]}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeNexar()
    monkeypatch.setattr(eng_module, "_a365_client", lambda: client)
    return client


# ---------------------------------------------------------------- confirm gates

def test_publish_model_refuses_without_confirm(fake_client):
    result = _tool("eng_a365_publish_model")("symbol", "https://ws/", "F1", "AMP2", "x.SchLib")
    assert result["ok"] is False and "confirm" in result["reason"]
    assert fake_client.calls == []


def test_release_component_refuses_without_confirm(fake_client):
    result = _tool("eng_a365_release_component")({"workspaceUrl": "https://ws/", "componentItemName": "AMP2"})
    assert result["ok"] is False and "confirm" in result["reason"]
    assert fake_client.calls == []


def test_update_parameters_refuses_without_confirm(fake_client):
    result = _tool("eng_a365_update_parameters")("C1", [{"name": "MPN", "value": "X"}], "note")
    assert result["ok"] is False and "confirm" in result["reason"]
    assert fake_client.calls == []


# ---------------------------------------------------------------- publish_model

def test_publish_model_succeeds_with_confirm(fake_client):
    result = _tool("eng_a365_publish_model")(
        "symbol", "https://ws/", "F1", "AMP2", "x.SchLib", confirm=True)
    assert result == {"ok": True, "id": "SYM1", "file_id": "F1"}
    assert fake_client.calls == [("create_model", "symbol", "https://ws/", "F1", "AMP2", "x.SchLib")]


def test_publish_model_rejects_an_unknown_kind(fake_client):
    result = _tool("eng_a365_publish_model")(
        "footprint_typo", "https://ws/", "F1", "AMP2", "x.SchLib", confirm=True)
    assert result["ok"] is False
    assert fake_client.calls == []


def test_publish_model_rejects_a_name_outside_owner_naming_policy(fake_client):
    result = _tool("eng_a365_publish_model")(
        "symbol", "https://ws/", "F1", "not a valid name!", "x.SchLib", confirm=True)
    assert result["ok"] is False
    assert fake_client.calls == []


# ---------------------------------------------------------------- release_component

def test_release_component_succeeds_with_confirm(fake_client):
    result = _tool("eng_a365_release_component")(
        {"workspaceUrl": "https://ws/", "componentItemName": "AMP2"}, confirm=True)
    assert result == {"ok": True, "released": "AMP2"}
    assert fake_client.calls == [("release_component",
                                  {"workspaceUrl": "https://ws/", "componentItemName": "AMP2"})]


# ---------------------------------------------------------------- update_parameters

def test_update_parameters_succeeds_and_verifies_by_readback(fake_client):
    result = _tool("eng_a365_update_parameters")(
        "C1", [{"name": "MPN", "value": "ABC123"}], "bump MPN", confirm=True)
    assert result == {"ok": True, "new_component_id": "C2", "parameters_verified": 1}
    assert ("update_parameters", "C1", [{"name": "MPN", "value": "ABC123"}],
           "bump MPN", False) in fake_client.calls
    assert ("component", "C2") in fake_client.calls


def test_update_parameters_catches_a_readback_mismatch(fake_client):
    fake_client._component_params = {"MPN": "WRONG_VALUE"}
    result = _tool("eng_a365_update_parameters")(
        "C1", [{"name": "MPN", "value": "ABC123"}], "bump MPN", confirm=True)
    assert result["ok"] is False
    assert "read-back" in result["reason"]


def test_update_parameters_replace_existing_requires_the_exact_confirmation_string(fake_client):
    result = _tool("eng_a365_update_parameters")(
        "C1", [{"name": "MPN", "value": "X"}], "note",
        replace_existing=True, confirm=True)
    assert result["ok"] is False
    assert "REPLACE ALL PARAMETERS" in result["reason"]
    assert fake_client.calls == []

    # A near-miss string must not be accepted either.
    result2 = _tool("eng_a365_update_parameters")(
        "C1", [{"name": "MPN", "value": "X"}], "note",
        replace_existing=True, confirm_replace="replace all parameters", confirm=True)
    assert result2["ok"] is False
    assert fake_client.calls == []


def test_update_parameters_replace_existing_proceeds_with_the_exact_string(fake_client):
    result = _tool("eng_a365_update_parameters")(
        "C1", [{"name": "MPN", "value": "ABC123"}], "note",
        replace_existing=True, confirm_replace="REPLACE ALL PARAMETERS", confirm=True)
    assert result["ok"] is True
    assert ("update_parameters", "C1", [{"name": "MPN", "value": "ABC123"}],
           "note", True) in fake_client.calls
