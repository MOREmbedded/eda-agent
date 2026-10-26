# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A request that timed out does not run later.

A timeout left request_<id>.json in the queue. When the dialog blocking
Altium was dismissed, the loop ran it with nobody waiting: 26 stranded
reads in one reported session (issue #94), and a stranded write would
apply an edit its caller had been told failed. The caller now takes back
its own request when Altium never read it, and says which happened.
"""
from __future__ import annotations

import pytest

from eda_agent.bridge import AltiumBridge
from eda_agent.bridge.altium_bridge import CommandRequest
from eda_agent.bridge.exceptions import AltiumTimeoutError


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    from eda_agent import config as config_module
    monkeypatch.setattr(
        config_module, "config",
        config_module.config.model_copy(update={"workspace_dir": tmp_path}))
    b = AltiumBridge()
    yield b
    b.detach()


def _timeout(bridge, monkeypatch, taken: bool):
    """Run one command whose poll times out; Altium read it or not."""
    def poll(request_id, timeout, max_extensions=None):
        if taken:
            bridge._request_path(request_id).unlink()
        raise AltiumTimeoutError("No response within 1s", details={})
    monkeypatch.setattr(bridge, "_poll_response", poll)
    with pytest.raises(AltiumTimeoutError) as info:
        bridge._execute_command("pcb.move_component", {"x": "1"}, 1.0)
    return info.value


def test_an_unread_request_is_taken_back(bridge, monkeypatch, tmp_path):
    exc = _timeout(bridge, monkeypatch, taken=False)
    assert not list(tmp_path.glob("request_*.json")), (
        "the timed-out request is still queued and will run later")
    assert exc.details["request_withdrawn"] is True
    assert "will not run later" in str(exc)


def test_a_request_altium_read_is_reported_as_possibly_landing(
        bridge, monkeypatch):
    exc = _timeout(bridge, monkeypatch, taken=True)
    assert exc.details["request_withdrawn"] is False
    assert "may still finish" in str(exc)


def test_only_the_callers_own_request_is_touched(bridge, monkeypatch, tmp_path):
    """Another session's queued request is not ours to cancel."""
    other = CommandRequest(command="application.ping")
    bridge._publish_request(other)
    _timeout(bridge, monkeypatch, taken=False)
    assert bridge._request_path(other.id).exists()
