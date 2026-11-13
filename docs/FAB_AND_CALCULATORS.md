# Fab output, stackups and calculators

Check what you are about to send to the fab, pick a stackup from bundled and sourced fab data, and size tracks, vias and spacing.

## Checking the fab package

`proj_run_outjob` writes Gerbers, NC drill and pick-and-place. Until
recently nothing ever read them back, which left the one artifact that
actually gets manufactured write-only and unverified.

`pcb_check_fab_output` reads an exported folder and compares it against
the live board:

| Check | Against |
|---|---|
| copper layer count | the board stackup |
| outline width and height | `pcb_get_board_outline` |
| drill hit count | pads with holes, plus vias |
| smallest drill | a fab minimum you supply |
| smallest copper feature | a fab minimum you supply (warning only) |

`checks_run` in the response names what was actually compared, so a
check that could not run is never mistaken for one that passed. If a
board read fails, that check is skipped and said so; the rest still run.

Two details worth knowing. A Gerber bounding box spans the drawn ink,
and a board outline is a stroked path whose centreline is the real
edge, so a 20 mm board drawn with a 0.2 mm pen measures 20.2 mm. The
stroke is subtracted and reported as `stroke_mm`, with the raw figure
kept alongside. And the smallest-feature check is a **warning**, never
an error, because apertures cover pads and text as well as tracks.

Needs the optional extra, which brings in
[gerbonara](https://pypi.org/project/gerbonara/) (Apache-2.0, Gerber and
Excellon in one library):

```bash
pip install -e .[gerber]
```

It requires Python 3.12+, while eda-agent itself supports 3.11, which is
why it is an extra rather than a core dependency. Without it the tool
says so plainly instead of failing obscurely.

## Fab design-rule database

Design rules for thirteen PCB fabs, read from each fab's own published
capability pages. No fab offers an API for this, so every figure is
stored with the fab's words (`quote`), the page and the date it was
read, exactly as the JLCPCB and PCBWay data below always has been.

| Fab | Country | Status | Tiers |
|---|---|---|---|
| JLCPCB | CN | data | standard |
| PCBWay | CN | data | standard, advanced_normal |
| Technoservice (TS PCB) | PL | data | standard |
| Satland Prototype | PL | data | standard, special (1-2 layers published) |
| Eurocircuits | BE | partial | standard_pool (pattern classes are an image) |
| Würth Elektronik | DE | pending | Basic Design Guide to be read |
| Multi Circuit Boards | DE | data | standard, special |
| AISLER | DE/NL | pending | help page moved |
| Beta LAYOUT (PCB-POOL) | DE/IE | partial | pool, overseas (summary only) |
| NextPCB | CN | data | standard, advanced_hdi |
| PCBCart | CN | partial | typical, ultimate (clearances and tolerances; no track or drill) |
| OSH Park | US | data | two_layer, four_layer, six_layer, after_dark |
| Advanced Circuits | US | partial | standard, custom |

`data` means the core minimums are stored; `partial` that some are not
readable yet; `pending` that the pages are known but not read.

```bash
eda-agent fab-list
eda-agent fab-rules technoservice --layers 4
eda-agent fab-compare --layers 4
eda-agent fab-refresh oshpark
```

| CLI verb | Tool | Does |
|---|---|---|
| `fab-list` | `design_list_fabs` | Every connector, its status, tiers and age |
| `fab-rules` | `design_fab_rules` | Every published rule of one build (fab, tier, layers, outer oz), each with its quote; missing ones with the reason |
| `fab-compare` | `design_fab_compare` | All fabs and tiers side by side for one build |
| `fab-refresh` | - | Re-read a fab's pages and check the stored quotes |

Rules covered, where the fab publishes them: track, gap, mechanical and
laser drill, non-plated hole, via and component ring, hole to hole,
inner hole to copper, mask dam and expansion, silkscreen width, height
and clearance to pads, copper to routed edge and to V-score, aspect
ratio, BGA pitch, hole and outline tolerances, thickness, copper
weights, board size, via in pad, impedance tolerance, castellation,
finishes and the IPC class the fab builds to. Lengths come back in
mils; the stored figure keeps the fab's own unit (mm, mil or µm).

**IPC class 2 and 3.** `--ipc-class` asks for the class floors on top
of the fab's minimums, each rule becoming the stricter of the two and
saying which governs. IPC values are licensed, so none ship
in the repository: the floors are read per profile from
`<profile dir>\standards\ipc_class_floors.json`, each citing its
clause. Without that file a class is **refused**: a class 3 rule set
built from fab minimums alone would look like one and not be. IPC
classes mostly set annular rings, hole wall, hole to copper and aspect
ratio; track, gap, mask and silkscreen stay the fab's.

