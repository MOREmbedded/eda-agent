# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""`eda-agent relaunch` starts the bridge only when asked, and harms nothing.

It must not purge request files another session may be waiting on, must
leave a bridge that already answers alone, and must not guess between
two Altium processes.
"""

from __future__ import annotations

import sys

import pytest

from eda_agent import cli
from eda_agent.bridge import process_manager as pm

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="relaunch drives Windows Altium")


def _one_altium(tmp_path, monkeypatch):
    exe = tmp_path / "X2.EXE"
    exe.write_bytes(b"")
    info = pm.AltiumProcessInfo(pid=4242, name="X2.EXE", exe_path=str(exe))
    monkeypatch.setattr(
        pm.AltiumProcessManager, "select_altium_process",
        lambda self: pm.AltiumSelection(info, [info], "only_candidate"))
    project = tmp_path / "Altium_API.PrjScr"
    project.write_text("[Design]", encoding="utf-8")
    return exe, project


def test_the_command_runs_startmcpserver_from_the_project():
    command = cli.relaunch_command(r"C:\A\X2.EXE", r"C:\S\Altium_API.PrjScr")
    assert command == [
        r"C:\A\X2.EXE",
        r"-RScriptingSystem:RunScript(ProjectName=C:\S\Altium_API.PrjScr"
        r"|ProcName=Dispatcher.pas>StartMCPServer)"]


def test_a_live_bridge_is_left_alone(tmp_path, monkeypatch, capsys):
    _, project = _one_altium(tmp_path, monkeypatch)
    import eda_agent.diag.doctor as doctor
    monkeypatch.setattr(doctor, "_ping_altium", lambda: {"pong": True})
    started = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    assert cli.cmd_relaunch(prjscr=str(project)) == 0
    assert started == []
    assert "already answers" in capsys.readouterr().out


def test_pending_requests_survive_a_relaunch(tmp_path, monkeypatch):
    exe, project = _one_altium(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pending = workspace / "request_abc.json"
    pending.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_WORKSPACE", str(workspace))
    import eda_agent.diag.doctor as doctor
    monkeypatch.setattr(doctor, "_ping_altium", lambda: None)
    started = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen",
                        lambda command, **k: started.append(command))
    assert cli.cmd_relaunch(prjscr=str(project)) == 0
    assert started == [cli.relaunch_command(str(exe), str(project))]
    assert pending.exists()


def test_two_altiums_are_not_guessed_between(tmp_path, monkeypatch, capsys):
    project = tmp_path / "Altium_API.PrjScr"
    project.write_text("[Design]", encoding="utf-8")
    monkeypatch.setattr(
        pm.AltiumProcessManager, "select_altium_process",
        lambda self: pm.AltiumSelection(None, [], "ambiguous", "2 running"))
    assert cli.cmd_relaunch(prjscr=str(project)) == 1
    assert "2 running" in capsys.readouterr().err


def test_a_missing_project_says_to_install_the_scripts(tmp_path, capsys):
    assert cli.cmd_relaunch(prjscr=str(tmp_path / "none.PrjScr")) == 1
    assert "install-scripts" in capsys.readouterr().err
