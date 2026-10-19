# eda-agent

MCP server that lets an AI (or any MCP-compatible client) **interact with a live Altium Designer session**, with KiCad and EasyEDA Pro available as additional backends. It exposes around 590 tools on Altium, covering schematic, PCB, library, project, and design-agent operations, over a persistent DelphiScript bridge. The AI reads the design you currently have open, asks questions about it, and can modify it in place while you watch. The [backend](#eda-backends) is selected at startup, so each user sees only their own tool set.

> **⚠️ Experimental.** Not all tools are extensively tested. Some can crash the Altium DelphiScript engine. See [Known limitations](#known-limitations) before using on any design you haven't backed up.

**Getting started:** [Requirements](#requirements) → [Installation](#installation) → [Check that it worked](#check-that-it-worked) → [Workspace and profiles](#workspace-and-profiles)

<details>
<summary><b>Full contents</b></summary>

- [Demo](#demo)
- [Dashboard](#dashboard)
- [How it works](#how-it-works)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation) - including [Altium-side scripts](#altium-side-scripts) and [Check that it worked](#check-that-it-worked)
- [Workspace and profiles](#workspace-and-profiles) - what lives where, and how to set your house style
- [Fab capabilities, stackups and laminates](#fab-capabilities-stackups-and-laminates) - JLCPCB and PCBWay, dated and sourced
- [Calculators](#calculators) - spacing, fusing, vias, impedance and the rest, on the CLI too
- [EDA backends](#eda-backends)
- [Finding the right tool](#finding-the-right-tool)
- [Tool count (clients that cap it)](#tool-count-clients-that-cap-it)
- [Checking the fab package](#checking-the-fab-package) - verify what you are about to send
- [Measuring performance](#measuring-performance) - where call time actually goes
- [Part sourcing](#part-sourcing)
- [Example use cases](#example-use-cases)
- [Known limitations](#known-limitations)
- [What the bundled data does not cover](#what-the-bundled-data-does-not-cover) - the figures nobody publishes, and why they are null - read this before trusting it with a real design
- [Timeout and server lifecycle](#timeout-and-server-lifecycle)
- [Tool reference](#tool-reference) - and the other documents
- [Architecture](#architecture)
- [CLI](#cli)
- [Configuration](#configuration)
- [Development](#development)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [License](#license)
- [Disclaimer](#disclaimer)

</details>

## Demo

Claude Code reviewing a buck converter through eda-agent. The feedback resistor divider on this schematic is intentionally wrong; Claude catches it among other recommendations.

[![eda-agent demo: Claude Code reviewing a buck converter](https://img.youtube.com/vi/snRyCx3OlxM/maxresdefault.jpg)](https://youtu.be/snRyCx3OlxM)

## Dashboard

<img src="assets/dashboard.png" alt="eda-agent dashboard inside Altium Designer" width="320">

Two dashboards ship with eda-agent:

- **In-Altium status window** - a floating Altium-side window showing live status, request count, cumulative Altium-side time, auto-shutdown countdown, and a per-command log with durations. `Hide pings` filters the 30 s keep-alive traffic; `Only >100ms` isolates slow calls. The **Detach** button exits the polling loop cleanly; it saves nothing, so unsaved edits stay in the editor.
- **Web dashboard** - a local browser dashboard at `http://127.0.0.1:8766`, focused on design review. A **Review** tab surfaces datasheet / MPN / manufacturer / footprint coverage gauges and an actionable issue queue (missing datasheet, missing MPN, orphan nets, ...); **Project**, **Components**, **Nets**, **Libraries** and **Plan** tabs give live structured views. Click any component or net to drill into a detail drawer; one click cross-probes it into Altium. Light / dark theme, server-sent-events live feed. It is auto-started by the MCP server - the **Open Dashboard** button on the in-Altium status window launches the browser.

## How it works

- Altium Designer stays open and in full control of your design
- A DelphiScript polling loop runs inside Altium's scripting engine
- `eda-agent` (Python, launched by your MCP client) sends commands via file-based IPC
- Altium executes, writes a response, and returns to polling
- You see the changes happen live in Altium

This is **not** a batch tool that opens a project, runs a script, and exits. It's a live connection for as long as you want it (conversational design review, guided refactoring, ad-hoc BOM queries, "what nets does this resistor connect to?"), all on the project you currently have open.

## Features

- **~590 tools on the default Altium backend** (670+ with both registered) across application, project, library, schematic/general, PCB, and design-agent categories
- **Generic primitives** (`obj_query`, `obj_modify`, `obj_create`, `obj_delete`, `run_process`) that work on almost any schematic or PCB object type via late-binding, avoiding per-type handler proliferation
- **Bulk batch primitives**: `obj_batch_modify`, `obj_batch_create`, `obj_batch_delete`, `pcb_place_tracks`, `pcb_move_components`, `sch_place_wires`, `place_net_labels`, `place_power_ports`, `sch_place_components`, `sch_set_components_parameters`, `get_sch_doc_pins`, `lib_add_pins`, `proj_get_connectivity_many`, `sim_attach_primitives`. Collapse N LLM turns + N IPC round-trips into one. Typical wall-time savings: 10 to 100x on multi-item edits
- **Design review snapshot**: `design_review_snapshot` bundles 8 to 12 review reads (project info, components, nets, rules, diff, messages, stats, unrouted, BOM) into a single call. One LLM turn instead of a dozen
- **Design-lint sweep**: `design_lint_report` runs 31 audit checks in one IPC pass and returns a structured violation list - schematic-side (component-parameter visibility per class, power-port orientation, floating ports, multi-output / no-driver nets, duplicate designators, off-grid components) and PCB-side (DNP variant components, tented-via ratio, near-miss track endpoints, signal vias without nearby return via, via antennas, removed pad shapes, components outside outline, pads too close to board edge, invalid polygon regions, optional DRC). Each check is also exposed as a standalone `audit_*` MCP tool; the dashboard's Status → Health subtab has a one-click Lint panel that calls `/api/lint` and groups results by Schematic / PCB
- **Canonical circuit blocks**: `design_add_circuit_block` folds a whole block into a `DesignPlan` in one call, allocating refdes, wiring every pin to the right net and tagging power / ground and roles. Twelve of them: `decoupling`, `pullup`, `pulldown`, `series_resistor`, `voltage_divider`, `rc_lowpass`, `rc_highpass`, `led_indicator`, `crystal`, `pi_filter`, `mosfet_low_side`, `mosfet_high_side`. Naming-agnostic: you supply the part identities, it owns only the wiring pattern. `design_list_circuit_blocks` returns each one's parameter contract, so the planner never guesses a parameter name
- **Datasheet-first discipline**: every component-surfacing response (`pcb_get_components`, `proj_get_bom`, `proj_get_component_info`, `proj_find_component`, `lib_search`, `design_review_snapshot`, `sim_get_readiness`) carries a `_datasheet_guidance` block with per-part vendor search queries. `app_attach` / `app_ping` carry a `_system_reminder` so every MCP client that connects sees the rule at session start. LLM-fabricated datasheet values are forbidden; WebFetch/WebSearch are called out by name
- **Sch <-> PCB netlist crossref**: `crossref_net(net_name)` compares the schematic pin list against the PCB pad list for the same net. Catches ECO drift, stale post-fabrication routing, phantom nets from port/sheet-entry rename conflicts. `in_sync` flag + `sch_only` / `pcb_only` diff
- **SPICE simulation workflow**: `sim_get_readiness` audits every component and partitions into ready / needs-primitive / needs-file. `sim_attach_primitives` sets SpicePrefix + Value on passives. `sim_attach_model` links a vendor `.mdl` / `.ckt`. `sim_run` dispatches the simulator. Built-in guardrail: never fabricate a SPICE model file, fetch the vendor one
- **Focus-independent PCB access**: every PCB handler falls back to `GetPCBBoardByPath` when `GetCurrentPCBBoard` returns nil (user has a sch tab focused). No more misleading "No PCB document is active" when the PCB is right there
- **Fast and compile-cached**: the polling loop runs at a 10 ms interval in active mode, which bounds the bridge's own latency rather than describing what a call costs - measure yours with `eda-agent perf`, which joins the two timing logs and separates time spent inside Altium from time spent getting there. `SmartCompile` caches `DM_Compile` with a 2 s TTL so a multi-read review pays for one compile instead of a dozen. Explicit `proj_force_recompile` + `proj_get_compile_freshness` probes for cases that need a guaranteed-fresh netlist (e.g. after user edits)
- **Persistent polling loop**: one script start, then a 10 ms poll interval in active mode (30 ms idle), so the loop is not what a call waits on
- **Annotation runs silently**: `proj_annotate` designates components without popping the annotate dialog
- **Drives the GUI where there is no API**: a great deal of Altium exists only as menus and dialogs. `app_click_menu` walks the real menu bar by name (and lists what a menu holds, so paths are discovered rather than guessed); `app_list_open_dialogs` reports what is on screen, what it says and whether Altium is stuck; `app_set_dialog_control` sets checkboxes, fields and grid rows; `app_press_dialog_button` presses one button; `app_drive_dialogs` answers a whole sequence reactively. **None of it uses the bridge**, only Win32 and the accessible layer, so it keeps working while a modal has the scripting engine blocked, which is exactly when you need it. `app_run_ui_command` fires a menu command and answers its dialogs in the same call, because a command that opens a modal blocks the bridge and a second call would never arrive. The driver decides from what is on screen rather than from a script, stops on any dialog it cannot classify, and gates the press that changes the design behind `allow_commit`. Dialog text that Altium paints into handle-less controls is recovered by OCR and labelled as read from pixels, not text
- **Deferred save for speed**: mutations mark documents as modified in memory; disk writes happen on explicit `app_save_all`, and `app_detach` saves the documents that were edited. A clean document is never rewritten. Before this, every edit triggered a full project save, which dominated latency
- **Two dashboards**: an in-Altium floating status window (status, request count, per-command performance, command log, Detach button) and a browser-based **web dashboard** (`127.0.0.1:8766`) for design review - datasheet / MPN / footprint coverage gauges, an actionable issue queue, component / net drill-in, one-click cross-probe into Altium, light / dark theme. The whole project view loads in one bundled IPC round-trip (`project.dashboard_snapshot`); the web dashboard auto-starts with the MCP server
- **DelphiScript trap linter**: `scripts/altium/lint.py` (wired into `build.py`) scans the Pascal sources for known parser hazards - `Cardinal()` casts, malformed hex literals, empty `.Add('')` arguments, braces inside comments, fixed-size arrays as function locals, reserved-word identifiers - and fails the build before a bad deploy
- **Activity logs**: every command is appended to `workspace/activity.log` (CSV with timestamps, durations, command name, response size). The bridge also writes `bridge_trace.log` for IPC-level diagnostics. On the Python side, a rotating, size-capped, secret-redacting JSON-lines log (`%USERPROFILE%\EDA Agent\logs\eda_agent.log` by default) runs alongside the server's existing stderr logging - see [Configuration](#configuration)
- **Engineering tools**: IPC-7351B footprint calculation and library validation (offline and against a live SchLib/PcbLib pair), owner naming/rules policies, design-intent validation, LTspice batch runs, pre-ECO schematic/PCB diff, and read/write access to an Altium 365 workspace through Nexar. Full detail, including which parts are offline-tested versus unverified against live Altium/Nexar, is in [`docs/ENGINEERING_TOOLS.md`](docs/ENGINEERING_TOOLS.md)
- **Fab output verification**: `pcb_check_fab_output` reads an exported Gerber/Excellon package back and compares it against the live board - copper layer count vs the stackup, outline size, drill hits vs pads and vias, and minimum drill/feature against your fab's limits. Closes a loop that was open: eda-agent could write a fab package but never read one. See [Checking the fab package](#checking-the-fab-package)
- **Logo / artwork on the board**: `pcb_place_image` turns a PNG or BMP into PCB region primitives (silkscreen logo, QR code, artwork). The image is decomposed into non-overlapping rectangles rather than traced outlines, so nothing is approximated away from the source art and holes need no special handling. Plans by default (`dry_run=True`) and reports the primitive count first, because anti-aliased artwork can decompose into thousands of pieces, with a `max_regions` cap that a photograph will hit immediately. Verified on a live board: an asymmetric test glyph was placed and read back to confirm scale, origin corner and layer, and that the art is neither mirrored nor flipped
- **Headless library reads**: `lib_read_pcblib_headers` parses a `.PcbLib`'s footprint names, pad counts, heights and descriptions straight out of the OLE container with Altium closed (the `.SchLib` side already backs the `altium_local` provider and the offline review). Pad geometry stays a live read: it lives in Altium's binary primitive region, which this project does not reverse-engineer without ground truth
- **Owner profiles**: per-workspace house style (footprint-authoring defaults like pad corner rounding, design rules, fab/stackup profiles, naming conventions, schematic units/grid, project templates), auto-discovered from a project-local `.eda-agent/profile/` or a centrally-switched `eda-agent profile-use <name>`. See [docs/PROFILES.md](docs/PROFILES.md)
- **Bulk-tool nudge**: when a singular tool is hit 2 to 3 times in 10 s, the response carries a `_hint_bulk` field pointing at the batch variant. Clients that missed the bulk tool in the docstring learn about it at runtime
- **Design agent surface**: six MCP tools (`design_get_discipline`, `design_snapshot_inventory`, `design_validate_plan`, `design_execute_plan`, `design_audit_schematic`, `design_validate`) that let an MCP-client LLM produce a structured `DesignPlan` JSON, instantiate it on a fresh sheet (parts + wires + labels + rail glyphs), audit the result for layout problems, and validate ERC + connectivity. Datasheet-first, NDA-isolated by construction
- **Motif composer + canonical priors + Sugiyama placement**: three-layer placement strategy. (1) Sugiyama / force-directed gives every part a baseline position. (2) The motif composer detects canonical sub-circuits in the netlist (bypass cap, voltage divider, fb_divider, lc_output, ...) via VF2 subgraph isomorphism and splats each match into its frozen canonical layout - same data shape, IC-anchored or self-contained. (3) Canonical priors apply per-role-pair nudges (e.g. `vcc_decoup` sits 400 mils from its IC). A final overlap-shove pass repairs any collisions; sheet-edge clamping keeps every glyph and port within the page boundary. Role-compatibility filter drops false-positive motif matches (a structural rc-lowpass that's actually a decoupling cap stays out of the filter motif). Topology-agnostic - works for a buck, an LDO, an MCU, an audio amp, anything with a clean net graph
- **Within-block schematic wiring**: stub wires from each pin endpoint outward to the label / port (no more "floating net labels" ERC warnings), Manhattan routing between same-net pins for signal nets, **rail consolidation** clusters power / ground pins so one VCC bar or GND triangle serves many pins instead of stacking N glyphs. Obstacle-aware: every L-path picks the orientation that crosses fewest component bodies, using real `BoundingRectangle` data queried from Altium
- **Atomic-parts contract**: every existing-status Part must carry `mpn`, `footprint`, `datasheet_url`; the inventory snapshot exposes those fields per component; `design_validate` emits `atomic_parts` warnings when the contract is missed. Aligns with the KiCad Atomic / Digi-Key Library / atopile / JITX convention
- **Schematic audit**: `design_audit_schematic` returns structured `{overlaps, wire_crossings, stacked_ports}` for the active schematic - pairs of components whose bboxes intersect, wire segments crossing a non-endpoint component body (real Pascal-side `Vertex.*` + `BoundingRectangle.*` accessors), and clusters of 3+ rail glyphs of the same net. Each violation carries enough geometry for the planner to compute a corrective move. Programmatic feedback loop without needing a visual snapshot
- **Health and doctor preflight**: `eda-agent health` (offline checks: workspace dir, pointer file, bundled scripts) and `eda-agent doctor` (full preflight talking to Altium: process running, script polling responsive, version match, save_all canary, optional `--library` lib-path checks). `--json` for machine-readable output
- **pip-installable**: no admin, no installer, no touching Altium's config

## Requirements

- Python 3.11+
- An EDA tool, one of:
  - **Altium Designer** (recent versions, AD20+ preferred) - Windows only
  - **KiCad 9+** with the IPC API server enabled (Preferences → Plugins → KiCad API server), plus `pip install -e .[kicad]`
  - **EasyEDA Pro**, through a browser extension you build and import into the editor. Note the connection runs the other way round: the editor dials out to this server, so nothing here can start it

The server picks a backend at startup (`EDA_AGENT_BACKEND`, default `altium`), so one install drives any of them. See [EDA backends](#eda-backends).

## Installation

```bash
git clone https://github.com/salitronic/eda-agent
cd eda-agent
python -m pip install -e .
```

Use `python -m pip` rather than a bare `pip`. It works even when pip's `Scripts` folder is not on your `PATH`, which is common when Python was installed for you by IT, and it guarantees the package goes into the same Python you will run it with.

Register the server with your MCP client. The binary is `eda-agent` and runs on stdio; consult your client's docs for how to add a local stdio-based server. Everywhere this README says `eda-agent`, `python -m eda_agent` does exactly the same thing and does not depend on `PATH`.

### Claude Code

```bash
claude mcp add altium eda-agent
```

Adds `eda-agent` as an MCP server named `altium` to your Claude Code project config. Use `-s user` to register it at the user level (available across every project):

```bash
claude mcp add -s user altium eda-agent
```

If `eda-agent` isn't on your `PATH` ("'eda-agent' is not recognized"), register it through Python instead. This needs no path at all:

```bash
claude mcp add -s user altium -- python -m eda_agent
```

The `--` matters: it tells `claude mcp add` that everything after it is the command to run, rather than options for `claude` itself.

To verify the connection: `/mcp` in a Claude Code session should list `altium` as connected.

### Other MCP clients

The server speaks standard MCP over stdio; any client that accepts a local stdio command will work. Invoke `eda-agent` (or `eda-agent serve`) as the subprocess, or `python -m eda_agent` if it is not on your `PATH`.

### Altium-side scripts

Drop the Altium script project somewhere you can find it:

```bash
eda-agent install-scripts
```

or, if `eda-agent` is not recognised:

```bash
python -m eda_agent install-scripts
```

Default destination: `%USERPROFILE%\EDA Agent\scripts\`. Use `--dest PATH` to put it elsewhere.

Register the script as a Global Project in Altium (once):

1. **DXP → Preferences → Scripting System → Global Projects** → **Install from file**
2. Select the `Altium_API.PrjScr` you just installed

From then on, every Altium startup compiles the script project and the polling loop is one click away:

1. **File → Run Script...**
2. Expand `Altium_API` → `Dispatcher.pas`, select **StartMCPServer**, click **Run**

The polling loop starts and your MCP client can drive Altium.

### Keeping it up to date

```bash
eda-agent update
```

Checks **your own remote** - the one you push to - and the three other
things that go stale independently, then fixes the one it safely can:

| | |
|---|---|
| your remote | reported if you are behind; **not pulled** unless you pass `--pull`, and refused outright on a dirty tree |
| the installed package | an editable install needs no reinstall; a copied one does, and a `git pull` into it changes nothing |
| the deployed DelphiScript files | **redeployed automatically** when they differ |
| the copy Altium has **compiled** | reported, and left to you |

**Other remotes are not checked** unless you ask. A checkout can have
your fork, the project it was forked from and a colleague's copy all
configured at once, and reporting someone else's commits every run
makes the to-do list something nobody reads:

```bash
eda-agent update --from upstream
```

That fetch passes `--no-tags`. Fetching another remote normally imports
its **release tags** too, so a fork checking the project it came from
quietly acquires that project's version tags - which is how this repo
briefly ended up holding a `v0.7.0` it had never released. Comparing
commits needs no tags.

### Updating from a local folder

No git and no network needed - point it at another eda-agent folder on
disk, a copy on a share, or a release directory:

```bash
eda-agent update --from D:\builds\eda-agent-0.7.1
```

It reads that folder's package version and `SCRIPT_VERSION`, compares
both against what you have, and **deploys its Altium scripts**. It will
not `pip install` for you - that is a bigger change than copying a
script, so it prints the exact command instead. Both a checkout
(`src/eda_agent/`) and an unpacked package (`eda_agent/`) are accepted.

`--from` takes a folder or a git remote name; anything that exists on
disk is treated as a folder.

### What it will not do

A `.pas` on disk is not compiled until Altium reloads the script
project, and there is **no API to do that**. So `update` finishes by
telling you to reload, rather than quietly leaving you on an old build.

`--check` changes nothing, `--fetch` refreshes remote refs so "up to
date" is current rather than from your last fetch, and `--json` gives
the steps as data. Exit code is 0 when everything is current, 1 when
something needs you, 2 on an error - so CI can branch on it without
parsing text.

The Altium check is a plain ping. Unlike `doctor` it never saves a
document, so it is safe to run mid-edit.

### Check that it worked

Two commands answer "is this set up correctly", so you find out here
rather than from a failing tool call later:

```bash
eda-agent health
```

Offline and instant: is the workspace directory writable, does the
pointer file agree with it, are the bundled scripts findable. Run it
any time; it never touches Altium.

```bash
eda-agent doctor
```

The full check, with Altium open and `StartMCPServer` running: everything
`health` covers, plus that Altium is running, that the polling loop
answers, that the deployed script version matches this source tree, and
a save round-trip. Every line is a pass, a warning or a fix instruction.

The common first failure is `workspace pointer matches config` reporting
two different paths. That means Python resolved one workspace while the
script was told another, which the next section explains.

## Workspace and profiles

Two separate ideas that are easy to confuse, because both are "somewhere
on disk that affects how this behaves". One is plumbing, the other is
your house style.

### The workspace: how Python and Altium talk

Altium's DelphiScript cannot be called directly from outside, so the two
sides pass files in a shared folder. Python writes `request_<id>.json`,
the polling loop inside Altium picks it up, acts, and writes
`response_<id>.json` back. That folder is the **workspace**, and it is
pure plumbing: nothing about your design lives there.

- Default: `%USERPROFILE%\EDA Agent\workspace\`
- Override with the `EDA_AGENT_WORKSPACE` environment variable
- DelphiScript cannot read environment variables, so Python also writes
  the resolved path to `C:\ProgramData\eda-agent\workspace-path.txt`.
  That pointer file is how the script finds the same folder.

**Both sides must agree.** If you set `EDA_AGENT_WORKSPACE` for your MCP
client but not in the terminal you later run `doctor` from, the two
resolve different folders and commands appear to vanish: one side is
posting letters to an address nobody reads. `eda-agent health` reports
exactly this as `workspace pointer matches config`. Either set the
variable the same way everywhere, or leave it unset and use the default.

One workspace per machine is normal. It is not per project, and there is
no reason to have several unless you deliberately run more than one
Altium.

### Profiles: your conventions, per company or project

A **profile** is the opposite kind of thing: no plumbing, just the rules
you would otherwise repeat on every call. Pad corner rounding, courtyard
and silkscreen policy, naming conventions, design rules, fab stackups,
schematic units and grid, project templates.

The motivating case is working for several companies on one machine. One
wants 1% pad rounding, another wants Altium's 25%; stating that on every
call is how it ends up inconsistent. Put it in a profile and the
authoring tools read it themselves, so `lib_add_footprint_pad` with no
`corner_radius` does the right thing for whoever you are working for
right now.

A profile is found in this order, first match winning:

1. `EDA_AGENT_PROFILE_DIR`, if set
2. a project-local `.eda-agent/profile/`, found by walking up from the
   working directory. Meant to be **committed** to that project's repo,
   so a team shares one house style
3. the centrally active profile, chosen with `eda-agent profile-use <name>`
   from `~/.eda-agent/profiles/`
4. none, in which case every tool keeps its built-in default

The project-local path is relative to the working directory, which
reaches the CLI but usually not the MCP server, since your client
launches it from wherever it likes. **For an MCP setup, use
`profile-use` or the environment variable.** `eda-agent profile-show`
always reports what actually applies from where you are standing, and
`eda-agent profile-validate` checks a profile before you rely on it.

A profile can also carry **templates**, which is the one place where
"your conventions" turns into actual files. A whole starter project goes
in `templates/<name>/` and is copied by
`eda-agent profile-new-project <name> <dest>`; a single sheet or board
goes in `templates/documents/` and is created, and opened in Altium, by
the `profile_new_document` tool. Both are plain copies: Altium has no
scripting API that applies a template, so a saved `.SchDoc` set up the
way you like it *is* the template. This is also the only way to pin a
new board's units and grid, since nothing can set those on a live PCB
document.

Full detail, and the format of each file, is in
[`docs/PROFILES.md`](docs/PROFILES.md).

In short: the **workspace** is how the two halves of this server reach
each other and you mostly set it once and forget it; a **profile** is
what you want the output to look like, and is worth revisiting whenever
you change who you are working for.

## Checking the fab package

`proj_run_outjob` writes Gerbers, NC drill and pick-and-place. Until
recently nothing ever read them back, which left the one artifact that
actually gets manufactured write-only and unverified.

`pcb_check_fab_output` reads an exported folder and compares it against
the live board:

| Check | Against |
|---|---|
| copper layer count | the board stackup |
| outline width and height | `pcb_get_board_outline` |
| drill hit count | pads with holes, plus vias |
| smallest drill | a fab minimum you supply |
| smallest copper feature | a fab minimum you supply (warning only) |

`checks_run` in the response names what was actually compared, so a
check that could not run is never mistaken for one that passed. If a
board read fails, that check is skipped and said so; the rest still run.

Two details worth knowing. A Gerber bounding box spans the drawn ink,
and a board outline is a stroked path whose centreline is the real
edge, so a 20 mm board drawn with a 0.2 mm pen measures 20.2 mm. The
stroke is subtracted and reported as `stroke_mm`, with the raw figure
kept alongside. And the smallest-feature check is a **warning**, never
an error, because apertures cover pads and text as well as tracks.

Needs the optional extra, which brings in
[gerbonara](https://pypi.org/project/gerbonara/) (Apache-2.0, Gerber and
Excellon in one library):

```bash
pip install -e .[gerber]
```

It requires Python 3.12+, while eda-agent itself supports 3.11, which is
why it is an extra rather than a core dependency. Without it the tool
says so plainly instead of failing obscurely.

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
cannot do, is in [`docs/BACKENDS.md`](docs/BACKENDS.md).

## Finding the right tool

Tools are grouped by the **document** they act on, and mixing them up is the
most common mistake: `lib_` acts on a `.PcbLib` or `.SchLib`, `pcb_` on an open
`.PcbDoc`, `sch_` and `obj_` on an open `.SchDoc`. A board tool aimed at a
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

The tools are deliberately **not** merged into generic dispatchers. Each one
carries its own name, description and schema, and those are what let a model
find the right operation and follow the per-tool discipline; collapsing them
into `pcb(action=...)` style entry points loses that. Hiding them from the
advertised list keeps the information available on demand via `tool_catalog`.

The trade-off: in `minimal` the model no longer sees
tool schemas up front, so it must discover before it can act, and an argument
mistake surfaces as the target tool's own error rather than a schema
validation message. Call `tool_catalog(query=..., with_schema=True)` to get a
tool's parameters and required list before invoking it, rather than guessing
argument names - some are not what they look like (`current_amps`, not
`current_a`), and the same tool can differ between backends. Prefer `full`
(the default) unless your client forces otherwise.

## Fab capabilities, stackups and laminates

Capability figures and stackup templates for JLCPCB and PCBWay ship
with eda-agent, each carrying **the page it was transcribed from and
the date**, with a staleness flag after a year.

```bash
eda-agent stackup-vendors
eda-agent stackup-show jlcpcb
eda-agent laminates
```

| CLI verb | Tool | Does |
|---|---|---|
| `stackup-vendors` | `design_list_fab_vendors` | The bundled vendors, with source, age and what each does not publish |
| `stackup-show` | `design_fab_vendor` | One vendor's minimums (mm and mils) and stackup templates |
| `stackup-profile` | `design_fab_vendor_profile` | Emit a fab profile for `design_synthesize_rules` |
| `laminates` | `design_list_laminates` | Laminate properties, with both dielectric constants |
| - | `design_compare_stackup` | A template against a `pcb_get_layer_stackup` reply |
| - | `pcb_check_stackup` | The same, reading the open board for you |
| - | `pcb_apply_stackup_template` | Set the open board's stackup to a template (dry run by default) |

**`design/fab_profile.py` ships zero capability numbers on purpose** -
its own docstring says every value must come from the fab and be cited.
This is a deliberate relaxation of that, not an abandonment: every
number is dated and sourced, and three rules keep the original point
intact.

- **A figure the vendor does not publish stays unpublished.** JLCPCB
  states no rigid hole-to-hole minimum and no mask sliver. Both are
  `null`, both are listed with the reason, and converting to a fab
  profile **refuses** and names them. Supply them yourself with
  `--fill min_hole_to_hole_mils=17.7` once the fab has told you, and
  they are recorded in the profile's `source` as yours rather than
  theirs. Nothing here will guess a design rule.
- **Nothing pretends to be current.** A transcription older than a year
  is reported as stale, in the result rather than in a README.
- **A fab's number for its own stack is not a material property.**
  JLCPCB's er of 4.6 describes what JLCPCB presses; it is not carried
  to anyone else's FR-4.

A fab that is not bundled goes in your profile's `fab_profiles/`
directory, where it travels with the project.

### Two dielectric constants

Manufacturers publish a **process** permittivity (one frequency,
clamped stripline) and often a **design** one (over a band,
differential phase length). RO4350B is 3.48 and 3.66 - about 5% apart,
and the design value is the one to size an impedance with. A table
carrying a single `er` silently picks one, so both are carried and
`er_design` is null where none is published.

The laminate list is deliberately short: only entries read off a
datasheet are in it. pcb-toolkit carries 45 presets extracted from the
Saturn PCB Toolkit binary, which its own README calls compatibility
data rather than manufacturer data; copying them here would turn an
acknowledged approximation into what looks like a reference table.

### Applying one to a board

`pcb_apply_stackup_template` sets the open board's ply table to a
template, **dry run by default** because a stack change can invalidate
routing. It modifies existing layers only and refuses when the layer
counts differ: adding one means inventing the name Altium's own Layer
Stack Manager would have chosen, on a board that has routing on it.

This needed a bridge fix. `PCB_ModifyLayer` parsed copper thickness
with `StrToIntDef`, and no real foil weight is a whole number of mils -
1 oz is 1.378 - so asking for 1 oz copper silently set the layer to
**zero** and reported success. Both thickness fields parse as floats
now. **Redeploy the Altium scripts** (`eda-agent install-scripts`) to
pick it up; `eda-agent health` reports the deployed version.

## Calculators

Ten calculators that this project did not have, each reachable as an MCP
tool and as a CLI verb:

```bash
eda-agent calc-spacing --voltage 280 --environment B2
```

| CLI verb | Tool | Answers |
|---|---|---|
| `calc-spacing` | `pcb_calc_conductor_spacing` | Minimum conductor spacing for a voltage (IPC-2221 Table 6-1) |
| `calc-fusing` | `pcb_calc_fusing_current` | Current that melts a track in a given time (Onderdonk) |
| `calc-via` | `pcb_calc_via_parasitics` | Via inductance, capacitance, barrel resistance |
| `calc-spiral` | `pcb_calc_spiral_inductor` | Planar spiral inductance (modified Wheeler) |
| `calc-reactance` | `pcb_calc_reactance` | Capacitive and inductive reactance |
| `calc-resonance` | `pcb_calc_resonance` | LC resonance and characteristic impedance |
| `calc-wavelength` | `pcb_calc_wavelength` | Wavelength, quarter-wave, twentieth-wave |
| `calc-pdn` | `pcb_calc_pdn_target` | Target power-distribution impedance |
| `calc-padstack` | `pcb_calc_padstack` | Pad and antipad sizes, gap to neighbours |
| `calc-wire` | `pcb_calc_wire_gauge` | AWG diameter, area, resistance |

They join the ones already here: `pcb_calc_impedance`,
`pcb_calc_trace_width_for_current`, `pcb_calc_trace_width_for_impedance`,
`pcb_calc_track_current_capacity`, `pcb_calc_termination`,
`pcb_calc_thermal_vias`, `pcb_calc_length_match`.

**Every result states how much to trust it**, in a `status` field:
`exact` for a definition or an identity, `published` for a named
standard or textbook closed form, `estimate` for a rule of thumb. An
algebraic identity and a rule of thumb must not arrive looking alike,
or the rule of thumb ends up in a specification. Add `--json` for the
full reply; caveats go to stderr so stdout is only the figures.

Two of these carry caveats worth repeating here:

- **Conductor spacing** is transcribed from secondary reproductions of
  IPC-2221 Table 6-1, because the IPC document is not freely available,
  and two reproductions disagree on two cells. The larger value is
  taken wherever they conflict, since a wider spacing is never the
  unsafe mistake, and the disagreement is reported rather than decided
  silently. Verify against the IPC publication before a design review
  relies on it. The voltage is the one **between conductors**, not the
  supply rail. The classes are the IPC-2221 **A/B** ones (B1-B4, A5-A7):
  revision C *redefines* those columns rather than renumbering them -
  B4 becomes solder-mask covered, a new B5 appears, the A-series shifts
  and the above-500 V step changes - so match the class on the
  description the result gives you, never on its letter.
- **Fusing current** is the destruction limit, not a rating, and
  Onderdonk is adiabatic: good under about a second, and far too low
  beyond that, where a track sheds heat into the laminate. Results past
  one second say so.

### Cross-checking against pcb-toolkit

If you *do* build pcb-toolkit, `calc-cross-check` runs a calculator
through both and reports where they differ. Exits 1 on a
disagreement, so CI can branch on it.

```bash
eda-agent calc-cross-check ipc2221_spacing --arg voltage=280 --arg environment=B2
```

Six calculators are mapped; the flags were read from pcb-toolkit's own
clap definitions rather than guessed. Four are deliberately unmapped
and say why when you ask for them - two because their flags were never
read, one (`pdn_target_impedance`) because pcb-toolkit models a
different quantity, and one because upstream folds resonance into
`reactance`.

Both replies come back in full, and fields pair **by name**, not
through a translation this project has verified. A pair sharing a name
and not a meaning reports as a disagreement, which is the safe
direction for it to fail, and anything unpaired is listed as not
compared.

One disagreement is expected and is not an error: pcb-toolkit's spacing
targets IPC-2221**C** and takes classes `b1`-`b5` / `a6`-`a8`, while
this carries the A/B table.

### Why these are native Python

The obvious way to get them was to call
[pcb-toolkit](https://github.com/akiselev/pcb-toolkit) (Rust, MIT OR
Apache-2.0), whose coverage is wider and whose figures are
upstream-validated. But its CLI is not published as a binary, so using
it means every user installs a Rust toolchain and builds from source.
Calculators that only work for people who did that are not calculators
this server can rely on, so they are implemented here instead, from the
same published sources. The `status` convention is pcb-toolkit's own
idea, and a good one.

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
[`docs/PART_SOURCING.md`](docs/PART_SOURCING.md).

## Example use cases

### Full-project design review

> *"Do a design review of the PoE front-end. Pull the snapshot, fetch the TPS2372 and TL072 datasheets, and flag anything that doesn't match."*

One `design_review_snapshot` call gives the AI project info, design stats, components, nets, rules, diff, messages, board stats, and BOM, plus a datasheet-fetch checklist. The AI then grounds every recommendation in the vendor datasheets it actually pulled. 8 to 12 separate queries collapse into one tool call.

### Schematic review

The AI reads your schematic live. Ask it anything a reviewer would:

> *"List every component connected to the 3V3 rail and flag anything whose datasheet limit is below that."*
>
> *"Find all net labels that appear only once across the whole project. Those are probably typos."*
>
> *"What's driving the /RESET net? Walk the connectivity and tell me where it resets and how."*
>
> *"Do any two components share a designator prefix with gaps in numbering (e.g. R1, R2, R4)? Re-annotate or tell me what's missing."*
>
> *"Compare the focused schematic to the version from 3 weeks ago. What parameter values changed?"*

Behind that, the AI calls tools like `query_objects(object_type="eSchComponent", scope="project")`, `get_connectivity_many(designators=[...])`, `get_nets(...)`, `modify_objects(...)`, and so on. You watch Altium repaint as it works.

### Sch ↔ PCB drift detection

> *"Run `obj_crossref_net` on POE_PG. The PCB seems to have R7 on this net but I'm not sure the schematic still does."*

The response shows sch pins, PCB pads, matched count, and the diff in each direction. A non-empty `pcb_only` list means the board was fabricated from an earlier schematic revision and a later edit broke the post-ECO merge; catch this before the next ECO push rips routed connections. `in_sync: false` plus the exact diff tells you which port or sheet-entry rename to undo.

### SPICE simulation setup

> *"Set this schematic up for an AC sweep. Attach SPICE primitives to every passive, fetch vendor SPICE models for the op-amps, and tell me if any part can't be simulated."*

`sim_get_readiness` partitions the design into `ready` / `needs_primitive` / `needs_file`. The AI batches primitives onto every passive in one `sim_attach_primitives` call, searches vendor sites for the IC models, attaches them with `sim_attach_model`, and reports any holdouts. It will not fabricate a SPICE model file; the rule is baked into the tool response.

### Library hygiene

> *"Open `Resistors.SchLib` and report every component missing a Value, ManufacturerPart1, or Description parameter. Fill in the missing Description from the datasheet URL if present."*
>
> *"Diff our `Caps.SchLib` against `Caps_vendor.SchLib` and tell me what's new or changed."*
>
> *"Create a new 48-pin symbol for STM32F411 with this pinout table."*

The last one uses `lib_add_pins`: one call places the whole pinout in a single transaction instead of 48 LLM turns.

### PCB spot-checks

> *"Any unrouted nets on the board?"*
>
> *"What's the total trace length for the USB differential pair, split by layer?"*
>
> *"Show me all vias on the 12V net and their drill sizes."*
>
> *"Run DRC and summarize the violations by severity."*
>
> *"What does the `Clearance_HV` rule actually enforce: clearance value, scope expressions, priority?"*

That last one uses `pcb_get_rule_properties`, which returns the actual numeric gap / widths / impedance targets, not just rule metadata.

### Bulk changes

> *"Every 0402 resistor with value 10k, set its Tolerance parameter to 1% and Voltage to 50V."*
>
> *"Rename the net OLD_CS to SPI_CS across every sheet in the project."*
>
> *"Move C1-C20 into this 200-mil grid layout pattern."*

Bulk tools like `obj_batch_modify`, `pcb_move_components`, and `sch_place_components` finish the whole operation in one IPC round-trip.

## Known limitations

**This tool is experimental. Please read this section before using on a design you haven't backed up.**

> Bridge changes are checked by Free Pascal and a linter before they ship, which cannot prove Altium's own DelphiScript engine accepts them: the two differ on which identifiers exist, and an undeclared one faults at runtime rather than at compile time. [`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md) is the procedure for closing that gap on a release, starting with a self-test that runs inside Altium and needs no document.

### UI automation synthesises real keyboard and mouse input

Most of this project talks to Altium through the scripting bridge, which addresses a window handle directly and cannot affect anything else. The `app_*` UI automation tools are different, and are used where Altium offers no other route: `application.execute_menu` reports success while invoking nothing, `GetMenu` returns 0 on Altium's DevExpress bars, and whole dialogs (Update From Libraries, Preferences, the wizards) have no scripting API at all.

**Synthesised input is not addressed to a window.** `keybd_event` and `mouse_event` are delivered to whatever is active at the instant they fire, and a click lands on whatever is under the pointer. So these tools:

- **take focus.** Menus and clicks need Altium in front, so running them while you are typing will interrupt you
- **cannot be confirmed the way a property write can.** A keystroke has no read-back; anything that matters is verified afterwards with a bridge read
- could, without containment, deliver an event to another application if focus or the pointer moved

What contains that: a foreground check runs **immediately before every event**, including between a key press and its release, and refocuses Altium rather than failing; coordinates are refused unless the point is over a window belonging to Altium's own process; and no tool accepts a window handle from the caller, so an arbitrary window cannot be addressed. `tests/test_foreground_guard.py` enforces all three, checking the event guard per line of source rather than per function, because a function that checks once and then emits five events in a loop would pass a naive test while firing four unchecked events.

**To switch it off entirely:**

```
EDA_AGENT_UI_AUTOMATION=0
```

Every synthesised event is then refused. Reading stays available on purpose, because dialog detection is how the rest of the system notices Altium is blocked on a modal.

One case is not solvable: Altium's menu bar carries entries that are commands rather than menus (Place a Comment, Share, Open Home page, Preferences), and nothing distinguishes them. Measured across all 17 bar items: identical MSAA state including `HASPOPUP`, identical `accDefaultAction`, identical UIA control type. Listing such an entry clicks it, and clicking it runs it.

Full detail in [`docs/ui-automation.md`](docs/ui-automation.md).

### Altium DelphiScript engine can crash

Some tool paths trigger DelphiScript compile or runtime errors ("Undeclared identifier…", "Could not convert variant of type (Dispatch) into type (OleStr)", etc.). When that happens, the script project halts mid-execution and the polling loop stops responding. You will see one of:

- An Altium error dialog stating the problem
- Your MCP client timing out waiting for a response

**Recovery:** in Altium Designer, open the script project tab and press the **red Stop** button in the Script IDE toolbar (equivalently **Run > Stop** from the menu, or **Ctrl+F3**; use **Ctrl+Pause/Break** if the script is stuck in an infinite loop). This stops the halted debugger. Then re-launch the polling loop via **File > Run Script... > StartMCPServer > Run**.

This is an ongoing reliability effort. Every identified crash is either fixed or guarded. If you hit a new one, the Altium error dialog tells you the exact identifier or line. Opening an issue with that text helps us harden the relevant path.

### UNC network paths need current scripts

A UNC path (`\\server\team\board.PrjPcb`) opens like any other with the current scripts. Scripts deployed before the fix drop one leading backslash, so the file is not found and the error names a path that looks almost right. Run `eda-agent install-scripts --force` to update them, or use a mapped drive letter (`Z:\team\board.PrjPcb`) in the meantime.

### Text above Latin-1 becomes question marks

Altium's DelphiScript strings are single-byte, so the bridge carries text as one byte per character. Any character above U+00FF is replaced with `?` on the way in, silently. Accented Latin, the micro sign, and the degree sign are all below that boundary and survive; the ohm sign and any CJK text do not, so `10Ω` arrives as `10?`.

This shows up most often on imported parts: LCSC descriptions are frequently Chinese, and `lib_easyeda_import` passes the description straight through. If you need those fields readable, set them to a transliteration before importing, or edit them in Altium afterwards.

### Altium tool buttons relying on internal scripting pause while the server is running

Altium itself uses DelphiScript internally for many built-in commands (some ribbon buttons, panel actions, menu items). **While the `eda-agent` polling loop is active, those built-in commands may become temporarily unresponsive** because Altium's scripting engine is single-threaded and currently owned by our polling loop.

**The polling loop owns the scripting engine for as long as it's running.** While it runs, Altium's own script-backed buttons sit waiting. The loop exits when either:

- The MCP client calls `app_detach` (or the dashboard **Detach** button is clicked); `app_detach` saves the edited documents (the button saves nothing), the loop exits within ~500 ms, and Altium becomes fully responsive, OR
- **10 minutes of total silence** from the MCP client (no commands AND no keep-alive pings) triggers the built-in auto-shutdown

In practice, while an MCP client is attached and sending keep-alive pings every 30 s, the loop will never time out on its own; you need to either have the AI call `app_detach` or close the MCP client session entirely. After the client disconnects, expect up to ~10 minutes for the loop to auto-exit unless you use **Detach** to release it immediately.

### ECO (sch → PCB update) opens a modal, and there is no silent API

The Altium Schematic API exposes no scripted ECO executor: `IECO` only records proposed changes, no `DM_Execute` is documented, and no factory hands a script an `IECO`. `PCB:UpdatePCBFromProject` turned out not to be a real process id, so the handler that called it no-opped while reporting success. `proj_sync_pcb` now invokes `WorkspaceManager:Compare`, which does the real work and blocks on the change-order dialog.

Two consequences worth knowing before you call it.

It **blocks the polling loop** until the dialog is answered, so it is not an unattended call on its own. It also **refuses while a schematic is focused**: Altium answers that case with "Cannot compare a source document against its owner project" and changes nothing. Focus the board with `app_set_active_document` first.

What it reports is narrower than it looks. `components_in_sync` counts component **presence** only, so footprint swaps, designator and parameter edits and net changes are all invisible to it, and `dialog_outcome_verified` is always false because the handler cannot see which button was pressed.

**To drive it end to end**, use the GUI tools rather than answering by hand:

```
app_run_ui_command("Design|Update PCB Document <board>.PcbDoc", allow_commit=True)
```

That clicks the menu and answers the dialogs in one call, which matters because a modal blocks the bridge and a second call would never arrive. It validates before executing, and without `allow_commit` it presents the change order instead of applying it.

### Tools vary in maturity

Not every one of these tools has been exercised on every Altium version or design size. The generic primitives (`obj_query`, `obj_modify`, `obj_create`, `obj_delete`, `run_process`) and the core `application` / `project` tools are the best-tested. Some PCB modify operations (polygon repour, room creation, align-components) are less battle-tested. Queries are generally safer than mutations.

### What the bundled data does not cover

The data this project ships is transcribed, dated and sourced, and
several figures are **missing on purpose** because nobody publishes
them. They come back `null` with a reason rather than filled in with a
plausible number, and the tools that need them refuse rather than
default.

- **No product-change notices and no errata.** `part_find_replacements`
  compares parameters, lifecycle and stock. No distributor API serves
  PCNs or errata, so nothing checks them, and every result repeats that.
  A PCN is the commonest reason a part that passes every check is still
  the wrong choice this month.
- **No pad geometry in a replacement check.** The package *name* is
  compared; a catalogue states "SOIC-8" and never states the pads. Use
  `lib_audit_footprint_vs_datasheet` against the candidate's datasheet.
- **JLCPCB publishes no rigid hole-to-hole minimum and no mask sliver.**
  Both are `null`, and `stackup-profile` refuses to build a fab profile
  without them. Supply them with `--fill` once the fab has told you, and
  they are recorded as yours, not theirs.
- **PCBWay publishes no named stackup**, so none is bundled.
- **The IPC-2221 spacing table is a secondary transcription.** The IPC
  document is not freely available, and two reproductions **disagree on
  two cells** (B2 at 251-300 V, A6 at 31-50 V). The larger value is
  taken, since a wider spacing is never the unsafe mistake, and both
  figures are reported in `disputed`. The classes are the **A/B** ones;
  revision C *redefines* those columns rather than renumbering them, so
  match on the description the result gives you, never on the letter.
- **The laminate list is deliberately short** - only entries read off a
  datasheet. Add your own to your profile.
- **Altium preferences cannot be templated.** There is no scripting API
  for the Preferences dialog. **Draftsman is unsupported entirely.**
- **PCB-side units and grid are documentation only.** No bridge command
  sets a board's display units or default snap grid; a project template
  sidesteps this for new projects.

### Where the Altium scripts are deployed

`install-scripts`, `eda-agent health` and `eda-agent update` use the
folder Altium itself loads `Altium_API.PrjScr` from, read from the
Global Projects list Altium keeps in the registry
(`HKCU\Software\Altium\<install>\DesignExplorer\Preferences\ScriptingSystem\Global Projects`).
Only when no install registers it, or several installs register
*different* folders, do they fall back to `%USERPROFILE%\EDA Agent\scripts\`,
and `update` says which of those happened.

This used to be the fixed default regardless, so with the Global
Project registered elsewhere those commands reported "deployed scripts
current" about a folder Altium never reads, while Altium compiled
something days old. `eda-agent update` still pings Altium for the
version it actually **compiled** and compares the two, which catches
any gap the registry cannot explain. To deploy somewhere specific:

```bash
eda-agent install-scripts --force --dest <that folder>
```

## Timeout and server lifecycle

The server has **three independent timeout mechanisms**:

### 1. Per-command timeout (Python side)

When the MCP client calls a tool, the Python bridge writes a request file and waits up to **10 seconds by default** for a response. Fast queries typically complete in under 100 ms, so a 10 s ceiling surfaces stalls quickly while leaving plenty of margin for real work. Long-running tools that are expected to take longer (`app_save_all`, `stop_server`, `pcb_get_unrouted_nets`) set their own larger timeouts internally.

Each request is published to its own `request_<id>.json` file; Altium replies in `response_<id>.json` with the matching ID. The bridge's keep-alive thread and MCP-client calls each use their own request IDs, so responses never race. The older single-`response.json` channel was retired in IPC v2.

### 2. Server auto-shutdown (Altium side)

The DelphiScript polling loop auto-stops after **10 minutes of inactivity** (`AUTO_SHUTDOWN_MS = 600000`). If the MCP client disconnects and the keep-alive pings stop arriving, the server releases Altium's scripting engine after ten minutes and `StartMCPServer` returns. To resume, re-launch via **File → Run Script... → StartMCPServer → Run**.

### 3. Python keep-alive pings

While an MCP client is attached, the Python bridge pings Altium every 30 seconds so the 10-minute auto-shutdown never fires mid-session. The sequence:

- **AI issues command A** → Altium busy, then idle
- **30 s later, Python pings** → Altium responds "pong", idle timer resets
- **10 min later, still no AI activity and no ping** → Altium auto-shuts down

In practice: the server stays alive as long as an MCP client is connected, and exits cleanly ~10 minutes after the client fully disconnects. No manual stop needed in the common case. For a hard exit, the AI calls `app_detach`, which saves the documents that were edited, leaves clean ones untouched, and returns control to Altium within ~500 ms. The **Detach** button on the dashboard window stops the loop without saving.

### Several Claude sessions on one Altium

Every session runs its own eda-agent server, and all of them talk to the one script loop in Altium. Request ids carry a session tag and a sequence number. A session working alone is served straight through, in the order it sent its requests, with no time slots. When more than one session has requests waiting, the session being served keeps going for up to 30 seconds (`SESSION_SLOT_MS`), then the next waiting session gets one slot, and so on in turn, so no session gets a second slot before every waiting session has had one. A command already running is never interrupted: slots change between commands, so one long command (a large repour, say) still holds everyone else up until it finishes. While sessions contend, Altium refreshes `queue.json` in the workspace, and a waiting client keeps waiting instead of reporting the loop as dead.

Things that affect every session at once: a deploy, a relaunch or a restart of Altium, and `app_detach`, which saves every document a tool edited, including another session's. To stop the loop without saving, put an empty file named `stop` in the workspace.

### Why this matters for Altium UI responsiveness

The polling loop goes into idle mode after ~1 second of no MCP commands. In idle mode it polls every 100 ms with a `ProcessMessages` yield in between, so Altium's UI stays responsive continuously. In active mode the loop polls every 10 ms (`ProcessMessages` every 5th tick), giving sub-50 ms round-trip latency for back-to-back commands. For a full release, call `app_detach` or click **Detach** on the dashboard.

## Tool reference

[`docs/TOOL_REFERENCE.md`](docs/TOOL_REFERENCE.md) lists every tool with
its arguments, its **maturity** (offline / simulator / live-verified) and
its **interaction** badge, flagging the ones that open a blocking dialog
or leave work incomplete. It is generated by
`python scripts/gen_tool_reference.py`, so it cannot drift from the code.

[`docs/ALTIUM_SCRIPTING_COVERAGE.md`](docs/ALTIUM_SCRIPTING_COVERAGE.md)
maps official Altium scripting examples and interfaces to those tools, records
live AD-version verification and known gaps, and gives future agents the
required extension trail.

[`CHANGELOG.md`](CHANGELOG.md) records what each change added or fixed,
and marks anything not yet watched working against a live Altium session
or a real Nexar account. The deeper references are
[`docs/ENGINEERING_TOOLS.md`](docs/ENGINEERING_TOOLS.md) for the `eng_*`
family, [`docs/PROFILES.md`](docs/PROFILES.md) for per-workspace house
style, [`docs/PART_SOURCING.md`](docs/PART_SOURCING.md) for part
providers, and [`docs/BACKENDS.md`](docs/BACKENDS.md) for the EDA
backends.

At runtime `tool_catalog` serves the same data, filtered by category,
maturity, interaction or substring, and `tool_invoke` calls anything it
lists. That pair is the whole advertised surface under
`EDA_AGENT_TOOLSET=minimal`.

## Architecture

```
    +-----------------------------+
    |    MCP-compatible client    |
    +-----------------------------+
            |              ^
            v              |
       tool call       tool result
       (JSON-RPC)      (JSON-RPC)
            |              |
            v              |
    +-----------------------------+
    |     eda-agent (Python)      |
    | application / project / lib |
    | / generic / pcb / design    |
    |              |              |
    |     Altium bridge (IPC)     |
    +-----------------------------+
            |              ^
            v              |
   request_<id>.json   response_<id>.json
            |              |
            v              |
    +-----------------------------+
    |      Altium Designer        |
    |  DelphiScript polling loop  |
    |     (Altium_API.PrjScr)     |
    +-----------------------------+
```

All intelligence lives in Python. The DelphiScript side is a pass-through layer for object iteration, property access, and process execution.

## CLI

| Command | Purpose |
|---|---|
| `eda-agent` | Start the MCP server on stdio (what the MCP client calls) |
| `eda-agent serve` | Explicit form of the above |
| `eda-agent --no-dashboard` / `eda-agent --headless` | MCP server only, no web dashboard. Required by strict-stdio MCP clients (Codex, etc) that can't tolerate the dashboard thread. Also via env var: `EDA_AGENT_DISABLE_DASHBOARD=1` or `EDA_AGENT_HEADLESS=1`. |
| `eda-agent scripts-path` | Print path to bundled DelphiScript sources |
| `eda-agent install-scripts [--dest PATH] [--force]` | Copy scripts to a directory of your choice |
| `eda-agent review --offline <file> [--json/--sarif] [--fail-on ...]` | **Offline** component-level design review of a `.SchDoc`/`.PrjPcb` (no Altium). Opt-in (`--offline` or `EDA_AGENT_HEADLESS_REVIEW=1`); exit 1 on findings at/above `--fail-on` - a hardware-CI gate |
| `eda-agent bom --offline <file> [--csv/--json]` | **Offline** consolidated BOM from a `.SchDoc`/`.PrjPcb` (no Altium). Opt-in |
| `eda-agent netlist --offline <file> [--json] [--fail-on ...]` | **Offline** geometric netlist reconstruction + connectivity ERC (`single_pin_net`, `net_short`) from a `.SchDoc` (no Altium). Opt-in; exit 1 on findings at/above `--fail-on` |
| `eda-agent health` | Fast offline preconditions: workspace dir + writable, pointer file + matches config, bundled scripts findable, bridge constructable. Exit 0 = clean, 1 = critical fail |
| `eda-agent doctor [--library PATH]... [--json]` | Full preflight talking to Altium: all `health` checks plus process running, script polling responsive, script-version matches bundled, `app_save_all` canary round-trip, optional `--library` lib reachability checks (no hardcoded paths; repeat the flag for each lib you want tested) |
| `eda-agent eco-execute --confirm [--validate-only] [--allow-confirm-dialogs] [--wait-first N] [--budget N]` | **Experimental, unverified.** Watches for and auto-presses Altium's Engineering Change Order dialog (Validate Changes / Execute Changes / Close), via the existing `app_drive_dialogs` reactive driver. Off unless `--confirm`; does not trigger an ECO itself (trigger separately with `proj_sync_pcb` or Design > Update PCB Document while this runs). See [ECO update](docs/ENGINEERING_TOOLS.md#eco-update-experimental) |
| `eda-agent perf [--json] [--limit N]` | Where call time actually goes. Joins `bridge_trace.log` (the round trip the caller saw) with `activity.log` (the handler time inside Altium) on the request id, and reports both plus the difference, per command. See [Measuring performance](#measuring-performance) |
| `eda-agent profile-init <name> [--here]` | Scaffold a new owner profile (naming/rules/fab/footprint conventions). `--here` makes it project-local (`.eda-agent/profile/`, meant to be committed); without it, a centrally-switchable `~/.eda-agent/profiles/<name>/`. See [docs/PROFILES.md](docs/PROFILES.md) |
| `eda-agent profile-list` | List central profiles and which one is active |
| `eda-agent profile-use <name>` | Set the centrally active profile |
| `eda-agent profile-show [--json]` | Show the profile actually in effect for the current directory |
| `eda-agent profile-validate [name] [--here]` | Load every file in a profile and report errors |
| `eda-agent profile-new-project <template> <dest> [--force]` | Copy a profile's `templates/<name>/` tree to a new project directory |
| `eda-agent calc-spacing --voltage V [--environment B2]` | Minimum conductor spacing for a voltage (IPC-2221) |
| `eda-agent calc-fusing --width-mils W [--copper-oz 1] [--time-s 1]` | Current that melts a track in a given time (Onderdonk) |
| `eda-agent calc-via --drill-mils D --length-mils L` | Via inductance, capacitance and barrel resistance |
| `eda-agent calc-spiral --turns N --outer-diameter-mils D --inner-diameter-mils d` | Planar spiral inductance |
| `eda-agent calc-reactance --frequency-hz F [--capacitance-f C] [--inductance-h L]` | Reactance at a frequency |
| `eda-agent calc-resonance --inductance-h L --capacitance-f C` | LC resonance and characteristic impedance |
| `eda-agent calc-wavelength --frequency-hz F [--dielectric-constant 4.3]` | Wavelength, quarter-wave and twentieth-wave |
| `eda-agent calc-pdn --voltage-v V --ripple-percent P --transient-current-a I` | Target power-distribution impedance |
| `eda-agent calc-padstack --drill-mils D --annular-ring-mils R [--pitch-mils P]` | Pad and antipad sizes, gap to neighbours |
| `eda-agent calc-wire --awg N [--length-m 1] [--temperature-c 20]` | AWG diameter, area and resistance |
| `eda-agent update [--check] [--pull] [--fetch] [--from FOLDER\|REMOTE] [--json]` | Bring the install up to date from your remote, or from a local folder |
| `eda-agent calc-cross-check <calculator> [--arg NAME=VALUE]` | Compare a calculator against a locally built pcb-toolkit |
| `eda-agent stackup-vendors [--json]` | Bundled fab vendors, with source and age |
| `eda-agent stackup-show <vendor> [--json]` | One vendor's minimums and stackup templates |
| `eda-agent stackup-profile <vendor> [--fill NAME=MILS]` | Emit a fab profile JSON, or say what the vendor does not publish |
| `eda-agent laminates [--json]` | Laminate properties, with both dielectric constants |

Every `calc-*` verb takes `--json` for the full reply and exits non-zero
on a refusal, so a script can tell "this geometry is impossible" from
"here is your answer". See [Calculators](#calculators).

## Configuration

Workspace (used for IPC files between Python and Altium):

- Default: `%USERPROFILE%\EDA Agent\workspace\`
- Override: set `EDA_AGENT_WORKSPACE` environment variable
- The DelphiScript side reads the resolved path from `C:\ProgramData\eda-agent\workspace-path.txt`, which Python writes at startup and on every `install-scripts` run

UI automation (synthesised keyboard and mouse input, used for the parts of Altium with no scripting API):

- Default: **on**
- Disable: set `EDA_AGENT_UI_AUTOMATION=0` (also `false`, `no`, `off`). Anything else leaves it on, so a typo cannot silently disable it
- Read at call time, so it takes effect on the next event rather than the next restart
- See [UI automation synthesises real keyboard and mouse input](#ui-automation-synthesises-real-keyboard-and-mouse-input) for what it is for and what it can do

Activity log (rotating, size-capped, secrets redacted; in addition to the
server's existing stderr logging):

- Default: `%USERPROFILE%\EDA Agent\logs\eda_agent.log`
- Override the directory: set `EDA_AGENT_LOG_DIR`
- `EDA_AGENT_LOG_MAX_MB` (default 10): rotate a file at this size
- `EDA_AGENT_LOG_BACKUPS` (default 5): rotated files kept
- `EDA_AGENT_LOG_TOTAL_MAX_MB` (default 200): hard cap for the whole log
  directory; oldest rotated files are deleted first
- `EDA_AGENT_LOG_MIN_FREE_MB` (default 1024): below this free disk space,
  DEBUG/INFO logging is suspended and one CRITICAL entry is written
- Bearer tokens, OAuth tokens and distributor/Nexar API keys are redacted
  before a line is written, regardless of which code logged it

Part sourcing, Nexar/Altium 365, and the rest of the `eng_*` engineering
tools have their own environment variables (`EDA_AGENT_NEXAR_CLIENT_ID`,
`EDA_AGENT_A365_WORKSPACES`, `EDA_AGENT_LTSPICE_EXE`, `EDA_AGENT_IPC_DIR`,
and more) - see [`docs/ENGINEERING_TOOLS.md`](docs/ENGINEERING_TOOLS.md) and
[`docs/PART_SOURCING.md`](docs/PART_SOURCING.md).

Coordinates throughout the API are in **mils** (1 mil = 0.0254 mm).

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

The test suite includes a Python Altium simulator for end-to-end integration tests, Free Pascal cross-validation that runs the actual DelphiScript functions against Python mirrors, and a regression suite for previously encountered edge cases.

Rebuild the monolithic DelphiScript file after editing sources under `scripts/altium/`:

```bash
cd scripts/altium
python build.py
```

## Project layout

```
eda-agent/
├── src/eda_agent/          Python package
│   ├── bridge/             Altium communication layer
│   ├── schemas/            Pydantic IPC envelope + per-command schemas
│   ├── tools/              MCP tool implementations (incl. design.py)
│   ├── design/             Design agent: plan / inventory / discipline / executor / validator
│   ├── diag/               Health and doctor checks
│   ├── cli.py              CLI subcommands
│   └── server.py           MCP server entry point
├── scripts/altium/         DelphiScript sources (dev source of truth)
│   ├── Main.pas, Utils.pas, Dispatcher.pas, …
│   └── Altium_API.PrjScr   Altium script project
└── tests/                  Python + Free Pascal test suite
```

At wheel build time `scripts/altium/` is copied into `src/eda_agent/scripts/` inside the wheel (via Hatchling `force-include`), so `eda-agent install-scripts` always finds the scripts.

## Troubleshooting

**"Altium Designer is not running"**: open Altium before invoking MCP tools.

**"Script not responding" / MCP tools time out**: confirm the script project is loaded and `StartMCPServer` is running. Re-launch it via **File > Run Script... > StartMCPServer > Run**. Check `%USERPROFILE%\EDA Agent\workspace\` is writable.

**Altium error dialog "Undeclared identifier: ..." or "Could not convert variant..."**: a DelphiScript crash in one of the bridge handlers. In Altium's Script IDE toolbar, press the red **Stop** button (or **Run > Stop** / **Ctrl+F3**; use **Ctrl+Pause/Break** if the script is stuck in an infinite loop) to halt the debugger. Then re-launch the polling loop via **File > Run Script... > StartMCPServer > Run**. If the identifier is one the scripts themselves define, Altium is running an older compile of a unit: run `app_ping`, which names it in `stale_units`, then close the script project in the Projects panel (right-click `Altium_API.PrjScr`, **Close Project**), reopen it and run StartMCPServer. Otherwise report the identifier or error text as an issue.

**Some Altium buttons don't respond while the server is running**: expected while the AI is actively issuing commands. Built-in Altium functions that depend on DelphiScript wait for the polling loop to yield. The loop enters an idle/yield mode within ~1 s of the last AI command; if a button is still unresponsive after that, call `app_detach` from the MCP client to fully release the scripting engine.

**Command timeouts on very large boards**: default is 10 s so stalls surface fast. Tools known to take longer (`app_save_all`, `pcb_get_unrouted_nets`, `stop_server`) set their own internal timeouts up to 60 s. If you hit a timeout on a custom long-running operation, embed the bridge directly and pass a higher `timeout=` to `send_command_async`. The polling loop itself adapts (10 ms active, 100 ms idle) so it doesn't add latency.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Disclaimer

**Use at your own risk.** `eda-agent` drives Altium Designer programmatically and can modify, save, or delete design data. An AI client operating it can issue rapid, irreversible changes. Before using this tool on any design:

- **Back up your project.** Commit to version control, copy the folder elsewhere, or both. Do not rely solely on Altium's own history.
- Expect the possibility of **data loss, corrupted documents, or Altium crashes**, especially on large boards, unusual object configurations, or untested API paths.
- Review automated changes before saving. Prefer working on a branch or a copy until you have trust in a given workflow.

This software is provided "as is", without warranty of any kind, express or implied. The authors and contributors are not liable for any damage to your designs, projects, data, or installation.

This project is not affiliated with, endorsed by, or sponsored by Altium Limited, the KiCad project, or EasyEDA. "Altium" and "Altium Designer" are trademarks of Altium Limited; "KiCad" and "EasyEDA" are trademarks of their respective owners. `eda-agent` is an independent community tool that interoperates with each of these applications through its own published API: Altium Designer via its scripting API, KiCad via its IPC API and command line, and EasyEDA Pro via its extension API.
