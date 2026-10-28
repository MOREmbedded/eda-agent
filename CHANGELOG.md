# Changelog

This file did not exist before this entry (no changelog convention was
established in the repository's history; releases were tracked only by
git tag and PyPI description). Added here using the common
[Keep a Changelog](https://keepachangelog.com/) format as a reasonable
default - adjust or drop if the project prefers a different convention.

Where an entry says UNVERIFIED, it means the code is covered by tests
but has not been watched working against a live Altium session or a real
Nexar account. Each such tool says so in its own docstring too; the
generated [`docs/TOOL_REFERENCE.md`](docs/TOOL_REFERENCE.md) carries a
maturity classification for every tool.

## [Unreleased]

### Added

- **Altium 365 can be reached directly, not only through Nexar.** Altium
  documents a GraphQL API on the workspace itself; this fork only ever
  spoke to Nexar, which proxies the design domain and carries none of
  the collaboration, workflow or script-execution contexts. Set
  `EDA_AGENT_A365_DOMAIN` and `EDA_AGENT_A365_TOKEN` and the same client
  posts to `https://{domain}/api/graphql/` with a bearer token created
  in the workspace under Admin > Developer.

  One client serves both, because the schema is the same `des*` one
  either way; what differs is the URL, where the token comes from, and
  that a workspace token has no refresh grant, so an expired one is
  reported rather than renewed against an identity server that never
  issued it. File uploads follow the route too: the Files Service at
  `/api/files` takes the bearer header, Nexar's upload service takes a
  `token` header, and sending the wrong one is a 401 with no clue.

- **`eng_a365_schema`** lists the fields of one type in the LIVE schema
  by introspection. That is how to reach the contexts Nexar does not
  proxy without anyone guessing a field name: start at `Query`, then
  name a type from the result. `eng_a365_query` already ran arbitrary
  read-only GraphQL, so no per-context tool had to be invented.

  UNVERIFIED against a live workspace. The twelve tests drive a fake
  transport, so what is proven is the request this code builds (URL,
  method, both headers, body, token precedence), not that Altium accepts
  it. Verifying needs a workspace domain and a token.


### Merged from upstream (salitronic/eda-agent, 63 commits, 2026-10-20 to 2026-10-26)

Both forks worked the same week, so several features existed twice. Where
they did, ONE survives. What was kept and what was dropped:

- **Decimal mils throughout.** Upstream's change: every handler now reads
  and writes fractional mils (`CoordToMilsF`, `MilsToCoordF`,
  `FloatToJsonStr`) instead of rounding to whole mils. Taken wholesale.
  Our extra JSON fields (`layer`, `bbox_basis`, `same_side_filter`,
  `bbox_includes`, `scanned_tracks`, `max_tracks`, `truncated`,
  `hole_size_mm`, `size_mm`) were folded back on top of it.

- **`rotation_audit`: upstream's engine, our measurement.** Both forks
  rewrote this check. Upstream's is broader: it scores every part with
  two or more pads rather than only two-pad parts, and it turns the
  part's courtyard and checks it against every other part's, so a
  suggestion can be applied as it stands. That engine was kept. Upstream
  withheld `new_rotation` on the bottom side, saying it needed an
  assumption about how a mirrored part's angle is counted; we measured
  that on 2026-10-25, so the value is now given on both sides and the
  measurement is recorded in the module and its test.

- **Bottom-side batch placement: ours kept.** Upstream's commit "Place
  bottom-side parts exactly" fixed the SINGLE-component path, where each
  placement has its own `PreProcess`/`PostProcess`. Their
  `PCB_PlaceComponents` still sets `Comp.Layer` inside one transaction
  wrapping the whole batch, which is the configuration measured here as
  NOT mirroring the primitives. Our fix stands: load on the top layer,
  flip after registration with `PCB_FlipComponent`, pre-compensating the
  requested rotation because a flip rewrites it to `180 - R`.

- **Calculators: ours kept, one of theirs ported.** Upstream built
  `design/calculators.py` (9 functions) while we built
  `engineering/pcb_calc.py` (10, plus 10 CLI verbs and three test
  files). Ours is kept, theirs deleted, because it covers everything
  theirs did plus IPC-2221 conductor spacing and a cross-check, and it
  is reachable from the CLI. Upstream's `via_inductance` was PORTED
  rather than dropped: it is a different question from our
  `pcb_calc_via_parasitics`, which gives a via in isolation and says
  outright that it ignores the return path, where theirs names the
  return and gives the loop inductance between two planes (Johnson,
  "Via Inductance II"). Both tools now exist.
  NOT taken: upstream's spiral-inductance sheet expressions, which cover
  a circular spiral that ours does not. `pcb_calc_spiral_inductor`
  remains modified-Wheeler, square, hexagonal and octagonal only.

- **ERC violations: upstream's handler, our field documentation.**
  Theirs adds paging (`offset`, `next_offset`, `include_suppressed`),
  ours added severity counts. The Pascal handler is upstream's, taken
  whole rather than hybridised, because it is live-only code that cannot
  be tested here. The tool now reports `violation_count`,
  `suppressed_count`, `not_reported_count` and `raw_violation_count`.
  LOST in the process: the per-severity `warning_count`, `error_count`
  and `fatal_count` this fork briefly had.

- **ECO: both.** Our refusal to fire while a modal is already open, and
  our bounded wait, are kept; upstream's `after_sync` report is folded
  in, so the board is read back against the schematic (`footprint_mismatch`,
  `missing_on_board`, `diff_pairs_removed`) rather than the dialog's
  outcome being trusted.

- **Footprint render and compare: upstream's kept.** Both forks built
  `lib_render_footprint` and `lib_compare_footprints`. Theirs takes
  `courtyard_layer` and `assembly_layer`; ours did not. Ours removed.
  Note the parameter rename: `output_path` is now `path`, `tol_mm` is
  now `tolerance_mm`.

- **Fab and stackup TOOLS: ours kept. The DATA is still duplicated.**
  Both forks shipped `design_list_fab_vendors`,
  `design_fab_vendor_profile`, `pcb_check_stackup` and
  `pcb_apply_stackup_template`. Ours (`tools/stackup.py` over
  `design/stackup_library.py`) is kept and upstream's four definitions
  were removed, so `pcb_check_stackup` keeps our `vendor` parameter
  rather than upstream's `template` / `thickness_tol_pct` / `er_tol`.

  But BOTH material datasets are still live, because upstream's
  `design_get_stackup`, `design_list_stackups` and `design_get_laminate`
  read theirs:

  | dataset | laminates |
  |---|---|
  | `data/stackups/laminates.json` (ours) | RO4003C, RO4350B, JLCPCB 7628 prepreg |
  | `design/fab_data/laminates.json` (upstream) | RO4003C, RO4350B, 370HR, MEGTRON 6 |

  Neither is a superset: each holds material the other does not, and
  they agree on only two. A board checked through our tools and one
  checked through theirs can therefore see different material. KNOWN
  GAP, not closed in this merge: the two want folding into one
  materials library.

- **The automatic schematic layout engine is gone**, with upstream's
  retirement of it: 84 files, including `design/layout.py`, the
  force-directed and Sugiyama placers, the quality model and the
  training scripts. All 84 were upstream's own code and this fork had
  never modified any of them. Four tools go with it:
  `design_layout_schematic`, `design_execute_plan`,
  `design_preview_plan` and `design_learn_from_layout`. The replacement
  is upstream's hand-template route, `sch_preview_template` and
  `sch_draw_template`.

- Other arrivals: 3D bodies in footprints (`lib_set_3d_body`,
  `lib_create_3d_model`, `lib_export_3d_model`), `lib_rotate_footprint`,
  `pcb_get_impedance_profiles`, ERC levels, `proj_create_outjob`,
  `proj_export_jlc_assembly`, `pcb_update_footprints_from_library`, a
  generic property layer for keepouts, polygon pours, primitive nets and
  via/pad tenting, and `server_update`, which offers updates once a day
  and installs them after you agree.

- Dead code removed on the way: `_execute_command_async`, which
  upstream's inline executor replaced as the only caller.
  `_poll_response_async` stays, since a test exercises it directly.

Tool count after the merge: **655 on the Altium backend**, 741 with both
registered, 990 across all backends. `SCRIPT_VERSION` is `2026.10.26.4`.

### Changed

- **The two version numbers are documented.** The package follows
  semantic versioning (`0.7.1`) and the Altium script is dated
  (`SCRIPT_VERSION`, `YYYY.MM.DD.N`), they cover different artifacts and
  they move independently, and nothing said so anywhere. A reader
  opening `Main.pas` met a year where a release number was expected.
  The README now gives both, says the script version exists to answer
  "is Altium running today's script", and says to bump the trailing
  `.N` on every `.pas` edit -- leaving it makes a reloaded Altium
  indistinguishable from a stale one, which has caused real confusion
  here more than once.

- `rotation_audit` now checks **bottom-side parts**. They were skipped
  while it was unknown whether a rotation value turns a mirrored part
  clockwise or anticlockwise in board coordinates. Measured against
  Altium 26.11.1.5 on 2026-10-25: four parts flipped with
  `pcb_flip_component` and read back turn their pads in world
  coordinates by exactly the delta applied to `Rotation`, the same as a
  top-side part, at 45 degrees as well as at right angles. The scoring
  never needed a side term -- it rotates the pad's WORLD position about
  the component origin, and the mirroring is already in the position
  the board reports.

### Fixed

- **`app_capability_probe` and `app_get_report` now ask Altium rather
  than the process table.** Both carried the shape the doctor entry
  below describes. `app_capability_probe(probe_live=True)` set
  `bridge_response` to None whenever the scan said Altium was absent, so
  every live feature read as missing while the bridge was answering.
  `app_get_report` gated the ping, the script version, the version match
  and the round-trip timing on that same scan, which is the worse of the
  two: that tool exists to say what is wrong with the plumbing, and on a
  machine whose process table cannot name a running Altium it reported
  the application down, with no version and no round trip.

  Both now ping first and keep the scan as a reported observation. The
  first attempt keeps the process gate, because where Altium really is
  absent it turns an IPC timeout into an immediate answer; only when
  that returns nothing does the ungated retry go out. `altium_running`
  is true when the scan saw Altium OR the bridge answered, and a new
  `process_scan_saw_altium` key carries what the scan alone said, so a
  caller can still see the two disagree. Checked against a live Altium
  with the scan forced to report nothing: script version, version match
  and a 38 ms round trip all came back where the old code returned
  None.

- **`eda-agent doctor` no longer reports a running Altium as absent.**
  The check printed `[FAIL] altium process running, no process named
  x2.exe found` with `fix: Start Altium Designer.` while Altium
  Designer 26.11.1.5 was on screen and answering bridge commands, and
  a FAIL there returned from `run_doctor_checks` before the bridge was
  ever pinged. So the weakest signal in the file set the exit code and
  silenced every better one, including the round trip that would have
  contradicted it.

  The doctor ran its own `psutil.process_iter` loop over one hardcoded
  name, while the bridge gates every command on `AltiumProcessManager`,
  which tries a Toolhelp snapshot first. On the reported machine those
  two disagreed: the bridge found Altium, and the doctor, `tasklist`,
  `Get-Process x2` and a scan over window titles all came back empty,
  so the executable was not merely renamed. The loop also looked only
  for `X2.exe`, never `DXP.exe`.

  The doctor now asks the same scan the command gate uses, and that
  scan no longer gates anything. A bridge round trip, or an
  `activity.log` entry under five minutes old, outranks it. With
  either, the check is a minor WARN that says the process table cannot
  see a running Altium and names the usual reasons - elevation, a
  different session or integrity level, restricted enumeration -
  instead of prescribing a fix for a state that is not the fault.
  `Start Altium Designer.` survives only where the scan finds nothing
  and nothing answers.

  `ping_with_version(require_process=False)` makes this reachable:
  without it the bridge's own gate reads the same process table the
  doctor cannot trust, refuses the ping before any IPC, and the run
  reports a healthy Altium as unresponsive. Only the doctor passes it.

- `pcb_place_components` documents that **`layer` sets the layer
  without mirroring**. A part placed with `layer="BottomLayer"` comes
  out with its pads on bottom copper in TOP-SIDE geometry, so its two
  pads are on the wrong sides; the same part through
  `pcb_flip_component` is mirrored properly and its rotation moves to
  180 minus the original. Found while measuring the rotation sign
  above. The docstring now says to place on the top and flip. The
  Pascal side still does not mirror, so this is a documented
  limitation rather than a repair.

### Merged from upstream 0.7.1

This fork absorbed the 24 commits it was behind
`salitronic/eda-agent`, which is where the version jumps 0.6.1 to
0.7.1. What arrives with them: footprint vias and via edits, via
tenting and the pad paste flag persisting, SchLib symbol shapes drawn
through the library so they show and query at once, region create and
delete on the geometry map, selective repour and the repour
preference, board save-copy, Altium 21 support, Unicode across the
bridge, library layer counts and origin reads, per-unit build stamps,
session time-slotting between Claude sessions sharing one Altium, loop
analysis, sheet fit, smart DRC and ngspice loop simulation.

**None of it has run against a live Altium from this fork.** The
suite passes and the Pascal guards pass; neither is the same claim.
Treat the first session on this build as verification, and see
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md).

