# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Full-preflight checks that talk to a running Altium.

Runs all the health checks first, then a series of canaries that exercise
the IPC bridge, script alive, version match, basic command round-trips,
the Pascal handlers we lean on most.

Library-path canaries are explicit-input only: the doctor accepts an
optional list of SchLib paths from the user, and reports whether each
one is reachable and parseable. It does not crawl directories or assume
any naming scheme.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from eda_agent.diag.checks import Check, Severity, Status
from eda_agent.diag.health import run_health_checks


#: How fresh an activity.log entry has to be to count as evidence that
#: Altium was alive. Long enough to cover a quiet minute between calls,
#: short enough that yesterday's session cannot vouch for today's.
_ACTIVITY_WINDOW_S = 300.0

#: Enough of the tail to hold the last entries without reading a log
#: that housekeeping has not trimmed yet.
_ACTIVITY_TAIL_BYTES = 65536

_PROCESS_CHECK = "altium process running"


def _altium_process_scan() -> tuple[Optional[bool], list[int], list[str]]:
    """``(running, pids, names)`` from the scan the bridge itself uses.

    ``running`` is True, False, or None when nothing on this host could
    answer the question. ``pids`` is filled only when the process table
    was readable from this session, so an empty list beside a True is a
    normal answer rather than a contradiction: the Toolhelp snapshot
    answers by name alone, while the candidate scan behind the pids
    fetches each process's exe and command line, which means opening it
    and can be refused where the name scan is not.

    The doctor used to run its own psutil loop over a single hardcoded
    name. That loop was blind in exactly the case worth reporting, and
    it disagreed with the gate in front of every command, so a run could
    say Altium was absent while the next call reached it.
    """
    try:
        from eda_agent.bridge.process_manager import AltiumProcessManager
    except Exception:
        # psutil missing, or the module cannot load on this host.
        override = os.environ.get("EDA_AGENT_ALTIUM_PROCESS", "").strip()
        return None, [], [override] if override else []

    manager = AltiumProcessManager()
    override = os.environ.get("EDA_AGENT_ALTIUM_PROCESS", "").strip()
    if override:
        # Shadows the class attribute for this instance only; the scan
        # methods all read it off self.
        manager.PROCESS_NAMES = [override]
    names = list(manager.PROCESS_NAMES)

    try:
        running = manager.is_altium_running()
    except Exception:
        return None, [], names

    try:
        pids = [c.pid for c in manager.select_altium_process().candidates]
    except Exception:
        pids = []

    # A pid from the slow path settles it even if the fast one said no.
    return (True if pids else running), pids, names


def _tail_text(path: Path, max_bytes: int = _ACTIVITY_TAIL_BYTES) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def _recent_activity(window_s: float = _ACTIVITY_WINDOW_S) -> Optional[float]:
    """Age in seconds of the newest activity.log entry, when it is
    inside ``window_s``. None when the log is missing, unreadable, or
    stale.

    Pascal writes a line per handler call, so a fresh entry says Altium
    was running that recently. Weaker than a ping, which says it is
    running now, and reported with its age so the difference shows.
    """
    try:
        from eda_agent.config import get_config
        from eda_agent.diag.perf import last_activity_at

        path = Path(get_config().workspace_dir) / "activity.log"
        if not path.exists():
            return None
        stamp = last_activity_at(_tail_text(path))
    except Exception:
        return None
    if stamp is None:
        return None

    age = (datetime.now() - stamp).total_seconds()
    # Altium stamps in its own local clock. A stamp a little in the
    # future is skew between that clock and this one, not a reason to
    # throw away the evidence.
    if abs(age) > window_s:
        return None
    return max(age, 0.0)


