# SPDX-License-Identifier: Apache-2.0
"""eda-agent start: per-workspace settings, kept in the workspace folder."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from eda_agent import profile as profile_mod
from eda_agent import workspace_start as ws
from eda_agent.server import build_parser


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("EDA_AGENT_ALTIUM365_WORKSPACES", raising=False)
    monkeypatch.setattr("eda_agent.altium365.saved_user_environment", lambda *a: {})


def _args(**kw):
    base = dict(workspace="", init=False, profile="", port=0, altium365_workspace=[],
                no_dashboard=True, no_doctor=True, no_login=True, relogin=False,
                shortcut=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_init_writes_settings_inside_the_workspace(tmp_path, capsys):
    root = tmp_path / "client_ws"
    root.mkdir()
    rc = ws.cmd_start(_args(workspace=str(root), init=True, profile="acme", port=8777,
                            altium365_workspace=["https://acme.365.altium.com/"]))
    assert rc == 0
    saved = json.loads((root / ".eda-agent" / "workspace.json").read_text("utf-8"))
    assert saved == {"profile": "acme", "dashboard_port": 8777,
                     "altium365_workspaces": ["https://acme.365.altium.com/"]}


def test_init_updates_only_what_is_given(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    ws.cmd_start(_args(workspace=str(root), init=True, profile="acme", port=8777))
    ws.cmd_start(_args(workspace=str(root), init=True, port=8800))
    saved = json.loads((root / ".eda-agent" / "workspace.json").read_text("utf-8"))
    assert saved == {"profile": "acme", "dashboard_port": 8800}


def test_found_from_a_project_subfolder(tmp_path):
    root = tmp_path / "w"
    (root / "board-a" / "sheets").mkdir(parents=True)
    ws.write_settings(ws.WorkspaceSettings(root=root, dashboard_port=8777))
    assert ws.find_workspace(root / "board-a" / "sheets") == root.resolve()
    assert ws.find_workspace(tmp_path) is None


@pytest.mark.parametrize("data, words", [
    ({"nexar_client_secret": "x"}, "looks like a secret"),
    ({"altium365_token": "x"}, "looks like a secret"),
    ({"colour": "red"}, "unknown key"),
    ({"dashboard_port": "8777"}, "port number"),
    ({"altium365_workspaces": ["http://acme.365.altium.com/"]}, "https://"),
])
def test_bad_settings_are_refused(tmp_path, data, words):
    with pytest.raises(ws.StartError, match=words):
        ws.parse_settings(tmp_path, data)


def test_no_settings_file_says_how_to_make_one(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ws.cmd_start(_args()) == 2
    assert "--init" in capsys.readouterr().err


def test_start_activates_profile_and_limits_altium365(tmp_path, monkeypatch):
    (profile_mod.central_profiles_root() / "acme").mkdir(parents=True)
    root = tmp_path / "w"
    root.mkdir()
    ws.write_settings(ws.WorkspaceSettings(
        root=root, profile="acme",
        altium365_workspaces=["https://acme.365.altium.com/"]))
    lines = []
    rc = ws.run_start(ws.load_settings(root), dashboard=False, doctor=False,
                      login=False, say=lines.append)
    assert rc == 0
    assert profile_mod.active_profile_pointer().read_text("utf-8") == "acme"
    import os
    assert os.environ["EDA_AGENT_ALTIUM365_WORKSPACES"] == "https://acme.365.altium.com/"
    monkeypatch.delenv("EDA_AGENT_ALTIUM365_WORKSPACES")


def test_unknown_profile_fails_but_carries_on(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    ws.write_settings(ws.WorkspaceSettings(root=root, profile="nope"))
    lines = []
    rc = ws.run_start(ws.load_settings(root), dashboard=False, doctor=False,
                      login=False, say=lines.append)
    assert rc == 1
    assert any("Altium bridge" in line for line in lines)


def test_saved_secrets_are_filled_in_but_never_printed(tmp_path, monkeypatch):
    monkeypatch.setattr("eda_agent.altium365.saved_user_environment",
                        lambda *a: {"EDA_AGENT_NEXAR_CLIENT_SECRET": "s3cr3t-value"})
    monkeypatch.delenv("EDA_AGENT_NEXAR_CLIENT_SECRET", raising=False)
    root = tmp_path / "w"
    root.mkdir()
    ws.write_settings(ws.WorkspaceSettings(root=root))
    lines = []
    ws.run_start(ws.load_settings(root), dashboard=False, doctor=False, login=False,
                 say=lines.append)
    import os
    assert os.environ["EDA_AGENT_NEXAR_CLIENT_SECRET"] == "s3cr3t-value"
    assert not any("s3cr3t" in line for line in lines)
    assert any("EDA_AGENT_NEXAR_CLIENT_SECRET" in line for line in lines)
    monkeypatch.delenv("EDA_AGENT_NEXAR_CLIENT_SECRET")


def test_foreign_listener_is_left_alone(monkeypatch):
    monkeypatch.setattr(ws, "_listeners", lambda port: [4242])
    monkeypatch.setattr(ws, "_is_ours", lambda pid: False)
    said = []
    assert ws.stop_port(8777, said.append) is False
    assert "not eda-agent" in said[0]


def test_parser_knows_start():
    args = build_parser().parse_args(
        ["start", "--init", "--port", "8777", "--altium365-workspace", "https://a/",
         "--altium365-workspace", "https://b/"])
    assert args.command == "start" and args.port == 8777
    assert args.altium365_workspace == ["https://a/", "https://b/"]
