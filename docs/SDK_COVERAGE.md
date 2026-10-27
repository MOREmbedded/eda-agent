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
   hand. That generator is `src/eda_agent/sdk/generate.py`; this report
   is its input, and the next section is the part the dump cannot
   answer.

## Declared is not exposed

The assembly is ground truth for what Altium's SDK **declares**. It is
not ground truth for what the scripting engine will **resolve**, and
the gap is large: of 1,688 distinct property names in the dump, 79.9%
are reachable as `Obj.Name`, 12.6% only through `Obj.GetState_Name`,
and 7.5% not at all.

Reaching for one it cannot resolve raises `Undeclared identifier` and
**halts the script debugger on that line**. Altium then refuses to
close the document ("cannot close the current document when a script is
stopped on a breakpoint") until the script is stopped.

`Try/Except` does not catch it. That was a documented assumption for a
long time; it is now a measured one. The handler wrapped the call, the
`Except` block did not run, and the debugger halted on the same
property as before.

### The engine's own symbol table

`ScriptingSystem.dll` carries the names the engine registers, as
UTF-16 runs, one triple per member:

```
DisplayMode / GetState_DisplayMode / SetState_DisplayMode
```

Which explains the error exactly. For the property that halted the
debugger twice:

| name | registered |
|------|-----------|
| `DescriptionString` | no |
| `GetState_DescriptionString` | yes |

The getter exists and the bare name does not, so property syntax has
nothing to resolve. "Undeclared identifier" is literal.

```
eda-agent sdk-symbols                      # what was extracted, and when
eda-agent sdk-symbols --refresh            # re-read the local install
eda-agent sdk-symbols --name DisplayMode   # how one member can be reached
```

The output is derived from a non-redistributable assembly, so it stays
in `reference/`, which `.gitignore` excludes, alongside the SDK dump.

### What the table is allowed to claim

Names are recovered as maximal runs of printable characters, so a short
name merges with whatever sits beside it in the binary. `X`, `Y`, `X1`,
`X2`, `Y1` and `Y2` are read off real objects at hundreds of sites in
our own Pascal and still do not survive extraction as standalone runs.

So the table **abstains** below `MIN_RELIABLE` characters rather than
calling those unreachable, and `emittable()` treats abstention as
permission. A confident wrong answer here deletes working code.

It was validated against the only ground truth available, which is
measurement rather than documentation:

| evidence | agreement |
|----------|-----------|
| 283 names across the four shipped property chains | no contradictions |
| 5 names a live probe read before the first halt | 5 / 5 |
| 1 name that halted the debugger, twice | 1 / 1 |
| 26 names the table approved, probed live in one pass | 26 / 26 |

A name the table calls unreachable that the Pascal dereferences would
be the table being wrong. The test
`test_the_table_never_contradicts_the_shipped_pascal` fails on exactly
that.

### The probe, now a confirmation

The probe reads names off a real component and reports which answered.
Since the generator gates on the table, it is no longer how exposure is
discovered, which is the point: discovery cost a halted debugger per
answer.

```
eda-agent sdk-probe                        # every approved candidate
eda-agent sdk-probe --designator R1        # a specific part
eda-agent sdk-probe --properties Handle,Dimmed
```

It is **read only**: several writable-only properties are internal
state (`ErrorColor`, `FilePosition`, `ComponentKind`), and probing a
write would edit a schematic to find out whether it could.

### When it does not come back

Every attempt is logged inside Altium *before* it is made, and
`AppendLog` closes the file per line, so the last `_sdk_probe,attempt=`
entry in `activity.log` names the property that stopped the loop, with
no matching `done=`. The CLI reads that back and says so.

Recovery is manual, because a halted debugger holds the application:
dismiss the dialog, **Run -> Stop** in the script editor, then start
the loop again. `eda-agent relaunch` cannot do it for you while the
script is stopped on a breakpoint.

### The generated block

It lives between markers in `scripts/altium/Generic.pas` rather than in
its own unit, so the build order, the lint file list, the deploy set
and the unit-stamp reporting do not need to learn about a file that
exists only to be rewritten. A test re-runs the generator and asserts
the block still matches, because generated code that has drifted from
its generator is worse than hand-written code: the next regeneration
silently reverts whatever was edited into it.

The generator refuses what it cannot express, and says why rather than
dropping it: an indexed property has no way to receive its index
through a name lookup, a declared `Object` or `Void` does not cross the
String bridge the chains report through, and a name the engine
registers only as a getter does not resolve as a property. Pass the
table and the block carries capabilities; omit it and the block carries
candidates.

## Running the tests without Altium

`tests/test_sdk_reflection.py` exercises the model against a small
synthetic dump and the chain parsing against the real Pascal sources,
so the whole suite runs on a machine that has never had Altium
installed. The single test that needs a real dump skips when there is
none.
