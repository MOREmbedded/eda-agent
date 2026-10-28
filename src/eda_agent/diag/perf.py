# SPDX-License-Identifier: Apache-2.0
"""Where the time actually goes, from logs already being written.

Two logs record every call, and until now only one of them was ever
read:

* ``activity.log`` is written by Pascal around ProcessCommand
  (StatusForm.pas), so it times the handler INSIDE Altium. All four
  existing views -- the CSV, the in-Altium Perf tab, the dashboard
  table and its p95 sparkline -- read this one.
* ``bridge_trace.log`` is written by Python (bridge/altium_bridge.py)
  and records the round trip the CALLER experienced, per request id,
  with poll counts, deadline extensions and timeouts.

Nothing joined them. The difference between the two, for one request,
is everything that is not the handler: writing the request file,
Pascal's poll pickup latency, reading the response back, and any time
spent queued behind another command (Pascal processes one at a time).
On a real session that difference turned out to be most of the wall
clock for short commands, which no existing view could show.

So this module joins them on the request id and reports both halves
side by side. It parses text and computes percentiles; it talks to
nothing.

The ids differ in width by design: bridge_trace writes the first 8
characters, activity.log carries the full 32 in its payload, so the
join is a prefix match.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: ``1791917156.118 SEND cmd=application.ping id=500aec90``
_SEND = re.compile(r"^(?P<ts>\d+\.\d+)\s+SEND\s+cmd=(?P<cmd>\S+)\s+id=(?P<id>[0-9a-f]+)")

#: POLL_MATCH and POLL_TIMEOUT share a shape; the outcome is the verb.
_OUTCOME = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+POLL_(?P<kind>MATCH|TIMEOUT)\s+id=(?P<id>[0-9a-f]+)\s+"
    r"elapsed=(?P<elapsed>\d+)ms\s+polls=(?P<polls>\d+)\s+"
    r"parse_errs=(?P<parse_errs>\d+)\s+extensions=(?P<extensions>\d+)")

#: ``POLL_DIALOG id=fd965e2f after=3.0s summary=...``
_DIALOG = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+POLL_DIALOG\s+id=(?P<id>[0-9a-f]+)\s+after=\S+\s+summary=(?P<summary>.*)$")

#: ``2026-10-15 00:00:16.751,1109,application.set_active_document,OK,517,{...}``
_ACTIVITY = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} [\d:.]+),(?P<ms>\d+),(?P<cmd>[^,]*),"
    r"(?P<tag>[^,]*),(?P<bytes>\d*),?(?P<payload>.*)$")


@dataclass
class Call:
    """One request, from both sides where both are available."""

    request_id: str = ""
    command: str = ""
    sent_at: float | None = None
    round_trip_ms: int | None = None
    handler_ms: int | None = None
    polls: int = 0
    extensions: int = 0
    parse_errors: int = 0
    timed_out: bool = False
    response_bytes: int | None = None
    blocked_by_dialog: str = ""

    @property
    def overhead_ms(self) -> int | None:
        """Round trip minus handler: IPC plus any time spent queued.

        None when only one side logged the call, because a missing half
        is not the same as a zero half.
        """
        if self.round_trip_ms is None or self.handler_ms is None:
            return None
        return self.round_trip_ms - self.handler_ms


@dataclass
class CommandStats:
    command: str
    calls: int = 0
    timeouts: int = 0
    round_trip: list[int] = field(default_factory=list)
    handler: list[int] = field(default_factory=list)
    overhead: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "calls": self.calls,
            "timeouts": self.timeouts,
            "round_trip_ms": _summary(self.round_trip),
            "handler_ms": _summary(self.handler),
            "overhead_ms": _summary(self.overhead),
            "total_round_trip_ms": sum(self.round_trip),
        }


def percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile; None for an empty sample.

    Nearest-rank rather than interpolated: these are millisecond
    counts from a modest number of calls, and an interpolated p95
    invents a value no call actually took.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def _summary(values: list[int]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": max(values) if values else None}


def parse_bridge_trace(text: str) -> dict[str, Call]:
    """Round trips as the caller experienced them, keyed by short id."""
    calls: dict[str, Call] = {}

    def call_for(request_id: str) -> Call:
        return calls.setdefault(request_id, Call(request_id=request_id))

    for line in text.splitlines():
        match = _SEND.match(line)
        if match:
            call = call_for(match["id"])
            call.command = match["cmd"]
            call.sent_at = float(match["ts"])
            continue
        match = _OUTCOME.match(line)
        if match:
            call = call_for(match["id"])
            call.round_trip_ms = int(match["elapsed"])
            call.polls = int(match["polls"])
            call.extensions = int(match["extensions"])
            call.parse_errors = int(match["parse_errs"])
            call.timed_out = match["kind"] == "TIMEOUT"
            continue
        match = _DIALOG.match(line)
        if match:
            call_for(match["id"]).blocked_by_dialog = match["summary"].strip()
    return calls


def parse_activity_log(text: str) -> dict[str, dict[str, Any]]:
    """Handler times from Altium, keyed by the full request id.

    Session markers (``_session_start`` / ``_session_end``) carry no id
    and are skipped rather than counted as calls.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = _ACTIVITY.match(line.strip())
        if not match or match["cmd"].startswith("_session"):
            continue
        payload = match["payload"]
        request_id = ""
        if payload:
            found = re.search(r'"id"\s*:\s*"([0-9a-f]+)"', payload)
            if found:
                request_id = found.group(1)
        if not request_id:
            continue
        try:
            response_bytes = int(match["bytes"]) if match["bytes"] else None
        except ValueError:
            response_bytes = None
        out[request_id] = {"handler_ms": int(match["ms"]),
                           "command": match["cmd"],
                           "tag": match["tag"],
                           "response_bytes": response_bytes}
    return out


