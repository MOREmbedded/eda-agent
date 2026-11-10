# Engineering tools imported from Altium AI Automation v3.1.0

The `eng_*` tools use the team's IPC, library-check, rules, design-intent,
LTspice and Altium 365 code. Offline calculations work on all EDA backends.
`eng_compare_live_ipc_footprint` is available only with the Altium backend;
it reads pads through the existing `library.get_pad_geometry` bridge command.

## IPC and libraries

- `eng_calculate_ipc`: package min/max dimensions in millimetres to an
  IPC-7351B footprint, including pad coordinates and courtyard. It refuses
  unconfigured solder-joint goals.
- `eng_compare_ipc_footprint`: compare an existing footprint's pads, expressed
  in integer micrometres, with an IPC calculation.
- `eng_compare_live_ipc_footprint`: read a `.PcbLib` footprint and compare its
  pads with an IPC calculation. The existing library tool supplies geometry
  in millimetres; this adapter converts it to integer micrometres.
- `eng_audit_footprint`, `eng_audit_symbol`, `eng_check_pin_pad_map`,
  `eng_check_name`, `eng_render_name` and `eng_scan_delphiscript` provide the
  remaining library checks.
- `eng_parse_pin_table`, `eng_generate_symbol`, and
  `eng_generate_datasheet_footprint` prepare library geometry from a cited
  datasheet pin table and its recommended land pattern.

Copy `src/eda_agent/engineering/data/ipc7351b_goals.json` and `naming.json`
to a policy directory, edit them to match your company standard, and set
`EDA_AGENT_IPC_DIR` to that directory. The bundled goals intentionally leave
some package families and density levels unconfigured.

- `eng_parse_ipc_footprint_name`: parse an IPC-7351B footprint name back into
  its implied pin count, pitch and row span, for cross-checking against the
  footprint's actual geometry.
- `eng_validate_library`: audit a whole SchLib/PcbLib pair at once -
  per-footprint and per-symbol checks, IPC-name consistency, footprint-link
  resolution, symbol pin vs. footprint pad counts, and (with a ruleset)
  required-parameter checks. Takes already-assembled `symbols`/`footprints`
  dicts; assemble them from `eng_calculate_ipc`/`eng_generate_datasheet_footprint`
  output, or use `eng_validate_library_live` below to read them from Altium.
- `eng_write_library_validation_report`: write an `eng_validate_library`
  result as timestamped CSV and HTML reports under a given directory.
- `eng_validate_library_live` (Altium backend only): the live version of
  `eng_validate_library` - reads every component (`library.get_component_details`)
  and footprint (`library.get_footprint_pads`) in a SchLib/PcbLib pair through
  the existing bridge commands, one round-trip each (capped by
  `max_components`/`max_footprints`; hitting the cap is reported in the
  result, not silently dropped).

  Altium has no per-footprint "this is the courtyard/silkscreen layer" API
  (board-level `LayerObject_V7.Kind` is the closest, but is unset in most
  vendor libraries - see `Lib_GetFootprintPads`'s comment in
  `scripts/altium/Library.pas`), so courtyard and silkscreen tracks are
  isolated by **layer name**, which you supply: `overlay_layer` (default
  `"TopOverlay"`, Altium's own default) for silkscreen, `courtyard_layer`
  (default `""` - unknown) for the courtyard. Set `courtyard_layer` to your
  library's actual courtyard layer (often a named Mechanical layer, e.g.
  `"Mechanical13"`) to also get courtyard and pin-1-marker checks; left
  blank, those two are skipped rather than reporting a false "no courtyard"
  on every single footprint (`libcheck.audit_footprint`'s `has_full_geometry`
  parameter). Pad spacing, numbering, origin, IPC-name consistency, symbol
  pin checks, footprint-link resolution, pin-vs-pad counts, 3D-body presence,
  and (with a ruleset) parameter checks always run against real geometry.

  `Lib_GetFootprintPads` (Library.pas) was extended in Phase 3 of the
  Booster integration to carry track coordinates and a 3D-body flag it
  previously computed but never returned (SCRIPT_VERSION bumped
  accordingly) - the courtyard/silkscreen gap flagged in Phase 2 is closed
  by this, not a remaining limitation. **Still unverified against live
  Altium** (no SchLib/PcbLib pair was available while porting this) -
  verify against a known library pair, with a real `courtyard_layer`, before
  relying on it.

## ECO preview

`eng_eco_preview` compares a schematic view against a PCB view - components
to add/remove, footprint changes, per-pin net changes, PCB-only stray
connections, and whole nets to add/remove - the things "Design > Update PCB
Document" itself would act on. It is a coarser, pre-ECO check than
`core.eco_verify.verify_eco` (used after an ECO runs, to catch pads an ECO
silently left unbound - see its module docstring); the two are
complementary, not redundant. Building the sch/pcb views from a live
schematic/PCB read is Phase 2 work; for now, supply them directly as
`{"components": {designator: footprint}, "pins": {"designator.pin": net}}`.
`eng_eco_preview` and `eng_eco_preview_live` only preview; they never run or
confirm an ECO. For actually triggering and completing one, see "ECO update"
below.