`SCRIPT_VERSION` is `2026.10.19.1`, ahead of both sides, because the
merged script is neither of their builds and a session running either
one must see a mismatch rather than a false match. **Redeploy and
reload** before using it: `eda-agent update` reports the gap between
what is on disk and what Altium has compiled.

The merge commit records the decisions it needed, including three
places where taking one side verbatim would have broken something
quietly: the profile's pad corner radius, `Gen_BatchDelete`'s
variable name, and the `truncated` field `proj_get_nets` pages on.


### Added

- Library file tools, the gaps found comparing against
  embedded-society/altium-designer-mcp, written here rather than taken
  from it (it is GPL-3.0 and writes Altium's binary formats, which this
  project does not). None writes a .SchLib or .PcbLib:
  - `lib_extract_step_models` lists or extracts the STEP models embedded
    in a .PcbLib with Altium closed (zlib streams indexed by an ASCII
    table). **Checked on two vendor libraries written by Altium** (one
    model and eleven): every model came out as a STEP file.
  - `lib_write_libpkg` writes a .LibPkg grouping libraries, in the layout
    measured from 44 packages Altium saved. **Checked live on
    2026-10-20**: a written package opened in Altium and compiled to an
    IntLib.
  - `lib_backup_library`, `lib_list_library_backups` and
    `lib_restore_library_backup` keep byte copies of one library file,
    for libraries `app_checkpoint` does not reach. A restore backs up the
    file it replaces first and needs `confirm=True`.
  - `lib_render_footprint` draws a footprint to SVG and PNG from the live
    pad and primitive reads; `lib_compare_footprints` diffs two footprints
    pad by pad and counts primitives per layer. **Checked live on
    2026-10-20** against a vendor library: a WE-PMI footprint drew
    correctly, and 0603 H0.5 against H1 reported the height difference
    and nothing else, which is what those two parts differ by.
