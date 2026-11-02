# NPI phase gates

Feasibility, prototype, EVT, DVT, PVT - as a gate the server can hold
you to, rather than a slide nobody opens after kickoff.

## Switching it on

**The gate is off out of the box.** Most boards are not run under a
formal NPI process, and one that is not should not be carrying three
tools and a stage vocabulary through every prompt to be told so.

```bash
eda-agent phases-enable evt dvt pvt
```

```
eda-agent phases-show       which stages are in force, and what decided it
eda-agent phases-enable     run the named stages, or all five if none named
eda-agent phases-disable    stop running them
```

Enable the stages you actually run, not all five out of tidiness: a
stage that is on and never answered holds the product at it forever,
which is how a gate stops being read.

The setting is one key in the [owner profile](PROFILES.md):

```json
{ "stages": ["evt", "dvt", "pvt"] }
```

in `<profile>/phase_gates.json`. It lives there because which gates a
programme runs is a house rule - it follows the company, not the board
in front of you - and because a project-local profile commits it with
the project, so a team shares one answer.

Two ways to overrule it for one run, both naming stages the same way:

| | |
| --- | --- |
| `eda-agent --phases evt,dvt,pvt` | a flag, for trying one on |
| `EDA_AGENT_PHASES=evt,dvt,pvt` | an environment variable, for an MCP client with an `env` block and no profile directory |

`none` is a value, and it has to be: an empty variable means "the profile
decides", so without the word there would be no way to turn the gate off
over a profile that turned it on.

**Off means not advertised.** The three tools below are not registered
at all, rather than registered and refusing - a tool that exists only to
say it is switched off still costs a call and a round trip to teach what
its absence teaches for free, and reads to a model as a capability that
is there. `tool_guide` withholds the recipe for the same reason.

**The tool surface is chosen at startup**, as with `--backend` and
`--toolset`, so changing the setting takes a restart.

**Enabling some stages and not others is a weaker claim**, and the reply
says so. `design_phase_standing` reviews only the stages in force and
reports them in `stages_run`; `in_production` means every one of *those*
cleared. A stage that is off is refused by `design_phase_gate` rather
than answered, and an answer recorded against one is refused too -
evidence nothing reviews is worse than no evidence.

**Switching stages off never touches the ledger.** The answers stay on
file and count again the moment the stages do.

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
stage that does not clear. Only the stages in force are reviewed, and
`stages_run` names them, so `in_production` can be read against what it
actually rests on.

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

A check whose stage nobody runs is still worth adding: it costs nothing
until someone enables that stage, and the argument for *why* it belongs
to that stage is the part that is hard to reconstruct later.

This fork had its own implementation of the same gates (`design_npi_*`,
`src/eda_agent/design/npi.py`) until 2026-11-01. Upstream's was the same
42 checks split the same way, rewritten, with expiry per check; it is
kept because it is the one being developed. See CHANGELOG.
