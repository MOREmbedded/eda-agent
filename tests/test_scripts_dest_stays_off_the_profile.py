# SPDX-License-Identifier: Apache-2.0
r"""The script project has one home, and it is not %USERPROFILE% by accident.

With no Altium install registering a folder, install-scripts and relaunch
fell back to %USERPROFILE%\EDA Agent\scripts while the checkout's own
.eda-local\scripts copy stayed open in Altium with an older compile
(2026-11-27). EDA_AGENT_SCRIPTS_DIR names the folder outright; without it
the checkout's folder, when it holds the project, comes before the profile.
"""
from __future__ import annotations

from eda_agent import cli

USES_CHECKOUT_SCRIPTS = True


def _checkout(monkeypatch, tmp_path, with_project=True):
    root = tmp_path / "repo"
    (root / "src" / "eda_agent").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    if with_project:
        (root / ".eda-local" / "scripts").mkdir(parents=True)
        (root / ".eda-local" / "scripts" / "Altium_API.PrjScr").write_text("")
    monkeypatch.setattr(cli, "get_package_root", lambda: root / "src" / "eda_agent")
    monkeypatch.setattr(cli, "get_registered_scripts_dirs", lambda: [])
    return root


def test_the_checkouts_folder_comes_before_the_profile(monkeypatch, tmp_path):
    root = _checkout(monkeypatch, tmp_path)
    assert cli.get_default_scripts_dest() == root / ".eda-local" / "scripts"
    assert cli.get_install_targets() == [root / ".eda-local" / "scripts"]


def test_a_checkout_without_the_project_still_uses_the_profile(monkeypatch, tmp_path):
    _checkout(monkeypatch, tmp_path, with_project=False)
    assert cli.get_default_scripts_dest() == cli.get_fallback_scripts_dest()


def test_the_environment_names_the_folder_outright(monkeypatch, tmp_path):
    _checkout(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "get_registered_scripts_dirs", lambda: [tmp_path / "reg"])
    monkeypatch.setenv(cli.SCRIPTS_DIR_ENV, str(tmp_path / "chosen"))
    assert cli.get_install_targets() == [tmp_path / "chosen"]
    assert cli.get_default_scripts_dest() == tmp_path / "chosen"


def test_relaunch_opens_the_project_in_that_folder(monkeypatch, tmp_path):
    root = _checkout(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    seen = {}

    class Proc:
        pid, exe_path = 1, str(tmp_path / "X2.EXE")

    (tmp_path / "X2.EXE").write_text("")

    class Sel:
        process, reason = Proc(), ""

    from eda_agent.bridge import process_manager as pm
    monkeypatch.setattr(pm.AltiumProcessManager, "select_altium_process", lambda self: Sel())
    monkeypatch.setattr(cli, "relaunch_command",
                        lambda exe, prj: seen.setdefault("prj", prj) and [])
    assert cli.cmd_relaunch(dry_run=True) == 0
    assert seen["prj"] == str(root / ".eda-local" / "scripts" / "Altium_API.PrjScr")
