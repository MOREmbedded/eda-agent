# SPDX-License-Identifier: Apache-2.0
"""NPI phase gates: feasibility, prototype, EVT, DVT, PVT.

A gate is only worth having if it is hard to pass by accident. These
tests pin the three properties that decide that:

  - evidence expires, and evidence from another revision does not count,
    so a gate cannot be passed with a measurement of a different board;
  - a waiver must carry a reason, so it cannot be used as a quiet skip;
  - the programme is at the first gate that does not pass, not the one
    somebody says it is at.

Plus the guard that earns its place every time the tool surface moves:
every tool a criterion names must actually be registered. The first
draft of the criteria named nineteen tools that did not exist, and
without this test the gate would have sent a client after every one.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from eda_agent.design import npi
from eda_agent.design.npi import (
    ADVISORY, AUTOMATIC, BLOCKER, FAIL, MANUAL, PASS, PENDING, WAIVED,
    Evidence, NpiRecord, Phase, criteria_for, evaluate_gate, next_phase,
    parse_phase, programme_status,
)

NOW = datetime(2026, 10, 26, 12, 0, 0)


def _ev(criterion_id: str, verdict: str = PASS, *, days_ago: int = 0,
        revision: str = "", note: str = "measured") -> Evidence:
    return Evidence(criterion_id=criterion_id, verdict=verdict,
                    at=NOW - timedelta(days=days_ago), by="test",
                    note=note, revision=revision)


def _pass_everything(phase: Phase, **kw) -> list[Evidence]:
    return [_ev(c.id, PASS, **kw) for c in criteria_for(phase)]


# --- the catalogue --------------------------------------------------------

def test_every_gated_phase_has_criteria():
    for phase in npi.GATED_PHASES:
        assert criteria_for(phase), f"{phase.value} has no criteria"
    # Mass production is the state after the last gate, not a gate.
    assert criteria_for(Phase.MP) == ()


def test_criterion_ids_are_unique_and_name_their_phase():
    """The id is what a client records against, so it has to be stable
    and self-describing. A duplicate would silently overwrite."""
    seen = set()
    for c in npi.ALL_CRITERIA:
        assert c.id not in seen, f"duplicate criterion id {c.id}"
        seen.add(c.id)
        head = c.id.split(".", 1)[0]
        assert head in (c.phase.value, "feas", "proto"), (
            f"{c.id} does not name its phase {c.phase.value}")


def test_every_criterion_says_what_it_asks_and_why_it_is_here():
    """A gate that cannot explain itself gets waived wholesale.

    The rationale is the whole argument for the phase order: it records
    what discovering this late would cost.
    """
    for c in npi.ALL_CRITERIA:
        assert c.title.strip(), c.id
        assert len(c.why.strip()) > 40, f"{c.id} has no real rationale"
        assert len(c.passes_when.strip()) > 20, f"{c.id} has no pass condition"
        assert c.severity in (BLOCKER, ADVISORY), c.id
        assert c.kind in (AUTOMATIC, MANUAL), c.id


def test_an_automatic_criterion_names_tools_and_a_manual_one_does_not():
    """Kind and tools must agree.

    An automatic criterion with no tools cannot be answered by anything,
    and a manual one with tools invites a client to loop on it.
    """
    for c in npi.ALL_CRITERIA:
        if c.kind == AUTOMATIC:
            assert c.tools, f"{c.id} is automatic but names no tool"
        else:
            assert not c.tools, f"{c.id} is manual but names {c.tools}"


@pytest.fixture(scope="module")
def registered_tools() -> set[str]:
    from mcp.server.fastmcp import FastMCP
    from eda_agent.tools import register_backend

    mcp = FastMCP("test")
    register_backend(mcp, "altium")
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_every_tool_a_criterion_names_actually_exists(registered_tools):
    """The guard that caught the first draft.

    Nineteen of the thirty-seven tools the criteria first named were
    inventions -- plausible, consistently spelled, and absent. A gate
    that sends a client after a tool that does not exist is worse than
    one that says "attest this", because the client cannot tell the
    difference between a missing tool and its own mistake.
    """
    named = {t for c in npi.ALL_CRITERIA for t in c.tools}
    missing = sorted(t for t in named if t not in registered_tools)
    assert not missing, f"criteria name tools that do not exist: {missing}"


def test_the_phases_run_in_the_order_a_programme_passes_them():
    assert next_phase(Phase.FEASIBILITY) is Phase.PROTOTYPE
    assert next_phase(Phase.EVT) is Phase.DVT
    assert next_phase(Phase.PVT) is Phase.MP
    assert next_phase(Phase.MP) is None


@pytest.mark.parametrize("given,expected", [
    ("evt", Phase.EVT), ("EVT", Phase.EVT),
    ("Engineering Validation Test", Phase.EVT),
    ("eng-validation", Phase.EVT),
    ("proto", Phase.PROTOTYPE), ("feasability", Phase.FEASIBILITY),
    ("design_validation", Phase.DVT), ("PVT", Phase.PVT),
])
def test_phase_names_are_accepted_as_people_write_them(given, expected):
    assert parse_phase(given) is expected


def test_an_unknown_phase_lists_the_real_ones():
    with pytest.raises(ValueError) as caught:
        parse_phase("qualification")
    assert "feasibility" in str(caught.value) and "evt" in str(caught.value)


# --- evaluation -----------------------------------------------------------

def test_an_empty_gate_blocks_on_every_blocking_criterion():
    result = evaluate_gate(Phase.PROTOTYPE, [], now=NOW)
    assert not result.passed
    blocking = [c for c in criteria_for(Phase.PROTOTYPE)
                if c.severity == BLOCKER]
    assert len(result.blockers) == len(blocking)
    assert result.counts()[PENDING] == len(criteria_for(Phase.PROTOTYPE))


def test_a_gate_passes_when_every_blocker_is_answered():
    result = evaluate_gate(Phase.PROTOTYPE, _pass_everything(Phase.PROTOTYPE),
                           now=NOW)
    assert result.passed and not result.blockers


def test_a_waiver_passes_the_gate_the_same_as_a_pass():
    """Waiving is a decision, not a failure. What stops it being a quiet
    skip is the note requirement at the recording layer, not here."""
    evidence = [_ev(c.id, WAIVED if i == 0 else PASS)
                for i, c in enumerate(criteria_for(Phase.PROTOTYPE))]
    assert evaluate_gate(Phase.PROTOTYPE, evidence, now=NOW).passed


def test_a_failure_blocks_and_is_named():
    evidence = _pass_everything(Phase.PROTOTYPE)
    evidence[0] = _ev(criteria_for(Phase.PROTOTYPE)[0].id, FAIL)
    result = evaluate_gate(Phase.PROTOTYPE, evidence, now=NOW)
    assert not result.passed
    assert [s.criterion.id for s in result.blockers] == [evidence[0].criterion_id]


def test_the_newest_record_wins_and_the_older_one_is_not_a_vote():
    cid = criteria_for(Phase.PROTOTYPE)[0].id
    evidence = _pass_everything(Phase.PROTOTYPE)
    evidence.append(_ev(cid, FAIL, days_ago=0))
    evidence.append(_ev(cid, PASS, days_ago=2))   # older, must not win
    result = evaluate_gate(Phase.PROTOTYPE, evidence, now=NOW)
    assert not result.passed


def test_stale_evidence_stops_counting_and_the_gate_reopens():
    """A check that passed four months ago is not a check on this board."""
    phase = Phase.PROTOTYPE
    old = [_ev(c.id, PASS, days_ago=400) for c in criteria_for(phase)]
    result = evaluate_gate(phase, old, now=NOW)
    assert not result.passed
    # Reverted to pending rather than failed: nobody measured a failure.
    assert all(s.verdict == PENDING for s in result.statuses if s.stale)
    assert any(s.stale for s in result.statuses)


def test_a_criterion_with_no_expiry_keeps_its_evidence():
    """A captured requirement does not decay the way a DRC run does."""
    forever = [c for c in npi.ALL_CRITERIA if c.stale_after_days is None]
    assert forever, "expected at least one criterion that never goes stale"
    c = forever[0]
    result = evaluate_gate(c.phase, [_ev(c.id, PASS, days_ago=5000)], now=NOW)
    status = next(s for s in result.statuses if s.criterion.id == c.id)
    assert not status.stale and status.verdict == PASS


def test_evidence_from_another_revision_does_not_count():
    phase = Phase.PROTOTYPE
    evidence = [_ev(c.id, PASS, revision="A.1") for c in criteria_for(phase)]
    assert evaluate_gate(phase, evidence, revision="A.1", now=NOW).passed
    later = evaluate_gate(phase, evidence, revision="B.0", now=NOW)
    assert not later.passed
    assert all(s.wrong_revision for s in later.statuses)


def test_evidence_without_a_revision_is_not_treated_as_a_mismatch():
    """Older practice recorded no revision. Discounting all of it would
    reopen every gate the first time somebody asked about a revision."""
    phase = Phase.PROTOTYPE
    evidence = _pass_everything(phase)           # revision=""
    assert evaluate_gate(phase, evidence, revision="B.0", now=NOW).passed


def test_an_advisory_criterion_does_not_block(monkeypatch):
    phase = Phase.PROTOTYPE
    original = criteria_for(phase)
    softened = (
        original[:1]
        + tuple(npi.Criterion(**{**c.__dict__, "severity": ADVISORY})
                for c in original[1:])
    )
    monkeypatch.setitem(npi.CRITERIA_BY_PHASE, phase, softened)
    result = evaluate_gate(phase, [_ev(original[0].id, PASS)], now=NOW)
    assert result.passed
    assert len(result.advisories) == len(original) - 1


def test_evidence_for_another_phase_is_ignored_not_rejected():
    """A caller passes the whole programme's log; the gate takes its own."""
    mixed = _pass_everything(Phase.PROTOTYPE) + _pass_everything(Phase.DVT)
    assert evaluate_gate(Phase.PROTOTYPE, mixed, now=NOW).passed


