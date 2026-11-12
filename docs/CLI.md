# CLI

Every flag of every command is in [All options](OPTIONS.md#command-line). This page is the overview, grouped the same way `eda-agent --help` groups them, so the two
listings can be read against each other.

## Server and setup

| Command | Purpose |
|---|---|
| `eda-agent` | Start the MCP server on stdio (what the MCP client calls) |
| `eda-agent serve` | Explicit form of the above |
| `eda-agent start [--workspace DIR] [--init] [--profile NAME] [--port N] [--altium365-workspace URL] [--no-dashboard] [--no-doctor] [--no-login] [--relogin] [--shortcut]` | Bring a design workspace up from `<workspace>/.eda-agent/workspace.json`: activate its profile, fill the Altium 365 settings in from your user environment, restart its dashboard on its own port (log in `.eda-agent/logs/`), run doctor, check the Nexar login (the browser opens only when it is not valid), and say whether the Altium bridge is up. `--init` writes the file; `--shortcut` makes a desktop shortcut. The file holds no secrets and refuses keys that look like one |
| `eda-agent --no-dashboard` / `eda-agent --headless` | MCP server only, no web dashboard. Required by strict-stdio MCP clients (Codex, etc) that can't tolerate the dashboard thread. Also via env var: `EDA_AGENT_DISABLE_DASHBOARD=1` or `EDA_AGENT_HEADLESS=1`. |
| `eda-agent install-scripts [--dest PATH] [--force]` | Copy scripts to every folder Altium loads them from, or to PATH |
| `eda-agent scripts-path` | Print path to bundled DelphiScript sources |
| `eda-agent relaunch [--prjscr PATH] [--dry-run] [--force] [--restart]` | Ask the running Altium to start the polling loop from the installed script project. Leaves pending requests alone and does nothing if the bridge already answers |
| `eda-agent update [--check] [--yes] [--no-relaunch]` | Show the new commits on the repository and, once you agree, update this clone, the Altium scripts and the polling loop |
| `eda-agent easyeda-extension path` / `... build [--out DIR]` | Print where the EasyEDA Pro extension source lives, or build the `.eext` to import into EasyEDA Pro (`build` needs Node.js) |

## Diagnostics

| Command | Purpose |
|---|---|
| `eda-agent doctor [--library PATH]... [--json]` | Full preflight talking to Altium: all `health` checks plus process running, script polling responsive, script-version matches bundled, a read-only command round-trip, optional `--library` lib reachability checks (no hardcoded paths; repeat the flag for each lib you want tested) |
| `eda-agent compute-status` | Whether the optional Rust compute wheel is installed, which API version it speaks, and which implementation (`python` or `rust`) the current `--compute` / `EDA_AGENT_COMPUTE` setting would run. Exit 1 when `rust` is requested and cannot be served. See [Rust compute](RUST_COMPUTE.md) |
| `eda-agent health` | Fast offline preconditions: workspace dir + writable, pointer file + matches config, bundled scripts findable, bridge constructable. Exit 0 = clean, 1 = critical fail |
| `eda-agent perf [--json] [--limit N]` | Where call time actually goes. Joins `bridge_trace.log` (the round trip the caller saw) with `activity.log` (the handler time inside Altium) on the request id, and reports both plus the difference, per command. See [Measuring performance](USING_THE_TOOLS.md#measuring-performance) |

## Browser UIs

| Command | Purpose |
|---|---|
| `eda-agent dashboard [--host H] [--port N] [--debug]` | Launch the local web dashboard (http://127.0.0.1:8766) on its own, rather than in-process with the server |
| `eda-agent stop-dashboard` | Stop a detached dashboard by the PID in `workspace/dashboard.pid`. Idempotent: no PID file means nothing to stop |
| `eda-agent vote` | Launch the pairwise layout-preference vote UI in a browser |

## Altium SDK reach

| Command | Purpose |
|---|---|
| `eda-agent sdk-coverage [--json] [--detail N] [--sdk-json PATH]` | How much of Altium's own SDK the generic property layer reaches. Reflects `Altium.SDK.Interfaces.dll` from the local install into `reference/altium_sdk.json` (via `scripts/altium_sdk_reflect.ps1`), then measures the four Pascal property chains against it and names every property that is declared but unreachable. See [SDK coverage](SDK_COVERAGE.md) |
| `eda-agent sdk-probe [--designator REF] [--properties A,B,C] [--json]` | Which declared properties the running Altium actually exposes. The assembly says a property exists; only the engine says it is reachable, and an unexposed one faults where `Try/Except` cannot catch it. Probes read-only, one name at a time, logging each attempt before it is made so a dead loop names the property that killed it. Recover with `eda-agent relaunch`. See [SDK coverage](SDK_COVERAGE.md) |
| `eda-agent sdk-symbols [--refresh] [--name NAME] [--json]` | Which declared members DelphiScript can actually resolve, read offline from the scripting engine's own registered symbol table in `ScriptingSystem.dll`. A member registered only as `GetState_Name` does not resolve as a property: touching it raises `Undeclared identifier` and halts the script debugger, which no `Try/Except` catches. It is reachable through that getter instead, so the generator checks the table and picks the spelling that resolves. See [SDK coverage](SDK_COVERAGE.md) |

## Owner profiles

| Command | Purpose |
|---|---|
| `eda-agent profile-init <name> [--here]` | Scaffold a new owner profile (naming/rules/fab/footprint conventions). `--here` makes it project-local (`.eda-agent/profile/`, meant to be committed); without it, a centrally-switchable `~/.eda-agent/profiles/<name>/`. See [Profiles](PROFILES.md) |
| `eda-agent profile-list` | List central profiles and which one is active |
| `eda-agent profile-show [--json]` | Show the profile actually in effect for the current directory |
| `eda-agent profile-use <name>` | Set the centrally active profile |
| `eda-agent profile-validate [name] [--here]` | Load every file in a profile and report errors |
| `eda-agent profile-new-project <template> <dest> [--force]` | Copy a profile's `templates/<name>/` tree to a new project directory |
| `eda-agent phases-show [--json]` | Which [NPI stages](USING_THE_TOOLS.md#npi-phase-gates) are in force, and which setting decided it |
| `eda-agent phases-enable [STAGE...]` | Run the named stages (`evt dvt pvt`), or all six when none are named. Off by default |
| `eda-agent phases-disable` | Stop running them. Answers already recorded are kept |

## Design modes

| Command | Purpose |
|---|---|
| `eda-agent mode-list` | List the [design modes](DESIGN_MODES.md) and which one is selected |
| `eda-agent mode-show [name] [--json]` | One mode in full: stages, inputs, rules, stage notes |
| `eda-agent mode-use <name>` / `--clear` | Select the mode a run uses when it names none |
| `eda-agent mode-init <name> [--extends MODE]` | Scaffold `modes/<name>.json` in the active profile |
| `eda-agent --mode <name> <command>` | Use a mode for one command (same as `EDA_AGENT_MODE`) |

## Offline analysis (no running Altium)

| Command | Purpose |
|---|---|
| `eda-agent review --offline <file> [--json/--sarif] [--fail-on ...]` | **Offline** component-level design review of a `.SchDoc`/`.PrjPcb` (no Altium). Opt-in (`--offline` or `EDA_AGENT_HEADLESS_REVIEW=1`); exit 1 on findings at/above `--fail-on` - a hardware-CI gate |
| `eda-agent bom --offline <file> [--csv/--json]` | **Offline** consolidated BOM from a `.SchDoc`/`.PrjPcb` (no Altium). Opt-in |
| `eda-agent netlist --offline <file> [--json] [--fail-on ...]` | **Offline** geometric netlist reconstruction + connectivity ERC (`single_pin_net`, `net_short`) from a `.SchDoc` (no Altium). Opt-in; exit 1 on findings at/above `--fail-on` |

## Engineering calculators

| Command | Purpose |
|---|---|
| `eda-agent calc [NAME] [KEY=VALUE ...] [--json]` | The flagless form of the verbs below: `eda-agent calc fusing-current width_mils=10`. Takes the verb (`calc-fusing`), the verb without its prefix (`fusing`) or the underlying function (`fusing-current`). Bare `eda-agent calc` lists them with the inputs each takes |
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
| `eda-agent calc-cross-check <calculator> [--arg NAME=VALUE]` | Compare a calculator against a locally built pcb-toolkit |

## Fab capabilities and materials

| Command | Purpose |
|---|---|
| `eda-agent stackup-vendors [--json]` | Bundled fab vendors, with source and age |
| `eda-agent stackup-show <vendor> [--json]` | One vendor's minimums and stackup templates |
| `eda-agent stackup-profile <vendor> [--fill NAME=MILS]` | Emit a fab profile JSON, or say what the vendor does not publish |
| `eda-agent laminates [--json]` | Laminate properties, with both dielectric constants |
| `eda-agent fab-list [--json]` | Fab connectors: which fabs have design-rule data, its status and age |
| `eda-agent fab-rules <fab> --layers N [--oz 1] [--tier T] [--ipc-class 2\|3]` | Every published design rule of one build, each with its quote |
| `eda-agent fab-compare --layers N [--oz 1] [--key K]` | All fabs and tiers side by side, in mils |
| `eda-agent fab-refresh <fab> [--file saved.html] [--write]` | Re-read a fab's pages and check every stored quote is still there |

## IPC standards

IPC values are never shipped; they come from your licensed copy, in the profile. See [IPC standards and rule values](IPC_STANDARDS.md).

| Command | Purpose |
|---|---|
| `eda-agent std-list [--domain D] [--priority 1\|2\|3] [--json]` | The IPC standards catalogue: current revision, scope, priority |
| `eda-agent std-rules [--category C] [--json]` | IPC rule definitions and which have values in the active profile |
| `eda-agent std-floors [--json]` | Write `standards/ipc_class_floors.json` (read by `fab-rules --ipc-class`) from the profile's values |

## Altium 365

| Command | Purpose |
|---|---|
| `eda-agent altium365-login` | Sign in to Nexar in the browser for the `altium365_` tools (needs `EDA_AGENT_NEXAR_APP_ID`) |
| `eda-agent altium365-logout` | Forget the saved Nexar login |
| `eda-agent altium365-scan-library` | Copy a workspace's managed library into the active profile, continuing an unfinished scan |
| `eda-agent altium365-update-library` | Refresh that copy without reading the whole library again |

## Experimental

| Command | Purpose |
|---|---|
| `eda-agent eco-execute --confirm [--validate-only] [--allow-confirm-dialogs] [--wait-first N] [--budget N]` | **Experimental, unverified.** Watches for and auto-presses Altium's Engineering Change Order dialog (Validate Changes / Execute Changes / Close), via the existing `app_drive_dialogs` reactive driver. Off unless `--confirm`; does not trigger an ECO itself (trigger separately with `proj_sync_pcb` or Design > Update PCB Document while this runs). See [ECO update](ENGINEERING_TOOLS.md#eco-update-experimental) |

Every `calc-*` verb takes `--json` for the full reply and exits non-zero
on a refusal, so a script can tell "this geometry is impossible" from
"here is your answer". See [Calculators](FAB_AND_CALCULATORS.md#calculators).
