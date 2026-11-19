# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Less traffic for Altium's one polling loop, and a faster CLI.

MEASURED 2026-11-19 on a working PC: pings were 70% of the 17,382
commands in a month's bridge trace, 752 of them queued behind real work
and 809 timed out there; one session polled a background job 102,868
times in 12.4 minutes; and every ``eda-agent`` command spent about two
seconds registering tools it never used.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from eda_agent import entry
from eda_agent.bridge import AltiumBridge


# --- keep-alive ------------------------------------------------------------

@pytest.fixture
def bridge(tmp_path, monkeypatch):
    from eda_agent import config as config_module
    monkeypatch.setattr(
        config_module, "config",
        config_module.config.model_copy(update={"workspace_dir": tmp_path}))
    b = AltiumBridge()
    b.KEEPALIVE_INTERVAL = 1
    b._attached = True
    monkeypatch.setattr(b, "_maybe_open_dashboard_sentinel", lambda: None)
    return b


def _run(bridge, monkeypatch, ticks, real_answer_ticks=(), version_age=0.0):
    """Run the keep-alive for ``ticks``; a real command is answered on
    each tick in ``real_answer_ticks``. Returns the ticks that pinged."""
    pings, now = [], [0]
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def wait(_seconds):
        now[0] += 1
        clock["t"] += 1.0
        if now[0] in real_answer_ticks:
            bridge._last_answer_at = clock["t"] - 0.5
        return now[0] > ticks

    bridge._keepalive_stop = type("S", (), {"wait": staticmethod(wait)})()
    bridge._loop_version_at = clock["t"] - version_age

    def send(command, timeout=None):
        pings.append(now[0])
        bridge._last_answer_at = clock["t"]
        bridge._loop_version_at = clock["t"]
        return {"pong": True}

    monkeypatch.setattr(bridge, "send_command", send)
    bridge._keepalive_loop()
    return pings


def test_an_idle_session_pings_every_interval(bridge, monkeypatch):
    """Its own ping's answer must not make it skip the next one."""
    assert _run(bridge, monkeypatch, 6) == [1, 2, 3, 4, 5, 6]


def test_a_busy_session_does_not_ping(bridge, monkeypatch):
    pings = _run(bridge, monkeypatch, 6, real_answer_ticks=range(1, 7))
    assert pings == []


def test_a_ping_still_goes_out_to_read_the_version(bridge, monkeypatch):
    """A redeploy changes the running script version, which only a ping
    reports, so a long run of real calls still pings now and then."""
    bridge.KEEPALIVE_MAX_SKIP = 3
    pings = _run(bridge, monkeypatch, 8, real_answer_ticks=range(1, 9),
                 version_age=0.0)
    assert pings and len(pings) < 8


def test_known_version_needs_a_recent_answer(bridge, monkeypatch):
    t = {"now": 5000.0}
    monkeypatch.setattr(time, "monotonic", lambda: t["now"])
    bridge._loop_script_version = "2026.11.19.1"
    bridge._loop_version_at = t["now"] - 10
    bridge._last_answer_at = t["now"] - 5
    assert bridge.known_loop_version(max_age=60) == "2026.11.19.1"
    bridge._last_answer_at = t["now"] - 120   # Altium went quiet
    assert bridge.known_loop_version(max_age=60) == ""
    bridge._last_answer_at = t["now"] - 5
    bridge._loop_version_at = t["now"] - 600  # read too long ago
    assert bridge.known_loop_version(max_age=60) == ""


# --- design_job_status -----------------------------------------------------

def _status_tool():
    from eda_agent.tools import design as design_tools
    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def reg(fn):
                tools[fn.__name__] = fn
                return fn
            return reg

        def __getattr__(self, n):
            return lambda *a, **k: (lambda fn: fn)

    design_tools.register_design_tools(_Mcp())
    return tools["design_job_status"]


def test_a_tight_no_wait_poll_is_held(monkeypatch):
    from eda_agent.design import jobs as jobs_mod
    store = jobs_mod.JobStore()
    monkeypatch.setattr(jobs_mod, "get_job_store", lambda: store)
    gate = threading.Event()

    def slow(params):
        gate.wait(10)
        return {}

    job = store.submit("route", slow, {})
    status = _status_tool()
    try:
        t0 = time.monotonic()
        asyncio.run(status(job))
        first = time.monotonic() - t0
        t0 = time.monotonic()
        asyncio.run(status(job))
        second = time.monotonic() - t0
    finally:
        gate.set()
    assert first < 1, "the first call must still answer at once"
    assert second >= 1.5, "a repeat within a second must be held"


# --- the eda-agent command -------------------------------------------------

@pytest.mark.parametrize("argv, light", [
    (["update", "--fetch"], True),
    (["std-list"], True),
    (["--help"], True),
    ([], False),                       # the MCP server
    (["serve"], False),
    (["--no-dashboard"], False),       # still the server
    (["review"], False),
    (["--backend", "kicad", "update"], False),  # unsure: full path
    (["dashboard"], False),
])
def test_only_light_commands_skip_the_tools(argv, light):
    assert entry.defers_tools(argv) is light


def test_ensure_tools_registers_once():
    from eda_agent import server
    before = len(server.mcp._tool_manager._tools)
    assert server.ensure_tools() == server.ACTIVE_BACKEND
    assert len(server.mcp._tool_manager._tools) == before > 100


# --- smaller replies -------------------------------------------------------

_PARTS = {"count": 3, "components": [
    {"designator": "U1", "x": 1, "y": 2, "footprint": "QFN"},
    {"designator": "C1", "x": 3, "y": 4, "footprint": "0402"},
    {"designator": "C12", "x": 5, "y": 6, "footprint": "0402"},
]}


def test_parts_are_cut_to_the_designators_and_fields_asked():
    from eda_agent.tools.pcb import _select_parts
    out = _select_parts(_PARTS, "c1*", "x")
    assert out["components"] == [{"designator": "C1", "x": 3},
                                 {"designator": "C12", "x": 5}]
    assert (out["count"], out["total"]) == (2, 3)
    assert _select_parts(_PARTS, "", "") is _PARTS
    assert _PARTS["count"] == 3, "the reply it was given is not changed"


def test_documents_are_cut_to_the_project_kind_and_loaded():
    from eda_agent.tools.application import _filter_documents
    docs = [
        {"file_path": r"D:\ws\A\a.PcbDoc", "document_kind": "PCB", "loaded": True},
        {"file_path": r"D:\ws\A\a.SchDoc", "document_kind": "SCH", "loaded": False},
        {"file_path": r"D:\ws\B\b.PcbDoc", "document_kind": "PCB", "loaded": True},
    ]
    assert len(_filter_documents(docs, "d:/ws/a", "", False)) == 2
    assert [d["file_path"] for d in _filter_documents(docs, "", "pcb", True)] \
        == [r"D:\ws\A\a.PcbDoc", r"D:\ws\B\b.PcbDoc"]
    assert len(_filter_documents(docs, "A", "sch", True)) == 0
    assert _filter_documents(docs, "", "", False) == docs
