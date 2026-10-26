# SPDX-License-Identifier: Apache-2.0
"""The scripts folder is the one Altium loads, not an assumed default.

`install-scripts`, `health` and `update` all used
%USERPROFILE%\\EDA Agent\\scripts\\. With the Global Project registered
anywhere else, they reported "deployed scripts current" about a folder
Altium never reads, while Altium compiled a build days old. Altium
keeps its Global Projects list in the registry, so the folder is read
from there.
"""
from __future__ import annotations

import sys

import pytest

from eda_agent import cli
from eda_agent.diag import update as mod
from eda_agent.diag.update import plan


def _registered(monkeypatch, *values):
    """``registered_script_projects`` yields a dict per install, not a path."""
    monkeypatch.setattr(
        cli, "registered_script_projects",
        lambda: [{"install": f"AD{i}", "path": v, "exists": True}
                 for i, v in enumerate(values)])


def test_one_registered_folder_is_the_default(monkeypatch, tmp_path):
    _registered(monkeypatch, str(tmp_path / "Altium_API.PrjScr"))
    assert cli.get_default_scripts_dest() == tmp_path


def test_nothing_registered_falls_back(monkeypatch):
    _registered(monkeypatch)
    assert cli.get_default_scripts_dest() == cli.get_fallback_scripts_dest()


def test_other_script_projects_are_ignored(monkeypatch, tmp_path):
    # A real machine here lists an unrelated AltiumLL.PrjScr too.
    _registered(monkeypatch, str(tmp_path / "other" / "AltiumLL.PrjScr"),
                str(tmp_path / "ours" / "altium_api.PRJSCR"))
    assert cli.get_registered_scripts_dirs() == [tmp_path / "ours"]


def test_two_installs_naming_one_folder_count_once(monkeypatch, tmp_path):
    _registered(monkeypatch, str(tmp_path / "Altium_API.PrjScr"),
                str(tmp_path / "Altium_API.PrjScr"))
    assert cli.get_default_scripts_dest() == tmp_path


def test_two_different_folders_both_get_the_scripts(monkeypatch, tmp_path):
    """Two Altium versions, each loading the project from its own folder.

    This used to refuse to choose and fall back to the default folder,
    which left BOTH registered folders stale while `health` reported on
    a third one nobody compiled from. Both are returned now, so
    `install-scripts` writes to each and every version compiles the same
    build; the first is what the default resolves to.
    """
    _registered(monkeypatch, str(tmp_path / "a" / "Altium_API.PrjScr"),
                str(tmp_path / "b" / "Altium_API.PrjScr"))
    assert cli.get_registered_scripts_dirs() == [tmp_path / "a", tmp_path / "b"]
    assert cli.get_default_scripts_dest() == tmp_path / "a"


def test_install_scripts_writes_where_altium_reads(monkeypatch, tmp_path):
    dest = tmp_path / "registered"
    _registered(monkeypatch, str(dest / "Altium_API.PrjScr"))
    assert cli.cmd_install_scripts(force=True) == 0
    assert (dest / "Altium_API.PrjScr").is_file()
    assert (dest / "Main.pas").is_file()


def test_update_compares_against_the_registered_folder(monkeypatch, tmp_path):
    dest = tmp_path / "registered"
    _registered(monkeypatch, str(dest / "Altium_API.PrjScr"))
    state = mod.gather_state()
    assert state["deployed_scripts"] == str(dest)
    if sys.platform == "win32":
        assert state["registered_scripts"] == [str(dest)]


def _plan_with(registered):
    state = {
        "repo_root": "/repo", "editable": True, "fetched": True,
        "behind": 0, "ahead": 0, "branch": "main", "dirty": False,
        "bundled_scripts": "/repo/scripts/altium",
        "deployed_scripts": "/home/scripts",
        "bundled_version": "2026.10.19.1",
        "deployed_version": "2026.10.19.1",
        "package_version": "0.7.1",
        "registered_scripts": registered,
    }
    return plan(state, compiled_version="2026.10.19.1")


def test_one_registered_folder_adds_no_step():
    steps = _plan_with(["/home/scripts"])
    assert not [s for s in steps if s.name == "script folder"]
    assert not any(s.needs_human for s in steps)


def test_several_registered_folders_are_an_action():
    steps = [s for s in _plan_with(["C:/a", "C:/b"])
             if s.name == "script folder"]
    assert len(steps) == 1 and steps[0].needs_human
    assert "C:/a" in steps[0].message and "C:/b" in steps[0].message
    assert "--dest" in steps[0].action


def test_no_registered_folder_is_said_but_does_not_fail_the_run():
    steps = _plan_with([])
    folder = [s for s in steps if s.name == "script folder"]
    assert len(folder) == 1 and folder[0].status == "unknown"
    assert not any(s.needs_human for s in steps)


def test_off_windows_nothing_is_said_about_the_registry():
    assert not [s for s in _plan_with(None) if s.name == "script folder"]


@pytest.mark.skipif(sys.platform != "win32", reason="the registry is Windows")
def test_the_real_registry_reader_returns_strings(monkeypatch):
    # Undo the conftest stub for this one read-only call.
    monkeypatch.undo()
    values = cli.registered_script_projects()
    assert isinstance(values, list)
    assert all(isinstance(v, dict) for v in values)
    assert all(isinstance(v["path"], str) for v in values)


def test_install_scripts_points_altium_at_the_servers_workspace(
        monkeypatch, tmp_path):
    # The pointer was <dest's parent>\workspace\, right only for the old
    # fixed default. Deploying into a registered folder inside a repo
    # would then point Altium at a directory the server never writes to.
    from eda_agent.config import _default_workspace_dir

    pointer = tmp_path / "pointer.txt"
    monkeypatch.setenv("EDA_AGENT_POINTER_FILE", str(pointer))
    dest = tmp_path / "repo" / ".eda-local" / "scripts"
    _registered(monkeypatch, str(dest / "Altium_API.PrjScr"))
    assert cli.cmd_install_scripts(force=True) == 0
    assert not (dest.parent / "workspace").exists()
    written = pointer.read_bytes().decode("mbcs" if sys.platform == "win32"
                                          else "cp1252").strip()
    assert written.rstrip("\\/") == str(_default_workspace_dir()).rstrip("\\/")
