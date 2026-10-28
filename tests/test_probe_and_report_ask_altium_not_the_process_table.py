# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The live probes must not repeat the doctor's process-table blind spot.

``eda-agent doctor`` used to call a running Altium absent because the
process table could not name it, and the fix there was to ask the
application and report the scan against the answer. Two tools in this
module still had the original shape:

* ``app_capability_probe(probe_live=True)`` set ``bridge_response`` to
  None whenever the scan said no, so every live feature read as missing
  while the bridge was answering;
* ``app_get_report`` gated the ping, the script version, the version
  match and the round-trip timing on the same scan, so the one tool
  whose job is to say what is wrong reported Altium down, with no
  version and no round trip, on a machine where it was running.

Both now ping first and keep the scan as a reported observation. The
gate stays on the first attempt, because where Altium really is absent
it turns an IPC timeout into an immediate answer.
"""

from __future__ import annotations

import time

import pytest


PING = {"pong": True, "script_version": "2026.10.27.7"}


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class ScriptedBridge:
    """Answers each ping from a script of ``(delay_s, response)``.

    ``is_altium_running`` is the process table. A gated ping on a table
    that says no is refused by the real bridge before any IPC, which is
    what makes the second, ungated attempt necessary.
    """

    def __init__(self, scan: bool, script):
        self.scan = scan
        self.script = list(script)
        self.calls: list[bool] = []

    def is_altium_running(self):
        return self.scan

    def ping_with_version(self, require_process: bool = True):
        self.calls.append(require_process)
        if require_process and not self.scan:
            return None
        delay, result = self.script.pop(0)
        if delay:
            time.sleep(delay)
        return dict(result) if result else None


def _probe(monkeypatch, bridge):
    from eda_agent.tools import application
    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    application.register_meta_tools(mcp)
    return mcp.tools["app_capability_probe"]


def _report(monkeypatch, bridge):
    from eda_agent.tools import application
    monkeypatch.setattr(application, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    application.register_application_tools(mcp)
    return mcp.tools["app_get_report"]


# --------------------------------------------------------------------
# app_capability_probe
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_reaches_an_altium_the_scan_cannot_see(monkeypatch):
    """The reported case: blind table, answering bridge."""
    bridge = ScriptedBridge(scan=False, script=[(0.0, PING)])

    result = await _probe(monkeypatch, bridge)(probe_live=True)

    assert result["bridge_response"] == PING
    assert result["altium_running"] is True
    assert result["process_scan_saw_altium"] is False
    # Gated first (refused without IPC), then ungated.
    assert bridge.calls == [True, False]


@pytest.mark.asyncio
async def test_probe_does_not_retry_when_the_scan_can_see_altium(monkeypatch):
    bridge = ScriptedBridge(scan=True, script=[(0.0, PING)])

    result = await _probe(monkeypatch, bridge)(probe_live=True)

    assert result["bridge_response"] == PING
    assert result["altium_running"] is True
    assert result["process_scan_saw_altium"] is True
    assert bridge.calls == [True]


@pytest.mark.asyncio
async def test_probe_still_reports_a_genuinely_absent_altium(monkeypatch):
    bridge = ScriptedBridge(scan=False, script=[(0.0, None)])

    result = await _probe(monkeypatch, bridge)(probe_live=True)

    assert result["bridge_response"] is None
    assert result["altium_running"] is False
    assert result["process_scan_saw_altium"] is False
    assert bridge.calls == [True, False]


@pytest.mark.asyncio
async def test_probe_stays_offline_unless_asked(monkeypatch):
    """The default must not ping at all; it is the offline inventory."""
    from eda_agent.tools import application

    monkeypatch.setattr(
        application, "get_bridge",
        lambda: (_ for _ in ()).throw(AssertionError("must stay offline")))
    mcp = _Mcp()
    application.register_meta_tools(mcp)

    result = await mcp.tools["app_capability_probe"]()

    assert result["live_probed"] is False
    assert "bridge_response" not in result


# --------------------------------------------------------------------
# app_get_report
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_measures_an_altium_the_scan_cannot_see(monkeypatch):
    """Version, match and round trip all used to be lost to the scan."""
    bridge = ScriptedBridge(scan=False, script=[(0.0, PING)])

    result = await _report(monkeypatch, bridge)()

    assert result["altium_running"] is True
    assert result["process_scan_saw_altium"] is False
    assert result["altium_script_version"] == "2026.10.27.7"
    assert result["round_trip_ms"] is not None
    assert bridge.calls == [True, False]


@pytest.mark.asyncio
async def test_report_still_says_down_when_nothing_answers(monkeypatch):
    bridge = ScriptedBridge(scan=False, script=[(0.0, None)])

    result = await _report(monkeypatch, bridge)()

    assert result["altium_running"] is False
    assert result["process_scan_saw_altium"] is False
    assert result["altium_script_version"] is None
    assert result["round_trip_ms"] is None
    assert result["version_match"] is False


@pytest.mark.asyncio
async def test_report_times_the_round_trip_that_answered(monkeypatch):
    """A slow failed attempt must not be billed to the one that worked."""
    bridge = ScriptedBridge(
        scan=True, script=[(0.25, None), (0.0, PING)])

    result = await _report(monkeypatch, bridge)()

    assert result["altium_script_version"] == "2026.10.27.7"
    assert result["round_trip_ms"] < 100, (
        f"round_trip_ms {result['round_trip_ms']} includes the 250ms "
        f"attempt that failed, so the timer did not restart")
    assert bridge.calls == [True, False]


@pytest.mark.asyncio
async def test_report_keeps_the_two_signals_apart(monkeypatch):
    """A caller must still be able to see what the scan alone said."""
    seen = ScriptedBridge(scan=True, script=[(0.0, PING)])
    blind = ScriptedBridge(scan=False, script=[(0.0, PING)])

    a = await _report(monkeypatch, seen)()
    b = await _report(monkeypatch, blind)()

    assert a["altium_running"] == b["altium_running"] is True
    assert a["process_scan_saw_altium"] is True
    assert b["process_scan_saw_altium"] is False


# --------------------------------------------------------------------
# What the process gate used to hide.
# --------------------------------------------------------------------


class OddReplyBridge:
    """A bridge whose ping answers in a shape the tool cannot read."""

    def __init__(self, reply):
        self.reply = reply

    def is_altium_running(self):
        return False

    def ping_with_version(self, require_process: bool = True):
        return self.reply


@pytest.mark.parametrize("reply", [[], ["pong"], "pong", 7])
@pytest.mark.asyncio
async def test_report_does_not_raise_on_a_reply_it_cannot_read(
    monkeypatch, reply
):
    """The reply shape is Pascal's to decide, so never assume a dict.

    Before the scan stopped gating this code, the version read sat
    behind ``if running:`` and a bridge that answered oddly never got
    that far. Raising here would hand an MCP client a stack trace, which
    is the one failure it cannot act on.
    """
    result = await _report(monkeypatch, OddReplyBridge(reply))()

    assert result["altium_running"] is True
    assert result["altium_script_version"] is None
    assert result["version_match"] is False
    # Something answered, so the round trip is a real measurement.
    assert result["round_trip_ms"] is not None
