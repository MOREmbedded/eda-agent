# SPDX-License-Identifier: Apache-2.0
"""The dashboard's background threads can be stopped, and are between modules.

create_app starts a log tailer, a sentinel watcher and a heartbeat as
daemon threads that ran until the process ended. In a test run every
module that built an app left three of them in its xdist worker, and one
test that patched time.sleep counted the tailer's 592 sleeps as its own.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("flask")

from eda_agent.web import dashboard  # noqa: E402

NAMES = ("dashboard-tailer", "dashboard-sentinel-watch", "dashboard-heartbeat")


def _alive() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name in NAMES and t.is_alive()]


def test_stop_background_ends_every_thread_an_app_started(tmp_path):
    dashboard.stop_background()                 # what earlier tests left
    assert _alive() == []
    dashboard.create_app(tmp_path)
    assert sorted(_alive()) == sorted(NAMES)
    started = time.monotonic()
    assert dashboard.stop_background() == 1
    assert _alive() == []
    # the tailer waits on its stop event, so it ends at once, not after a sleep
    assert time.monotonic() - started < 1.5
    assert dashboard.stop_background() == 0


def test_the_tailer_does_not_spin_when_time_sleep_is_patched(tmp_path, monkeypatch):
    """Its pauses are waits on the stop event, so a patched time.sleep in
    another test does not make it loop without pausing."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)
    tailer = dashboard.ActivityTailer(tmp_path / "missing.log")
    tailer.start()
    try:
        threading.Event().wait(0.3)
    finally:
        tailer.stop()
    assert calls == []