`eng_eco_preview_live` (Altium backend only) builds the sch/pcb views from
eda-agent's existing reads and runs the diff: the schematic side comes from
one `project.get_bom` call (components, footprints and pin-nets together);
the PCB side comes from `pcb.get_components` plus a paginated sweep of
`pcb.get_pad_properties`. A BOM truncated at `bom_limit` is refused rather
than diffed against a partial component list - raise `bom_limit` instead of
trusting a partial result. **Unverified against live Altium** - verify on a
project with one added/removed component and one footprint swap before
relying on it for anything that gates an actual ECO.

## ECO update (experimental)

Triggering an ECO is unchanged: `proj_sync_pcb` fires "Design > Update PCB
Document" and waits (bounded by `wait_seconds`) for a human to answer the
Engineering Change Order dialog, verifying pad-net completeness afterward
(see its own docstring). That dialog is non-suppressible by Altium's own
design - it will always open - but **pressing its buttons can be
automated**, through the reactive UI Automation driver eda-agent already
has (`app_drive_dialogs` / `ui.dialog_driver.drive`, the same mechanism
`app_update_from_libraries` already uses end-to-end against a live ECO
dialog). This is what Altium Booster 3.2's `EcoDialogDriver` does; eda-agent
reuses its own existing driver rather than porting a second one.

Two ways to use it, both gated behind an explicit opt-in (never a silent
default) and both **unverified against a live Altium session** (none was
available while building this - verify with Booster's own E-01/E-02
procedures, in its manual, before trusting either on a real design):

- `app_drive_dialogs(intent="proceed", allow_commit=True, dry_run=False, ...)` -
  the existing MCP tool, usable from any MCP client. `allow_commit=True` is
  the opt-in; `dry_run=False` is required to actually press anything.
- `eda-agent eco-execute --confirm` - a CLI-only wrapper around the same
  driver, for running it deliberately from a terminal rather than through
  the casual MCP tool surface. `--validate-only` presses Validate Changes
  and stops there, leaving Execute Changes/Close for a human; `--wait-first`
  and `--budget` bound how long it waits. It does not trigger the ECO
  itself - start it, then trigger one separately (`proj_sync_pcb`, or
  Design > Update PCB Document in Altium) while it runs.

Neither path adds a second bridge or a second polling loop: both read the
ECO dialog over the same Win32 UI layer `app_list_open_dialogs` already
uses, and press buttons the same way `app_update_from_libraries` already
does.

## Rules and design intent

`eng_validate_ruleset` and `eng_evaluate_rules` take the JSON ruleset and the
design data as arguments. The caller can get design data from existing
`proj_*`, `sch_*`, and `pcb_*` tools. `eng_validate_design_intent` and
`eng_diff_design_intent` validate or compare JSON design-intent documents.
`eng_parse_rul` extracts field records from a PCB `.RUL` export; the source
package marks this format as unverified for AD26.
These tools do not write a project or maintain revision history.

## LTspice

The `eng_ltspice_check_netlist`, `eng_ltspice_parse_log` and
`eng_ltspice_parse_trace` tools work without LTspice. To run a simulation,
install LTspice or set `EDA_AGENT_LTSPICE_EXE` to its executable. Batch runs
are stored under `%USERPROFILE%\EDA Agent\ltspice` by default; override with
`EDA_AGENT_LTSPICE_RUNS`. Optional `EDA_AGENT_LTSPICE_MODEL_ROOTS` is a
semicolon-separated allowlist of model directories. The runner checks file
directives, limits parallel processes, and enforces a timeout. On Windows the
LTspice process is started hidden (`SW_HIDE`), so batch runs never flash a
window on screen.

### Simulating the schematic you have open

`eng_ltspice_deck_from_schematic` builds the deck for you, which is the
step that connects LTspice to the design rather than leaving it a
calculator on the side. It reads the live sheet twice - the same SPICE
classification `sim_get_readiness` reports, then the pin-to-net
connectivity - and emits deck text. It runs nothing: pass the result to
`eng_ltspice_run`, which sandboxes and safety-checks it.

Read two parts of the response before running anything:

- **`pin_order_assumptions`** lists every diode, transistor and FET whose
  node order came from pin NUMBER order. SPICE reads terminals
  positionally (`D1 anode cathode`, `Q1 collector base emitter`), so a
  wrong order produces a deck that simulates cleanly and answers wrongly
  - the worst failure available. R, L and C are never listed, because
  their order cannot be wrong.
- **`skipped`** gives a reason per omitted part, and **`net_renames`**
  lists nets whose names SPICE could not accept (a `/` in a bus name,
  for instance).

`ground_net` (default `GND`) picks the net that becomes node 0. A
schematic where nothing matched is refused rather than emitted: without
a node 0 the deck either fails to converge or solves a different
circuit.

Parts needing a manufacturer model are the caller's decision.
`on_missing_model="refuse"` (the default) emits nothing and names them,
because a deck that silently drops a regulator still runs and still
looks plausible. `on_missing_model="placeholder"` emits the rest and
leaves each missing part as a comment, which is useful for an early look
at a passive sub-circuit but will not run until the models are added.
Fetch the vendor `.lib`/`.mdl` and attach it with `sim_attach_model`, or
fix a `needs_primitive` part with `sim_attach_primitives`, then generate
again.

