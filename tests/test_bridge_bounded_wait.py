# SPDX-License-Identifier: Apache-2.0
"""A caller must be able to bound its own wait on a modal-prone command.

The progress heartbeat is written ONCE per request by the Pascal dispatcher
and deleted after the response -- it is a presence marker, not a tick. So a
handler parked on a modal Altium dialog looks exactly like one making slow
progress, and the poll loop extended the deadline the full 30 times (300 s)
before failing, then blamed a "stuck handler" and told the user to restart
the script. The script was fine; a dialog was open.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from eda_agent.bridge import AltiumBridge
from eda_agent.bridge.exceptions import AltiumTimeoutError
from eda_agent.bridge.altium_bridge import (
    PROTOCOL_VERSION,
    _MAX_HEARTBEAT_EXTENSIONS,
)


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def bridge(temp_workspace):
    from eda_agent.config import configure
    configure(workspace_dir=temp_workspace)
    b = AltiumBridge()
    b.ensure_workspace()
    return b


def _stale_heartbeat(bridge, request_id):
    """Write the progress marker and never touch it again -- exactly what a
    handler blocked on a modal leaves behind."""
    path = bridge._progress_path(request_id)
    path.write_text(json.dumps({"id": request_id}), encoding="utf-8")
    return path


def test_bounded_wait_gives_up_quickly_instead_of_burning_the_default(bridge):
    _stale_heartbeat(bridge, "modalid")
    with pytest.raises(AltiumTimeoutError) as excinfo:
        # 3 windows of 0.05 s, not 30 windows of the configured timeout.
        bridge._poll_response("modalid", timeout=0.05, max_extensions=3)
    details = excinfo.value.details
    assert details["bounded_wait"] is True
    assert details["fault"] == "modal_dialog"
    assert details["waited_seconds"] == pytest.approx(0.15)


def test_bounded_timeout_points_at_the_dialog_tools_not_a_restart(bridge):
    _stale_heartbeat(bridge, "modalid")
    with pytest.raises(AltiumTimeoutError) as excinfo:
        bridge._poll_response("modalid", timeout=0.05, max_extensions=2)
    message = str(excinfo.value)
    assert "modal dialog" in message
    assert "app_list_dialogs" in message
    assert "app_click_dialog_button" in message
    # The old advice was to stop and relaunch the script; that is the wrong
    # fix for a dialog and it cost a restart of a healthy loop.
    assert "Stop button" not in message


def test_unbounded_wait_keeps_the_stuck_handler_diagnosis(bridge):
    # Without an explicit bound, exhausting the default budget really does
    # mean a runaway handler, and the existing advice still applies.
    _stale_heartbeat(bridge, "stuckid")
    with pytest.raises(AltiumTimeoutError) as excinfo:
        bridge._poll_response("stuckid", timeout=0.01)
    details = excinfo.value.details
    assert details["bounded_wait"] is False
    assert details["fault"] == "stuck_handler"
    assert str(_MAX_HEARTBEAT_EXTENSIONS) in str(excinfo.value)


def test_a_bounded_wait_still_returns_a_response_that_arrives_in_time(
        bridge, temp_workspace):
    request_id = "fastid"
    (temp_workspace / f"response_{request_id}.json").write_text(
        json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "success": True,
            "data": {"ok": 1},
            "error": None,
        }),
        encoding="utf-8",
    )
    response = bridge._poll_response(request_id, timeout=2, max_extensions=1)
    assert response.success is True
    assert response.data == {"ok": 1}


def test_no_heartbeat_is_still_diagnosed_as_a_dead_loop(bridge, monkeypatch):
    # No progress file at all: the loop is not running. A bounded wait must
    # not relabel that as a dialog.
    #
    # The dialog probe is stubbed because it reads the REAL desktop's
    # Win32 windows. Finding anything there makes the message name a
    # dialog instead of a dead loop, so without this the test passes or
    # fails on what the developer happens to have open. It cost a green
    # full run and a red single run on 2026-10-26, after the merge put
    # that probe in this path.
    monkeypatch.setattr(bridge, "_dialog_probe", lambda: None)
    with pytest.raises(AltiumTimeoutError) as excinfo:
        bridge._poll_response("noloop", timeout=0.05, max_extensions=3)
    assert "polling loop is probably not running" in str(excinfo.value)


@pytest.mark.asyncio
async def test_async_polling_keeps_the_same_bounded_timeout_contract(bridge):
    """The MCP tool path is async, so sync-only recovery is insufficient."""
    _stale_heartbeat(bridge, "asyncmodal")
    with pytest.raises(AltiumTimeoutError) as excinfo:
        await bridge._poll_response_async(
            "asyncmodal", timeout=0.01, max_extensions=2)
    details = excinfo.value.details
    assert details["bounded_wait"] is True
    assert details["fault"] == "modal_dialog"
    assert details["waited_seconds"] == pytest.approx(0.02)