#: Every activity.log line opens with Altium's local wall clock, whether
#: it is a handler call or a session marker.
_STAMP = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}),")


def last_activity_at(text: str) -> datetime | None:
    """When the newest parseable activity.log line was written.

    Pascal appends a line per handler call plus the session markers, so
    the newest stamp is the last moment Altium is known to have been
    alive. Written in Altium's LOCAL time, so the value is comparable
    with ``datetime.now()`` and with nothing else.

    Scanned from the end, taking the first stamp that parses: lines are
    appended in order, and a half-written final line then costs a step
    rather than the answer.
    """
    for line in reversed(text.splitlines()):
        match = _STAMP.match(line.strip())
        if not match:
            continue
        try:
            return datetime.strptime(match["stamp"], "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
    return None


def correlate(trace: dict[str, Call],
              activity: dict[str, dict[str, Any]]) -> list[Call]:
    """Attach each handler time to its round trip.

    bridge_trace holds the first 8 characters of the id and activity.log
    the full 32, so the index is built on the prefix.
    """
    by_prefix = {full[:8]: data for full, data in activity.items()}
    for short, call in trace.items():
        data = by_prefix.get(short[:8])
        if not data:
            continue
        call.handler_ms = data["handler_ms"]
        call.response_bytes = data["response_bytes"]
        if not call.command:
            call.command = data["command"]
    return sorted(trace.values(), key=lambda c: c.sent_at or 0.0)


def build_report(calls: list[Call]) -> dict[str, Any]:
    stats: dict[str, CommandStats] = {}
    for call in calls:
        name = call.command or "(unknown)"
        entry = stats.setdefault(name, CommandStats(command=name))
        entry.calls += 1
        if call.timed_out:
            entry.timeouts += 1
        if call.round_trip_ms is not None:
            entry.round_trip.append(call.round_trip_ms)
        if call.handler_ms is not None:
            entry.handler.append(call.handler_ms)
        overhead = call.overhead_ms
        if overhead is not None:
            entry.overhead.append(overhead)

    ranked = sorted(stats.values(), key=lambda s: sum(s.round_trip), reverse=True)
    correlated = [c for c in calls if c.overhead_ms is not None]
    return {
        "calls": len(calls),
        "correlated": len(correlated),
        "timeouts": sum(1 for c in calls if c.timed_out),
        "dialog_blocks": [
            {"command": c.command, "summary": c.blocked_by_dialog}
            for c in calls if c.blocked_by_dialog],
        "extended": sum(1 for c in calls if c.extensions),
        "totals": {
            "round_trip_ms": _summary([c.round_trip_ms for c in calls
                                       if c.round_trip_ms is not None]),
            "handler_ms": _summary([c.handler_ms for c in calls
                                    if c.handler_ms is not None]),
            "overhead_ms": _summary([c.overhead_ms for c in correlated]),
        },
        "commands": [s.to_dict() for s in ranked],
    }


def report_from_workspace(workspace: Path) -> dict[str, Any]:
    """Read both logs from a workspace directory and report.

    A missing log is not an error: bridge_trace alone still gives round
    trips, and activity.log alone still gives handler times. The report
    says how many calls it could correlate so a half-answer is never
    mistaken for a whole one.
    """
    workspace = Path(workspace)
    trace_path = workspace / "bridge_trace.log"
    activity_path = workspace / "activity.log"
    trace = parse_bridge_trace(
        trace_path.read_text(encoding="utf-8", errors="replace")
        if trace_path.exists() else "")
    activity = parse_activity_log(
        activity_path.read_text(encoding="utf-8", errors="replace")
        if activity_path.exists() else "")
    report = build_report(correlate(trace, activity))
    report["sources"] = {
        "bridge_trace": str(trace_path) if trace_path.exists() else None,
        "activity_log": str(activity_path) if activity_path.exists() else None,
    }
    return report


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value}"


