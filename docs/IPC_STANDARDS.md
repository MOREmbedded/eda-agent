# IPC standards and rule values

eda-agent keeps a catalogue of the IPC standards that matter for PCB and
PCBA work and a set of rule definitions built on them. It does **not** ship
IPC values: IPC documents are sold per copy, so the numbers come from your
own licensed copy and stay in your profile.

## What ships in the repository

| File | Holds |
| --- | --- |
| `src/eda_agent/design/standards_data/catalogue.json` | 54 IPC documents: number, current revision, year, title, domain (design, fabrication, materials, assembly, data), priority, and scope in our own words. `revision_confirmed` says whether the revision was checked on an IPC or ANSI page (`checked_on`, `checked_against`). |
| `src/eda_agent/design/standards_data/rules.json` | Rule definitions: id, category, kind of limit (`min`, `max`, `enum`, `info`), unit, what the value is keyed by (class, layer, producibility level, ...), the governing standards, the fab rule key it floors, and the check that uses it. No numbers. |

A test fails if a value, a `values.json` or an `ipc_class_floors.json` ever
appears under `src/`.

## What lives in your profile

```
<profile dir>/
  standards/
    values.json             # your values, each with provenance
    ipc_class_floors.json   # generated from values.json for the fab rule builder
```

`values.json`:

```json
{
  "schema": 1,
  "values": [
    {
      "rule_id": "ipc.annular_ring.pth.external.min",
      "key": {"class": "3"},
      "value": 0.0,
      "note": "optional",
      "provenance": {"kind": "licensed_copy", "document": "IPC-6012F", "clause": "<clause>",
                     "entered_by": "<who>", "entered_at": "2026-11-12"}
    }
  ]
}
```

Lengths are mils, plating thickness is micrometres. Every value needs a
complete provenance, or the whole file is refused:

| `kind` | Required fields | Use |
| --- | --- | --- |
| `licensed_copy` | `document`, `clause` | Read from your copy of the standard |
| `secondary` | `url`, `quote`, `read_on` | A public source (fab page, application note) |
| `derived` | `formula` | Worked out from other values |
| `user_override` | `reason` | A deliberate house rule, always reported as an override |

A rule with no value is reported as not configured, with the standards to
read it from. Nothing is guessed.

A `licensed_copy` value whose `document` is not the catalogue's current
revision (for example `IPC-6012E` while the catalogue lists `IPC-6012F`)
still loads, but every load reports it as a warning: an older revision can
be what a customer contract names, and it should never pass silently.

## Commands

| Command | What it does |
| --- | --- |
| `eda-agent std-list [--domain D] [--priority N]` | The catalogue, with current revisions |
| `eda-agent std-rules [--category C]` | Every rule definition and how many values your profile holds for it |
| `eda-agent std-floors` | Regenerates `standards/ipc_class_floors.json` from `values.json` |
| `eda-agent fab-rules <fab> --layers N --ipc-class 2` | A fab's rules with your class floors applied |
| `pcb_rules_fab_floors` (tool) | The open board's rules against those floors; see below |

## Class floors for the fab rules

`eda_agent.design.standards.write_class_floors(profile_dir)` writes
`standards/ipc_class_floors.json` from the class-keyed values whose rule has
a fab key (via and component annular ring, inner hole to copper). The fab
rule builder takes the stricter of that floor and the fab's published
minimum and says which one governs.

IPC classes mostly set rings, hole wall copper, hole to copper and aspect
ratio. Track, gap, solder mask and silkscreen limits stay the fab's.

## Floors on a board's design rules (on demand)

`pcb_rules_fab_floors` checks the open board's design rules against one
fab build and, optionally, an IPC class. It never runs on its own: ask for
it ("check this board's rules against JLCPCB 4 layers, class 3").

```
pcb_rules_fab_floors(fab="jlcpcb", copper_layers=4, ipc_class=3)              # report
pcb_rules_fab_floors(fab="jlcpcb", copper_layers=4, ipc_class=3, apply=True)  # raise
```

Each floor is the fab's published minimum for the build, or the class
floor from `ipc_class_floors.json` where that is stricter; the reply
cites the fab quote or the IPC clause for every one. With `ipc_class` and
no class floor file, the call is refused.

| Board rule | Floor | Written with |
| --- | --- | --- |
| Clearance | `min_gap` | gap (mm) |
| Width | `min_track` | min width, and preferred if it would sit under the new min |
| Hole Size | `min_drill` | min hole |
| Routing Via | `min_drill`, and min drill + 2 x via ring for the diameter | min hole and min diameter (and preferred) |
| Hole To Hole Clearance | `min_hole_to_hole` | gap (mm) |
| Board Outline Clearance | `copper_to_edge` | gap (mm) |
| Minimum Solder Mask Sliver | `min_mask_sliver` | gap (mm) |
| Silk To Solder Mask | `silk_to_pad` | gap (mm) |
| Minimum Annular Ring | `min_annular_ring` | reported only (no writer) |

What it will and will not do:

- A rule stricter than its floor is left alone. A fab minimum is what the
  fab can make, not the value to design at.
- A board-wide rule (scope `All`) below its floor is raised to the floor
  with `apply=True`. Values are rounded up, never down.
- A scoped rule below its floor (a waiver for one part, a BGA neck-down)
  is listed under `to_decide` and raised only when named in
  `raise_rules`.
- A rule DRC does not check, a rule from the schematic, or a rule whose
  maximum is below the floor is reported, not written.
- Nothing is created or deleted. A floor with no rule of its kind on the
  board is listed under `no_rule_on_board`; floors no Altium rule holds
  (PTH annular ring, aspect ratio, inner hole to copper, silkscreen line
  and text) come back under `report_only`.
- Every write is logged in the board's rule log, so the next DRC names it.

## Not covered

Safety creepage and clearance for mains and isolation come from IEC 62368-1
and IEC 60664-1, not IPC. IPC-2221 spacing alone is not enough for a
certified product.