# --- what to do next ------------------------------------------------------

def test_next_tools_covers_the_blocking_automatic_checks_in_order():
    result = evaluate_gate(Phase.PROTOTYPE, [], now=NOW)
    tools = result.next_tools()
    assert tools and len(tools) == len(set(tools)), "tools must be unique"
    first_auto = next(c for c in criteria_for(Phase.PROTOTYPE)
                      if c.kind == AUTOMATIC)
    assert tools[0] == first_auto.tools[0]


def test_tools_are_not_suggested_for_criteria_only_a_human_can_answer():
    result = evaluate_gate(Phase.PROTOTYPE, [], now=NOW)
    waiting = result.awaiting_attestation()
    assert waiting
    assert all(s.criterion.kind == MANUAL for s in waiting)
    manual_tools = {t for s in waiting for t in s.criterion.tools}
    assert not manual_tools


def test_an_answered_criterion_drops_out_of_the_next_steps():
    phase = Phase.PROTOTYPE
    first = next(c for c in criteria_for(phase) if c.kind == AUTOMATIC)
    after = evaluate_gate(phase, [_ev(first.id, PASS)], now=NOW)
    assert first.tools[0] not in after.next_tools() or any(
        first.tools[0] in c.tools for c in criteria_for(phase)
        if c.id != first.id)


