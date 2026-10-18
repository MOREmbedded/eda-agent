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

### Fixed

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
