"""Altium 365 / Nexar client: the 3.2 additions (update_parameters, entity,
workspace_infos, library_components_params) confirmed new via Booster 3.2's
own test_v32.py (update_parameters) and absent from its 3.1 test suite.

Uses NexarClient's constructor injection (opener, store) rather than hitting
any network or disk, following this module's own design for that purpose.
"""
from __future__ import annotations

import io
import json
import time

import pytest

from eda_agent.engineering.a365 import NexarClient
from eda_agent.engineering.config import A365Policy
from eda_agent.engineering.errors import NotFound, PermissionDenied, ValidationFailed


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _QueueOpener:
    """Returns one canned response per call, in call order; records each request body."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data.decode()))
        if not self._responses:
            raise AssertionError("fake opener ran out of canned responses")
        return _Response(json.dumps(self._responses.pop(0)).encode())


class _FixedStore:
    """A token that never needs a refresh, so _token() never touches disk."""

    def load(self):
        return {"access_token": "tok", "obtained_at": time.time(), "expires_in": 99999}


def _client(responses: list[dict]) -> tuple[NexarClient, _QueueOpener]:
    opener = _QueueOpener(responses)
    cfg = A365Policy(client_id="cid")
    return NexarClient(cfg, store=_FixedStore(), opener=opener), opener


def test_workspace_infos_adds_id_and_description():
    client, _ = _client([
        {"data": {"desWorkspaceInfos": [
            {"url": "https://ws.nexar.com/w/", "workspaceId": "w1",
             "name": "WS", "description": "demo workspace"},
        ]}},
    ])
    out = client.workspace_infos()
    assert out == [{"url": "https://ws.nexar.com/w/", "workspaceId": "w1",
                    "name": "WS", "description": "demo workspace"}]


def test_workspace_infos_respects_the_allow_list():
    client, _ = _client([
        {"data": {"desWorkspaceInfos": [
            {"url": "https://allowed/", "workspaceId": "w1", "name": "A", "description": ""},
            {"url": "https://other/", "workspaceId": "w2", "name": "B", "description": ""},
        ]}},
    ])
    client.cfg = A365Policy(client_id="cid", workspaces=("https://allowed/",))
    out = client.workspace_infos()
    assert [w["url"] for w in out] == ["https://allowed/"]


def test_library_components_params_returns_each_components_parameters():
    client, opener = _client([
        {"data": {"desLibrary": {"components": {"nodes": [
            {"id": "c1", "name": "R1",
             "details": {"parameters": [{"type": "Value", "name": "Value", "value": "10k"}]}},
        ]}}}},
    ])
    client.cfg = A365Policy(client_id="cid", workspaces=("https://ws/",))
    out = client.library_components_params("https://ws/")
    assert out[0]["details"]["parameters"][0]["value"] == "10k"
    assert opener.requests[0]["variables"] == {"ws": "https://ws/"}


def _query_type_response(field_name: str, inner_type_name: str) -> dict:
    return {"data": {"__type": {"fields": [
        {"name": field_name, "args": [{"name": "id"}],
         "type": {"name": None, "kind": "NON_NULL",
                  "ofType": {"name": inner_type_name, "kind": "OBJECT", "ofType": None}}},
    ]}}}


def test_entity_reads_scalar_fields_from_the_live_schema():
    client, opener = _client([
        _query_type_response("desFootprintById", "DesFootprint"),
        {"data": {"__type": {"fields": [
            {"name": "id", "type": {"kind": "SCALAR", "ofType": None}},
            {"name": "name", "type": {"kind": "SCALAR", "ofType": None}},
            {"name": "subFolder", "type": {"kind": "OBJECT", "ofType": None}},
        ]}}},
        {"data": {"desFootprintById": {"id": "F1", "name": "SOIC8"}}},
    ])
    out = client.entity("desFootprintById", "F1")
    assert out == {"type": "DesFootprint", "fields": {"id": "F1", "name": "SOIC8"},
                   "object_fields_available": ["subFolder"]}
    # the final query asks only for the scalar fields, not the object one
    final_query = opener.requests[-1]["query"]
    assert "subFolder" not in final_query
    assert "id" in final_query and "name" in final_query


def test_entity_rejects_a_query_field_shaped_like_something_else():
    client, _ = _client([])
    with pytest.raises(ValidationFailed):
        client.entity("desWorkspaces", "x")


def test_entity_reports_a_query_field_absent_from_the_live_schema():
    client, _ = _client([{"data": {"__type": {"fields": [
        {"name": "desProjectById", "type": {"kind": "SCALAR", "ofType": None}}]}}}])
    with pytest.raises(NotFound):
        client.entity("desNoSuchThingById", "x")


def test_update_parameters_returns_the_new_revisions_component_id():
    client, opener = _client([
        {"data": {"__type": {"fields": [
            {"name": "desUpdateComponentRevisionParameters"}]}}},
        {"data": {"desUpdateComponentRevisionParameters": {"componentId": "C2"}}},
    ])
    out = client.update_parameters("C1", [{"name": "MPN", "value": "ABC123"}],
                                   "bump MPN", replace_existing=False)
    assert out == {"new_component_id": "C2"}
    mutation_call = opener.requests[-1]["query"]
    assert mutation_call.startswith(
        "mutation { desUpdateComponentRevisionParameters(input: ")
    assert 'componentId: "C1"' in mutation_call
    assert 'replaceExisting: false' in mutation_call


def test_update_parameters_is_disabled_when_the_mutation_is_absent_from_the_schema():
    client, _ = _client([{"data": {"__type": {"fields": []}}}])
    with pytest.raises(NotFound):
        client.update_parameters("C1", [{"name": "MPN", "value": "x"}], "note")


def test_execute_refuses_an_inline_mutation_that_does_not_match_its_template():
    """The _mutation_template guard must check the actual string, not trust the caller."""
    client, _ = _client([])
    with pytest.raises(PermissionDenied):
        client.execute("mutation { desDeleteEverything }", {},
                       _mutation_template="update_parameters_inline")
