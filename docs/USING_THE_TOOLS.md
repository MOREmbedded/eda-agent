# Using the tools

How to find the right tool among several hundred, how to keep the tool list small for clients that cap it, and the larger workflows built on top of them.

## Finding the right tool

Tools are grouped by the **document** they act on, and mixing them up is the
most common mistake: `lib_` acts on a `.PcbLib` or `.SchLib`, `pcb_` on an open
`.PcbDoc`, `sch_` on an open `.SchDoc`, and `obj_` on either, by the object
type it is given (`eTrackObject` acts on the active board). A board tool aimed at a
library does not reliably fail. With no board focused it can resolve some other
open board and report success for work you never asked for.

Two tools answer different questions about the surface:

- `tool_catalog` - search by name, category, maturity or interaction. Use it
  when you know roughly what the operation is called.
- `tool_guide` - ask in plain words what you are trying to do. It answers the
  tool and what it needs first, the tool you were probably reaching for and why
  it acts on a different document, and the short list of things that are
  genuinely impossible with the reason.

That last answer is the one worth knowing about. Without it there is no way to
tell "you missed it" from "it does not exist", so the same dead ends get
investigated repeatedly. An empty result from `tool_guide` means the guide has
nothing on the subject, not that the server cannot do it.

The same split is stated in the server instructions your MCP client receives at
startup, so a client that reads them starts out knowing it.

## Tool count (clients that cap it)

Some MCP clients limit how many tools a server may expose, or serialize every
schema into the model context at startup and slow noticeably. This server
registers several hundred. Set `EDA_AGENT_TOOLSET=minimal` (or pass
`--toolset minimal`) to advertise just two:

- `tool_catalog` - find an operation by category, maturity, interaction or name,
  and get its parameters with `with_schema=True`.
- `tool_invoke` - run any tool by name with an arguments dict.

Every other tool stays registered and reachable through that pair; only the
advertised list shrinks, from several hundred to two.

```bash
claude mcp add -s user altium -e EDA_AGENT_TOOLSET=minimal eda-agent
```

Only families of tools that differ by a mode are advertised as one tool each:
the calculators (`pcb_calc`), the audits (`audit`), the dimension and 3D body
kinds, the menu runners, the dialog drivers and the checkpoints. Each takes a
`kind` and that kind's arguments. Every member keeps its own name,
description and schema, which `tool_catalog` returns and `tool_invoke` runs.
Everything else keeps a tool of its own; collapsing whole areas into
`pcb(action=...)` style entry points would lose the per-tool descriptions a
model finds the right operation by.

The trade-off: in `minimal` the model no longer sees
tool schemas up front, so it must discover before it can act, and an argument
mistake surfaces as the target tool's own error rather than a schema
validation message. Call `tool_catalog(query=..., with_schema=True)` to get a
tool's parameters and required list before invoking it, rather than guessing
argument names - some are not what they look like (`current_amps`, not
`current_a`), and the same tool can differ between backends. Prefer `full`
(the default) unless your client forces otherwise.

## EDA backends

One tool surface, chosen at startup by `EDA_AGENT_BACKEND` (or `--backend`).

| Backend | Reached through | Status |
|---|---|---|
| `altium` | a persistent DelphiScript bridge | default, most complete |
| `kicad` | KiCad's IPC API and `kicad-cli` | optional |
| `easyeda` | a browser extension you import into EasyEDA Pro | optional |

The EasyEDA connection runs the other way round: the editor dials out to
this server, so nothing here can start it or make it connect.

Full detail, including what differs between them and what each one
cannot do, is in [EDA backends](BACKENDS.md).

## Design modes

A design run does not have to be the whole spec-to-board pipeline. A
mode picks the stages, adds the rules for that kind of job and names the
inputs it needs: `reference_redraw` redraws a vendor dev kit's schematic
faithfully in your own libraries, `devkit_derivative` builds a new board
around a dev kit's proven core, `reusable_block` designs one
self-contained block with a defined interface and a layout fragment to
publish as an Altium reuse block, `schematic_only` and `layout_only` do
half the pipeline each, `full_design` all of it. Start a run with
`design_session_start(requirement, mode="reference_redraw",
inputs={...})`, pick one from the client's prompt list (`mode_<name>`),
or select a default with `eda-agent mode-use`. Your own modes go in the
owner profile's `modes/`. See [Design modes](DESIGN_MODES.md).

## NPI phase gates

Feasibility, research, prototype, EVT, DVT and PVT as gates the server holds you
to, rather than a slide nobody opens after kickoff.

**Off until you ask for them.** Most boards are not run under a formal
NPI process, and one that is not should not be carrying three tools and
a stage vocabulary through every prompt to be told so. Turn on the
stages you actually run:

```bash
eda-agent phases-enable evt dvt pvt
```

```
design_phase_gate      what one stage asks, and where each check stands
design_phase_answer    record the outcome of one check, with its source
design_phase_standing  which stage the product is actually in
```

`phases-show` says which stages are in force and which setting decided
it; `phases-disable` turns them off again and keeps every answer already
recorded. The setting lives in the [owner profile](PROFILES.md),
because which gates a programme runs is a house rule rather than a
property of the board in front of you, and `EDA_AGENT_PHASES` or
`--phases` overrules it for one run.

47 checks across the six stages, 32 of them answered by tools this
server already has: ERC and DRC results, footprint audits against the
datasheet, impedance profiles, diff-pair matching, fab-profile DFM,
part lifecycle, fiducials. The rest need a person, and say so
(`answered_by: people`): pilot yield, EMC pre-scan and enclosure sign-off
are not things a server can know. Each check records what counts as done
and **why it belongs to that stage rather than a later one**.

