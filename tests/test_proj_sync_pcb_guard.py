# SPDX-License-Identifier: Apache-2.0
"""proj_sync_pcb must fail safely on a modal dialog, not wedge the bridge.

Live failure this covers: the ECO handler blocked on Altium's
non-suppressible change-review dialog, burned all 30 heartbeat extensions
(300 s), was misdiagnosed as a stuck handler, and the DelphiScript loop
had to be restarted. And on the run that DID succeed, the ECO silently
left one component's pads unbound.

The bridge is faked here -- no Altium, no dialogs, no file IPC.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import eda_agent.tools.answering as answering
import eda_agent.tools.project as project_tools


class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class _FakeBridge:
    """Records calls; replays scripted responses or raises."""

    def __init__(self, responses=None):
        self.config = SimpleNamespace(poll_timeout=10.0)
        self.responses = responses or {}
        self.calls: list[tuple[str, dict, dict]] = []

    @contextlib.contextmanager
    def expecting(self, _title):
        """send_answering tells the bridge which dialog it is driving so a
        reply that arrives mid-press is not read as a stall."""
        yield

    async def send_command_async(self, command, params=None, timeout=None,
                                 max_extensions=None):
        self.calls.append((command, dict(params or {}),
                           {"timeout": timeout,
                            "max_extensions": max_extensions}))
        handler = self.responses.get(command)
        if handler is None:
            raise AssertionError(f"unexpected command: {command}")
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(params or {})
        return handler


@pytest.fixture()
def tools():
    fake = _CapturingMCP()
    project_tools.register_project_tools(fake)
    return fake.tools


@pytest.fixture(autouse=True)
def _no_real_dialogs(monkeypatch):
    """Default: no dialogs open. Individual tests override."""
    monkeypatch.setattr(project_tools, "_list_open_dialogs", lambda: [])


@pytest.fixture(autouse=True)
def _dialogs_left_to_the_user(monkeypatch):
    """Answer no dialogs here.

    ``driver`` returns a presser only when Dialogs Automatic is on AND
    Altium's process can be named, so without this the test takes one
    path on a developer machine with Altium open and the other in CI.
    The pressing path is exercised where it belongs, against the
    answering module.
    """
    monkeypatch.setattr(answering, "driver", lambda *a, **k: None)


def _install(monkeypatch, bridge):
    monkeypatch.setattr(project_tools, "get_bridge", lambda: bridge)


def _run(coro):
    return asyncio.run(coro)


def _eco_ok(_params=None):
    """Fresh dict per call -- proj_sync_pcb annotates the response in
    place, and a shared literal would leak between tests."""
    return {"success": True, "pcb_path": "C:\\b.PcbDoc",
            "in_sync": True, "dialog_may_have_opened": False}


# ---------------------------------------------------------------------------
# Pre-flight: refuse to queue behind an already-open dialog
# ---------------------------------------------------------------------------


def test_refuses_when_a_dialog_is_already_open(tools, monkeypatch):
    bridge = _FakeBridge({"project.update_pcb": _eco_ok})
    _install(monkeypatch, bridge)
    monkeypatch.setattr(project_tools, "_list_open_dialogs",
                        lambda: [{"handle": 1, "title": "Confirm"}])

    res = _run(tools["proj_sync_pcb"]())

    assert res["success"] is False
    assert res["error"]["code"] == "MODAL_DIALOG_ALREADY_OPEN"
    assert res["eco_fired"] is False
    assert res["eco_state"] == "not_started"
    assert res["dialog_count"] == 1
    # The ECO must NOT have been fired -- stacking a second modal behind
    # the first is what guaranteed the 300 s starvation.
    assert bridge.calls == []


# ---------------------------------------------------------------------------
# The bounded wait, now carried by send_answering
# ---------------------------------------------------------------------------


def test_the_wait_is_bounded_by_wait_seconds(tools, monkeypatch):
    """Upstream moved the ECO onto send_answering, which takes the budget
    as a timeout. The knob kept working only because it is passed on; an
    unused parameter would still have appeared in the tool's schema."""
    seen = {}

    async def _fake(bridge, command, params, title, steps, timeout=360.0,
                    on_the_way=None):
        seen["timeout"] = timeout
        return _eco_ok(), None

    bridge = _FakeBridge({
        "pcb.get_differential_pairs": {"pairs": []},
        "project.get_bom": {"components": []},
        "pcb.get_components": {"components": []},
    })
    _install(monkeypatch, bridge)
    monkeypatch.setattr(project_tools, "send_answering", _fake)

    _run(tools["proj_sync_pcb"](wait_seconds=45))
    assert seen["timeout"] == 45