- `rotation_audit`, an advisory check in `pcb_layout_audit`: each
  two-pad top-side part is tried at 0/90/180/270 degrees about its
  origin, scored by net airline (minimum spanning tree) for routable
  nets and by distance to the nearest return pad for plane nets, and
  reported when another rotation beats the current one by 25 mils.
  The idea comes from coffeenmusic/altium-mcp's `check_orientation`,
  reimplemented on the LayoutBoard model. Bottom-side parts are skipped
  until the rotation sign of a mirrored part is checked. **Checked live
  on 2026-10-20** against a real board (BDDFS2MWC01A), where its
  suggestions were sensible. Still covered by synthetic-board tests for
  the scoring itself, and the TOP-SIDE path is all that has been
  exercised: a board with connected two-pad parts on the bottom has not
  been run.

#### Owner profiles

A per-workspace house style, so one machine can work for several
companies without re-stating their conventions on every call. See
[`docs/PROFILES.md`](docs/PROFILES.md).

- A profile directory carries naming conventions, design rules,
  fab/stackup profiles, footprint-authoring defaults, footprint-policy
  pins, schematic unit/grid settings, and project templates. Formats
  that already existed (`naming.json`, rulesets, fab profiles) are
  reused rather than reinvented.
- Found from `EDA_AGENT_PROFILE_DIR`, then a project-local
  `.eda-agent/profile/` discovered by walking up from the working
  directory, then the centrally active profile. **The project-local path
  is cwd-relative**, so it reaches the CLI but usually not the MCP
  server, whose client launches it from elsewhere; an MCP setup wants
  the central profile or the environment variable.
