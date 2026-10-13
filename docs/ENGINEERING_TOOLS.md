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
  and footprint (`library.get_pad_geometry`) in a SchLib/PcbLib pair through
  the existing bridge commands, one round-trip each (capped by
  `max_components`/`max_footprints`; hitting the cap is reported in the
  result, not silently dropped). **Known gap, left for a future phase**: no
  bridge command currently returns a footprint's courtyard, silkscreen or
  3D-body presence for a *library* footprint (only pad-only geometry is
  available; `library.get_library_geometry`, used internally by
  `lib_audit_footprint_policies`, computes a richer per-footprint record
  but never returns it to the caller, and `library.get_footprint_pads`,
  reachable only through `lib_export_kicad_footprint`, discards everything
  but a pad count). So `eng_validate_library_live` passes
  `has_full_footprint_geometry=False`, which skips the courtyard and
  pin-1-marker checks entirely rather than reporting a false "no courtyard"
  on every single footprint (see `libcheck.audit_footprint`'s
  `has_full_geometry` parameter). Pad spacing, numbering, origin,
  IPC-name consistency, symbol pin checks, footprint-link resolution,
  pin-vs-pad counts, and (with a ruleset) parameter checks all run against
  real geometry. **Unverified against live Altium** (no SchLib/PcbLib pair
  was available while porting this) - verify against a known library pair
  before relying on it.

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
Booster's ECO trigger and its UI-Automation dialog driver are out of scope
(see BOOSTER_INTEGRATION_BRIEF.md) - this integration does not run or
confirm an ECO, only previews what one would change.

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

## Altium 365

Set `EDA_AGENT_NEXAR_CLIENT_ID` and optionally
`EDA_AGENT_NEXAR_CLIENT_SECRET`. To restrict workspace queries, set
`EDA_AGENT_A365_WORKSPACES` to comma-separated workspace URLs. Run
`eda-agent a365-login` interactively once; the token is stored for the
current Windows user using DPAPI. `eda-agent a365-logout` removes it.
The MCP tools expose workspace, project and library reads plus read-only
GraphQL queries. Publishing and release mutations from the source package
are not exposed because its owner-approval job service was not imported.

The original DelphiScript bridge is not installed here. Its Altium operations
are marked as requiring AD26 validation, while `eda-agent` already has a
larger working bridge.