# --- programme status -----------------------------------------------------

def test_the_programme_is_at_the_first_gate_that_does_not_pass():
    """Not the one anybody says it is at.

    A board whose feasibility gate never closed is at feasibility
    however much layout has been done, and the whole value of the model
    is being willing to say so.
    """
    evidence = _pass_everything(Phase.FEASIBILITY)
    state = programme_status(evidence, now=NOW)
    assert state["phase"] == Phase.PROTOTYPE.value
    assert not state["complete"]


def test_work_done_out_of_order_does_not_advance_the_phase():
    state = programme_status(_pass_everything(Phase.PVT), now=NOW)
    assert state["phase"] == Phase.FEASIBILITY.value


def test_passing_every_gate_reaches_mass_production():
    evidence = [e for phase in npi.GATED_PHASES
                for e in _pass_everything(phase)]
    state = programme_status(evidence, now=NOW)
    assert state["phase"] == Phase.MP.value and state["complete"]


# --- the durable record ---------------------------------------------------

def test_a_record_round_trips(tmp_path):
    record = NpiRecord(tmp_path / "prog.jsonl")
    record.record("proto.drc", PASS, by="pcb_run_drc",
                  note="0 violations, 0 unrouted", revision="A.2", now=NOW)
    replayed = record.replay()
    assert len(replayed) == 1
    assert replayed[0].criterion_id == "proto.drc"
    assert replayed[0].note == "0 violations, 0 unrouted"
    assert replayed[0].revision == "A.2"


