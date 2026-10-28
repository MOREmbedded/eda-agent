# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A process scan that cannot see Altium must not outrank Altium itself.

Reported 2026-10-27. ``eda-agent doctor`` reported a critical failure:

    [FAIL] altium process running, no process named x2.exe found
             fix: Start Altium Designer.

Altium Designer Professional 26.11.1.5 was on screen at the time, with a
visible top-level window, and answered a 26-property SDK probe over the
bridge minutes later. Nothing on that machine could name the process:
``tasklist``, ``Get-Process x2`` and a scan over ``MainWindowTitle`` all
came back empty, so the executable was not merely renamed. Reading a pid
means opening the process, which fails across an integrity level; the
Toolhelp snapshot the bridge uses reads names out of the kernel instead,
which is why the bridge worked while the doctor's own psutil loop did
not.

Two faults, both here:

* the doctor ran a private psutil loop over one hardcoded name, so it
  could disagree with the scan gating every command;
* a FAIL on that check returned from ``run_doctor_checks`` before the
  bridge was ever pinged, so the weakest signal silenced every better
  one and the run exited non-zero.

So the scan is reported, never obeyed: a bridge round trip, or a fresh
activity.log entry, outranks it, and the check says plainly that the
process cannot be seen rather than telling the user to start an
application that is already open.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from eda_agent.diag import doctor
from eda_agent.diag.checks import Severity, Status, overall_exit_code
from eda_agent.diag.perf import last_activity_at


PING = {"pong": True, "script_version": "1.4.2"}


@pytest.fixture
def quiet_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health checks talk to this machine's workspace; not under test."""
    monkeypatch.setattr(doctor, "run_health_checks", lambda: [])


def _process_check(checks) -> object:
    found = [c for c in checks if c.name == doctor._PROCESS_CHECK]
    assert len(found) == 1, f"expected one process check, got {len(found)}"
    return found[0]


# --------------------------------------------------------------------
# The reported case.
# --------------------------------------------------------------------


def test_process_not_found_but_bridge_responds_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, quiet_health: None
) -> None:
    monkeypatch.setattr(
        doctor, "_altium_process_scan", lambda: (False, [], ["X2.exe", "DXP.exe"])
    )
    monkeypatch.setattr(doctor, "_ping_altium", lambda: PING)
    monkeypatch.setattr(
        doctor, "_check_command_canary",
        lambda: doctor.Check(name="canary", status=Status.PASS))

    checks = doctor.run_doctor_checks()
    check = _process_check(checks)

    assert check.status == Status.WARN
    assert check.severity == Severity.MINOR
    # The run may still exit non-zero on a real fault found further
    # down; what it must never do again is exit non-zero on this check.
    criticals = [c.name for c in checks
                 if c.status == Status.FAIL and c.severity == Severity.CRITICAL]
    assert doctor._PROCESS_CHECK not in criticals


def test_the_false_critical_does_not_tell_the_user_to_start_altium(
    monkeypatch: pytest.MonkeyPatch, quiet_health: None
) -> None:
    monkeypatch.setattr(
        doctor, "_altium_process_scan", lambda: (False, [], ["X2.exe"]))
    monkeypatch.setattr(doctor, "_ping_altium", lambda: PING)
    monkeypatch.setattr(
        doctor, "_check_command_canary",
        lambda: doctor.Check(name="canary", status=Status.PASS))

    check = _process_check(doctor.run_doctor_checks())

    assert check.fix is None
    assert "Start Altium" not in (check.fix or "")
    # Says what it saw and what it could not see, in that order.
    assert "no process named X2.exe" in check.message
    assert "the bridge answered a ping" in check.message
    assert "1.4.2" in check.message


def test_a_responding_bridge_still_gets_its_own_checks_run(
    monkeypatch: pytest.MonkeyPatch, quiet_health: None
) -> None:
    """The early return was the real damage: it hid every later check."""
    monkeypatch.setattr(
        doctor, "_altium_process_scan", lambda: (False, [], ["X2.exe"]))
    monkeypatch.setattr(doctor, "_ping_altium", lambda: PING)
    monkeypatch.setattr(
        doctor, "_check_command_canary",
        lambda: doctor.Check(name="canary", status=Status.PASS))

    names = [c.name for c in doctor.run_doctor_checks()]

    assert "DelphiScript polling loop responsive" in names
    assert "script version match" in names
    assert "canary" in names


def test_recent_activity_log_alone_also_outranks_the_scan(
    monkeypatch: pytest.MonkeyPatch, quiet_health: None
) -> None:
    """No ping, but Altium wrote a handler line seconds ago."""
    monkeypatch.setattr(
        doctor, "_altium_process_scan", lambda: (False, [], ["X2.exe"]))
    monkeypatch.setattr(doctor, "_ping_altium", lambda: None)
    monkeypatch.setattr(doctor, "_recent_activity", lambda: 12.0)

    checks = doctor.run_doctor_checks()
    check = _process_check(checks)

    assert check.status == Status.WARN
    assert "activity.log" in check.message
    assert "12s ago" in check.message
    # The polling loop really is unresponsive, and still says so.
    assert any(c.name == "DelphiScript polling loop responsive"
               and c.status == Status.FAIL for c in checks)


# --------------------------------------------------------------------
# The case the critical failure was meant for, which must survive.
# --------------------------------------------------------------------


