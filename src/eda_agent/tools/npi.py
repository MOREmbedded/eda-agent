# SPDX-License-Identifier: Apache-2.0
"""NPI phase gates as MCP tools: feasibility, prototype, EVT, DVT, PVT.

The gates themselves are data in ``design/npi.py``; this is the surface
a client drives them through. Three tools, and the split between them
is the point:

    design_npi_gate      what this gate asks, and where it stands
    design_npi_record    answer one criterion, with evidence
    design_npi_status    which gate the programme is actually at

The gate does NOT run the checks. It names the tools that answer each
criterion and reports what has been recorded, which keeps three things
true that an auto-running gate would lose: it works without Altium
open, it never hides which evidence is fresh, and a client can see what
it is about to spend thirty seconds on before it does.

Evidence lives in one append-only JSONL per programme under the
workspace, the same arrangement the design session journal uses.
"""

from __future__ import annotations

from typing import Any, Optional


def _record_path(programme: str):
    """Where one programme's evidence log lives.

    The name arrives as a tool argument and ends up in a path, so it is
    reduced to characters that cannot leave the directory. A dot is kept
    because "board-a.v2" is a reasonable name, but runs of them are
    collapsed and the ends trimmed, so "../../escaped" becomes
    "escaped" rather than a legal-but-alarming "....escaped".
    """
    import re

    from ..config import get_config

    safe = "".join(ch for ch in (programme or "default")
                   if ch.isalnum() or ch in "-_.")
    safe = re.sub(r"\.{2,}", ".", safe).strip("._-") or "default"
    return get_config().workspace_dir / "npi" / f"{safe}.jsonl"


def _criterion_payload(status, *, include_why: bool) -> dict[str, Any]:
    c = status.criterion
    out: dict[str, Any] = {
        "id": c.id,
        "title": c.title,
        "severity": c.severity,
        "kind": c.kind,
        "verdict": status.verdict,
        "passes_when": c.passes_when,
    }
    if c.tools:
        out["tools"] = list(c.tools)
    if include_why:
        out["why_this_phase"] = c.why
    if status.evidence is not None:
        out["evidence"] = {
            "verdict": status.evidence.verdict,
            "at": status.evidence.at.isoformat(timespec="seconds"),
            "by": status.evidence.by,
            "note": status.evidence.note,
            "revision": status.evidence.revision,
        }
        # Say WHY a recorded pass is not counting. Silently reverting it
        # to pending is the behaviour that makes a gate untrustworthy.
        if status.stale:
            out["discounted"] = (
                f"recorded {status.evidence.at.date()}, older than this "
                f"criterion's {c.stale_after_days}-day limit; re-check it")
        elif status.wrong_revision:
            out["discounted"] = (
                f"recorded against revision {status.evidence.revision!r}, "
                f"not the one asked about; re-check it")
    return out


def register_npi_tools(mcp) -> None:

    @mcp.tool()
    async def design_npi_gate(phase: str, revision: str = "",
                              programme: str = "default",
                              include_rationale: bool = True
                              ) -> dict[str, Any]:
        """Show one NPI phase gate and where the programme stands against it.

        ``phase`` is one of feasibility, prototype, evt, dvt, pvt (the
        usual spellings and abbreviations are accepted). Returns every
        criterion with its current verdict, the tools that answer the
        automatic ones, and the criteria that need a human.

        This reports; it does not run the checks. Run the tools listed
        in ``next_tools``, then record each result with
        ``design_npi_record``.

        ``revision`` is the design revision the question is about. When
        given, evidence recorded against a different revision stops
        counting and says so, because a check that passed on last
        month's board is not a check on this one.
        """
        from ..design.npi import NpiRecord, evaluate_gate, parse_phase

        try:
            target = parse_phase(phase)
        except ValueError as bad:
            return {"error": str(bad)}

        record = NpiRecord(_record_path(programme))
        result = evaluate_gate(target, record.replay(), revision=revision)

        return {
            "phase": target.value,
            "phase_label": target.label,
            "programme": programme,
            "revision": revision,
            "passed": result.passed,
            "counts": result.counts(),
            "criteria": [_criterion_payload(s, include_why=include_rationale)
                         for s in result.statuses],
            "blockers": [s.criterion.id for s in result.blockers],
            "next_tools": result.next_tools(),
            "awaiting_attestation": [
                s.criterion.id for s in result.awaiting_attestation()],
        }

    @mcp.tool()
    async def design_npi_record(criterion_id: str, verdict: str,
                                note: str = "", by: str = "",
                                revision: str = "",
                                programme: str = "default"
                                ) -> dict[str, Any]:
        """Record the answer to one NPI criterion.

        ``verdict`` is pass, fail, waived or pending. A waiver needs a
        note saying why the criterion does not apply or who accepted the
        risk; without one it is refused, because an unexplained waiver
        and a skipped check look identical six months later.

        ``by`` is who or what produced the answer: a tool name for an
        automatic check, a person or role for an attestation. ``note``
        should carry the evidence itself -- the number measured, the
        violation count, the document reference -- not just a verdict.

        The log is append-only, so recording again supersedes without
        erasing. Returns the gate that criterion belongs to, re-scored.
        """
        from ..design.npi import (CRITERIA_BY_ID, NpiRecord, evaluate_gate)

        criterion = CRITERIA_BY_ID.get(criterion_id)
        if criterion is None:
            return {"error": f"unknown criterion {criterion_id!r}",
                    "hint": "call design_npi_gate for the ids of a phase"}

        record = NpiRecord(_record_path(programme))
        try:
            record.record(criterion_id, verdict, by=by, note=note,
                          revision=revision)
        except ValueError as refused:
            return {"error": str(refused)}

        result = evaluate_gate(criterion.phase, record.replay(),
                               revision=revision)
        return {
            "recorded": criterion_id,
            "verdict": verdict,
            "phase": criterion.phase.value,
            "gate_passed": result.passed,
            "counts": result.counts(),
            "blockers": [s.criterion.id for s in result.blockers],
        }

    @mcp.tool()
    async def design_npi_status(revision: str = "",
                                programme: str = "default"
                                ) -> dict[str, Any]:
        """Which NPI gate the programme is at, and what the next one needs.

        The current phase is the FIRST gate that does not pass, not the
        one anybody says the programme is in. A board whose feasibility
        gate never closed is at feasibility however much layout has been
        done, and saying so is the only useful thing this tool can do.
        """
        from ..design.npi import NpiRecord, programme_status

        record = NpiRecord(_record_path(programme))
        state = programme_status(record.replay(), revision=revision)

        gates = []
        for result in state["gates"]:
            gates.append({
                "phase": result.phase.value,
                "label": result.phase.label,
                "passed": result.passed,
                "counts": result.counts(),
                "blockers": [s.criterion.id for s in result.blockers],
            })

        current = state["phase"]
        active = next((g for g in gates if g["phase"] == current), None)
        return {
            "programme": programme,
            "revision": revision,
            "phase": current,
            "phase_label": state["phase_label"],
            "complete": state["complete"],
            "blockers": active["blockers"] if active else [],
            "next_tools": next(
                (r.next_tools() for r in state["gates"]
                 if r.phase.value == current), []),
            "gates": gates,
        }
