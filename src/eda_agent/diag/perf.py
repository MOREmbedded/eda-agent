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

The two logs write the id differently, and the join has to respect
that. A request id is ``<session tag>-<sequence>-<random>``, and the
tag is one value for the whole server process. bridge_trace used to
write its first 8 characters, which name the SESSION, not the call,
so every line a session wrote carried the same id. The join then
collapsed a session into a single entry and the report presented the
survivors as the whole story: on a real workspace 1244 trace lines
became 129 calls, each holding only its session's last request.

So bridge_trace now writes ``<tag>-<sequence>``, which is unique
within a session; activity.log still carries the full id in its
payload; and ``request_key`` reduces either form to the same key.
Ids predating session tags were a bare hex uuid whose first 8
characters already named one call, so logs from then still join as
they always did. Lines carrying a session tag alone cannot be split
after the fact, and the report counts them rather than dropping them
out of sight.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: ``1793176203.785 SEND cmd=application.ping id=a3c05b5f-00000001``
_SEND = re.compile(r"^(?P<ts>\d+\.\d+)\s+SEND\s+cmd=(?P<cmd>\S+)\s+id=(?P<id>[0-9a-f][0-9a-f-]*)")

#: POLL_MATCH and POLL_TIMEOUT share a shape; the outcome is the verb.
_OUTCOME = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+POLL_(?P<kind>MATCH|TIMEOUT)\s+id=(?P<id>[0-9a-f][0-9a-f-]*)\s+"
    r"elapsed=(?P<elapsed>\d+)ms\s+polls=(?P<polls>\d+)\s+"
    r"parse_errs=(?P<parse_errs>\d+)\s+extensions=(?P<extensions>\d+)")

#: ``POLL_DIALOG id=fd965e2f-00000004 after=3.0s summary=...``
_DIALOG = re.compile(
    r"^(?P<ts>\d+\.\d+)\s+POLL_DIALOG\s+id=(?P<id>[0-9a-f][0-9a-f-]*)\s+after=\S+\s+summary=(?P<summary>.*)$")

#: ``2026-10-15 00:00:16.751,1109,application.set_active_document,OK,517,{...}``
_ACTIVITY = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} [\d:.]+),(?P<ms>\d+),(?P<cmd>[^,]*),"
    r"(?P<tag>[^,]*),(?P<bytes>\d*),?(?P<payload>.*)$")


def request_key(request_id: str) -> str:
    """The part of a request id that names one call.

    The mirror of ``trace_id`` in bridge/altium_bridge.py, which
    decides what reaches bridge_trace.log: ``<tag>-<sequence>`` for an
    id that carries a session tag, the leading 8 characters for a bare
    uuid from before tags existed. It lives here rather than being
    imported so this module stays a parser of text that loads nothing
    from the bridge; a test holds the two spellings together.
    """
    tag, dash, rest = request_id.partition("-")
    if not dash:
        return request_id[:8]
    return f"{tag}-{rest.partition('-')[0]}"


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
    #: SEND lines that landed on this id. 1 for an id that names one
    #: call; more means a log written while the trace recorded only the
    #: session tag, where several requests are indistinguishable. The
    #: report reads this back so they are counted, not quietly lost.
    requests: int = 0

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
    """Round trips as the caller experienced them, keyed by logged id.

    One entry per id, and ``Call.requests`` says how many SEND lines
    claimed that id. Above one the log predates the widened id: those
    requests overwrite each other here and only the last survives,
    which is why the count is kept rather than inferred from the
    entries that are left.
    """
    calls: dict[str, Call] = {}

    def call_for(request_id: str) -> Call:
        return calls.setdefault(request_id, Call(request_id=request_id))

    for line in text.splitlines():
        match = _SEND.match(line)
        if match:
            call = call_for(match["id"])
            call.command = match["cmd"]
            call.sent_at = float(match["ts"])
            call.requests += 1
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

    Pascal writes its own markers to this log (``_session_start``,
    ``_session_end``, ``_sdk_probe``, ``_purge_requests``,
    ``_pcb_geometry``, ``_tick_first``). They are bookkeeping, not
    requests: every one is named with a leading underscore, so the
    whole family is skipped rather than only the two that were known
    when this was written. On a real workspace they were 161 of 410
    rows, and letting them through made the id-less rows look like
    calls whose handler time had gone missing.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = _ACTIVITY.match(line.strip())
        if not match or match["cmd"].startswith("_"):
            continue
        payload = match["payload"]
        request_id = ""
        if payload:
            found = re.search(r'"id"\s*:\s*"([0-9a-f][0-9a-f-]*)"', payload)
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


def correlate(trace: dict[str, Call],
              activity: dict[str, dict[str, Any]]) -> list[Call]:
    """Attach each handler time to its round trip.

    activity.log carries the full id, bridge_trace the part of it that
    names one call, so the index is built by reducing the full id the
    same way. A trace id that is a bare session tag matches nothing
    here, and that is deliberate: any handler time it picked up would
    belong to whichever call of that session happened to be logged
    last, which is how the report came to report fiction.
    """
    by_key = {request_key(full): data for full, data in activity.items()}
    for logged, call in trace.items():
        data = by_key.get(logged)
        if not data:
            continue
        call.handler_ms = data["handler_ms"]
        call.response_bytes = data["response_bytes"]
        if not call.command:
            call.command = data["command"]
    return sorted(trace.values(), key=lambda c: c.sent_at or 0.0)


def build_report(calls: list[Call], trace_lines: int = 0,
                 activity_rows: int = 0) -> dict[str, Any]:
    """Statistics over the calls, with the tallies that bound them.

    ``trace_lines`` and ``activity_rows`` are what the two files held;
    ``calls`` and ``correlated`` are what could be resolved from them.
    Reporting only the second pair is how an under-count passes for an
    answer, so both go into the report and both get printed.
    """
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
    # A call parsed from POLL lines alone never saw its SEND, so it
    # counts as the one request it plainly is.
    requests = sum(max(c.requests, 1) for c in calls)
    return {
        "trace_lines": trace_lines,
        "requests": requests,
        "unresolved": requests - len(calls),
        "activity_rows": activity_rows,
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
    says how much of each file it could resolve so a half-answer is
    never mistaken for a whole one.
    """
    workspace = Path(workspace)
    trace_path = workspace / "bridge_trace.log"
    activity_path = workspace / "activity.log"
    trace_text = (trace_path.read_text(encoding="utf-8", errors="replace")
                  if trace_path.exists() else "")
    activity = parse_activity_log(
        activity_path.read_text(encoding="utf-8", errors="replace")
        if activity_path.exists() else "")
    trace = parse_bridge_trace(trace_text)
    report = build_report(
        correlate(trace, activity),
        trace_lines=sum(1 for line in trace_text.splitlines() if line.strip()),
        activity_rows=len(activity))
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
    lines.append(f"  {report['trace_lines']} trace line(s) read, "
                 f"{report['requests']} request(s) sent, "
                 f"{report['calls']} call(s) resolved")
    if report["unresolved"]:
        lines.append(f"  {report['unresolved']} request(s) could not be told "
                     "apart: those trace lines name")
        lines.append("  the session rather than the call, and no later reading "
                     "can split")
        lines.append("  them. The figures below cover the calls that were "
                     "resolved.")
    lines.append(f"  {report['correlated']} call(s) carry a handler time "
                 f"({report['activity_rows']} activity.log row(s) held a "
                 "usable id)")
    lines.append(f"  {report['timeouts']} timed out, {report['extended']} "
                 "needed a deadline extension")
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