**Connectors.** `fab_data/fab_sources.json` lists each fab's pages.
`fab-refresh` fetches them (HTML or PDF), obeying robots.txt, keeps a
dated plain-text snapshot under `%USERPROFILE%\.eda-agent\fab_snapshots\`
(never in the repository), and looks for every stored quote in it. A
quote still on the page confirms its figure; a quote that is gone means
the fab changed or moved it, and is listed for a person to read again.
A refresh never rewrites a figure. `--write` moves the reading date on,
and only when every quote was found. For a site that refuses automated
readers, save the page from a browser and pass `--file`.

The figures added on 2026-11-11 were read by an automated page reader.
Run `fab-refresh` for a fab before relying on it for an order: it
proves each quote against the live page.

## Fab capabilities, stackups and laminates

Capability figures and stackup templates for JLCPCB and PCBWay ship
with eda-agent, each carrying **the page it was transcribed from and
the date**, with a staleness flag after a year.

```bash
eda-agent stackup-vendors
eda-agent stackup-show jlcpcb
eda-agent laminates
```

| CLI verb | Tool | Does |
|---|---|---|
| `stackup-vendors` | `design_list_fab_vendors` | The bundled vendors, with source, age and what each does not publish |
| `stackup-show` | `design_fab_vendor` | One vendor's minimums (mm and mils) and stackup templates |
| `stackup-profile` | `design_fab_vendor_profile` | Emit a fab profile for `design_synthesize_rules` |
| `laminates` | `design_list_laminates` | Laminate properties, with both dielectric constants |
| - | `design_compare_stackup` | A template against a `pcb_get_layer_stackup` reply |
| - | `pcb_check_stackup` | The same, reading the open board for you |
| - | `pcb_apply_stackup_template` | Set the open board's stackup to a template (dry run by default) |

**`design/fab_profile.py` ships zero capability numbers on purpose** -
its own docstring says every value must come from the fab and be cited.
This is a deliberate relaxation of that, not an abandonment: every
number is dated and sourced, and three rules keep the original point
intact.

- **A figure the vendor does not publish stays unpublished.** JLCPCB
  states no rigid hole-to-hole minimum and no mask sliver. Both are
  `null`, both are listed with the reason, and converting to a fab
  profile **refuses** and names them. Supply them yourself with
  `--fill min_hole_to_hole_mils=17.7` once the fab has told you, and
  they are recorded in the profile's `source` as yours rather than
  theirs. Nothing here will guess a design rule.
- **Nothing pretends to be current.** A transcription older than a year
  is reported as stale, in the result rather than in a README.
- **A fab's number for its own stack is not a material property.**
  JLCPCB's er of 4.6 describes what JLCPCB presses; it is not carried
  to anyone else's FR-4.

A fab that is not bundled goes in your profile's `fab_profiles/`
directory, where it travels with the project.

### Two dielectric constants

Manufacturers publish a **process** permittivity (one frequency,
clamped stripline) and often a **design** one (over a band,
differential phase length). RO4350B is 3.48 and 3.66 - about 5% apart,
and the design value is the one to size an impedance with. A table
carrying a single `er` silently picks one, so both are carried and
`er_design` is null where none is published.

Not every maker publishes both. Isola and Panasonic tabulate **typical**
values by frequency instead, and for those both columns stay empty
rather than borrowing a typical figure and calling it a process or
design one. Every laminate also carries `er_for_design`: the one number
to size an impedance with, which is the design value where there is one
and the nearest typical value otherwise, never the process value, and it
says which kind it chose.

The laminate list is deliberately short: only entries read off a
datasheet are in it. pcb-toolkit carries 45 presets extracted from the
Saturn PCB Toolkit binary, which its own README calls compatibility
data rather than manufacturer data; copying them here would turn an
acknowledged approximation into what looks like a reference table.
A fab's own figure for its own stack, like JLCPCB's 4.6, is reported
separately under `fab_figures` for the same reason: it has no
measurement conditions and does not carry to another fab's FR-4.

### Applying one to a board

`pcb_apply_stackup_template` sets the open board's ply table to a
template, **dry run by default** because a stack change can invalidate
routing. It modifies existing layers only and refuses when the layer
counts differ: adding one means inventing the name Altium's own Layer
Stack Manager would have chosen, on a board that has routing on it.

This needed a bridge fix. `PCB_ModifyLayer` parsed copper thickness
with `StrToIntDef`, and no real foil weight is a whole number of mils -
1 oz is 1.378 - so asking for 1 oz copper silently set the layer to
**zero** and reported success. Both thickness fields parse as floats
now. **Redeploy the Altium scripts** (`eda-agent install-scripts`) to
pick it up; `eda-agent health` reports the deployed version.

## Calculators

The field-solver impedance calculators, the trace thermal model and the
dashboard's Calculators tab are described in [PCB toolkit](PCB_TOOLKIT.md).
The tools below are the earlier single-formula calculators; both sets
are reached through `pcb_calc`.

Eleven calculators, each reachable as an MCP tool, and ten of them as a
CLI verb as well:

```bash
eda-agent calc-spacing --voltage 280 --environment B2
```

| CLI verb | Tool | Answers |
|---|---|---|
| `calc-spacing` | `pcb_calc_conductor_spacing` | Minimum conductor spacing for a voltage (IPC-2221 Table 6-1) |
| `calc-fusing` | `pcb_calc_fusing_current` | Current that melts a track in a given time (Onderdonk) |
| `calc-via` | `pcb_calc_via_parasitics` | Via inductance, capacitance, barrel resistance |
| (tool only) | `pcb_calc_via_inductance` | Loop inductance between two planes, with the return named (Johnson). A different question from the row above, which ignores the return path |
| `calc-spiral` | `pcb_calc_spiral_inductor` | Planar spiral inductance (modified Wheeler) |
| `calc-reactance` | `pcb_calc_reactance` | Capacitive and inductive reactance |
| `calc-resonance` | `pcb_calc_resonance` | LC resonance and characteristic impedance |
| `calc-wavelength` | `pcb_calc_wavelength` | Wavelength, quarter-wave, twentieth-wave |
| `calc-pdn` | `pcb_calc_pdn_target` | Target power-distribution impedance |
| `calc-padstack` | `pcb_calc_padstack` | Pad and antipad sizes, gap to neighbours |
| `calc-wire` | `pcb_calc_wire_gauge` | AWG diameter, area, resistance |

They join the ones already here: `pcb_calc_impedance`,
`pcb_calc_trace_width_for_current`, `pcb_calc_trace_width_for_impedance`,
`pcb_calc_track_current_capacity`, `pcb_calc_termination`,
`pcb_calc_thermal_vias`, `pcb_calc_length_match`.

**Every result states how much to trust it**, in a `status` field:
`exact` for a definition or an identity, `published` for a named
standard or textbook closed form, `estimate` for a rule of thumb. An
algebraic identity and a rule of thumb must not arrive looking alike,
or the rule of thumb ends up in a specification. Add `--json` for the
full reply; caveats go to stderr so stdout is only the figures.

Two of these carry caveats worth repeating here:

- **Conductor spacing** is transcribed from secondary reproductions of
  IPC-2221 Table 6-1, because the IPC document is not freely available,
  and two reproductions disagree on two cells. The larger value is
  taken wherever they conflict, since a wider spacing is never the
  unsafe mistake, and the disagreement is reported rather than decided
  silently. Verify against the IPC publication before a design review
  relies on it. The voltage is the one **between conductors**, not the
  supply rail. The classes are the IPC-2221 **A/B** ones (B1-B4, A5-A7):
  revision C *redefines* those columns rather than renumbering them -
  B4 becomes solder-mask covered, a new B5 appears, the A-series shifts
  and the above-500 V step changes - so match the class on the
  description the result gives you, never on its letter.
- **Fusing current** is the destruction limit, not a rating, and
  Onderdonk is adiabatic: good under about a second, and far too low
  beyond that, where a track sheds heat into the laminate. Results past
  one second say so.

### Cross-checking against pcb-toolkit

If you *do* build pcb-toolkit, `calc-cross-check` runs a calculator
through both and reports where they differ. Exits 1 on a
disagreement, so CI can branch on it.

```bash
eda-agent calc-cross-check ipc2221_spacing --arg voltage=280 --arg environment=B2
```

Six calculators are mapped; the flags were read from pcb-toolkit's own
clap definitions rather than guessed. Four are deliberately unmapped
and say why when you ask for them - two because their flags were never
read, one (`pdn_target_impedance`) because pcb-toolkit models a
different quantity, and one because upstream folds resonance into
`reactance`.

Both replies come back in full, and fields pair **by name**, not
through a translation this project has verified. A pair sharing a name
and not a meaning reports as a disagreement, which is the safe
direction for it to fail, and anything unpaired is listed as not
compared.

One disagreement is expected and is not an error: pcb-toolkit's spacing
targets IPC-2221**C** and takes classes `b1`-`b5` / `a6`-`a8`, while
this carries the A/B table.

### Why these are native Python

The obvious way to get them was to call
[pcb-toolkit](https://github.com/akiselev/pcb-toolkit) (Rust, MIT OR
Apache-2.0), whose coverage is wider and whose figures are
upstream-validated. But its CLI is not published as a binary, so using
it means every user installs a Rust toolchain and builds from source.
Calculators that only work for people who did that are not calculators
this server can rely on, so they are implemented here instead, from the
same published sources. The `status` convention is pcb-toolkit's own
idea, and a good one.
