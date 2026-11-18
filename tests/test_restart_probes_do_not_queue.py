# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A restart's own probes end at their deadline, and a dead Altium is said.

MEASURED (2026-11-18, logs/crashes/*_project.close_*.json): after a crash
the restart wrote the maintenance marker, then pinged for the new loop.
Each ping waited behind that same marker, 30 windows, and _ping_altium
tries twice, so the restart answered "the new loop did not answer within
90s" after 206 s; Altium's scripting engine had died, which no line said.
"""
from __future__ import annotations

import inspect
import time

import pytest

from eda_agent import cli
from eda_agent.bridge import altium_bridge as ab
from eda_agent.bridge.exceptions import AltiumTimeoutError


@pytest.fixture
def bridge(tmp_path):
    from eda_agent.config import configure
    configure(workspace_dir=tmp_path)
    b = ab.AltiumBridge()
    yield b
    b.detach()


def test_a_probe_does_not_wait_behind_a_restart(bridge, tmp_path):
    rid = ab._short_id()
    (tmp_path / f"request_{rid}.json").write_text("{}")
    (tmp_path / ab.MAINTENANCE_FILE).write_text("{}")
    started = time.monotonic()
    with ab.no_queue_wait():
        with pytest.raises(AltiumTimeoutError):
            bridge._poll_response(rid, timeout=0.3)
    assert time.monotonic() - started < 3.0


def test_the_guard_is_undone_after_the_block():
    with ab.no_queue_wait():
        assert ab._CALL.no_queue_wait
    assert not getattr(ab._CALL, "no_queue_wait", False)


def test_the_restart_pings_without_queueing(monkeypatch):
    seen = []

    class Bridge:
        def ping_with_version(self, require_process=True, wait_in_queue=True):
            seen.append(wait_in_queue)
            return {"pong": True}

    import eda_agent.bridge as bridge_pkg
    from eda_agent.diag import doctor

    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: Bridge())
    assert doctor._ping_altium(wait_in_queue=False) == {"pong": True}
    assert seen == [False]
    source = inspect.getsource(cli._restart)
    assert "with no_queue_wait():" in source
    assert source.count("_ping_altium(") == 1, "every restart ping goes through probe()"


def test_the_restart_gives_up_at_once_on_an_exited_altium(monkeypatch, tmp_path):
    from eda_agent.config import configure
    from eda_agent.diag import doctor
    from eda_agent.ui import dialog_report, windows

    configure(workspace_dir=tmp_path)
    monkeypatch.setattr(dialog_report, "report", lambda pid: {"dialog_count": 0})
    monkeypatch.setattr(windows, "visible_main_frames", lambda pid: [])
    monkeypatch.setattr(doctor, "_ping_altium", lambda: None)
    monkeypatch.setattr(cli, "_stop_halted_script", lambda command: None)
    monkeypatch.setattr(cli, "_reshow_hidden_frames", lambda *a: None)
    monkeypatch.setattr(cli, "_altium_gone", lambda pid: "Altium is no longer running")

    class Launcher:
        def __init__(self, *a, **k):
            pass

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", Launcher)
    started = time.monotonic()
    assert cli._restart(["X2.EXE"], 4242) == 1
    assert time.monotonic() - started < cli.RESTART_PING_WAIT_S / 3
    assert not (tmp_path / ab.MAINTENANCE_FILE).exists()


def test_an_exited_altium_is_named_with_the_next_step(monkeypatch):
    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: False)
    why = cli._altium_gone(4321)
    assert "no longer running" in why and "StartMCPServer" in why


def test_a_running_responsive_altium_is_not_gone(monkeypatch):
    import psutil

    from eda_agent.ui import windows

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(windows, "visible_main_frames", lambda pid: [])
    assert cli._altium_gone(4321) == ""
