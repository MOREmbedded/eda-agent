# Owner profiles

A **profile** is a directory of optional files declaring one owner's house
style: naming conventions, design rules, fab/stackup profiles,
footprint-authoring defaults (pad corner rounding and the like),
footprint-policy pins, schematic unit/grid settings, and project templates.
Useful when you work across several companies or clients, each with their own
conventions, and want eda-agent to pick up the right one depending on where
you're working.

Every file in a profile is optional. A profile can define just one piece
(say, only `footprint_defaults.json`) and leave the rest to eda-agent's
bundled defaults. With no profile active at all, every tool behaves exactly
as it always has -- this is purely additive.

## Directory shape

```
profile/
  profile.json              # {"name": "...", "description": "..."}
  naming.json                # engineering.naming.NamingPolicy's own format
  company_goals.json         # engineering.ipc7351.load_goals's own format
  rules/                     # engineering.rules.RuleStore's own format
    company.json              #   (one ruleset per file)
  fab_profiles/               # design.fab_profile.load_fab_profile's own format
    jlcpcb_4layer.json         #   (one stackup per file)
  footprint_defaults.json    # {"pad_corner_radius_pct": 1}
  footprint_policy.json      # design.footprint_policy's explicit policy dict
  document_settings.json     # schematic units/grid (see below)
  design_rules.json          # targets the board's DRC rules are built from
  erc_levels.json            # the project's ERC matrix (the default set)
  erc_levels/                 # named ERC sets, each able to extend another
    strict.json                #   (one policy per file)
  phase_gates.json           # which NPI stages this owner runs (default: none)
  preferences.json           # value format, safety switches, default design mode
  modes/                      # design modes (see DESIGN_MODES.md)
    nxp_redraw.json            #   (one mode per file)
  templates/                  # plain files, copied as-is
    default/                   #   a whole project (profile-new-project)
      ...
    documents/                 #   single sheets/boards (profile_new_document)
      A4_Titleblock.SchDoc
```

`naming.json`, `company_goals.json`, `rules/*.json` and `fab_profiles/*.json`
reuse formats that already existed before profiles did -- see
[ENGINEERING_TOOLS.md](ENGINEERING_TOOLS.md) for `naming.json` and
`company_goals.json`, and `engineering/rules.py` / `design/fab_profile.py` for
the other two. `footprint_defaults.json`, `footprint_policy.json`,
`document_settings.json`, `design_rules.json`, `erc_levels.json`,
`erc_levels/`, `phase_gates.json`, `preferences.json` and `templates/` are
new, documented below.

## How a profile is found

First match wins:

1. `EDA_AGENT_PROFILE_DIR` environment variable, if set.
2. Walking up from the current directory for `.eda-agent/profile/` -- a
   **project-local** profile. Meant to be committed to that project's own
   repo so a whole team shares the same conventions. Unlike `.eda-local/`
   (gitignored, per-developer-machine scratch space for the IPC workspace and
   deployed scripts), this directory is **not** gitignored by default.
3. The centrally active profile: `~/.eda-agent/active_profile` names one of
   `~/.eda-agent/profiles/<name>/` -- a **named, switchable** profile, for
   picking which company you're working for right now regardless of which
   folder you're in.
4. None found -- no profile applies; nothing changes from eda-agent's
   out-of-the-box behavior.

A profile found via (2) overrides one found via (3): a project's own
committed profile always wins over whichever company you last switched to
centrally.

### Which one to use when

**The project-local profile is found by walking up from the current working
directory, so it only applies when eda-agent is RUN from inside that
project.** That holds for the CLI (you `cd` into the project and run
`eda-agent profile-show`). It usually does NOT hold for the MCP server: an
MCP client launches `eda-agent` from whatever directory it likes -- its own
install directory, your home directory -- and nothing re-points that at the
board you happen to have open in Altium. eda-agent has no way to ask Altium
for the active project's path on the Python side today, so there is nothing
better to walk up from.

So, in practice:

- **Driving Altium through an MCP client** -- use the central profile
  (`eda-agent profile-use acme`), or set `EDA_AGENT_PROFILE_DIR` in the
  client's own server configuration. Both are independent of cwd.
