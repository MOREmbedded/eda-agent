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
  templates/                  # project starter kits (plain files, copied as-is)
    default/
      ...
```

`naming.json`, `company_goals.json`, `rules/*.json` and `fab_profiles/*.json`
reuse formats that already existed before profiles did -- see
[ENGINEERING_TOOLS.md](ENGINEERING_TOOLS.md) for `naming.json` and
`company_goals.json`, and `engineering/rules.py` / `design/fab_profile.py` for
the other two. `footprint_defaults.json`, `footprint_policy.json`,
`document_settings.json` and `templates/` are new, documented below.

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

## `templates/<name>/`

Plain files, copied as-is by `profile-new-project <name> <dest>` -- Altium
project files are just files on disk, so no DelphiScript is involved. Build
a template by setting up a project the way you want new ones to start
(units, grid, a starter schematic sheet, whatever), then copy its files into
`templates/<name>/` inside the profile.

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