def test_the_log_appends_rather_than_replaces(tmp_path):
    """History is the point: a waiver granted in March must still be
    visible beside the one that replaced it."""
    record = NpiRecord(tmp_path / "prog.jsonl")
    record.record("proto.drc", FAIL, note="3 violations", now=NOW)
    record.record("proto.drc", PASS, note="fixed", now=NOW)
    assert [e.verdict for e in record.replay()] == [FAIL, PASS]


def test_a_waiver_without_a_reason_is_refused(tmp_path):
    """An unexplained waiver and a skipped check are indistinguishable
    six months later, which is exactly what a gate exists to prevent."""
    record = NpiRecord(tmp_path / "prog.jsonl")
    with pytest.raises(ValueError, match="note"):
        record.record("proto.drc", WAIVED, note="   ", now=NOW)
    with pytest.raises(ValueError, match="note"):
        record.record("proto.drc", WAIVED, now=NOW)
    # The same verdict with a reason is accepted.
    record.record("proto.drc", WAIVED, note="no copper on this revision",
                  now=NOW)
    assert record.replay()[0].verdict == WAIVED


def test_an_unknown_criterion_or_verdict_is_refused(tmp_path):
    record = NpiRecord(tmp_path / "prog.jsonl")
    with pytest.raises(ValueError, match="unknown criterion"):
        record.record("proto.invented", PASS, now=NOW)
    with pytest.raises(ValueError, match="verdict"):
        record.record("proto.drc", "probably", now=NOW)
    assert record.replay() == []


def test_a_torn_line_costs_only_itself(tmp_path):
    """A crash mid-write must not cost the programme's history."""
    path = tmp_path / "prog.jsonl"
    record = NpiRecord(path)
    record.record("proto.drc", PASS, now=NOW)
    record.record("proto.bom", PASS, now=NOW)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"criterion_id": "proto.erc", "verd\n')
    record.record("proto.erc", PASS, now=NOW)

    replayed = record.replay()
    assert [e.criterion_id for e in replayed] == [
        "proto.drc", "proto.bom", "proto.erc"]


def test_replaying_a_log_that_does_not_exist_yet_is_empty(tmp_path):
    assert NpiRecord(tmp_path / "never-written.jsonl").replay() == []


# --- the MCP surface ------------------------------------------------------

