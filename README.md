<div align="center">

# eda-agent

**Let an AI work inside your live Altium Designer session.**

An MCP server that gives Claude, or any MCP client, around 800 tools on the design you have open:
schematic, PCB, library, project and review. You watch every change happen in Altium.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-website-0A7E8C.svg)](https://morembedded.github.io/eda-agent/)
[![Tests](https://github.com/MOREmbedded/eda-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/MOREmbedded/eda-agent/actions/workflows/tests.yml)

[**Website**](https://morembedded.github.io/eda-agent/) ·
[Quick start](#quick-start) ·
[Features](docs/FEATURES.md) ·
[Documentation](#documentation) ·
[Changelog](CHANGELOG.md)

[![eda-agent demo: a round RGB LED board from spec to Gerbers in Altium Designer](assets/demo_round_led.jpg)](https://youtu.be/JnQsJlrpzBM)

*From a written spec to Gerbers: a 4-layer round LED board designed live in Altium ([watch the video](https://youtu.be/JnQsJlrpzBM)).*

</div>

> [!WARNING]
> **Experimental.** Not every tool is tested on every Altium version, and some can crash Altium's script engine.
> Back up your project first and read [Known limitations](docs/LIMITATIONS.md).

## Why eda-agent

- **Live, not batch.** Altium stays open and in charge. The AI reads the design you have open, answers questions about it, and edits it in place.
- **Built for real review work.** One call gives a design snapshot. A 31-check lint sweep, schematic and PCB netlist cross-checks, and fab package verification come with it.
- **Datasheet first.** The tools tell the AI to fetch the vendor datasheet, and never to make values up.
- **Fast.** Nearly every tool takes a list, so 20 edits are one call and one undo step. The auto-placer scores only what each move changed (6 to 13 times faster), and an optional Rust core speeds up routing.
- **Your house style.** Profiles hold pad rounding, design rules, ERC levels, fab stackups and templates, one per company or project.
- **More than Altium.** KiCad 10 and EasyEDA Pro backends use the same tool surface. 773+ with both backends registered.

## Quick start

You need **Windows**, **Python 3.11+** and **Altium Designer** (AD20 or newer is best).

**1. Install**

```bash
git clone https://github.com/MOREmbedded/eda-agent
cd eda-agent
python -m pip install -e .
eda-agent install-deps --all     # optional: every extra feature in one go
```

`eda-agent features` shows what is installed and what each feature needs;
the dashboard's **Features** page does the same with install buttons. See
[Features and dependencies](docs/FEATURES_AND_DEPENDENCIES.md).

**2. Register it with your MCP client**

```bash
claude mcp add -s user altium eda-agent
```

<details>
<summary>"eda-agent is not recognized", or a client other than Claude Code</summary>

If `eda-agent` is not on your `PATH`, register it through Python. The `--` tells Claude Code that the rest is the command to run:

```bash
claude mcp add -s user altium -- python -m eda_agent
```

Any other MCP client: add a local **stdio** server that runs `eda-agent` (or `python -m eda_agent`).

</details>

**3. Load the scripts into Altium**

```bash
eda-agent install-scripts
```

In Altium, open **Preferences → Scripting System → Global Projects → Install from file** and pick `Altium_API.PrjScr`. You only do this once.
Then use **File → Run Script → Altium_API → Dispatcher.pas → StartMCPServer** to start the link.

**4. Check it works**

```bash
eda-agent doctor
```

Every line should pass. In Claude Code, `/mcp` lists `altium` as connected. Now ask something like *"List every part on the 3V3 rail and flag any whose datasheet limit is below 3.3 V."*

The full guide, with updates, workspaces and profiles, is in [Getting started](docs/GETTING_STARTED.md).

## What it can do

| | |
|---|---|
| 🔍 **Design review** | Snapshot, lint, ERC and DRC, schematic and PCB cross-checks, rotation suggestions for top and bottom parts. See [Features](docs/FEATURES.md) |
| ✏️ **Edit schematics and boards** | Place, wire, route, move and set parameters, in bulk with one undo. See [Tool reference](docs/TOOL_REFERENCE.md) |
| 📚 **Libraries and parts** | Symbols, footprints, 3D models, and part search across distributors. Preview a part's symbol and footprint before placing it (`lib_preview_part`, or the dashboard's Library pane). See [Part sourcing](docs/PART_SOURCING.md) |
| 🏭 **Fab output** | Read the Gerbers back and check them against the board, plus JLCPCB and PCBWay stackups and a cited rules database for 13 fabs. See [Fab and calculators](docs/FAB_AND_CALCULATORS.md) |
| 📏 **Fab and IPC rule floors** | On demand, check the board's design rules against a fab's minimums and your IPC class floors, and raise the ones below them (`pcb_rules_fab_floors`). See [IPC standards](docs/IPC_STANDARDS.md) |
| 🧮 **Calculators** | A PCB toolkit of 44 calculators on the dashboard's Calculators tab and as `pcb_calc` tools: impedance and differential pairs from a 2D field solver, trace and via current from a thermal model of the board, planes, crosstalk, passives. See [PCB toolkit](docs/PCB_TOOLKIT.md) and [Fab and calculators](docs/FAB_AND_CALCULATORS.md#calculators) |
| 📈 **Simulation** | SPICE model library and LTspice round trip: find models by MPN, check pin maps, export and compare decks. See [SPICE models](docs/SPICE_MODELS.md) |
| 📡 **EMI/EMC and RF** | EMI pre-scan of a routed board (`sim_emc_prescan`), openEMS S-parameters and antennas, Touchstone files. See [EMI/EMC and RF](docs/EMC_RF.md) |
| 🧪 **Simulation engines** | One job model for every simulator: this project's own solvers, scikit-rf and openEMS today, plugins tomorrow. Each engine's answer is kept beside the others and compared (`sim_job_run`, `sim_job_compare`, the dashboard's Simulation page). See [Simulation engines](docs/SIMULATION.md) |
| 🧩 **Features and dependencies** | `eda-agent install-deps --all` installs every optional part in one command; `eda-agent features` and the dashboard's Features page show what is installed, its version and license, and switch engines on and off. See [Features and dependencies](docs/FEATURES_AND_DEPENDENCIES.md) |
| 🔋 **Power integrity (PDN)** | DC IR drop and current density on the real copper (`sim_pdn_ir_drop`), AC impedance against a target with a field-solved plane pair and every capacitor's mounting (`sim_pdn_impedance`). See [PDN analysis](docs/PDN.md) |
| 🤖 **Guided design runs** | Spec to board, reference redraw, dev-kit derivative. See [Design modes](docs/DESIGN_MODES.md) and [Autonomous design](docs/AUTONOMOUS_DESIGN.md) |
| 🚦 **NPI phase gates** | EVT, DVT and PVT checklists, answered by tools where possible. See [NPI phase gates](docs/NPI_PHASE_GATES.md) |
| ☁️ **Altium 365** | Workspaces, the managed library and releases, through Nexar or directly, plus an offline copy of the managed library. See [Engineering tools](docs/ENGINEERING_TOOLS.md) |

<p align="center">
<img src="assets/dashboard.png" alt="eda-agent status window inside Altium Designer" width="300">
<img src="assets/dashboard_config.png" alt="eda-agent status window, Config tab with AI permission switches" width="300">
</p>

## Documentation

The same documents, with search and navigation, are on the [website](https://morembedded.github.io/eda-agent/).

**Start here**

| Document | What it covers |
|---|---|
| [Getting started](docs/GETTING_STARTED.md) | Requirements, install, updates, checking the setup, workspace and profiles |
| [Features](docs/FEATURES.md) | How it works, the dashboards, the full feature list, example prompts |
| [Using the tools](docs/USING_THE_TOOLS.md) | Finding the right tool, clients that cap the tool count, backends, modes, phase gates, part sourcing, Altium 365, performance |
| [Known limitations and troubleshooting](docs/LIMITATIONS.md) | What can go wrong, how to recover, timeouts, common errors |

<details>
<summary><b>Guides</b></summary>

| Document | What it covers |
|---|---|
| [Profiles](docs/PROFILES.md) | Per-workspace house style: naming, rules, fab profiles, templates |
| [Fab output, stackups and calculators](docs/FAB_AND_CALCULATORS.md) | Checking the fab package, bundled fab data, laminates, calculators |
| [IPC standards and rule values](docs/IPC_STANDARDS.md) | Catalogue of IPC standards, rule definitions, your licensed values per profile |
| [PCB toolkit calculators](docs/PCB_TOOLKIT.md) | The Calculators tab: field-solver impedance and pairs, trace thermal model, vias, planes, crosstalk, passives, with measured accuracy |
| [EMI/EMC and RF field solving](docs/EMC_RF.md) | EMI pre-scan of a routed board, openEMS S-parameters and antennas, Touchstone files |
| [Simulation engines](docs/SIMULATION.md) | Engines, jobs, comparing answers, writing an engine plugin |
| [Features and dependencies](docs/FEATURES_AND_DEPENDENCIES.md) | What each optional part offers and needs, the one-command installer, the Features page |
| [PDN analysis](docs/PDN.md) | DC IR drop, current density, and AC impedance against a target impedance |
| [Design modes](docs/DESIGN_MODES.md) | Reference-design redraw, dev-kit derivative, schematic only, layout only |
| [Autonomous design](docs/AUTONOMOUS_DESIGN.md) | Driving a full spec-to-board run from any MCP client |
| [NPI phase gates](docs/NPI_PHASE_GATES.md) | The stage checks, and which ones a tool can answer |
| [Part sourcing](docs/PART_SOURCING.md) | The part providers, what each one answers, and what none of them do |
| [EDA backends](docs/BACKENDS.md) | The Altium, KiCad and EasyEDA Pro backends |
| [Engineering tools](docs/ENGINEERING_TOOLS.md) | The `eng_*` family, Altium 365, and what is unverified live |
| [SPICE models and LTspice](docs/SPICE_MODELS.md) | Vendor model index, pin maps, LTspice decks, export and compare |
| [Token usage](docs/TOKEN_USAGE.md) | Tokens the AI spends on each EDA tool, where it is shown and kept |
| [Hardware CI](docs/HARDWARE_CI.md) | `eda-agent review --offline` as a CI gate, with no Altium and no license |
| [Autorouter engines](docs/ROUTER_ENGINES.md) | The default router and Euan Mutch's, how to switch, what differs and what both keep |
| [Rust compute](docs/RUST_COMPUTE.md) | The optional compiled router core: the idea, the options considered, settings, measured speed-ups |
| [UI automation](docs/ui-automation.md) | What synthesised input can reach, and what contains it |

</details>

<details>
<summary><b>Reference</b></summary>

| Document | What it covers |
|---|---|
| [Tool reference](docs/TOOL_REFERENCE.md) | Every tool with its arguments, maturity and interaction badge (generated) |
| [All options](docs/OPTIONS.md) | Every setting in one generated list: flags, environment variables, Config tab, `workspace.json`, profile keys |
| [CLI](docs/CLI.md) | Every `eda-agent` command, grouped as `--help` groups them |
| [Configuration](docs/CONFIGURATION.md) | Environment variables, paths and logs |
| [The Config tab](docs/CONFIG.md) | Every setting on the Altium panel's Config tab, including the pre-build checks, and what to do when one refuses |
| [Tool naming](docs/TOOL_NAMING.md) | The `ns_verb_object` convention every tool name follows |
| [SDK coverage](docs/SDK_COVERAGE.md) | How much of Altium's object model the generic layer reaches |
| [Altium scripting coverage](docs/ALTIUM_SCRIPTING_COVERAGE.md) | Official Altium scripting examples mapped to tools, with known gaps |

</details>

<details>
<summary><b>Development</b></summary>

| Document | What it covers |
|---|---|
| [Development](docs/DEVELOPMENT.md) | Architecture, tests, project layout, the two version numbers |
| [Release verification](docs/RELEASE_VERIFICATION.md) | How a release's Pascal is recorded as read live, or declared unverified |
| [STEP placement verification](docs/step-placement-verification.md) | How placing a 3D body at an X/Y was checked on Altium 21 |
| [AI field notes on Altium](docs/AI_ALTIUM_FIELD_NOTES.md) | Altium behaviours measured while driving it, written as operating rules |
| [Altium autonomy runbook](docs/ALTIUM_AUTONOMY_RUNBOOK.md) | The by-hand runbook that drives a real Altium |
| [Changelog](CHANGELOG.md) | What each change added or fixed |
| [Contributing](CONTRIBUTING.md) | How to send a change |

</details>

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Disclaimer

**Use at your own risk.** `eda-agent` drives Altium Designer programmatically and can modify, save, or delete design data. An AI client operating it can issue rapid, irreversible changes. Before using this tool on any design:

- **Back up your project.** Commit to version control, copy the folder elsewhere, or both. Do not rely solely on Altium's own history.
- Expect the possibility of **data loss, corrupted documents, or Altium crashes**, especially on large boards, unusual object configurations, or untested API paths.
- Review automated changes before saving. Prefer working on a branch or a copy until you have trust in a given workflow.

This software is provided "as is", without warranty of any kind, express or implied. The authors and contributors are not liable for any damage to your designs, projects, data, or installation.

This project is not affiliated with, endorsed by, or sponsored by Altium Limited, the KiCad project, or EasyEDA. "Altium" and "Altium Designer" are trademarks of Altium Limited; "KiCad" and "EasyEDA" are trademarks of their respective owners. `eda-agent` is an independent community tool that interoperates with each of these applications through its own published API: Altium Designer via its scripting API, KiCad via its IPC API and command line, and EasyEDA Pro via its extension API.

## About

eda-agent is maintained in this fork by [MOREmbedded](https://github.com/MOREmbedded). It builds on, and tracks, the upstream [salitronic/eda-agent](https://github.com/salitronic/eda-agent) project. Thanks to everyone who has contributed to either, see the [contributors](https://github.com/MOREmbedded/eda-agent/graphs/contributors).
