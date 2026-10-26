# SPDX-License-Identifier: Apache-2.0
"""eda-agent eco-execute: the CLI wrapper around the existing
ui.dialog_driver.drive() reactive driver.

EXPERIMENTAL, UNVERIFIED against a live Altium session (see cli.py's
cmd_eco_execute docstring and docs/ENGINEERING_TOOLS.md) - these tests
cover the command's own glue logic (the --confirm gate, the
Altium-not-running refusal, and that it calls drive() with the right
arguments), not drive() itself, which is already tested elsewhere
(tests/test_dialog_driver.py).
"""
from __future__ import annotations

from eda_agent import cli


def test_eco_execute_refuses_without_confirm(capsys):
    rc = cli.cmd_eco_execute(confirm=False, validate_only=False,
                             allow_confirm_dialogs=False,
                             wait_first=60.0, budget=300.0)
    assert rc == 1
    assert "--confirm" in capsys.readouterr().err


def test_eco_execute_refuses_when_altium_is_not_running(monkeypatch, capsys):
    import eda_agent.bridge.process_manager as pm
    monkeypatch.setattr(pm.AltiumProcessManager, "get_altium_pid", lambda self: None)

    rc = cli.cmd_eco_execute(confirm=True, validate_only=False,
                             allow_confirm_dialogs=False,
                             wait_first=60.0, budget=300.0)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_eco_execute_drives_with_commit_allowed_by_default(monkeypatch, capsys):
    import eda_agent.bridge.process_manager as pm
    import eda_agent.ui.dialog_driver as dd

    monkeypatch.setattr(pm.AltiumProcessManager, "get_altium_pid", lambda self: 4242)
    calls = []

    def fake_drive(pid, **kwargs):
        calls.append((pid, kwargs))
        return {"committed": True, "stopped_for_a_human": False}

    monkeypatch.setattr(dd, "drive", fake_drive)

    rc = cli.cmd_eco_execute(confirm=True, validate_only=False,
                             allow_confirm_dialogs=True,
                             wait_first=12.0, budget=99.0)
    assert rc == 0
    assert calls == [(4242, {
        "intent": "proceed", "allow_commit": True, "allow_confirm": True,
        "dry_run": False, "wait_first": 12.0, "budget": 99.0,
    })]
    assert '"committed": true' in capsys.readouterr().out.lower()


def test_eco_execute_validate_only_does_not_allow_commit(monkeypatch):
    import eda_agent.bridge.process_manager as pm
    import eda_agent.ui.dialog_driver as dd

    monkeypatch.setattr(pm.AltiumProcessManager, "get_altium_pid", lambda self: 4242)
    calls = []
    monkeypatch.setattr(dd, "drive", lambda pid, **kw: (calls.append(kw) or
                        {"committed": False, "stopped_for_a_human": False}))

    cli.cmd_eco_execute(confirm=True, validate_only=True,
                        allow_confirm_dialogs=False,
                        wait_first=60.0, budget=300.0)
    assert calls[0]["allow_commit"] is False


def test_eco_execute_reports_a_dialog_needing_a_human_with_a_distinct_exit_code(
        monkeypatch, capsys):
    import eda_agent.bridge.process_manager as pm
    import eda_agent.ui.dialog_driver as dd

    monkeypatch.setattr(pm.AltiumProcessManager, "get_altium_pid", lambda self: 4242)
    monkeypatch.setattr(dd, "drive", lambda pid, **kw: {
        "committed": False, "stopped_for_a_human": True,
        "why": "an error dialog needs a human"})

    rc = cli.cmd_eco_execute(confirm=True, validate_only=False,
                             allow_confirm_dialogs=False,
                             wait_first=60.0, budget=300.0)
    assert rc == 2
    assert "needed attention" in capsys.readouterr().err