- CLI: `profile-init`, `profile-list`, `profile-use`, `profile-show`,
  `profile-validate`, `profile-new-project`.
- Tools: `profile_show`, `profile_list`, `eng_list_rulesets`,
  `eng_load_ruleset`, `eng_profile_fab_profile`,
  `profile_apply_document_settings`.
- `profile_new_document`: create a schematic or board from one of the
  profile's `templates/documents/` files and open it. Altium's own New
  Document makes a blank sheet and so does the bridge's
  `create_document`; there is no API that applies a template, so this
  copies the file, which is what applying one amounts to on disk. The
  template's own extension wins over the destination's, an existing
  file is refused unless `overwrite=True`, and a copy that Altium then
  declines to open still reports the path it wrote. Altium
  **preferences** stay out of reach (no scripting API at all) and
  **Draftsman** is unsupported, both stated plainly in
  [`docs/PROFILES.md`](docs/PROFILES.md) rather than left to be
  discovered.
- `lib_add_footprint_pad` / `lib_create_standard_footprint` /
  `lib_add_footprint_pads` take their pad corner radius from the active
  profile instead of a hardcoded 25%, and
  `lib_audit_footprint_policies` uses the profile's footprint policy
  when none is passed.

#### `eda-agent update`

Updating meant four manual steps and no way to tell which of them you
had missed. The checkout, the installed package, the deployed
DelphiScript files and the copy Altium has COMPILED each go stale
independently, and the symptom of all four is the same: a tool
behaving like an older build.

