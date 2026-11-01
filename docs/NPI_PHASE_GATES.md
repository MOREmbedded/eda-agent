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
design_phase_gate      what one stage asks, and where each check stands
design_phase_answer    record the outcome of one check, with its source
design_phase_standing  which stage the product is actually in
```

### `design_phase_gate(stage, revision="", product="default", with_reasons=True)`

Every check of one stage with its outcome so far. `stage` takes the ways
people write it: `"EVT"`, `"design validation"`, `"proto"`. Each check
carries `asks`, `done_when`, `answered_by` (`tools` or `people`) and, for
the former, the `tools` that answer it, written the way they are called
(a folded tool appears as its family call). `with_reasons=False` drops
`why_now` once you have read it.

### `design_phase_answer(key, outcome, detail="", source="", revision="", product="default")`

Records one outcome: `met`, `unmet`, `waived` or `open`. `key` is the
check's key, such as `evt:return-paths`. Name in `source` what the outcome
rests on: a tool's result, a report, a person.

### `design_phase_standing(revision="", product="default")`

The stage the product is in and what that stage still needs: the first
stage that does not clear.

## Three rules that make the gate hard to pass by accident

**An answer expires.** A check carries `good_for_days`; past it, a
recorded answer stops counting and the review says why. A DRC run from
four months ago is not a check on this board. The one fact about the
product rather than the board, a closed specification, never expires.

**An answer belongs to a revision.** Pass `revision`, and anything
recorded against a different one stops counting, with the reply naming
the revision it came from rather than quietly reverting it.

**A waiver needs a reason.** Waiving is a legitimate decision and it
clears the check. What it cannot be is a quiet skip, so a waiver with no
`detail` is refused: six months later an unexplained waiver and a
forgotten check look identical.

## What the checks are

42 checks across the five stages, 29 of them answered by tools this
server already has:

| Stage | Checks | Answered by tools |
| --- | ---: | ---: |
| Feasibility | 8 | 4 |
| Prototype | 8 | 6 |
| EVT | 9 | 7 |
| DVT | 9 | 6 |
| PVT | 8 | 6 |

Each one records what it asks, what counts as done, and **why it belongs
to that stage rather than a later one**, which is the whole argument for
the order. Pilot yield, EMC pre-scan results, enclosure sign-off and a
bring-up plan are not things a server can know, and those checks say so.

A check that names a tool names one that exists: a test resolves every
reference against the registered tools, folded family members included.

## Several products

`product` keys the ledger, so two boards in flight do not share one.
Ledgers live at `<workspace>/phase_gates/<product>.jsonl`, one JSON
object per line.

## Extending it

The checks are data in `src/eda_agent/design/phase_gates.py`. Adding one
is a `Check(...)` entry with a key that names its stage, the question it
asks, why it is asked now, what counts as done, how long an answer stays
good, and the tools that answer it if any.

This fork had its own implementation of the same gates (`design_npi_*`,
`src/eda_agent/design/npi.py`) until 2026-11-01. Upstream's was the same
42 checks split the same way, rewritten, with expiry per check; it is
kept because it is the one being developed. See CHANGELOG.
