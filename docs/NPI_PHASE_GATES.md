# NPI phase gates

Feasibility, prototype, EVT, DVT, PVT - as a gate the server can hold
you to, rather than a slide nobody opens after kickoff.

## The model

A PCBA programme passes through gates, and each gate asks a different
question:

| Gate | The question | What failing it costs |
| --- | --- | --- |
| **Feasibility** | Can this be built at all, at this cost, from parts that exist? | Nothing yet. That is the point of asking here. |
| **Prototype** | Does a board exist and come up? | A build, and the bring-up days around it. |
| **EVT** | Does the design meet its electrical specification? | A respin. |
| **DVT** | Does it meet *every* specification, including environmental, regulatory and sourcing? | A respin plus a chamber booking. |
| **PVT** | Can the *process* build it repeatably at volume? | The line, and the launch date. |

Mass production is not a gate. It is where a programme arrives by
passing PVT.

Running them out of order is the common and expensive mistake. A board
that passes DVT on a design whose regulator went end-of-life during EVT
has validated the wrong thing, thoroughly.

## The tools

```
design_npi_gate     what this gate asks, and where it stands
design_npi_record   answer one criterion, with evidence
design_npi_status   which gate the programme is actually at
```

### `design_npi_gate(phase, revision="", programme="default")`

Returns every criterion of that gate with its current verdict, the
tools that answer the automatic ones, and the criteria that need a
person.

It does **not** run the checks. That keeps three things true: it works
with Altium closed, it never hides which evidence is fresh, and a
client can see what it is about to spend thirty seconds on before it
does. The driving loop is the client's, the same way
`design_next_action` works.

```
design_npi_gate(phase="EVT", revision="A.2")
-> passed: false
   blockers: [evt.prototype_holds, evt.impedance, evt.diffpairs, ...]
   next_tools: [pcb_get_net_classes, design_synthesize_rules,
                pcb_get_impedance_profiles, ...]
   awaiting_attestation: [evt.test_results, evt.issue_list]
```

`next_tools` is for the blocking automatic checks only.
`awaiting_attestation` is deliberately separate: those need a human,
and telling a client to keep calling tools at them is how a loop spins.

### `design_npi_record(criterion_id, verdict, note, by, revision)`

`verdict` is `pass`, `fail`, `waived` or `pending`. `note` should carry
the evidence itself - the number measured, the violation count, the
document reference - not just a restatement of the verdict.

```
design_npi_record(
    criterion_id="proto.drc", verdict="pass", by="pcb_run_drc",
    note="0 violations, 0 unrouted connections", revision="A.2")
```

The log is append-only. Recording again supersedes without erasing, so
a waiver granted in March is still visible beside the one that replaced
it.

### `design_npi_status(revision="", programme="default")`

The current phase is **the first gate that does not pass**, not the one
anybody says the programme is in. A board whose feasibility gate never
closed is at feasibility however much layout has been done, and saying
so is the only useful thing this tool can do.

## Three rules that make the gate hard to pass by accident

**Evidence expires.** Each criterion carries a `stale_after_days`;
past it, a recorded pass reverts to pending. A DRC run from four months
ago is not a check on this board. Facts about the programme rather than
the board - a captured requirement, say - never expire.

**Evidence belongs to a revision.** Pass `revision` and anything
recorded against a different one stops counting, and the reply says
which revision it came from. Silently reverting a recorded pass to
pending is what makes a gate untrustworthy.

**A waiver needs a reason.** Waiving is a legitimate decision and it
passes the gate. What it cannot be is a quiet skip, so a waiver with no
note is refused: six months later an unexplained waiver and a forgotten
check look identical.

## What the criteria are

42 criteria across the five gates, 29 of them answerable by tools this
server already has:

| Gate | Criteria | Automatic |
| --- | ---: | ---: |
| Feasibility | 8 | 4 |
| Prototype | 8 | 6 |
| EVT | 9 | 7 |
| DVT | 9 | 6 |
| PVT | 8 | 6 |

Each one records what it asks, what passing looks like, and **why it
belongs to that gate rather than a later one** - which is the whole
argument for the phase order. Call `design_npi_gate` to read them, or
`include_rationale=false` once you have.

The split between automatic and manual is roughly half and half, and
pretending otherwise would be the one failure mode that makes a gate
worse than no gate. Pilot yield, EMC pre-scan results, enclosure
sign-off and a bring-up plan are not things a server can know.

A criterion that names a tool names one that exists: a test asserts
every reference against the live tool registry. The first draft named
nineteen plausible, consistently spelled, entirely invented tools.

## Running several programmes

`programme` keys the evidence log, so two boards in flight at once do
not share one. Logs live at `<workspace>/npi/<programme>.jsonl`, one
JSON object per line, in the same arrangement the design session
journal uses.

## Extending it

The criteria are data in `src/eda_agent/design/npi.py`. Adding one is a
`Criterion(...)` entry with an id, a rationale, a pass condition, and
either the tools that answer it or `kind=MANUAL`. The tests enforce
that an automatic criterion names tools, a manual one does not, every
id is unique and names its phase, and every tool reference resolves.
