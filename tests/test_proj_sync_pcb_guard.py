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
from types import SimpleNamespace

import pytest

import eda_agent.tools.project as project_tools
from eda_agent.bridge.exceptions import AltiumTimeoutError


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
# Bounded wait
# ---------------------------------------------------------------------------


def test_bounds_the_wait_instead_of_inheriting_the_300s_ceiling(
        tools, monkeypatch):
    bridge = _FakeBridge({"project.update_pcb": _eco_ok})
    _install(monkeypatch, bridge)

    _run(tools["proj_sync_pcb"](wait_seconds=60, verify_pad_nets=False))

    # The sync reads the differential pairs before firing the ECO, so
    # the ECO is no longer call 0; find it by name.
    _, _, kwargs = next(c for c in bridge.calls
                        if c[0] == "project.update_pcb")
    assert kwargs["timeout"] == 10.0
    # 60 s budget over a 10 s poll window = 6 extensions, not the default 30.
    assert kwargs["max_extensions"] == 6


def test_timeout_returns_actionable_modal_error_not_an_exception(
        tools, monkeypatch):
    bridge = _FakeBridge({
        "project.update_pcb": AltiumTimeoutError("no response"),
    })
    _install(monkeypatch, bridge)

    # Nothing open at pre-flight; the ECO's own modal appears afterwards.
    seen: list[int] = []

    def _dialogs():
        seen.append(1)
        if len(seen) == 1:
            return []
        return [{"handle": 7, "title": "Engineering Change Order"}]

    monkeypatch.setattr(project_tools, "_list_open_dialogs", _dialogs)

    res = _run(tools["proj_sync_pcb"](wait_seconds=30))

    assert res["success"] is False
    assert res["error"]["code"] == "ECO_DIALOG_BLOCKING"
    # The ECO was fired and is still pending -- the caller must not assume
    # it was cancelled.
    assert res["eco_fired"] is True
    assert res["eco_state"] == "pending_on_dialog"
    assert res["dialogs"][0]["title"] == "Engineering Change Order"
    steps = " ".join(res["next_steps"]).lower()
    assert "app_list_dialogs" in steps
    assert "app_click_dialog_button" in steps
    assert res["recovery"]["fault"] == "modal_dialog"


def test_timeout_still_reports_when_the_dialog_inventory_is_empty(
        tools, monkeypatch):
    # Win32 may return nothing (inventory failed, or the modal is not a
    # top-level window). The timeout must still be reported as a blocking
    # dialog rather than degrading into a generic failure.
    bridge = _FakeBridge({
        "project.update_pcb": AltiumTimeoutError("no response"),
    })
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"](wait_seconds=30))

    assert res["error"]["code"] == "ECO_DIALOG_BLOCKING"
    assert res["eco_fired"] is True
    assert res["dialogs"] == []


def test_dialog_inventory_failure_never_raises(monkeypatch):
    # _list_open_dialogs is the one call made while Altium is wedged; it
    # must not turn a diagnosable timeout into a traceback.
    import eda_agent.tools.application as app_tools

    def _boom():
        raise RuntimeError("win32 unavailable")

    monkeypatch.setattr(app_tools, "_get_ui_inspector", _boom)
    assert project_tools._list_open_dialogs() == []


# ---------------------------------------------------------------------------
# Post-ECO completeness verification
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


def test_verification_reports_pads_the_eco_left_unbound(tools, monkeypatch):
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
    bridge = _FakeBridge({
        "project.update_pcb": _eco_ok,
        "pcb.get_pad_properties": lambda p: _pads_page(
            pads, int(p.get("offset", 0)), int(p.get("limit", 500))),
        "project.get_connectivity_batch": netlist,
    })
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"]())

    check = res["pad_net_verification"]
    assert check["checked"] is True
    assert check["complete"] is False
    assert check["unbound_count"] == 2
    assert check["unbound_by_component"] == {"U15": 2}


def test_verification_pages_past_the_first_pad_window(tools, monkeypatch):
    # 1200 pads over a 500-pad window: reading only page one is exactly the
    # mistake this verification exists to catch.
    pads = [{"component": f"U{i // 4}", "name": str(i % 4), "net": "GND"}
            for i in range(1200)]
    netlist = {"components": []}
    bridge = _FakeBridge({
        "project.update_pcb": _eco_ok,
        "pcb.get_pad_properties": lambda p: _pads_page(
            pads, int(p.get("offset", 0)), int(p.get("limit", 500))),
        "project.get_connectivity_batch": netlist,
    })
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"]())

    pad_calls = [c for c in bridge.calls
                 if c[0] == "pcb.get_pad_properties"]
    assert len(pad_calls) == 3  # 500 + 500 + 200
    assert res["pad_net_verification"]["pads_checked"] == 1200


def test_verification_can_be_switched_off(tools, monkeypatch):
    bridge = _FakeBridge({"project.update_pcb": _eco_ok})
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"](verify_pad_nets=False))

    assert "pad_net_verification" not in res
    # The pairs are read before the ECO and the board after it, so a
    # sync that "ran" and never reached the board is caught.
    assert "project.update_pcb" in [c[0] for c in bridge.calls]


def test_verification_failure_is_reported_not_silently_passed(
        tools, monkeypatch):
    # A check that could not run must never look like a check that passed.
    bridge = _FakeBridge({
        "project.update_pcb": _eco_ok,
        "pcb.get_pad_properties": RuntimeError("board closed"),
    })
    _install(monkeypatch, bridge)

    res = _run(tools["proj_sync_pcb"]())

    check = res["pad_net_verification"]
    assert check["checked"] is False
    assert "complete" not in check
    assert "board closed" in check["reason"]