def _check_altium_process(running: Optional[bool], pids: list[int],
                          names: list[str], evidence: str) -> Check:
    """Report the process scan against what Altium actually did.

    ``evidence`` is how the application showed itself alive, empty when
    it did not. A process-name scan is the weakest signal here, so it
    never fails the run on its own while a better one says the
    application is answering.
    """
    wanted = "/".join(names) if names else "the Altium executable"

    if running:
        where = f"pid(s): {pids}" if pids else (
            "found by name, pid not readable from this session")
        return Check(name=_PROCESS_CHECK, status=Status.PASS, message=where)

    if running is None:
        return Check(
            name=_PROCESS_CHECK,
            status=Status.SKIP,
            message="no process scan on this host could answer",
            severity=Severity.MINOR,
        )

    if evidence:
        return Check(
            name=_PROCESS_CHECK,
            status=Status.WARN,
            message=(
                f"no process named {wanted} in this session's process "
                f"table, but {evidence}: Altium is running and the scan "
                f"cannot see it, which is usually elevation, a different "
                f"session or integrity level, or restricted process "
                f"enumeration. Not a fault to fix; the checks below are "
                f"the ones that matter"
            ),
            severity=Severity.MINOR,
            details={"evidence": evidence, "process_names": names},
        )

    return Check(
        name=_PROCESS_CHECK,
        status=Status.FAIL,
        message=(f"no process named {wanted} found, and no answer from "
                 f"the bridge or activity.log"),
        fix="Start Altium Designer.",
    )


def bundled_script_version() -> str:
    """SCRIPT_VERSION as this tree declares it, or '' if unreadable."""
    try:
        from eda_agent.cli import get_bundled_scripts_path
        main_pas = get_bundled_scripts_path() / "Main.pas"
        if main_pas.exists():
            for line in main_pas.read_text(encoding="utf-8").splitlines():
                if "SCRIPT_VERSION" in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip(";").strip("'")
    except Exception:
        pass
    return ""


def _ping_altium() -> Optional[dict]:
    """Send a ping. Returns the response dict, or None on any failure.

    Tries the ordinary gated call first, then the same round trip with
    the process gate off. The gate reads the process table, which is
    the thing this run cannot trust: with it left on, an Altium the
    table cannot see is refused here and reported as not responding,
    which is the diagnosis the scan already got wrong.
    """
    try:
        from eda_agent.bridge import get_bridge
        bridge = get_bridge()
    except Exception:
        return None
    try:
        result = bridge.ping_with_version()
    except Exception:
        result = None
    if result is not None:
        return result
    try:
        return bridge.ping_with_version(require_process=False)
    except Exception:
        return None


def _check_script_responsive(ping: Optional[dict]) -> Check:
    if ping is None:
        return Check(
            name="DelphiScript polling loop responsive",
            status=Status.FAIL,
            message="ping_altium did not return",
            fix=(
                "Open Altium_API.PrjScr in Altium and run "
                "Dispatcher.pas > StartMCPServer."
            ),
        )
    return Check(
        name="DelphiScript polling loop responsive",
        status=Status.PASS,
        message=f"version {ping.get('script_version', '?')}",
    )


def _check_script_version_match(ping: Optional[dict]) -> Check:
    if ping is None:
        return Check(
            name="script version match",
            status=Status.SKIP,
            message="cannot check, script not responding",
            severity=Severity.MINOR,
        )

    altium_v = ping.get("script_version", "")
    bundled_v = bundled_script_version()

    if not altium_v or not bundled_v:
        return Check(
            name="script version match",
            status=Status.SKIP,
            message=f"altium={altium_v!r} bundled={bundled_v!r}, incomplete",
            severity=Severity.MINOR,
        )

    if altium_v != bundled_v:
        return Check(
            name="script version match",
            status=Status.FAIL,
            message=f"altium={altium_v} but bundled={bundled_v}",
            fix=(
                "Re-run `eda-agent install-scripts --force`, then close "
                "Altium fully (the script cache survives Altium_API.PrjScr "
                "reload) and reopen the script project."
            ),
        )
    return Check(
        name="script version match",
        status=Status.PASS,
        message=altium_v,
    )