The gate reports; it does not run the checks. Each check names the tools
that answer it, the client runs them and writes the outcome back, so the
loop stays the client's and nothing happens that you did not ask for.

**In the dashboard**, the Workspace view's Settings card switches the
gates on, and a *Project stages* card draws every tracked `.PrjPcb` as
a stepper from Feasibility to Production, with each gate's checklist a
click away. See [NPI phase gates](NPI_PHASE_GATES.md).

Three rules make it hard to pass by accident: an answer expires, an
answer belongs to a board revision and stops counting against another
one, and a waiver without a reason is refused. The current stage is the
first one that does not pass: a board whose feasibility never closed is
at feasibility however much layout has been done.

See [NPI phase gates](NPI_PHASE_GATES.md).

## Part sourcing

`part_search` queries every enabled provider and merges the results, each
hit attributed to the source that found it; `part_fetch` then pulls one
part's detail from a provider you name. **No provider is enabled by
default**, and there is no fallback order, so a result always names its
source.

`part_find_replacements` judges substitutes for a part you already
have. Every parameter is compared with the direction a designer would
apply - more voltage rating is safe, more ESR is not, a narrower
temperature range is not, a different dielectric is not - alongside
lifecycle and what is in stock.

It will not tell you two parts are equivalent, and that is deliberate.
It reports evidence per attribute and names four things it did **not**
check: product-change notices and errata (no distributor API serves
them), pad geometry (the package *name* is compared, never the land
pattern - use `lib_audit_footprint_vs_datasheet` for that), anything
the catalogues do not publish, and any attribute the candidate simply
does not state. An attribute nobody checked is reported as unchecked,
never as a pass.

Two tools sit alongside them: `part_parse_lcsc_label` decodes the QR
payload on an LCSC reel or bag into part number, MPN and quantity, and
`part_cache_datasheet` keeps a datasheet PDF on disk with its URL,
SHA-256 and fetch time, so a value can still be checked against the
document after the conversation that found it.

The providers, their credentials and their access policies are in
[Part sourcing](PART_SOURCING.md).

## Altium 365, company policies, simulators

**Altium 365.** The `altium365_` tools read workspaces, projects and the
managed library, and can publish, release and change parameters (only with
`confirm=True`). Two routes. Nexar works on every plan: put your Nexar
app's client id in `EDA_AGENT_NEXAR_APP_ID` (and, for an app made in the
Nexar portal, its secret in `EDA_AGENT_NEXAR_CLIENT_SECRET`), then run
`eda-agent altium365-login` once (`eda-agent altium365-logout` forgets it).
The login lists every workspace the account can reach; each tool takes the
workspace URL it acts on. The workspace's own API, with
`EDA_AGENT_ALTIUM365_DOMAIN` and a workspace token in
`EDA_AGENT_ALTIUM365_TOKEN`, needs a plan whose admin pages offer API
tokens; many plans do not (Admin > Apps lists only third-party
integrations), and then Nexar is the way in.
`EDA_AGENT_ALTIUM365_WORKSPACES` limits the tools to the workspaces it
lists. Set these with `setx` in your own terminal (never paste a secret
into a chat); the tools also read your saved user environment, so no
restart is needed after `setx`. `altium365_check_setup` says which route
is set up.

`altium365_scan_library` copies a workspace's managed library into the
active profile (`<profile>/library/<workspace host>/`, JSON and CSV),
reading past the 10,000-row `totalCount` cap with several readers, and
continues an interrupted scan. `altium365_update_library` refreshes it in
a minute or two, `altium365_search_library` and `lib_search` search it
offline, and `altium365_library_report` reports gaps and, given your
design folders, placements of an older revision. Apart from this copy,
which you ask for, nothing Altium 365 returns is written to disk.

**Company policies** are your files in `EDA_AGENT_POLICY_DIR`: a
`naming.json` (a `template` and `pattern` per footprint, symbol and
component) for `lib_check_naming` and `lib_make_name`, and rules files in
`rules/*.json` for `design_check_company_rules`. With none set, nothing is
imposed.

**Simulators.** `design_simulate_deck` runs a deck in ngspice when it is
installed, or in LTspice when you ask for it or it is the only one
installed. `design_simulators` lists what is installed.

## Measuring performance

Two logs in the workspace time every call, from opposite ends:

- **`activity.log`** is written by the Pascal side around the handler,
  so it measures work done **inside Altium**.
- **`bridge_trace.log`** is written by Python, so it measures the
  **round trip the caller experienced** - per request id, with poll
  counts, deadline extensions and timeouts.

Every built-in view (the in-Altium Perf tab, the dashboard table, its
p95 sparkline) reads the first one. `eda-agent perf` joins them on the
request id, so the gap between the two becomes visible for the first
time: that gap is the IPC cost plus any time the call spent **queued**,
since Altium answers one request at a time.

```
                  p50      p95      max
  round trip     1777    90043   145529
  in Altium        31   106469   145485
  overhead        187     4992     5008
```

Read it this way: `in Altium` is the handler's own cost - a slow one is
a slow handler, and batching or caching is the fix. `overhead` is
everything else, and a large value on a *fast* handler usually means
the call waited behind a slow one rather than that the IPC is slow.
The report also counts timeouts, deadline extensions, and any call that
was blocked by a modal dialog, naming the dialog.

`--json` emits the same data for scripting. Nothing is sent anywhere;
it reads two local files.