@pytest.fixture
def npi_tools(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from eda_agent.config import configure
    from eda_agent.tools.npi import register_npi_tools

    configure(workspace_dir=tmp_path)
    mcp = FastMCP("test")
    register_npi_tools(mcp)

    def call(name, **kwargs):
        result = asyncio.run(mcp.call_tool(name, kwargs))
        payload = result[0][0] if isinstance(result, tuple) else result[0]
        return json.loads(payload.text)

    return call


def test_the_gate_tool_reports_without_running_anything(npi_tools):
    gate = npi_tools("design_npi_gate", phase="evt", revision="A.2")
    assert gate["phase"] == "evt"
    assert gate["passed"] is False
    assert gate["next_tools"]
    assert gate["awaiting_attestation"]
    ids = [c["id"] for c in gate["criteria"]]
    assert ids == [c.id for c in criteria_for(Phase.EVT)]
    assert all("why_this_phase" in c for c in gate["criteria"])


def test_the_rationale_can_be_left_out_of_the_reply(npi_tools):
    """It is most of the payload, and a client that has read it once
    does not need it on every poll."""
    gate = npi_tools("design_npi_gate", phase="evt", include_rationale=False)
    assert all("why_this_phase" not in c for c in gate["criteria"])


def test_an_unknown_phase_is_an_error_not_an_empty_gate(npi_tools):
    """An empty gate would read as "nothing required", which is the most
    dangerous possible answer to a misspelled phase."""
    bad = npi_tools("design_npi_gate", phase="qualification")
    assert "error" in bad and "feasibility" in bad["error"]


def test_recording_through_the_tool_moves_the_gate(npi_tools):
    before = npi_tools("design_npi_gate", phase="evt")
    npi_tools("design_npi_record", criterion_id="evt.net_classes",
              verdict="pass", by="pcb_get_net_classes",
              note="214 nets, all classed")
    after = npi_tools("design_npi_gate", phase="evt")
    assert "evt.net_classes" in before["blockers"]
    assert "evt.net_classes" not in after["blockers"]


def test_the_tool_refuses_a_bare_waiver_and_an_unknown_criterion(npi_tools):
    refused = npi_tools("design_npi_record", criterion_id="evt.thermal",
                        verdict="waived")
    assert "error" in refused
    unknown = npi_tools("design_npi_record", criterion_id="evt.invented",
                        verdict="pass")
    assert "error" in unknown and "hint" in unknown


def test_discounted_evidence_says_why_it_is_not_counting(npi_tools):
    """Silently reverting a recorded pass to pending is what makes a
    gate untrustworthy. It has to say which revision it was measured on."""
    npi_tools("design_npi_record", criterion_id="evt.net_classes",
              verdict="pass", note="all classed", revision="A.1")
    gate = npi_tools("design_npi_gate", phase="evt", revision="B.0")
    entry = next(c for c in gate["criteria"] if c["id"] == "evt.net_classes")
    assert entry["verdict"] == PENDING
    assert "A.1" in entry["discounted"]


def test_status_names_the_first_open_gate(npi_tools):
    status = npi_tools("design_npi_status")
    assert status["phase"] == "feasibility"
    assert status["blockers"]
    assert [g["phase"] for g in status["gates"]] == [
        p.value for p in npi.GATED_PHASES]


def test_programmes_do_not_share_a_record(npi_tools):
    """Two boards in flight at once is the normal case, not the exception."""
    npi_tools("design_npi_record", criterion_id="feas.bom_cost",
              verdict="pass", note="within target", programme="board-a")
    other = npi_tools("design_npi_gate", phase="feasibility",
                      programme="board-b")
    assert "feas.bom_cost" in other["blockers"]


def test_a_programme_name_cannot_escape_the_workspace(npi_tools, tmp_path):
    """The name reaches a file path, and it arrives from a tool argument."""
    npi_tools("design_npi_record", criterion_id="feas.bom_cost",
              verdict="pass", note="ok", programme="../../escaped")
    written = list((tmp_path / "npi").glob("*.jsonl"))
    assert len(written) == 1
    assert written[0].parent == tmp_path / "npi"
    assert ".." not in written[0].name and "/" not in written[0].name