def format_report(report: dict[str, Any], limit: int = 15) -> str:
    lines = ["eda-agent perf", "==============", ""]
    sources = report.get("sources", {})
    for label, key in (("bridge_trace.log", "bridge_trace"),
                       ("activity.log", "activity_log")):
        lines.append(f"  {label:18s} {sources.get(key) or '(not found)'}")
    lines.append("")

    totals = report["totals"]
    lines.append(f"  {report['calls']} call(s), {report['correlated']} correlated "
                 f"with a handler time, {report['timeouts']} timed out, "
                 f"{report['extended']} needed a deadline extension")
    lines.append("")
    lines.append(f"  {'':10s} {'p50':>8s} {'p95':>8s} {'max':>8s}")
    for label, key in (("round trip", "round_trip_ms"), ("in Altium", "handler_ms"),
                       ("overhead", "overhead_ms")):
        s = totals[key]
        lines.append(f"  {label:10s} {_ms(s['p50']):>8s} {_ms(s['p95']):>8s} "
                     f"{_ms(s['max']):>8s}")
    lines.append("")
    lines.append("  Overhead is round trip minus handler: IPC plus any time")
    lines.append("  queued behind another command, since Altium answers one")
    lines.append("  request at a time.")
    lines.append("")

    lines.append(f"  {'command':44s} {'n':>4s} {'rt p95':>8s} {'alt p95':>8s} "
                 f"{'ovh p95':>8s} {'total':>9s}")
    for entry in report["commands"][:limit]:
        lines.append(
            f"  {entry['command'][:44]:44s} {entry['calls']:>4d} "
            f"{_ms(entry['round_trip_ms']['p95']):>8s} "
            f"{_ms(entry['handler_ms']['p95']):>8s} "
            f"{_ms(entry['overhead_ms']['p95']):>8s} "
            f"{entry['total_round_trip_ms']:>9d}")
    if len(report["commands"]) > limit:
        lines.append(f"  ... and {len(report['commands']) - limit} more")

    if report["dialog_blocks"]:
        lines.append("")
        lines.append("  Blocked by a modal dialog:")
        for block in report["dialog_blocks"][:5]:
            lines.append(f"    {block['command']}: {block['summary'][:90]}")
    lines.append("")
    return "\n".join(lines)
