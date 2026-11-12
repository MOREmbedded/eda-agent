# SPICE models and LTspice

This page covers how Altium Booster links vendor SPICE models to schematic
parts, builds a correct LTspice deck from the sheet, hands the deck to
LTspice, and checks an LTspice circuit against the Altium schematic.

Running LTspice in batch mode and reading its logs and waveforms are
described in [Engineering tools](ENGINEERING_TOOLS.md#ltspice).

## Why this matters

SPICE connects a subcircuit's terminals by position. `XU1 a b c LT3080`
connects net `a` to the first node of `.SUBCKT LT3080`, whatever the
symbol calls that pin. A deck that uses the symbol's pin-number order, or
the part's Comment instead of the model name, still runs. It just gives
the wrong answers. Every step below either uses the model's own node
order or says plainly that it could not.

## Workflow

| Step | Tool | Altium needed |
|---|---|---|
| 1. Index the models on this computer | `sim_scan_models` | no |
| 2. Find a model by name or MPN | `sim_find_models` | no |
| 2b. Candidates for every part on the sheet | `sim_find_schematic_models` | yes |
| 3. Check the pin-to-node map | `sim_get_pin_map` | yes |
| 3b. Save a manual pin map | `sim_set_pin_map` | no |
| 4. Link the model to the part | `sim_attach_library_model` | yes |
| 5. Build the deck from the sheet | `eng_ltspice_deck_from_schematic` | yes |
| 6a. Run it in LTspice (batch) | `eng_ltspice_run` | no |
| 6b. Open it in the LTspice GUI | `eng_ltspice_export` | no |
| 7. Compare an LTspice circuit with the sheet | `eng_ltspice_compare_schematic` | yes |

### 1. The model index

`sim_scan_models` reads:

- **Model files**: `.lib`, `.sub`, `.mod`, `.cir`, `.ckt`, `.mdl`, `.sp`,
  `.inc`, `.txt` and the LTspice `.bjt`/`.dio`/`.mos`/... files. Every
  `.SUBCKT` (name, node list with continuation lines, parameters) and every
  top-level `.MODEL` (name and device type) is indexed.
- **LTspice symbols** (`.asy`): the model name, the model file, and each
  pin's name in `SpiceOrder`. Many models that ship with LTspice are
  encrypted, so their symbol is the only readable record of which node is
  which. Those models are still indexed through the symbol.

With no folders given, it scans:

1. the design workspace's `models`, `spice`, `spice_models` and
   `Simulation` folders;
2. the folders listed in `EDA_AGENT_SPICE_MODEL_DIRS` (separated by `;` on
   Windows);
3. LTspice's own library (`%LOCALAPPDATA%\LTspice\lib\{sub,cmp,sym}` for
   LTspice 24, `Documents\LTspiceXVII\lib` for XVII).

The index is per design workspace:
`<workspace>\.eda-agent\spice-models\index.json`, where the workspace is
the folder `eda-agent start` runs in. Without a workspace it goes in the
active profile's folder, otherwise `~\.eda-agent\spice-models`. It is
never written into the eda-agent repository. A rescan reads only files
whose size or modification time changed.

### 2. Finding a model

`sim_find_models` ignores case, punctuation and package or reel tails, so
`LT3080EDD#TRPBF` finds `LT3080`, and `LT3080#PBF` is an exact match.
Model names shorter than four characters never match a longer part number.

`sim_find_schematic_models` does the same for every part on the sheet
that `sim_get_readiness` lists as needing a vendor model. It searches by
manufacturer part number, then Comment, then library reference.

### 3. Pin maps

`sim_get_pin_map` matches each model node to the symbol's pins by name:

1. a saved map (`sim_set_pin_map`) wins;
2. otherwise each node must match a pin name, either exactly or through a
   short alias table (`VIN`=`IN`, `GND`=`VSS`, `SHDN`=`EN`, `IN+`=`+IN`=`INP`,
   and so on). Overline spellings (`S\H\D\N\`, `~SHDN`) are the same pin, and
   `IN+` and `IN-` are never confused. A match that needed an alias is
   marked `needs_review`;
3. only when no name matched at all and the counts are equal is pin-number
   order offered. That map is always marked `needs_review`.

Several pins with one name (two GND pins, four drain pins) all join that
node. Two nodes claiming one pin is refused.

A model port that no symbol pin reaches is kept as an unused node. LTspice
symbols can skip `SpiceOrder` numbers (ADI's `LT3080.asy` starts at 2 for a
five-port model), and dropping the gap would shift every later pin. In the
deck, an unused port gets its own floating node (`NC_U1_1`), which is how
LTspice treats an unconnected port.

`sim_set_pin_map(model_name, pins)` saves a map: one entry per model node
in `.SUBCKT` order, `"4+9"` to join two pins to one node, `"-"` for a port
left unconnected. The map is kept
next to the index (`pin_maps.json`); with `lib_ref` it applies to one
symbol only. A map whose length differs from the model's node count is
refused.

D, Q, M and J parts need no map when their pins are named A/K, C/B/E or
D/G/S. A three-pin power FET is emitted with three nodes, the way LTspice's
VDMOS models expect.

### 4. Attaching

`sim_attach_library_model(designator, model_name)` checks the pin map
first and attaches only when every node is matched. A map that needed
aliases or pin-number order also needs `allow_review=True`. It then writes
the model through the same bridge call as `sim_attach_model`.

The pin map is used by `eng_ltspice_deck_from_schematic`. Altium's own
mixed-signal simulator keeps a separate pin map inside the model link,
and this tool does not set it.

### 5 to 6. The deck and LTspice

`eng_ltspice_deck_from_schematic` now:

- includes parts that have `SpicePrefix` set but no Altium SIM link (LTspice
  does not need the link);
- reads each part's model link, completes it from the index, and emits
  subcircuits with the model's name and node order;
- adds `.include "<file>"` for each model file and returns `model_files`
  (`[{path, include_as}]`). Two files with the same base name get distinct
  names;
- reports `pin_maps` (what was used), `pin_map_problems` (parts whose pins
  could not be matched, or a node whose pins sit on different nets) and
  `model_notes` (for example, a model missing from the index);
- refuses the deck when a part with a model has pins that could not be
  matched, because a deck without that part simulates a different circuit.
  `on_missing_model="placeholder"` returns the rest as a starting point,
  with the part left as a comment.

Pass `model_files` to `eng_ltspice_run` and each file is copied into the
sandboxed run folder. `eng_ltspice_export(folder, deck, model_files)`
writes `<name>.cir` and the model files into a folder you can open in the
LTspice GUI. It refuses unsafe directives (`.FERRET`, includes that leave
the folder), and it does not overwrite an existing deck unless you pass
`overwrite=True`.

### 7. LTspice to Altium

`eng_ltspice_compare_schematic(ltspice_file)` checks a circuit designed or
proven in LTspice (a vendor reference design, a tuned filter) against the
schematic open in Altium. The file can be:

- an `.asc`: the installed LTspice netlists it with `-netlist` inside a
  sandbox folder, together with the symbols and models beside it, so
  nothing is written next to your file;
- a `.net` or `.cir` netlist.

The report gives:

- `only_in_ltspice` and `only_in_altium` (use `designator_map` when the
  two sides are numbered differently);
- `value_differences` for R, L and C, compared as numbers (`4K7` = `4.7k`,
  `100nF X7R` = `100n`);
- `wiring_differences`: nets are paired by the terminals they join, since
  the names rarely agree (`N003` against `VOUT`), and a terminal that
  disagrees with that pairing is listed with the net it should be on;
- `nets_merged_in_altium`: two LTspice nets that are one net in Altium;
- `unchecked`: subcircuits with no way to line their terminals up. Pass
  `pin_maps` (`{"U1": ["3", "1", "2"]}`, Altium pins in LTspice node order).

## Rules

- Vendor models only. Never build a model from a datasheet; see
  `sim_get_readiness`'s `_spice_guidance`.
- The index and pin maps are workspace data. Do not commit them to the tool
  repository.

## Status

Covered by offline tests with a fake bridge and a fake LTspice. Checked
live against LTspice XVII on Windows: the library scan indexed 11,703
files, `find("LT3080")` and `resolve` found the encrypted model through its
symbol, `-netlist` turned an example `.asc` into a netlist that parsed, and
a deck with the copied `LT3080.sub` ran and gave the expected output
voltage. Not yet checked against a live Altium session.

All LTspice tools find LTspice 24 or XVII the same way, or use
`EDA_AGENT_LTSPICE_EXE`. Batch runs go under `%USERPROFILE%\EDA Agent\ltspice`
unless `EDA_AGENT_LTSPICE_RUNS` names another folder.