def _check_command_canary() -> Check:
    """Round-trip a real command, not just ping, that changes nothing.

    This was application.save_all, described as a no-op: it writes every
    open document, so running the doctor rewrote files in every open
    project. Listing the open documents exercises the same dispatch.
    """
    try:
        from eda_agent.bridge import get_bridge
        bridge = get_bridge()
        bridge.send_command("application.get_open_documents", {}, timeout=15.0)
    except Exception as exc:
        return Check(
            name="command canary",
            status=Status.FAIL,
            message=f"get_open_documents round-trip failed: {exc}",
            fix=(
                "Script may be hung behind a modal Altium dialog, "
                "check Altium for a popup and click OK, then re-run."
            ),
        )
    return Check(
        name="command canary",
        status=Status.PASS,
    )


def _check_lib_paths(library_paths: Sequence[str]) -> list[Check]:
    """Check each user-supplied SchLib path. No hardcoded paths."""
    if not library_paths:
        return [
            Check(
                name="library paths",
                status=Status.SKIP,
                message=(
                    "no --library paths supplied, pass one or more "
                    "--library PATH.SchLib to test lib reachability"
                ),
                severity=Severity.MINOR,
            )
        ]

    checks: list[Check] = []
    for raw in library_paths:
        p = Path(raw).expanduser()
        name = f"library: {p.name}"
        if not p.exists():
            checks.append(
                Check(
                    name=name,
                    status=Status.FAIL,
                    message=f"{p} does not exist",
                    fix="Check the path or correct the spelling.",
                    severity=Severity.MINOR,
                )
            )
            continue
        try:
            from eda_agent.bridge import get_bridge
            bridge = get_bridge()
            raw_resp = bridge.send_command(
                "library.get_components",
                {"library_path": str(p)},
                timeout=30.0,
            )
        except Exception as exc:
            checks.append(
                Check(
                    name=name,
                    status=Status.FAIL,
                    message=f"could not enumerate components: {exc}",
                    fix=(
                        "Confirm Altium is running and the script is "
                        "responsive (run `eda-agent doctor` and look at "
                        "earlier checks)."
                    ),
                    severity=Severity.MINOR,
                )
            )
            continue
        count = 0
        if isinstance(raw_resp, dict):
            count = len(raw_resp.get("components") or raw_resp.get("results") or [])
        elif isinstance(raw_resp, list):
            count = len(raw_resp)
        if count == 0:
            checks.append(
                Check(
                    name=name,
                    status=Status.WARN,
                    message=f"{p}: 0 components found",
                    severity=Severity.MINOR,
                )
            )
        else:
            checks.append(
                Check(
                    name=name,
                    status=Status.PASS,
                    message=f"{count} components",
                )
            )
    return checks


def run_doctor_checks(library_paths: Optional[Sequence[str]] = None) -> list[Check]:
    """Compose health + Altium-side preflight."""
    checks: list[Check] = []

    # Health checks first, early failures often explain later ones.
    checks.extend(run_health_checks())
    if any(c.status == Status.FAIL and c.severity == Severity.CRITICAL for c in checks):
        return checks

    # The process scan is reported first but no longer gates anything.
    # It used to return here on a FAIL, so the bridge was never asked,
    # and an Altium the process table could not see was reported absent
    # while it was answering commands. Ask the application itself, then
    # report the scan against what the answer showed.
    running, pids, names = _altium_process_scan()
    ping = _ping_altium()
    evidence = ""
    if ping is not None:
        version = ping.get("script_version") or "?"
        evidence = f"the bridge answered a ping (script version {version})"
    else:
        age = _recent_activity()
        if age is not None:
            evidence = f"activity.log has a handler entry {age:.0f}s ago"

    checks.append(_check_altium_process(running, pids, names, evidence))
    if checks[-1].status == Status.FAIL:
        return checks

    checks.append(_check_script_responsive(ping))
    checks.append(_check_script_version_match(ping))

    if ping is None:
        return checks

    checks.append(_check_command_canary())
    checks.extend(_check_lib_paths(library_paths or []))
    return checks
