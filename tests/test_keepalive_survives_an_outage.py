# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The keep-alive outlives a stopped polling loop.

It used to exit after three unanswered pings. A bridge restart that took
a few minutes therefore ended the keep-alive of every attached session,
and the restarted loop, pinged by nobody, shut itself down idle twenty
minutes later (measured 2026-10-22). It now slows down instead and goes
back to the normal interval on the first answer.
"""
from __future__ import annotations

import pytest

from eda_agent.bridge import AltiumBridge


class _Clock:
    """Stands in for the stop event: every wait is one tick, and the loop
    ends after a fixed number of ticks."""

    def __init__(self, ticks):
        self.left = ticks

    def wait(self, _seconds):
        self.left -= 1
        return self.left < 0


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    from eda_agent import config as config_module
    monkeypatch.setattr(
        config_module, "config",
        config_module.config.model_copy(update={"workspace_dir": tmp_path}))
    b = AltiumBridge()
    b.KEEPALIVE_INTERVAL = 1
    b.KEEPALIVE_SLOW_INTERVAL = 4
    b._attached = True
    monkeypatch.setattr(b, "_maybe_open_dashboard_sentinel", lambda: None)
    return b


def _run(bridge, monkeypatch, ticks, answers):
    """Run the loop for ticks; answers[i] says whether ping i is answered.
    Returns the tick of every ping."""
    pings, now = [], [0]
    clock = _Clock(ticks)

    def wait(seconds):
        now[0] += 1
        return clock.wait(seconds)

    bridge._keepalive_stop = type("S", (), {"wait": staticmethod(wait)})()

    def send(command, timeout=None):
        i = len(pings)
        pings.append(now[0])
        if not (answers[i] if i < len(answers) else True):
            raise TimeoutError("no answer")
        return {"pong": True}

    monkeypatch.setattr(bridge, "send_command", send)
    bridge._keepalive_loop()
    return pings


def test_pings_continue_through_an_outage_and_recover(bridge, monkeypatch):
    answers = [True, False, False, False, False, False, True, True]
    pings = _run(bridge, monkeypatch, 30, answers)
    assert len(pings) >= len(answers), (
        "the keep-alive stopped during the outage, so a restarted loop "
        "would be left with nobody pinging it")
    gaps = [b - a for a, b in zip(pings, pings[1:])]
    # Normal while answered, slower once three in a row went unanswered,
    # normal again after the first answer.
    assert gaps[:3] == [1, 1, 1]
    assert set(gaps[3:6]) == {4}
    assert gaps[6] == 1


def test_detaching_still_ends_it(bridge, monkeypatch):
    bridge._attached = False
    assert _run(bridge, monkeypatch, 30, []) == []
