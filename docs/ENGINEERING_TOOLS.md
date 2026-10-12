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
directives, limits parallel processes, and enforces a timeout.

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
