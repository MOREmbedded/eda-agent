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

## Class floors for the fab rules

`eda_agent.design.standards.write_class_floors(profile_dir)` writes
`standards/ipc_class_floors.json` from the class-keyed values whose rule has
a fab key (via and component annular ring, inner hole to copper). The fab
rule builder takes the stricter of that floor and the fab's published
minimum and says which one governs.

IPC classes mostly set rings, hole wall copper, hole to copper and aspect
ratio. Track, gap, solder mask and silkscreen limits stay the fab's.

## Not covered

Safety creepage and clearance for mains and isolation come from IEC 62368-1
and IEC 60664-1, not IPC. IPC-2221 spacing alone is not enough for a
certified product.
