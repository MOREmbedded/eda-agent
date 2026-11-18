# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Usage view keeps to its own workspace: each workspace is
usually one client's design folder, and the list of the others named
them. They are counted, not named, until the viewer asks for all."""
from __future__ import annotations

import json

import pytest
from flask import Flask

from eda_agent.diag import usage
from eda_agent.web import usage_view


@pytest.fixture
def two_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mine, other = tmp_path / "client_a" / "hw", tmp_path / "client_b" / "hw"
    for ws in (other, mine):
        usage.Recorder(ws, None, session="s", remember=True,
                       switch=lambda: True).record("pcb_get_nets", {}, [])
    yield mine, other
    usage.set_recorder(None)


def _dashboard_in(folder):
    usage.set_recorder(usage.Recorder(folder, None, session="", remember=False,
                                      switch=lambda: True))


def test_only_the_own_workspace_is_offered_by_default(two_clients):
    mine, other = two_clients
    _dashboard_in(mine)
    got = usage_view.state()
    assert got["workspaces"] == [str(mine)]
    assert got["workspace"] == str(mine)
    assert got["hidden"] == 1 and got["all"] is False
    assert "client_b" not in json.dumps(got)


def test_a_named_foreign_workspace_is_ignored_without_all(two_clients):
    mine, other = two_clients
    _dashboard_in(mine)
    got = usage_view.state(workspace=str(other))
    assert got["workspace"] == str(mine)
    assert "client_b" not in json.dumps(got)


def test_all_shows_every_workspace(two_clients):
    mine, other = two_clients
    _dashboard_in(mine)
    got = usage_view.state(workspace=str(other), show_all=True)
    assert set(got["workspaces"]) == {str(mine), str(other)}
    assert got["workspace"] == str(other) and got["hidden"] == 0


def test_a_subfolder_dashboard_scopes_to_its_design_workspace(two_clients):
    """Started in a project folder below the workspace `eda-agent start`
    set up, the dashboard still offers the workspace's records."""
    mine, other = two_clients
    (mine / ".eda-agent").mkdir(parents=True, exist_ok=True)
    (mine / ".eda-agent" / "workspace.json").write_text("{}", encoding="utf-8")
    sub = mine / "projects" / "board"
    sub.mkdir(parents=True)
    _dashboard_in(sub)
    got = usage_view.state()
    assert got["workspaces"] == [str(mine)]
    assert got["scope"] == str(mine.resolve())


def test_no_own_workspace_shows_none(two_clients):
    usage.set_recorder(usage.Recorder(None, None, session="", remember=False,
                                      switch=lambda: True))
    got = usage_view.state()
    assert got["workspaces"] == [] and got["report"] is None and got["hidden"] == 2


def test_the_endpoint_takes_all_as_an_opt_in(two_clients):
    mine, other = two_clients
    _dashboard_in(mine)
    app = Flask(__name__)
    usage_view.register(app)
    client = app.test_client()
    assert client.get("/api/wb/usage").get_json()["workspaces"] == [str(mine)]
    assert client.get("/api/wb/usage?all=0").get_json()["workspaces"] == [str(mine)]
    both = client.get("/api/wb/usage?all=1").get_json()["workspaces"]
    assert set(both) == {str(mine), str(other)}


def test_the_page_has_the_opt_in_box_off_by_default():
    page = usage_view.Path(usage_view.__file__).parent / "dashboard_static" / "workbench.html"
    text = page.read_text(encoding="utf-8")
    assert 'all: S.usageAll ? "1" : ""' in text
    assert "data-usageall" in text and 'T("All workspaces")' in text
    assert "usageAll: true" not in text
