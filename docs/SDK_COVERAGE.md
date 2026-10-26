# SDK coverage

How much of Altium this server can actually reach, measured against
Altium's own declarations rather than estimated.

## Why this exists

The generic property layer is four hand-written chains:

| Chain | File | Lines |
| --- | --- | --- |
| `GetSchProperty` | `scripts/altium/Generic.pas` | 82 |
| `SetSchProperty` | `scripts/altium/Generic.pas` | 56 |
| `GetPCBProperty` | `scripts/altium/PCBGeneric.pas` | 95 |
| `SetPCBProperty` | `scripts/altium/PCBGeneric.pas` | 67 |

Every property any caller can read or write exists because somebody
typed `If PropName = 'Width' Then ...` for it. That is a perfectly
good way to build the first hundred. It is a bad way to answer "is the
whole API implemented?", because the only available method was to read
Altium's documentation page by page and compare by eye, which produced
a different answer every time anyone counted.

## The ground truth

`Altium.SDK.Interfaces.dll` ships inside every Altium Designer install
(`<install>\System\`). It is a .NET assembly that declares every
interface the scripting engine exposes: the `ISch_*`, `IPCB_*`,
`IDXP_*`, EDP and ComponentLib families. Delphi properties cross the
COM boundary as `GetState_<Name>` / `SetState_<Name>` methods, so for
every property the assembly states:

- its exact type (`String`, `Int32`, `Boolean`, `Double`, `Byte`, and
  the Altium structs),
- whether it is readable, writable or both,
- whether it is indexed, and by what.

That is exactly what the chains need, and it is more than the HTML API
reference lists. Reflecting it turns an opinion into a measurement.

## Producing the dump

On a machine with Altium installed:

```bash
powershell -File scripts/altium_sdk_reflect.ps1
```

It finds the assembly beside the running `X2.EXE` (so a machine with
several Altium versions measures the one actually in use), falls back
to scanning the usual install roots, and writes
`reference/altium_sdk.json`.

`reference/` is excluded by `.gitignore`: the dump is derived from a
non-redistributable assembly and does not belong in the repository.
Nothing generated from it is committed verbatim either.

`--Dll PATH` points at a specific assembly, `--Out PATH` writes
elsewhere, and `EDA_AGENT_SDK_JSON` overrides where the reader looks.

### What the reflection cannot read

Windows PowerShell runs on .NET Framework, and a few EDP interfaces
carry signatures typed against .NET 8's `System.Runtime`. Those
members are skipped and counted; the dump records how many, so a
consumer can tell a complete picture from a partial one. On AD
26.11.1.5 the loss is 37 methods, none of them in `SCH` or `PCB`.

## Reading the report

```bash
eda-agent sdk-coverage              # the summary
eda-agent sdk-coverage --detail 8   # with missing property names
eda-agent sdk-coverage --json       # every missing name, for diffing
```

Measured on AD 26.11.1.5, script `2026.10.26.4`:

```
655 of 2207 declared properties reachable (29.7%)

Schematic: 401 of 1040 (38.6%) across 32 object types
Board:     221 of 1024 (21.6%) across 12 object types
```

Per object type, the widest gaps:

| Object type | Interface | Reached | Declared |
| --- | --- | ---: | ---: |
| `text` | `IPCB_Text` | 18 | 110 |
| `component` | `IPCB_Component` | 23 | 104 |
| `poly` | `IPCB_Polygon` | 28 | 105 |
| `dimension` | `IPCB_Dimension` | 15 | 101 |
| `pad` | `IPCB_Pad` | 26 | 101 |
| `component` | `ISch_Component` | 27 | 68 |
| `pin` | `ISch_Pin` | 16 | 63 |

## How the measurement is made

1. Each of the four chains is parsed for every `PropName = '...'` it
   tests.
2. Each supported object type is mapped to the interface the SDK
   declares for it. Most are mechanical (`eRectangle` to
   `ISch_Rectangle`); the rest are in two small override tables in
   `src/eda_agent/sdk/coverage.py`, because Altium's own naming is not
   uniform.
3. The SDK's inheritance is resolved transitively, so a component is
   measured against everything `ISch_GraphicalObject` and
   `ISch_BasicContainer` give it too, not only its own 46 properties.
4. The sets are compared per interface.

### Two things the number does not claim

**It is matched by head segment.** The chains accept paths the SDK has
no single member for: `Designator.Text` reads the component's
designator label and then that label's text; `Location.X` reads one
field of a `TLocation`. Both go through the member named by the first
segment, so `Designator.Text` counts as reaching `Designator`. This
can only overstate coverage, never understate it, and overstating is
the honest direction here: the caller really can get at the member.

**It counts properties, not capability.** A tool like
`pcb_get_layer_stackup` or `proj_export_step` reaches Altium through a
dedicated handler, not through the property chains, and none of that
work is in this figure. The number measures one specific surface: what
a caller can reach generically, by naming a property. Treat it as the
size of the generic layer, not as a score for the server.

## What this is for

Three things, in order:

1. **Answering the question.** "Is the whole API implemented?" now has
   a number with a method behind it, and `--detail` names the missing
   members rather than gesturing at them.
2. **Choosing what to add.** The report sorts by the size of the gap,
   so the next properties to implement are the first rows.
3. **Generating rather than typing.** Everything the chains need per
   property - name, type, direction - is in the dump, which is what
   makes generating the chains possible instead of extending them by
   hand. That generator is not written yet; this report is its input.

## Running the tests without Altium

`tests/test_sdk_reflection.py` exercises the model against a small
synthetic dump and the chain parsing against the real Pascal sources,
so the whole suite runs on a machine that has never had Altium
installed. The single test that needs a real dump skips when there is
none.