def test_nothing_running_and_nothing_answering_is_still_critical(
    monkeypatch: pytest.MonkeyPatch, quiet_health: None
) -> None:
    monkeypatch.setattr(
        doctor, "_altium_process_scan", lambda: (False, [], ["X2.exe", "DXP.exe"]))
    monkeypatch.setattr(doctor, "_ping_altium", lambda: None)
    monkeypatch.setattr(doctor, "_recent_activity", lambda: None)

    checks = doctor.run_doctor_checks()
    check = _process_check(checks)

    assert check.status == Status.FAIL
    assert check.severity == Severity.CRITICAL
    assert check.fix == "Start Altium Designer."
    assert overall_exit_code(checks) == 1


# --------------------------------------------------------------------
# What the scan can and cannot say on its own.
# --------------------------------------------------------------------


def test_a_name_without_a_readable_pid_is_a_pass_that_says_so() -> None:
    check = doctor._check_altium_process(True, [], ["X2.exe"], "")
    assert check.status == Status.PASS
    assert "pid not readable" in check.message


def test_pids_are_reported_when_the_process_table_can_be_read() -> None:
    check = doctor._check_altium_process(True, [8124], ["X2.exe"], "")
    assert check.status == Status.PASS
    assert "8124" in check.message


def test_an_unanswerable_scan_is_a_skip_not_an_absence() -> None:
    """None means "could not look", which is not "Altium is absent"."""
    check = doctor._check_altium_process(None, [], ["X2.exe"], "")
    assert check.status == Status.SKIP
    assert check.severity == Severity.MINOR


def test_the_scan_asks_for_the_same_names_the_bridge_gate_does() -> None:
    """Drift between the two is what let the doctor contradict itself."""
    from eda_agent.bridge.process_manager import AltiumProcessManager

    _running, _pids, names = doctor._altium_process_scan()
    assert names == list(AltiumProcessManager.PROCESS_NAMES)


def test_a_renamed_executable_can_still_be_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EDA_AGENT_ALTIUM_PROCESS", "AltiumRenamed.exe")
    _running, _pids, names = doctor._altium_process_scan()
    assert names == ["AltiumRenamed.exe"]


# --------------------------------------------------------------------
# The activity.log evidence.
# --------------------------------------------------------------------


def test_last_activity_at_reads_the_newest_stamp() -> None:
    text = (
        "2026-10-27 09:00:00.100,5,_session_start,version=1.4.2,protocol=3\n"
        "2026-10-27 09:00:01.250,12,application.ping,OK,42,{}\n"
        "2026-10-27 09:14:33.900,118,generic.probe_sdk_properties,OK,900,{}\n"
    )
    assert last_activity_at(text) == datetime(2026, 10, 27, 9, 14, 33, 900000)


def test_last_activity_at_steps_over_a_half_written_final_line() -> None:
    text = (
        "2026-10-27 09:14:33.900,118,generic.probe_sdk_properties,OK,900,{}\n"
        "2026-10-27 09:14:3"
    )
    assert last_activity_at(text) == datetime(2026, 10, 27, 9, 14, 33, 900000)


def test_last_activity_at_is_none_without_a_usable_line() -> None:
    assert last_activity_at("") is None
    assert last_activity_at("not a log line at all\n") is None


def test_recent_activity_accepts_a_fresh_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    stamp = datetime.now() - timedelta(seconds=30)
    log = tmp_path / "activity.log"
    log.write_text(
        f"{stamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]},9,application.ping,OK,8,{{}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        doctor, "_tail_text", lambda p, *a, **k: log.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        "eda_agent.config.get_config",
        lambda: type("Cfg", (), {"workspace_dir": tmp_path})())

    age = doctor._recent_activity()
    assert age is not None
    assert 25 <= age <= 40


def test_recent_activity_rejects_a_stale_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Yesterday's session cannot vouch for Altium being up now."""
    stamp = datetime.now() - timedelta(hours=8)
    log = tmp_path / "activity.log"
    log.write_text(
        f"{stamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]},9,application.ping,OK,8,{{}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        doctor, "_tail_text", lambda p, *a, **k: log.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        "eda_agent.config.get_config",
        lambda: type("Cfg", (), {"workspace_dir": tmp_path})())

    assert doctor._recent_activity() is None


# --------------------------------------------------------------------
# The ping the doctor sends.
# --------------------------------------------------------------------


def test_doctor_ping_retries_past_the_bridge_process_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads the same process table the doctor cannot trust.

    Left on, it refuses the ping before any IPC happens, and the run
    reports a healthy Altium as unresponsive on the scan's word.
    """
    calls: list[bool] = []

    class FakeBridge:
        def ping_with_version(self, require_process: bool = True):
            calls.append(require_process)
            return None if require_process else PING

    monkeypatch.setattr("eda_agent.bridge.get_bridge", lambda: FakeBridge())

    assert doctor._ping_altium() == PING
    assert calls == [True, False]


def test_doctor_ping_does_not_retry_when_the_gated_call_worked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    class FakeBridge:
        def ping_with_version(self, require_process: bool = True):
            calls.append(require_process)
            return PING

    monkeypatch.setattr("eda_agent.bridge.get_bridge", lambda: FakeBridge())

    assert doctor._ping_altium() == PING
    assert calls == [True]
