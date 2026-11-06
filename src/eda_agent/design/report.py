# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The design report: a run written up from its session journal.

A finished board should come with an account of how it was made, and the
journal already holds one: the requirement, every stage's outcome, the
numbers each exit gate was judged on, the decisions and notes logged on
the way. This module assembles that into markdown. It adds nothing the
run did not record, and a section nothing was recorded for says so in a
line rather than vanishing, so a missing stack-up reads as missing.

Notes reach a section through their ``topic`` (``SECTIONS``, with the
synonyms in ``TOPIC_ALIASES``); a note with no topic, or one this module
does not know, is a decision. Gate evidence is read through
``autonomy.evaluate_gate``, the same reading the harness acts on, so the
report cannot pass a stage the harness sent back.
"""

from __future__ import annotations

from typing import Any

from .autonomy import MEASURED_GATES, apply_measured_gates, stage_evidence
from .session import (KIND_ARTIFACT, KIND_BLOCKED, KIND_NOTE,
                      KIND_PLAN_REVISION, KIND_RESOLVED, KIND_STAGE_RESULT,
                      STAGES)

NOTHING = "Nothing recorded."

#: The report's body, in order: (topic, heading).
SECTIONS = (
    ("placement", "Placement strategy"),
    ("stackup", "Stack-up and impedance"),
    ("critical_routes", "Critical routes"),
    ("power", "Power paths"),
    ("planes", "Planes and fan-out"),
    ("silkscreen", "Silkscreen policy"),
    ("verification", "Verification results"),
    ("simulation", "Simulations"),
    ("issue", "Open issues"),
)

#: Other words a note may be filed under, and the topic they mean.
TOPIC_ALIASES = {
    "stack-up": "stackup", "stack_up": "stackup", "impedance": "stackup",
    "critical": "critical_routes", "critical_route": "critical_routes",
    "routes": "critical_routes",
    "power_path": "power", "power_paths": "power",
    "plane": "planes", "pour": "planes", "pours": "planes",
    "fanout": "planes", "fan-out": "planes", "fan_out": "planes",
    "via_in_pad": "planes", "via-in-pad": "planes",
    "silk": "silkscreen",
    "sim": "simulation", "simulations": "simulation",
    "issues": "issue", "open_issue": "issue", "open_issues": "issue",
    "decisions": "decision",
}

#: The stage whose logged verdict belongs in each section.
_STAGE_OF = {"placement": "placement", "stackup": "rules_stackup",
             "critical_routes": "routing", "planes": "pours_tuning",
             "verification": "verification"}

#: Findings listed per kind before "and N more".
SHOWN = 10


def topic_of(raw: str) -> str:
    """A note's section topic, or "decision" for anything else."""
    t = (raw or "").strip().lower().replace(" ", "_")
    t = TOPIC_ALIASES.get(t, t)
    return t if t in dict(SECTIONS) else "decision"


def _cell(v: Any) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, float):
        v = round(v, 3)
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def _line(text: Any) -> str:
    return " ".join(str(text).split())


def _at(item: dict) -> str:
    at = item.get("at")
    if isinstance(at, (list, tuple)) and len(at) == 2:
        return f" at ({_cell(at[0])}, {_cell(at[1])})"
    return ""


def _finding(item: Any) -> str:
    if not isinstance(item, dict):
        return _line(item)
    parts = [f"{k} {_cell(v)}" for k, v in item.items() if k != "at"]
    return ", ".join(parts) + _at(item)


def _findings(items: Any, label: str) -> list[str]:
    if not isinstance(items, list) or not items:
        return []
    out = [f"  - {label}: {_finding(i)}" for i in items[:SHOWN]]
    if len(items) > SHOWN:
        out.append(f"  - and {len(items) - SHOWN} more")
    return out


def _counts(rec: dict) -> str:
    counts = rec.get("counts") if isinstance(rec.get("counts"), dict) else {
        k: v for k, v in rec.items() if isinstance(v, (int, float, str, bool))}
    return ", ".join(f"{k} {_cell(v)}" for k, v in counts.items())