`update` reports all four and fixes the one it safely can, redeploying
the scripts when they differ from the tree. Two it deliberately does
not do:

- **Only your own remote is checked**, the one you push to. A checkout
  can have your fork, the project it was forked from and a colleague's
  copy all configured at once, and reporting a third party's commits
  every run turns the to-do list into something nobody reads. Name one
  with `--from <remote>` when you want it.
- **`--from <folder>` updates from a local copy**, with no git and no
  network in it: another checkout, a copy on a share, a release
  directory. It reads that folder's package version and
  `SCRIPT_VERSION`, compares both, and deploys its Altium scripts.
  Both a checkout layout and an unpacked package are accepted. It will
  not `pip install` for you, which is a bigger change than copying a
  script, so it prints the command.
- **It will not pull** unless asked. A command called "update"
  silently rebasing someone's uncommitted work is a bad trade, so
  being behind is reported and `--pull` opts in. A pull onto a dirty
  tree is refused outright rather than handing back a conflict.
- **It cannot reload Altium's script project.** There is no API for
  it, so that step is reported as an action for you rather than
  quietly omitted, which would leave you on an old build believing
  you were current.

The Altium check is a plain ping. `doctor`'s canary calls
`application.save_all`, which saves every open document; an update
that silently did that to read a version number would be a worse trade
than not reading it.

Exit codes: 0 current, 1 something needs you, 2 an error, so CI can
branch without parsing prose. `--check` changes nothing, `--fetch`
refreshes remote refs so "up to date" is not an answer from last
week's fetch, and `--json` gives the steps as data.

#### Cross-checking against pcb-toolkit