## Altium 365

Altium 365 is reached through the `altium365_` tools
(`src/eda_agent/altium365.py`), by either route: straight to a workspace with
`EDA_AGENT_ALTIUM365_DOMAIN` and `EDA_AGENT_ALTIUM365_TOKEN`, or through Nexar
with `EDA_AGENT_NEXAR_APP_ID` and `eda-agent altium365-login` once. The direct
route needs a workspace token, which only some plans offer; where the admin
pages have no API token page (Admin > Apps lists only third-party
integrations), Nexar is the way in. The login prints every workspace the
account can reach, so with more than one, pass the right workspace URL to
each tool or list the ones to use in `EDA_AGENT_ALTIUM365_WORKSPACES`. An app made
in the Nexar portal is a confidential client: set its secret in
`EDA_AGENT_NEXAR_CLIENT_SECRET` too. It is sent only to Nexar's token endpoint
and never printed. A refused sign-in names Nexar's reason (`invalid_client`,
`invalid_grant`, ...) with a hint.
`eda-agent altium365-logout` forgets the saved login.
`EDA_AGENT_ALTIUM365_WORKSPACES` limits the tools to the workspaces it lists.

Set these yourself, in your own terminal, with `setx NAME value`; never
paste a secret into a chat. A setting missing from the running process is
read from your saved user environment (`HKCU\Environment`, where `setx`
writes), so a value set after the server or the shell started works
without a restart. `EDA_AGENT_ALTIUM365_TIMEOUT_S` (default 60, 5 to 900)
bounds each request.

### The library database

`altium365_scan_library` reads a workspace's whole managed library into an
offline database: every component with its folder, lifecycle, parameters,
manufacturer parts, symbols, footprints and 3D link, and every symbol,
footprint and folder, as JSON and CSV. It only reads.

The database belongs to a profile, not to eda-agent: it is kept in
`<profile>/library/<workspace host>/` of the active profile, and the
profile names its workspace in `profile.json`:

```json
{ "name": "acme", "altium365_workspaces": ["https://example.365.altium.com/"] }
```

With one workspace listed, the library tools need no `workspace_url`.
With no active profile they refuse unless given `database_dir`; nothing is
kept in the repository or in a folder that profiles share. This is the
one place Altium 365 data is written to disk, at your request and into
your own profile.

How it reads. `desLibrary.components` reports a `totalCount` that stops at
10,000, so the list is read until `hasNextPage` is false. The list is
sorted by name and its cursors are the base64 of a row index, so several
readers (`workers`, default 4) read different ranges at once; the scheme
is checked first, and when a server answers differently one reader
follows its cursors. Deep pages run into the server's 90-second limit, so
a page that times out is asked for again at half the size, down to five
rows. Every page is kept in `scan/` as it arrives: a scan stopped by
`max_seconds` (default 600), a lost connection or a closed laptop goes on
from where it stopped on the next call. `eda-agent altium365-scan-library`
runs one to the end with progress.

`altium365_update_library` refreshes it in a minute or two. Everything
before the bulk block at the end of the list (by default the trailing run
of components under one top folder, such as a generic value range) is
read again, with every symbol, footprint and folder; the row just past
the last known one is then read, and `full_scan_needed` says when the
bulk block grew or shrank.

`altium365_search_library` searches it offline (every word of the query
in the name, comment, description, folder, MPN or parameters), and
`lib_search` folds the same rows into its answer as `source: "altium365"`.

`altium365_library_report` writes `report.json` and `report.md`:
components without a symbol, footprint, manufacturer part, 3D link or
description, unused symbols and footprints, MPNs on several components.
Given `design_roots`, it reads every `.SchDoc` under them from disk (no
Altium) and joins each placed managed part to the library on its item id
without the revision, listing placements of an older revision and parts
placed but missing from the library.

3D models: the GraphQL schema has no list of 3D model items. A
component's `model3D` link is all it gives, and a footprint's revision
only counts its child items without naming them. `altium365_scan_3d_models`
reads them from Altium's Explorer panel instead (Altium running and signed
in, Explorer open): it expands and selects each folder under the named
top folders (default `3D Models`) and reads the item grid, writing
`models3d.json` and `.csv` next to the database. It selects folder rows
and nothing else: no clicks, typing or opened items.

Any reading GraphQL query may be run; one containing `mutation` or
`subscription` is refused. Changes go only through the fixed write tools
(`altium365_publish_library_file`, `altium365_release_component`,
`altium365_set_component_parameters`), each of which needs `confirm=True`.

Until 2026-11-01 this fork also had its own client and fifteen `eng_a365_*`
tools. They covered the same ground under other names and gave way to
upstream's, which is maintained; only the logout was carried across, because
upstream had none. See CHANGELOG.

The original DelphiScript bridge is not installed here. Its Altium operations
are marked as requiring AD26 validation, while `eda-agent` already has a
larger working bridge.