def test_dialog_inventory_failure_never_raises(monkeypatch):
    # _list_open_dialogs is the one call made while Altium is wedged; it
    # must not turn a diagnosable timeout into a traceback.
    import eda_agent.tools.application as app_tools

    def _boom():
        raise RuntimeError("win32 unavailable")

    monkeypatch.setattr(app_tools, "_get_ui_inspector", _boom)
    assert project_tools._list_open_dialogs() == []


# ---------------------------------------------------------------------------
# Post-ECO pad-net check, read through after_sync
# ---------------------------------------------------------------------------


def _pads_page(pads, offset, limit):
    window = pads[offset:offset + limit]
    return {
        "pads": window,
        "count": len(window),
        "returned": len(window),
        "total_matching": len(pads),
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(window) < len(pads),
        "next_offset": offset + len(window),
    }


def _sync_bridge(pads, netlist, designators=("U14", "U15")):
    return _FakeBridge({
        "project.update_pcb": _eco_ok,
        "pcb.get_differential_pairs": {"pairs": []},
        "project.get_bom": {"components": [{"designator": d, "footprint": "F"}
                                           for d in designators]},
        "pcb.get_components": {"components": [
            {"designator": d, "footprint": "F"} for d in designators]},
        "pcb.get_pad_properties": lambda p: _pads_page(
            pads, int(p.get("offset", 0)), int(p.get("limit", 500))),
        "project.get_connectivity_batch": netlist,
    })


def test_the_check_reports_pads_the_eco_left_unbound(tools, monkeypatch):
    pads = [
        {"component": "U14", "name": "1", "net": "GND"},
        {"component": "U15", "name": "1", "net": ""},
        {"component": "U15", "name": "2", "net": ""},
    ]
    netlist = {"components": [
        {"designator": "U14", "pins": [{"pin_number": "1", "net": "GND"}]},
        {"designator": "U15", "pins": [{"pin_number": "1", "net": "GND"},
                                       {"pin_number": "2", "net": "3V3"}]},
    ]}
    bridge = _sync_bridge(pads, netlist)
    _install(monkeypatch, bridge)

    nets = _run(tools["proj_sync_pcb"]())["after_sync"]["pad_nets"]

    assert nets["unbound_count"] == 2
    assert nets["unbound_by_component"] == {"U15": 2}
    assert nets["sweep_complete"] is True


def test_the_check_pages_past_the_first_pad_window(tools, monkeypatch):
    """pcb.get_pad_properties windows at 500 pads and says so in
    `truncated`. Reading page one and reporting the unbound list as the
    board's is the bug this whole check exists to catch, and the merged
    code did exactly that until it was wired through the pager."""
    pads = [{"component": "U14", "name": str(i), "net": "GND"}
            for i in range(1200)]
    netlist = {"components": []}
    bridge = _sync_bridge(pads, netlist, designators=("U14",))
    _install(monkeypatch, bridge)

    nets = _run(tools["proj_sync_pcb"]())["after_sync"]["pad_nets"]

    pad_calls = [c for c in bridge.calls if c[0] == "pcb.get_pad_properties"]
    assert len(pad_calls) == 3  # 500 + 500 + 200
    assert nets["pads_swept"] == 1200


def test_the_sweep_counters_do_not_overwrite_the_verifier_s_own(
        tools, monkeypatch):
    """verify_eco already answers `complete` ("nothing was wrong") and
    `pads_checked` (pads it could pair with a pin). The sweep's two
    counters are about something else and must not land on those names."""
    pads = [{"component": "U14", "name": "1", "net": "GND"}]
    netlist = {"components": [
        {"designator": "U14", "pins": [{"pin_number": "1", "net": "GND"}]}]}
    bridge = _sync_bridge(pads, netlist, designators=("U14",))
    _install(monkeypatch, bridge)

    nets = _run(tools["proj_sync_pcb"]())["after_sync"]["pad_nets"]

    assert nets["complete"] is True          # verify_eco's meaning
    assert nets["sweep_complete"] is True    # the pager's
    assert nets["pads_checked"] == 1
    assert nets["pads_swept"] == 1


def test_a_check_that_could_not_run_is_reported_not_passed(tools, monkeypatch):
    # A check that could not run must never look like a check that passed.
    bridge = _sync_bridge([], {"components": []})
    bridge.responses["pcb.get_pad_properties"] = RuntimeError("board closed")
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"]())

    assert "board closed" in res["after_sync"]["error"]
    assert "pad_nets" not in res["after_sync"]