`calc-cross-check` / `pcb_calc_cross_check` runs a calculator through
this implementation and through a locally built
[pcb-toolkit](https://github.com/akiselev/pcb-toolkit), and reports
where they differ. Optional, and absent for most people, because
upstream's CLI is not published as a binary; the refusal when it is
missing says how to build it rather than just reporting a missing file.

Six calculators are mapped, with every flag read from pcb-toolkit's own
clap definitions rather than guessed, since a guessed flag name fails
at the moment a user runs it. Four are deliberately unmapped and carry
the reason, because "no mapping" and "I forgot" look identical
otherwise.

Fields pair BY NAME rather than through a translation this project has
not verified: a pair sharing a name and not a meaning reports as a
disagreement, which is the safe direction, and anything unpaired is
listed as not compared.

#### Fab capabilities, stackup templates and laminates

JLCPCB and PCBWay capability figures and stackup templates ship with
eda-agent, each carrying the page it was transcribed from and the date,
with a staleness flag after a year. CLI: `stackup-vendors`,
`stackup-show`, `stackup-profile`, `laminates`. Tools:
`design_list_fab_vendors`, `design_fab_vendor`,
`design_fab_vendor_profile`, `design_list_laminates`,
`design_compare_stackup`, plus `pcb_check_stackup` and
`pcb_apply_stackup_template` on Altium.

`design/fab_profile.py` ships zero capability numbers on purpose. This
is a deliberate relaxation of that policy, and three rules keep its
point intact:

- **An unpublished figure stays unpublished.** JLCPCB states no rigid
  hole-to-hole minimum and no mask sliver; both are null with a reason,
  and converting to a fab profile REFUSES and names them. Supply them
  with `--fill` once the fab has told you, and they are recorded in the
  profile's `source` as yours rather than the vendor's. Nothing guesses
  a design rule.
- **Nothing pretends to be current**, with the staleness check in the
  result rather than in a README.
- **A fab's figure for its own stack is not a material property.**
  JLCPCB's er of 4.6 is not carried to anyone else's FR-4.

Laminates carry BOTH dielectric constants. Manufacturers publish a
process value (one frequency, clamped stripline) and often a design
value (over a band); RO4350B is 3.48 and 3.66, about 5% apart, and the
design value is the one to size an impedance with. A table with a
single `er` silently picks one. The list is short on purpose: only
entries read off a datasheet are in it, rather than the 45 presets
pcb-toolkit extracted from the Saturn binary and labels as
compatibility data.

`pcb_apply_stackup_template` sets a board's ply table from a template,
dry run by default. It modifies existing layers only and refuses when
the layer counts differ, because adding one means inventing the name
Altium's Layer Stack Manager would have chosen on a board that has
routing on it. A failure part way through reports which layers did
change.

#### Ten calculators, on the CLI as well as the tool surface

`calc-spacing`, `calc-fusing`, `calc-via`, `calc-spiral`,
`calc-reactance`, `calc-resonance`, `calc-wavelength`, `calc-pdn`,
`calc-padstack` and `calc-wire`, each also an MCP tool
(`pcb_calc_conductor_spacing` and so on). The maths lives in
`engineering/pcb_calc.py` so it is testable without a tool surface, and
the argparse subcommands are generated from one table so a flag cannot
drift from the parameter it fills.

- **Every result states how much to trust it**, in a `status` field:
  `exact` for a definition, `published` for a named standard or
  textbook closed form, `estimate` for a rule of thumb. The convention
  is borrowed from pcb-toolkit, which had the right instinct: an
  algebraic identity and a rule of thumb must not arrive looking alike,
  or the rule of thumb ends up in a specification.
- **Conductor spacing is transcribed from secondary reproductions** of
  IPC-2221 Table 6-1, because the IPC document is not freely available,
  and two reproductions disagree on two cells. The larger value is
  taken wherever they conflict, since a wider spacing is never the
  unsafe mistake, and both figures are reported in `disputed` rather
  than the disagreement being settled silently.
- **Fusing current says when it has left its range.** Onderdonk is
  adiabatic; past a second or so a track sheds heat into the laminate
  and the answer falls below the track's own continuous IPC-2221
  rating, which is the formula failing rather than a result.
- Implemented in Python rather than by calling
  [pcb-toolkit](https://github.com/akiselev/pcb-toolkit) (Rust, MIT OR
  Apache-2.0), whose coverage is wider and upstream-validated, because
  its CLI is not published as a binary: using it would mean every user
  installs a Rust toolchain and builds from source.

#### Finding a replacement part

- `part_find_replacements`: judges candidate substitutes on
  parameters, lifecycle and stock. Every attribute carries a direction,
  because the direction is the whole safety argument: more voltage
  rating is safe, more ESR is not, a narrower temperature range is not,
  a different dielectric is not. Values parse into base SI with their
  unit, so `100 nF` and `0.1 uF` compare equal while `16 V` and `16 A`
  are refused rather than compared, and attribute names are matched
  across catalogues so Nexar's `voltagerating_dc` meets Digi-Key's
  `Voltage - Rated`.

  It does not say two parts are equivalent. It reports evidence per
  attribute and names what it did not check: **product-change notices
  and errata** (no distributor API serves them), **pad geometry** (the
  package name is compared, never the land pattern - that is
  `lib_audit_footprint_vs_datasheet`'s job), anything the catalogues do
  not publish, and any attribute the candidate does not state. An
  attribute with no rule is reported as unreviewed, never as a pass,
  and there is no default direction: defaulting to "equal" would block
  harmless differences and defaulting to "at least" would approve
  dangerous ones.

#### Fab output verification, and finding where time goes

- `pcb_check_fab_output`: reads an exported Gerber/Excellon package back
  and compares it against the live board - copper layer count vs the
  stackup, outline size, drill hits vs pads and vias, and minimum drill
  and feature size against fab limits you supply. eda-agent could write
  a fab package but never read one, so the artifact that actually gets
  manufactured was unverified. Needs the optional `[gerber]` extra
  (gerbonara, Apache-2.0, Python 3.12+). The outline check subtracts the
  stroke width, since a Gerber bounding box spans the ink and a 20 mm
  board drawn with a 0.2 mm pen measures 20.2 mm.
- `eda-agent perf`: reports where call time goes by joining the two logs
  already being written - `bridge_trace.log` (the round trip the caller
  saw) and `activity.log` (the handler time inside Altium) - on the
  request id. Every existing view read only the second, so the gap
  between them, which is IPC plus time queued behind another call, had
  never been visible.

#### Board artwork, offline library reads, part labels and datasheets

- `pcb_place_image`: raster artwork (silkscreen logo, QR code) as PCB
  region primitives. Decomposed into non-overlapping rectangles rather
  than traced outlines, so nothing is approximated away from the source
  art and holes need no special handling. Plans by default
  (`dry_run=True`) and reports the primitive count first, with a
  `max_regions` cap. Verified on a live board: an asymmetric test glyph
  placed at a known origin and read back with `pcb_query_region`
  confirmed the scale, the bottom-left origin corner, the layer, and
  that the art is neither mirrored nor flipped.
- `lib_read_pcblib_headers`: a `.PcbLib`'s footprint names, pad counts,
  heights and descriptions with Altium closed, for library hygiene in
  CI. Reads the ASCII streams only; pad geometry lives in the binary
  region and stays a live read through `lib_get_pad_geometry`.
- `part_parse_lcsc_label`: decodes the QR payload on an LCSC reel or
  bag. Only `pc`, `pm` and `qty` have a documented meaning, so the
  remaining keys are returned verbatim rather than given invented names.
- `part_cache_datasheet` / `part_list_cached_datasheets`: keep the PDF
  on disk with its URL, SHA-256 and fetch time. The host allowlist is
  deny-by-default from `EDA_AGENT_DATASHEET_HOSTS`, and the payload is
  verified to be a PDF by magic bytes rather than by `Content-Type`.

#### Altium Booster 3.2.0 feature integration

- IPC-7351B footprint validation against a live SchLib/PcbLib pair
  (`eng_validate_library_live`), plus a CSV/HTML report writer
  (`eng_write_library_validation_report`) and standalone IPC-name
  parsing (`eng_parse_ipc_footprint_name`).
- Pre-ECO schematic/PCB diff, both offline (`eng_eco_preview`) and fed
  from live Altium reads (`eng_eco_preview_live`).
- LTspice batch runs no longer flash a window on Windows.
- Nexar Supply search (`NexarProvider`) now returns pricing, stock,
  lifecycle status and similar parts, not just identity and datasheet.
- Altium 365 additions: `update_parameters`/`entity`/`workspace_infos`/
  `library_components_params` on the Nexar client, and three new write
  tools (`eng_a365_publish_model`, `eng_a365_release_component`,
  `eng_a365_update_parameters`), each off unless `confirm=True`.
  **UNVERIFIED against a real Nexar account**: covered by a test double
  only.
- Rotating, size-capped, secret-redacting activity log
  (`activity_log.py`), alongside the existing stderr logging.
- `Lib_GetFootprintPads` (DelphiScript) extended to also return track
  coordinates (for courtyard/silkscreen checks) and a 3D-body flag.
  Verified against a live Altium session.
- **Experimental, UNVERIFIED**: `eda-agent eco-execute`, a CLI command
  that auto-presses Altium's Engineering Change Order dialog via the
  existing reactive dialog driver. Off unless `--confirm`.

#### Documented gaps

A README section listing what the bundled data does NOT cover, because
a missing figure that is simply absent reads as an oversight rather
than as a decision: no PCNs or errata (no API serves them), no pad
geometry in a replacement check, JLCPCB's unpublished hole-to-hole and
mask sliver, PCBWay's absent stackup table, the IPC-2221 table's
secondary provenance and its two disputed cells, Altium preferences
and Draftsman, and PCB-side units and grid.

Also where the Altium scripts are deployed, and the trap behind it: if
the Global Project is registered somewhere other than the default,
`health` and `install-scripts` report a directory Altium never reads
and will say "deployed scripts current" while Altium compiles
something years old. `eda-agent update` catches it because it pings
for the version Altium actually COMPILED and compares the two, which
is how a stale 2026.10.13.5 was found running against a 2026.10.18.1
on disk.

### Fixed

- **`install-scripts`, `health` and `update` reported on a folder
  Altium might never read.** They all used
  `%USERPROFILE%\EDA Agent\scripts\`, so with the Global Project
  registered anywhere else they said "deployed scripts current" while
  Altium compiled a build days old. They now use the folder Altium's
  own Global Projects list (in the registry) loads `Altium_API.PrjScr`
  from, falling back to the old default only when no install
  registers it or several register different folders; `update` says
  which. `install-scripts` also stopped deriving the workspace pointer
  from the destination (`<dest>\..\workspace`), which was right only
  for the old default and would have pointed Altium at a directory the
  server never writes to.
- **`pcb_modify_layer` advertised its thicknesses as integers.** The
  Pascal fix below was unreachable from an MCP client that validates
  against the schema: 1.378 was refused or rounded to 1 before the call
  left it. Both are numbers now. Found by the live run of
  `RELEASE_VERIFICATION.md`'s copper-thickness step, which also
  confirmed the Pascal fix, `pcb_apply_stackup_template` and
  `pcb_check_stackup` on Altium 26.11.1.5.
- **A test for the merge's Pascal breaks.** Every routine in
  `scripts/altium/` must close each `Begin`/`Try`/`Case`/`Record` it
  opens and end on `End;`. Neither FPC nor the linter caught the four
  breaks the 0.7.1 merge introduced; this does, including both that
  reached a commit, and its self-tests delete real `End;` lines to
  prove it.
- **The IPC-2221 spacing table did not say which revision it was.**
  Investigating pcb-toolkit's interface surfaced that it targets
  IPC-2221**C**, whose classes are b1-b5 and a6-a8, while this carries
  the A/B table with B1-B4 and A5-A7. Revision C does not renumber
  those columns, it REDEFINES them: B4 becomes solder-mask covered, a
  new B5 appears, the A-series shifts and the above-500 V step
  changes. So a class letter does not carry across revisions, and
  reading "B4" here against a Rev C document silently selects a
  different environment. Every reply now names its revision and tells
  the reader to match on `environment_meaning` rather than the letter.
- **Setting a layer's copper thickness silently set it to ZERO.**
  `PCB_ModifyLayer` parsed `copper_thickness_mils` and
  `dielectric_height_mils` with `StrToIntDef`, and no real foil weight
  is a whole number of mils: 1 oz is 1.378, 0.5 oz is 0.689.
  `StrToIntDef('1.378', 0)` does not round, it returns the DEFAULT, so
  asking for 1 oz copper set the layer to zero and the tool reported
  success. The same applied to any fractional prepreg height. Both now
  parse as floats through the locale-safe helper and convert with
  `MilsToCoordF`, which keeps 1e-4 mil, and the read-back verification
  uses the same parse so a fractional value is no longer reported as
  rejected. **Redeploy the Altium scripts to pick this up**;
  SCRIPT_VERSION moved to 2026.10.18.1 and `eda-agent health` reports
  the deployed one.
- **Ten new calculators were invisible on the Altium backend.** They
  were added to `register_calc_tools`, which runs only for KiCad and
  EasyEDA because the names in it duplicate what `tools/pcb.py` already
  gives Altium. These ten have no Altium counterpart, so the opposite
  rule applies and they now register on every backend. Caught by the
  generated tool reference, not by any test of the tools themselves.
- **Four of the five distributor providers described data their parsers
  never read.** Digi-Key's, Mouser's and element14's class descriptions
  promised lifecycle or stock and none of the three requested any
  field; TME's promised stock that `Search.json` does not return at
  all. The three that can answer now do (lifecycle, stock and the full
  parametric map, in one shared key shape), TME's description says
  plainly that it cannot, and a guard test fails any description that
  gets ahead of its parser again. An absent figure is an absent key
  rather than a zero, because "no stock figure" and "none in stock"
  must not read alike.
- **Nexar's parametric map was built and then thrown away.** The query
  always asked for every `spec`, and the parser always assembled them,
  but only `lifecyclestatus` was published. The whole map is what a
  parametric comparison runs on, so dropping it made the richest source
  the least useful one.
- **Nexar's similar parts were flattened and truncated.** They were
  joined into `"<manufacturer> <mpn>"` strings, which cannot be split
  back apart when either half contains a space, and capped at five,
  which silently truncates the candidate set a replacement search runs
  on. Both halves are kept separate now, and all of them.
- The README claimed "~10 ms per call in active mode". That is the
  poll interval, not a measured round trip, and the Pascal side notes
  the timer floor is really about 16 ms. Both occurrences now say what
  the number is, and point at `eda-agent perf` for the measured one.
- Five CLI commands carried whole paragraphs in argparse's `help=`,
  which it reprints in the top-level command list, making the list
  unreadable. Moved to `epilog=`, so each subcommand's own `--help`
  still carries every word.
- Reading an owner profile no longer raises. It is read during server
  startup and on the pad-authoring path, where a malformed JSON file
  took the whole MCP server down and broke `lib_add_footprint_pad`, and
  a non-numeric corner radius was forwarded to Altium verbatim by one
  tool while raising `TypeError` in another. A present-but-unusable file
  now degrades to its default; `profile-validate` is where such problems
  are reported, and it applies the same checks the consuming tools do.