def _table(head: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


class _Journal:
    """What the report reads, gathered in one pass over the events."""

    def __init__(self, journal):
        self.session_id = journal.session_id
        self.events = journal.events()
        self.state = journal.state()
        self.measured, self.gates = apply_measured_gates(self.state, self.events)
        self.evidence = stage_evidence(self.events)
        self.notes: dict[str, list[dict]] = {}
        self.verdicts: dict[str, str] = {}
        self.decisions: list[str] = []
        self.artifacts: list[dict] = []
        asked: list[str] = []
        for ev in self.events:
            p = ev.payload or {}
            if ev.kind == KIND_NOTE:
                self.notes.setdefault(topic_of(p.get("topic", "")), []).append(p)
            elif ev.kind == KIND_STAGE_RESULT and p.get("stage"):
                self.verdicts[p["stage"]] = p.get("verdict", "") or ""
            elif ev.kind == KIND_PLAN_REVISION:
                summary = p.get("summary", "")
                self.decisions.append(f"Plan revision {p.get('revision', '?')}"
                                      + (f": {_line(summary)}" if summary else ""))
            elif ev.kind == KIND_BLOCKED:
                asked.append(_line(p.get("question", "")))
            elif ev.kind == KIND_RESOLVED:
                q = asked.pop() if asked else ""
                self.decisions.append((f"Asked: {q} Answered: " if q else "Answered: ")
                                      + _line(p.get("answer", "")))
            elif ev.kind == KIND_ARTIFACT:
                self.artifacts.append(p)

    def note_lines(self, topic: str) -> list[str]:
        return [f"- {_line(n.get('text', ''))}" for n in self.notes.get(topic, [])
                if n.get("text")]

    def stage_line(self, stage: str) -> list[str]:
        text = self.verdicts.get(stage)
        return [f"- Stage {stage}: {_line(text)}"] if text else []

    def record(self, check: str) -> tuple[str, dict] | tuple[None, None]:
        """The latest logged result of one check, and the stage it came with."""
        for stage in reversed(STAGES):
            data = self.evidence.get(stage)
            if not isinstance(data, dict):
                continue
            for holder in (data.get("checks"), data):
                if isinstance(holder, dict) and isinstance(holder.get(check), dict):
                    return stage, holder[check]
        return None, None

    def lists(self, key: str) -> list:
        """Every list logged under ``key``, in stage results and note data."""
        out: list = []
        sources = [self.evidence.get(s) for s in STAGES]
        sources += [n.get("data") for ns in self.notes.values() for n in ns]
        for data in sources:
            if isinstance(data, dict) and isinstance(data.get(key), list):
                out.extend(data[key])
        return out

    def artifact_lines(self, *words: str) -> list[str]:
        return [f"- Artifact ({a.get('kind') or 'file'}): {a.get('path', '')}"
                for a in self.artifacts
                if any(w in (a.get("kind") or "").lower() for w in words)]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _placement(j: _Journal) -> list[str]:
    out = j.note_lines("placement") + j.stage_line("placement")
    stage, rec = j.record("placement_audit")
    if rec:
        out.append(f"- placement_audit (logged with {stage}): {_counts(rec)}")
        for key, label in (("overlaps", "overlap"), ("pad_gaps", "pad gap"),
                           ("on_keepouts", "on a keepout"),
                           ("on_mounting_holes", "on a mounting hole"),
                           ("off_board", "off the board")):
            out += _findings(rec.get(key), label)
    return out


def _stackup(j: _Journal) -> list[str]:
    out = j.note_lines("stackup") + j.stage_line("rules_stackup")
    data = j.evidence.get("rules_stackup")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                out.append(f"- {k}: " + ", ".join(f"{a} {_cell(b)}" for a, b in v.items()))
            elif isinstance(v, list):
                out.append(f"- {k}: {len(v)} entries")
                out += [f"  - {_finding(i)}" for i in v[:SHOWN]]
            else:
                out.append(f"- {k}: {_cell(v)}")
    return out + j.artifact_lines("stack", "impedance")


def _critical_routes(j: _Journal) -> list[str]:
    out = j.note_lines("critical_routes") + j.stage_line("routing")
    routes = [r for r in j.lists("critical_routes") if isinstance(r, dict)]
    if routes:
        out += [""] + _table(
            ["Net", "Length (mils)", "Skew (ps)", "Layer changes", "Return vias",
             "Exceptions"],
            [[r.get("net"), r.get("length_mils"), r.get("skew_ps"),
              r.get("layer_changes"), r.get("return_vias"), r.get("exceptions")]
             for r in routes]) + [""]
    stage, rec = j.record("pcb_calc_length_match")
    if rec:
        out.append(f"- Length match (logged with {stage}): worst skew "
                   f"{_cell(rec.get('worst_skew_ps'))} ps against a budget of "
                   f"{_cell(rec.get('skew_budget_ps'))} ps; all matched: "
                   f"{_cell(rec.get('all_matched'))}")
        members = [m for m in rec.get("members") or [] if isinstance(m, dict)]
        if members:
            out += [""] + _table(
                ["Net", "Length (mils)", "Mismatch (mils)", "Skew (ps)", "Within tolerance"],
                [[m.get("net"), m.get("length_mils"), m.get("mismatch_mils"),
                  m.get("skew_ps"), m.get("within_tolerance")] for m in members]) + [""]
    stage, rec = j.record("return_via_audit")
    if rec:
        out.append(f"- Return vias (logged with {stage}): {_counts(rec)}"
                   + (f"; scope: {rec['scope']}" if rec.get("scope") else ""))
        for e in (rec.get("exceptions") or [])[:SHOWN]:
            if isinstance(e, dict):
                near = e.get("nearest_return_mils")
                out.append(f"  - exception: net {_cell(e.get('net'))}{_at(e)}, nearest "
                           + ("return via none on the board" if near is None
                              else f"return via {_cell(near)} mil"))
        extra = len(rec.get("exceptions") or []) - SHOWN
        if extra > 0:
            out.append(f"  - and {extra} more")
    stage, rec = j.record("corner_audit")
    if rec:
        out.append(f"- Corners (logged with {stage}): {_counts(rec)}")
        out += _findings(rec.get("corners"), "corner")
    return out


def _power(j: _Journal) -> list[str]:
    return j.note_lines("power") + j.artifact_lines("power")


def _planes(j: _Journal) -> list[str]:
    out = j.note_lines("planes") + j.stage_line("pours_tuning")
    stage, rec = j.record("plane_region_audit")
    if rec:
        out.append(f"- plane_region_audit (logged with {stage}): {_counts(rec)}")
        for n in (rec.get("nets") or [])[:SHOWN]:
            if isinstance(n, dict):
                out.append(f"  - {_cell(n.get('net'))} on {_cell(n.get('layer'))}: "
                           f"{_cell(n.get('pieces'))} piece(s), "
                           + ("one piece" if n.get("one_piece") else "NOT one piece"))
        out += _findings(rec.get("islands"), "island")
    return out


def _silkscreen(j: _Journal) -> list[str]:
    return j.note_lines("silkscreen")


def _verification(j: _Journal) -> list[str]:
    out = j.note_lines("verification") + j.stage_line("verification")
    for stage in MEASURED_GATES:
        verdict = j.gates.get(stage)
        if not verdict:
            continue
        out += ["", f"Gate for {stage}: {verdict['verdict']}", ""]
        rows = []
        for r in verdict["criteria"]:
            # Where the number came from: the gate's own check, or the other
            # report of it that was logged instead (pcb_run_drc for drc).
            check, metric = (r["source"].split(".", 1) if r["got"] is not None
                             else (r["check"], r["metric"]))
            rows.append([check, metric,
                         f"{r['op']} {r['want']}" if r["op"] != "listed" else "listed",
                         r["got"], {True: "yes", False: "NO", None: "not logged"}[r["pass"]]])
        out += _table(["Check", "Metric", "Wants", "Got", "Pass"], rows)
    stage, rec = j.record("drc")
    if rec:
        out += ["", f"- drc (logged with {stage}): {_counts(rec)}"]
        out += _findings(rec.get("violations"), "violation")
    return out


def _simulation(j: _Journal) -> list[str]:
    out = j.note_lines("simulation")
    out += [f"- {_finding(s)}" for s in j.lists("simulations")]
    return out + j.artifact_lines("sim")


def _issues(j: _Journal) -> list[str]:
    out = []
    if j.state.open_question:
        out.append(f"- Waiting on the user: {_line(j.state.open_question)}")
    reached = [s for s in STAGES if s in j.state.stage_status]
    for stage in reached:
        status = j.state.stage_status[stage]
        if status != "ok":
            text = j.verdicts.get(stage)
            out.append(f"- Stage {stage} is {status}" + (f": {_line(text)}" if text else ""))
    for stage, verdict in j.gates.items():
        if verdict["verdict"] == "fail":
            out.append(f"- Gate for {stage} fails: " + "; ".join(verdict["failed"]))
        elif verdict["verdict"] == "incomplete":
            out.append(f"- Gate for {stage} not fully measured; not logged: "
                       + "; ".join(verdict["missing"]))
    # Only the run's own stages: a stage its mode leaves out was never
    # going to be reached, and listing it reads as unfinished work.
    unreached = [s for s in j.state.stages if s not in j.state.stage_status]
    if unreached:
        out.append("- Stages not reached: " + ", ".join(unreached))
    return out + j.note_lines("issue")


_BUILDERS = {
    "placement": _placement, "stackup": _stackup, "critical_routes": _critical_routes,
    "power": _power, "planes": _planes, "silkscreen": _silkscreen,
    "verification": _verification, "simulation": _simulation, "issue": _issues,
}


def build_report(journal) -> str:
    """The run's report as markdown, from a ``session.SessionJournal``."""
    j = _Journal(journal)
    st = j.state
    out = [f"# Design report: {j.session_id}", "",
           f"Written from the session journal ({st.event_count} events"
           + (f", started {st.created}" if st.created else "")
           + "). It reports what the run logged and nothing else.", ""]

    out += ["## Requirement", "", _line(st.requirement) if st.requirement.strip() else NOTHING, ""]

    if st.mode:
        title = (st.mode_def or {}).get("title") or st.mode
        out += ["## Mode", "", f"{st.mode}: {_line(title)}"]
        out += [f"- {k}: {_line(v)}" for k, v in sorted(st.mode_inputs.items())]
        skipped = [s for s in STAGES if s not in st.stages]
        if skipped:
            out += [f"- Stages outside this mode: {', '.join(skipped)}"]
        out += [""]

    out += ["## Stage outcomes", ""]
    rows = []
    for stage in STAGES:
        logged = st.stage_status.get(
            stage, "not reached" if stage in st.stages else "not in mode")
        verdict = j.gates.get(stage)
        rows.append([stage, logged, st.attempts.get(stage, 0) or "-",
                     verdict["verdict"] if verdict else "-"])
    out += _table(["Stage", "Logged", "Attempts", "Gate"], rows) + [""]
    said = [f"- {s}: {_line(j.verdicts[s])}" for s in STAGES if j.verdicts.get(s)]
    out += (said + [""]) if said else []

    decisions = [f"- {d}" for d in j.decisions] + j.note_lines("decision")
    out += ["## Decisions", ""] + (decisions or [NOTHING]) + [""]

    for topic, heading in SECTIONS:
        body = _BUILDERS[topic](j)
        while body and body[0] == "":
            body.pop(0)
        while body and body[-1] == "":
            body.pop()
        out += [f"## {heading}", ""] + (body or [NOTHING]) + [""]
    return "\n".join(out).rstrip() + "\n"