- **Running the CLI inside a project, or sharing conventions with a team
  through that project's repo** -- use the project-local
  `.eda-agent/profile/`.
- `eda-agent profile-show` always reports what would actually apply from
  where you are standing, which is the quickest way to check.

## CLI

```
eda-agent profile-init <name> [--here]   # scaffold a new profile
eda-agent profile-list                    # central profiles + which is active
eda-agent profile-use <name>              # set the centrally active profile
eda-agent profile-show [--json]           # the profile in effect right now
eda-agent profile-validate [name|--here]  # load everything, report errors
eda-agent profile-new-project <template> <dest> [--force]
```

Single documents are the `profile_new_document` tool rather than a CLI
command, because the useful half of it is opening the new sheet in the
Altium session you already have in front of you.


`profile-init acme` scaffolds `~/.eda-agent/profiles/acme/` with commented
example files; `profile-init acme --here` scaffolds `.eda-agent/profile/` in
the current directory instead, named "acme" in its `profile.json`, meant to
be committed.

Mutating a profile is a CLI-only action -- the MCP tool surface (below) is
read-only, the same discipline `engineering/rules.py` already documents for
rulesets ("the owner writes them; AI clients can list, read and run them but
never edit them").

### When a profile file is broken

A file that is present but unusable (malformed JSON, or valid JSON that
isn't an object) **degrades to its default rather than raising**. A profile
is read from places that must not care whether it is any good: at server
startup, and on the pad-authoring path. One stray comma should not take the
MCP server down or break `lib_add_footprint_pad`, and before this it did
both.

Those problems are not swallowed, they are relocated: `profile-validate`
exits non-zero and names every one, `profile-show` prints them, and
`profile_show()` returns them in an `errors` list. Run
`eda-agent profile-validate` after editing a profile.

The same command also applies the checks the consuming tools apply (an
unknown `schematic_units` name, a non-numeric grid or corner radius), and
names any **misspelled setting key** -- a typo like `schematic_unit` would
otherwise be ignored in silence, which looks exactly like the profile not
being picked up at all. Keys starting with `_` are left alone, so
`_comment` is a usable place for a note.

## MCP tools

- `profile_show()` -- the profile in effect right now.
- `profile_list()` -- central profile names + which is active.
- `eng_list_rulesets()` / `eng_load_ruleset(name)` -- list/load a ruleset from
  the active profile's `rules/`, ready for `eng_evaluate_rules`.
- `eng_profile_fab_profile(name)` -- read a named fab/stackup profile as a
  dict, ready for `design_load_fab_profile` / `design_synthesize_rules`.
- `profile_apply_document_settings()` -- apply `document_settings.json`'s
  schematic units/grid to the current schematic (Altium backend only).
- `profile_new_document(template, destination)` -- create a schematic or
  board from one of the profile's `templates/documents/` files and open it
  (Altium backend only).

## Dashboard

The dashboard (`eda-agent dashboard`, or the button on the Altium status
form) has a **Workspace** mode (key `8`), and the top bar carries a chip
naming the profile in effect. The chip's dot says where it came from:
green for the central choice, amber for a project profile, blue for
`EDA_AGENT_PROFILE_DIR`, grey for none. A red border means the profile has
problems `profile-validate` would report.

The mode shows:

- **Where it comes from.** The three steps of the resolution order above,
  each with what it holds, and which one decided. This is read inside the
  server process, so it is the answer the AI's tools get, which is not
  always the answer a shell in another folder gets.
- **Switch the central profile.** One button per profile under
  `~/.eda-agent/profiles/`. It writes the same pointer `profile-use` does.
  If a project profile or `EDA_AGENT_PROFILE_DIR` decides here, the
  switch is recorded and the page says it does not apply yet. The
  naming/IPC pointer `apply_env` set at startup follows the switch; one you
  set yourself is never touched. Which phase tools are advertised is fixed
  when the server starts, so a switch between profiles with different
  `phase_gates.json` says a restart is needed.
- **What it declares.** Design-rule targets, with a button that shows the
  rules `pcb_rules_plan` would put on a board (pure Python, Altium is not
  asked); rulesets with their rule counts by severity; every ERC set,
  resolved through `extends`; fab profiles' limits; project and document
  templates; naming, document settings, footprint defaults and policy, and
  phase gates. Any file name opens the file read-only. The one profile
  file the page edits is `preferences.json`, from the Settings card.
- **Settings.** Every setting of the Config tab on the Altium panel
  (component value format, edits, deleting, closing documents, menus,
  dialogs and keys, keep the board in view, dialogs a tool opens) and the
  design mode a run starts in. Each row shows the value in effect, where it
  comes from (this profile, the Altium panel, `EDA_AGENT_MODE`,
  `eda-agent mode-use` or the default) and a menu that sets it in this
  profile, or "Not set here" to take it out again. Changes are written to
  the profile's `preferences.json` (see below), so switching profile
  switches them. The panel's own file is shown but never written from the
  page: the panel rewrites it whole from what it holds whenever you click
  there. With no profile in effect the card shows the panel's choices and
  cannot save.
- **Hold the open design to it.** Compare (`profile_diff_all`), preview
  (`profile_apply_all`, dry run) and apply (`profile_apply_all`,
  `dry_run=False`). Apply asks for a second click, since it writes to
  Altium. It only adds and updates, never deletes.

## `footprint_defaults.json`

```json
{
  "pad_corner_radius_pct": 1
}
```

Overrides the hardcoded default (25, Altium's own typical roundrect corner
percentage) used by `lib_add_footprint_pad`, `lib_create_standard_footprint`
and `lib_add_footprint_pads` whenever a call doesn't specify its own
`corner_radius`. Explicitly passing `corner_radius` on any call always wins
over the profile.

This is the one example from the field that motivated this file: a company
convention of 1% rounding (effectively square corners with just enough
rounding to help solder paste release during reflow) instead of Altium's own
25% default.

## `footprint_policy.json`

Matches `design/footprint_policy.py`'s explicit-policy dict verbatim -- the
same shape you'd pass inline to `lib_audit_footprint_policies`'s `policy`
argument:

```json
{
  "silk_layer": "Top Overlay",
  "assembly_layer": "Mechanical 13",
  "courtyard": true,
  "three_d_model": true,
  "pin1_marker": true,
  "designator_layer": "Top Overlay",
  "designator_height": 50,
  "designator_center_tol": 20,
  "designator_present": true
}
```

Every key is optional; an omitted dimension is inferred from the library's
own dominant convention instead of pinned to a required value (unchanged
from `lib_audit_footprint_policies`'s existing inferred mode). When the
active profile defines this file, `lib_audit_footprint_policies` uses it
automatically unless the caller passes an explicit `policy` argument
(including an explicit `{}`, which forces pure inferred mode).

## `document_settings.json`

```json
{
  "schematic_units": "mil",
  "pcb_units": "mm",
  "schematic_snap_grid_mil": 100,
  "schematic_visible_grid_mil": 100
}
```

`profile_apply_document_settings()` applies `schematic_units` (via the
existing `sch_set_units`) and the two grid values (via the existing
`obj_set_grid`) to the currently open schematic.

**PCB-side units/grid are documentation only for now.** No bridge command
exists yet to set a board's display units or default snap grid -- there is no
tool to set them in `scripts/altium/` today, only `pcb_snap_to_grid` (snaps
existing objects) and `pcb_make_paste_grid` (a paste-mask pattern), which are
different utilities. The pcb_units setting above is still read by
`profile_show`/`profile-show` so the convention is visible, it just isn't
auto-applied to a live PCB document. A project template (below) sidesteps
this for brand-new projects, since a template's own documents already carry
whatever unit/grid state was saved into them.

## `design_rules.json`

```json
{
  "fab_profile": "jlcpcb_4layer",
  "net_classes": {"VBUS": "power", "GND": "power"},
  "options": {"class_current_a": {"power": 2.0}, "diff_pair_target_ohms": 90}
}
```

The board's Altium design rules, managed from the profile by three tools:

| | |
|---|---|
| `pcb_rules_plan` | what the profile asks for. Pure Python, no Altium. |
| `pcb_rules_diff` | how the open board differs from it. |
| `pcb_rules_apply` | closes the gap. `dry_run=True` by default. |

**This file holds the TARGETS, not the rules.** `fab_profile` names a file
in the profile's `fab_profiles/` (or carries the profile inline);
`net_classes` and `options` feed the same synthesis `design_synthesize_rules`
uses. The rules themselves are derived on every call, because
`design/rule_synthesis.py` asserts that each number it emits traces to a
fab-profile field or a verified calculator (IPC-2221 width, IPC-2141
impedance). A rule stored here could only drift away from the calculator
that justified it.

**Apply is idempotent, and never deletes.** A rule already on the board is
updated in place. Altium's own `PCB_CreateDesignRule` sets the name and adds
with no check for one already there, so applying a set twice without this
leaves two rules sharing a name and Altium's priority order picks the
winner. Three things are reported rather than written:

- `extra` -- on the board, not in the profile. Altium ships default rules on
  every board, and a rule somebody added for this board deliberately is not
  drift. Seeing them is the point; removing them is the owner's call.
- `skipped` -- owned by the schematic (`from_schematic`). The next update
  from the schematic rewrites it, so writing it here is undone silently.
- `unwritable` -- a rule kind with no in-place writer. Deleting and
  recreating would lose the rule's priority and any scope edited by hand.

**Reading values back goes through Altium's prose.** Only a
clearance-family rule reports a number of its own; every other kind is
readable solely as `Rule.Descriptor`, the text Altium writes for the rules
dialog, so the comparison parses it. `tolerance_mils` (default 0.02) absorbs
the round trip through the millimetre descriptor: a rule written as 6 mil
reads back as `0.152mm`, which is 5.984 mil.

**`enabled` is not the flag that matters.** A rule has both `Enabled` and
`DRCEnabled`, and only the second decides whether DRC checks it.
Measured on a real six-layer board: 37 of its 159 rules read `enabled: true`
while DRC ignored them, and the only sign was the word `(Disabled)` inside
the descriptor. `pcb_get_design_rules` and `pcb_get_rule_properties` now
report `drc_enabled` directly.

## `erc_levels.json` and `erc_levels/`

```json
{
  "extends": "company",
  "levels": {
    "Nets with multiple names": "no_report",
    "Nets with no driving source": "error",
    "Floating power object": "fatal"
  },
  "unlisted": "leave"
}
```

Project Options > Error Reporting: the level every ERC violation type is
reported at, one of `no_report`, `warning`, `error` or `fatal`. The matrix
lives in the `.PrjPcb`, so every new project starts from Altium's defaults
and a house policy survives only as long as somebody remembers to re-enter
it. Three tools manage it from the profile:

| | |
|---|---|
| `proj_erc_plan` | what the profile asks for. Pure Python, no Altium. |
| `proj_erc_diff` | how the open project differs from it. |
| `proj_erc_apply` | closes the gap. `dry_run=True` by default. |

**Sets.** `erc_levels.json` at the profile root is the default set, reached
by naming no set at all. Any number of named ones live in
`erc_levels/<name>.json` and are chosen with `set_name`. A set may
`extends` another, which is resolved before anything is compared, with the
nearer file winning; `chain` in the reply names the files that were merged,
base first, so a level nobody expected can be traced to the set that asked
for it. That is what keeps a company base policy and a per-workspace
narrowing of it two files rather than two copies.

A level of `null` **drops** an entry an extended set made. "This workspace
does not manage that type" is a different statement from "set it to
no_report", and without it a company set could be overridden but never
narrowed.

**A type is named by Altium's own prose.** `DM_ViolationTypeDescription` is
the only name there is, so a type is written exactly as
`proj_get_erc_levels` lists it, matched ignoring case and surrounding
space. Its kind number works too. A name this Altium does not have is
reported under `unknown` rather than skipped: a policy quietly missing half
its entries, because it was written against a version that spelled
something differently, is worse than one that says so.

**Unlisted types are left alone.** A set naming 12 types is a policy about
those 12, not an assertion that the other 140 belong at Altium's defaults,
so the rest come back under `unmanaged` untouched. `"unlisted": "<level>"`
opts into pinning the whole matrix for an owner who wants exactly that, and
`unlisted=` on any of the three tools overrides the file for one call.

"The whole matrix" is the types Altium actually has, which is a narrower
claim than it used to be. `DM_ViolationTypeDescription` answers by index
with no end marker and, past the end of the enum, returns adjacent memory
read as a string; the reader used to scan a fixed range and keep anything
non-empty, which on AD 26.11 reported 295 types for a project that has 167.
It now stops at the first empty description and reports `type_count` and
`beyond_table` so the bound is visible. Without that, pinning the matrix
would have written levels into the 128 indices past the end of the array
`DM_SetErrorLevels` writes to.

**Apply writes only what differs**, so a second apply writes nothing.
Altium reads every write back, and a level that did not take arrives with a
`problem` rather than being counted as set. The project is saved afterwards
unless `save=False`, because an unsaved matrix is lost when the project
closes.

`eda-agent profile-validate` resolves every set in the profile, including
the ones only reached through `extends`, so a set naming a base that is not
there is a problem reported there rather than a policy that silently sets
nothing.

## Applying the whole profile at once

```
profile_diff_all    # how what is open differs from everything the profile declares
profile_apply_all   # closes the gap. dry_run=True by default.
```

Each section runs its own manager: `pcb_rules_diff` over the board's design
rules, `proj_erc_diff` over the project's ERC matrix. Two rules keep the
combined answer honest.

**A section the profile does not define is skipped, not failed.** A profile
declaring only an ERC policy is a complete profile, and saying
"design_rules.json is not a file" about it would be noise dressed as a
problem.

**One failing section does not hide the others.** The design rules need a
board open and the ERC matrix needs a project, so a caller with only one of
them still gets the other's answer. `applied` and `failed` name the
sections either way, because a partial apply is a real outcome rather than
an error.

`in_step` reports only the sections that actually compared something.
`document_settings` is reported as apply-only: nothing reads a schematic's
units and grid back, so there is no comparison to make, and leaving it out
entirely would make a profile that sets it look ignored. It is also the one
section that reads the **active** profile rather than `profile_dir`, so
pointing `profile_dir` elsewhere refuses it by name instead of writing
another profile's units.

## `phase_gates.json`

```json
{
  "stages": ["evt", "dvt", "pvt"]
}
```

Which [NPI stages](NPI_PHASE_GATES.md) this owner runs. An empty list is
the default, and it leaves the three `design_phase_*` tools unadvertised
rather than advertised and refusing.

It is a profile setting because which gates a programme runs follows the
company rather than the board in front of you, and a project-local
profile commits it with the project, so a team shares one answer. Set it
with `eda-agent phases-enable evt dvt pvt`, which preserves anything else
in the file; `eda-agent phases-show` says which setting is deciding.

`EDA_AGENT_PHASES` and `--phases` overrule it for one run, which is how
an MCP client with an `env` block and no profile directory reaches it.
The stages take the same spellings everywhere, plus `all` and `none`;
`none` exists because an empty variable means "the profile decides", so
without the word a shell could enable stages over a profile but never
disable them.

The tool surface is chosen at startup, so a change takes a restart.

## `preferences.json`

```json
{
  "_comment": "Acme: SI values with units, never delete from the AI",
  "value_format": "si",
  "value_unit": "with",
  "deleting": "refused",
  "default_mode": "reference_redraw"
}
```

The same keys as the Config tab on the Altium panel, which keeps its own
`preferences.json` in the bridge folder (one per machine), plus
`default_mode`. Every key is optional. Set it from the dashboard's Settings
card or by hand.

| Key | Values | When both say something |
|---|---|---|
| `value_format` | `none`, `rkm` (4K7, 2u2), `si` (4.7k, 2.2u) | the profile wins |
| `value_unit` | `without`, `with` (only with `si`) | travels with `value_format` |
| `keep_in_view` | `off`, `on` | the profile wins |
| `dialogs` | `auto`, `manual` | the profile wins |
| `edits` | `allowed`, `readonly` | the stricter wins |
| `deleting` | `allowed`, `refused` | the stricter wins |
| `closing` | `allowed`, `refused` | the stricter wins |
| `ui_automation` | `on`, `off` | the stricter wins |
| `default_mode` | any mode's name | see [DESIGN_MODES.md](DESIGN_MODES.md) |

`value_format` and `value_unit` are one choice: a profile that sets the
format also decides the unit (`without` unless it says `with`), so a format
from the profile is never combined with a unit from the panel, and a
`value_unit` alone does nothing.

The four safety switches can only be made stricter by a profile. A profile
can switch edits to read-only for a client whose designs the AI must not
change; it cannot switch back on what you turned off on the panel. A
refusal names the file that switched it off.

The Altium panel shows only its own file, so with a profile in effect its
Config tab can differ from what the tools do; the Settings card shows both
and which one decides. The Pascal side also enforces the panel's own
safety switches by itself, which is why a profile can add to them but never
remove them.

`profile-validate` names an unknown key, a value a key cannot take, a
`value_unit` without `value_format`, and a `default_mode` that is not a
mode. A broken file is skipped: the panel's choices apply.

## `templates/<name>/` and `templates/documents/`

Two kinds, kept apart because they are used differently.

**Whole projects** live in `templates/<name>/`, one directory each, copied
as-is by `profile-new-project <name> <dest>`. Altium project files are just
files on disk, so no DelphiScript is involved. Build one by setting up a
project the way you want new ones to start (units, grid, a starter sheet,
whatever) and copying its files in.

**Single documents** live in `templates/documents/`, one file each, used by
the `profile_new_document` tool:

```
templates/
  StarterProject/        <- a whole project, for profile-new-project
    Board.PrjPcb
    Sheet1.SchDoc
  documents/             <- single sheets and boards
    A4_Titleblock.SchDoc
    FourLayer.PcbDoc
```

`profile_new_document("A4_Titleblock", "D:/work/Sheet2.SchDoc")` copies the
template to that path and opens it in Altium. Altium exposes **no API that
applies a document template** -- its own New Document makes a blank sheet,
and so does the bridge's `create_document` -- so copying the file and
opening it is what applying a template amounts to. Altium's own sheet
templates are `.SchDot`, but a saved `.SchDoc` works just as well as a
starting point and is easier to edit, so both are listed.

The template's own extension always wins: asking for a `.SchDoc` template
at a `.PcbDoc` destination would write a file Altium cannot open. An
existing destination is refused unless `overwrite=True`, and if the copy
succeeds but Altium will not open it, the result still reports the file
that was written rather than losing it.

### Not covered: Altium preferences and Draftsman

**Altium preferences** cannot be templated from here. There is no scripting
API for the Preferences dialog: `app_get_preferences` reads the active
*document's* grid and units, not DXP preferences, there is no writer, and
dispatching `Tools|Preferences` as a process returns without opening
anything. Reaching it would mean UI automation, which this project keeps
for cases with no alternative.

**Draftsman** documents are not supported at all -- no tool, no bridge
command, no handler. That is a feature in its own right rather than a
gap in this one.

Document units and grid are a separate mechanism that *does* work: see
`document_settings.json` above.

## What this does not cover yet

- **Auditing existing footprints** for corner-radius/pad-shape compliance:
  the bridge command behind `lib_add_footprint_pad`'s reads
  (`library.get_footprint_pads`) doesn't return a pad's actual shape or
  corner radius today, so there's nothing to check an existing library
  against yet. Would need a DelphiScript extension.
- **EasyEDA's own footprint-authoring tools** use a different convention
  entirely (absolute mils, default 0, not a percentage) and are not covered
  by `footprint_defaults.json` today.
- **PCB-side units/grid automation** -- see above.
- **Symbol-specific conventions** (line widths, pin lengths): `eng_audit_symbol`
  only takes a `grid` parameter today, with no policy hook at all.
