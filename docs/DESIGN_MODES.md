# Design modes

A **design mode** is a preset for the kind of job a design run is doing.
The autonomy harness (see [AUTONOMOUS_DESIGN.md](AUTONOMOUS_DESIGN.md))
walks one 13-stage pipeline from a written requirement to a fab package.
Most real jobs are a part of that, with rules of their own: redrawing a
vendor's dev kit schematic is five of the stages and must not "improve"
anything; laying out a finished schematic is the other eight and must not
touch the schematic. A mode says:

- which **stages** the run walks (always in pipeline order);
- which extra **rules** bind it, served with every `design_next_action`;
- what each stage should do differently (**stage notes**, appended to the
  stage's guidance);
- which **inputs** the run needs before it can start (vendor, board,
  source document...);
- which pieces of the owner profile it expects (**profile hints**: an ERC
  set, a ruleset, a fab profile, a template). These are reported, never
  applied on their own.

A mode does not change which tools exist. Picking one never needs a
server restart, and every mode works on every backend the design harness
runs on.

## Built-in modes

| Mode | Stages | For |
|---|---|---|
| `full_design` | all 13 | A requirement to a finished board. The default, and exactly what a run did before modes existed. |
| `schematic_only` | requirement → schematic_emit | A requirement to an ERC-clean schematic, no board. |
| `layout_only` | sch_to_pcb → outputs | A finished schematic to a routed, checked board. The schematic is not changed; a needed change is a question for the user. |
| `reference_redraw` | requirement, plan, plan_verification, library_readiness, schematic_emit | Redraw the schematic of a vendor dev kit or reference design (NXP EVK, ST Nucleo/Discovery, TI LaunchPad, Espressif DevKit...) in your own libraries and house style. Faithful copy: every part cites its reference page, DNP parts stay not fitted, net names are kept, substitutions need your yes. Inputs: `vendor`, `board`, `source`, optional `scope`. |
| `devkit_derivative` | all 13 | A new board from a dev kit: keep the proven core (processor, power, clocks, boot, debug) as drawn, drop what you do not need, add your own blocks. Every deviation from the reference is listed with its reason. Inputs: `vendor`, `board`, `source`, optional `changes`. |
| `reusable_block` | all 13 | One self-contained circuit block (a regulator, a PHY, a USB-C input, a sensor front end) to publish as an Altium reuse block or managed sheet. The architecture stage defines its interface (ports, rails, limits, what the host must provide); every connection goes through a named port; parts only from the profile's library; a compact routed layout fragment inside a stated area with marked port exits; no board outline, fab package or connectors. Outputs are the block's datasheet; publishing is your step. Inputs: `block`, optional `interface`, optional `publish_to` (default "Altium 365 reuse block"). |

`eda-agent mode-show <name>` prints a mode in full: its stages, the
stages it skips, inputs, rules and stage notes.

A schematic-only reusable block (no layout fragment) is a profile mode:
`{"extends": "reusable_block", "stages": ["requirement", "architecture",
"plan", "plan_verification", "library_readiness", "schematic_emit"]}`.

Both reference modes use only what the vendor publishes for that board.
The NDA rule of the design discipline still holds: no other client's
design is ever a source.

## Choosing a mode

First match wins:

1. The `mode` argument of `design_session_start`.
2. `EDA_AGENT_MODE`, also set for one command by the global `--mode`
   flag (`eda-agent --mode layout_only serve`).
3. `default_mode` in the active profile's `preferences.json`, so each
   company or client starts its runs in its own mode. Set it from the
   dashboard (Workspace, key `8`, Settings card) or by hand; see
   [PROFILES.md](PROFILES.md).
4. `~/.eda-agent/active_mode`, written by `eda-agent mode-use <name>`.
   One per machine; `mode-use` says so when the profile's `default_mode`
   overrules it.
5. `full_design`.

An unknown name is refused with the list of modes there are. A run never
falls back to the full pipeline when you asked for a redraw.

The mode a run starts in is **recorded in its journal**, whole: the stage
list, rules and notes as they were at the start, and the inputs. Editing
or deleting the mode file later does not change a run under way, and a
resumed run walks the stages it started with. A journal written before
modes existed walks all 13 stages.

## From an MCP client

```
design_list_modes()                       the modes, and which is selected
design_get_mode("reference_redraw")       one mode in full
design_session_start(requirement, mode="reference_redraw",
                     inputs={"vendor": "NXP",
                             "board": "MIMXRT1170-EVKB rev B",
                             "source": "https://.../MIMXRT1170-EVKB-schematic.pdf",
                             "scope": "MCU, power, debug"})
design_next_action(session_id)            now carries mode, mode_note, mode_rules
```

A required input left out is refused with the names of the missing ones.

Each mode is also an **MCP prompt**, `mode_<name>` (Claude Desktop lists
them under "+", Claude Code as `/mcp__<server>__mode_reference_redraw`).
Its arguments are the mode's inputs; the text it sends asks the model to
start the run in that mode and loop the harness. An input left blank is
marked "ask me".

The design report (`design_session_report`) gains a **Mode** section
with the inputs, marks stages outside the mode as "not in mode", and
lists as unreached only the mode's own stages.

## From the CLI

```
eda-agent mode-list                          modes, which is selected and why
eda-agent mode-show [name] [--json]          one mode in full
eda-agent mode-use <name> | --clear          select the default mode
eda-agent mode-init <name> [--extends MODE] [--force]
                                             scaffold modes/<name>.json in
                                             the active profile
eda-agent --mode <name> <command>            for one command
```

## Your own modes

Modes live in the owner profile, in `modes/<name>.json`, so each company
or client you work for can have its own (see [PROFILES.md](PROFILES.md)).
The file name is the mode's name: lowercase letters, digits and
underscores. Every key is optional:

```json
{
  "_comment": "ACME's way of redrawing an NXP evaluation kit",
  "extends": "reference_redraw",
  "title": "Redraw an NXP EVK",
  "description": "NXP EVK schematic into ACME libraries, A3 title block.",
  "inputs": [
    {"name": "vendor", "description": "Vendor", "default": "NXP"},
    {"name": "board", "description": "EVK name and revision"},
    {"name": "source", "description": "Schematic PDF URL or path"}
  ],
  "rules": ["Use the ACME A3 title block on every sheet."],
  "stage_notes": {
    "library_readiness": "Look in the ACME_NXP DbLib before anything else."
  },
  "profile_hints": {"erc_set": "strict", "document_template": "A3_Titleblock"}
}
```

How a file combines with what it `extends`:

- `rules` are **added** to the base's;
- `stage_notes` are **merged** stage by stage;
- `profile_hints` are merged key by key;
- `stages` and `inputs` **replace** the base's when given;
- `title` and `description` replace when given.

A file with no `extends` is the whole mode. Given a built-in's name, it
replaces that built-in. `extends` may name a built-in or another profile
mode; a loop is reported.

`profile_hints` keys: `erc_set`, `ruleset`, `fab_profile`,
`document_template`, `project_template`.

A broken mode file never stops the server: it is skipped and named by
`eda-agent profile-validate`, `eda-agent mode-list` and
`design_list_modes` (unreadable JSON, an unknown stage or key, a misspelt
setting, an `extends` that names no mode).
