# Altium scripting coverage and extension guide

This document maps Altium's published scripting examples and object-model
interfaces to the MCP surface in this repository. It separates three claims:

1. **Registered**: a Python MCP tool exists and appears in
   [`TOOL_REFERENCE.md`](TOOL_REFERENCE.md).
2. **Implemented**: the tool reaches a dispatched DelphiScript handler.
3. **Live verified**: that exact path completed against a running Altium build.

The generated tool reference is the authoritative inventory of every public
tool. This file is the architectural map and gap ledger for Altium scripting.

## Source authority and limitations

Use official Altium material first:

- [Automating Design Tasks with Scripting](https://www.altium.com/documentation/altium-designer/scripting)
- [Scripting Examples Reference](https://www.altium.com/documentation/altium-designer/scripting/examples-reference)
- [DelphiScript language support](https://www.altium.com/documentation/altium-designer/scripting/delphiscript/support)
- [Workspace Manager API](https://www.altium.com/documentation/altium-dxp-developer/workspace-manager-api)
- [PCB API overview](https://www.altium.com/documentation/altium-dxp-developer/pcb-api)
- [PCB system interfaces](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-system-interfaces-reference)
- [PCB design-object interfaces](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-design-objects-interfaces-reference)
- [Schematic API overview](https://www.altium.com/documentation/altium-dxp-developer/schematic-api)
- [Schematic system interfaces](https://www.altium.com/documentation/altium-dxp-developer/schematic-api-system-interfaces-reference)
- [Schematic design-object interfaces](https://www.altium.com/documentation/altium-dxp-developer/schematic-api-design-objects-interfaces-reference)

Altium explicitly warns that the scripting API evolves and some legacy example
scripts no longer work in current versions. The older developer references are
also SDK/RTL-derived work in progress. Therefore "documented by Altium" is an
implementation lead, not a claim that a member works in AD26. The local
[`altium-delphiscript`](altium-delphiscript/README.md) reference deliberately
records only members exercised by this bridge.

There is no stable, machine-readable list of every runtime DelphiScript member,
and COM interfaces are late-bound. An exhaustive "all Altium methods" promise
would be misleading. Coverage is audited by official example family, confirmed
interface members, existing handlers, and live tests.

## How an agent discovers the available surface

- Read [`TOOL_REFERENCE.md`](TOOL_REFERENCE.md) for every registered tool,
  interaction class, maturity, and one-line description. It is generated; do
  not edit it by hand.
- Call `tool_catalog` at runtime to search tools by category/capability.
- Call `app_capability_probe` against a running Altium instance to inventory
  the APIs/features visible in that installation.
- Read [`altium-delphiscript/README.md`](altium-delphiscript/README.md) when
  implementing Pascal. Its signatures are taken from code the bridge calls.
- Read [`AI_ALTIUM_FIELD_NOTES.md`](AI_ALTIUM_FIELD_NOTES.md) and
  [`RELEASE_VERIFICATION.md`](RELEASE_VERIFICATION.md) before live mutation.

## Additions from the 2026-08-29 official-documentation audit

| Public MCP tool | DelphiScript API/handler | Safety | AD 26.9.1 status |
|---|---|---|---|
| `pcb_query_region` | `IPCB_Board.SpatialIterator_Create`, `AddFilter_Area` | read-only; primitives only | live verified |
| `sch_query_region` | `ISch_Iterator.AddFilter_Area` | read-only | live verified |
| `pcb_get_used_layers` | `IPCB_Board.LayerIsUsed[layer]` | read-only | live verified |
| `pcb_get_drill_layer_pairs` | `DrillLayerPairsCount`, `LayerPair[index]` | read-only | live verified |
| `pcb_get_internal_planes` | `InternalPlaneNetName[layer]`, layer stack V7 | read-only | live verified; test board has no internal-plane layers |
| `pcb_get_special_strings` | `IPCB_Text.UnderlyingString`, `ConvertedString` | read-only | live verified |
| `sch_get_component_models` | `ISch_Implementation` plus datafile links | read-only | live verified |
| `obj_measure_distance` | existing pure DelphiScript geometry handler | read-only/pure | live verified (3-4-5 result) |
| `sch_place_compile_mask` | existing `SchObjectFactory(eCompileMask, ...)` handler | mutating; can hide ERC/connectivity | registered and statically verified; deliberately not run on the client's unfinished schematic |

The generic schematic type mapper now also recognizes `eArc`, `ePolygon`,
`eCompileMask`, `eHarnessConnector`, `eCrossSheetConnector`, `eProbe`, and
`eTextFrame`. This lets `obj_query`, `obj_modify`, `obj_delete`, and
`sch_query_region` address object kinds already used by dedicated handlers.

## Additions from the 2026-10-13 Booster 3.2 integration

Extended an existing handler rather than adding a new command. The full
implementation trail above was followed: handler extended, dispatch
unchanged since the action name already existed, Python wrapper added,
`SCRIPT_VERSION` bumped to `2026.10.13.5`, reachability/consistency tests
updated, `lint.py`/`build.py` run clean, and the result live-tested.

| Public MCP tool | DelphiScript API/handler | Safety | Status |
|---|---|---|---|
| `eng_validate_library_live` (via bridge command `library.get_footprint_pads`) | `Lib_GetFootprintPads`, extended: `IPCB_Track.X1/Y1/X2/Y2` cast from `IPCB_Primitive` (same pattern as the existing `PCB_GetNetRouting`), plus the already-computed 3D `BodyCount` now also surfaced as `has_3d` | read-only | **live-verified 2026-10-13** - read back against a PcbLib authored in a running Altium session (a two-pad footprint with a courtyard on Mechanical1 and silkscreen on TopOverlay); track coordinates and the `has_3d` flag both returned correctly |

`IPCB_Track.X1/Y1/X2/Y2` was already used by `PCB_GetNetRouting` before this
change (presumably live-verified when that handler was added, though this
reference's own `altium-delphiscript/README.md` does not carry an entry for
it either) - this addition is a new call site for an existing, working
pattern, not a new interface member for this codebase.

Arcs, regions and fills still report `kind` and `layer` only. A courtyard or
silkscreen outline drawn with arcs rather than tracks is therefore invisible
to the geometry checks, which was a scope decision rather than an oversight.

## Official example-family coverage

This table is intentionally by capability family. Individual public tools are
all listed in the generated reference.

| Official family/example | Current MCP coverage | Status/notes |
|---|---|---|
| Run/open script projects, server/process calls | `app_attach`, `app_ping`, `obj_run_process`, documented COM launch URI | covered; dispatch is not proof that an undocumented process succeeded |
| Workspace/project/document traversal | `proj_*`, `app_list_documents`, `app_open_document`, `obj_get_document_info` | covered through Workspace Manager and client document interfaces |
| Schematic object iteration and editing | `obj_query/count/create/modify/delete/batch_*`, `sch_place_*` | broad coverage; undo/registration rules live in the local interface reference |
| ModelsOfAComponent / simulation models | `sch_get_component_models`, model/link library tools, simulation tools | placed-component reader added and live verified |
| Schematic spatial iterator | `sch_query_region` | added and live verified; explicit coordinates replace mouse selection |
| PCB board/group/library iterators | `obj_query`, `pcb_get_*`, `lib_get_*`, region-specific tools | covered; spatial and group-object restrictions are explicit |
| PCB used layers, stack, mechanical layers, drill pairs, internal planes | `pcb_get_used_layers`, `pcb_get_layer_stackup`, `pcb_get_mech_layer_names`, `pcb_get_drill_layer_pairs`, `pcb_get_internal_planes` | covered for readback; stack edits use dedicated guarded tools |
| PCB special strings/font data | `pcb_get_special_strings`, `obj_get_font_id`, `obj_get_font_spec`, text tools | covered; authored and rendered values are separate |
| Nets/classes/differential pairs/rules | PCB net/class/diff-pair/rule readers and writers | covered by dedicated tools rather than raw interface exposure |
| Violations/DRC/ERC | `pcb_get_clearance_violations`, `pcb_run_drc`, `proj_run_erc`, audit tools | covered; checker execution and result readback remain separate stages |
| PCB/schematic/library placement and movement | `pcb_place_*`, `pcb_move_components`, `sch_place_*`, `lib_add_*` | covered with non-interactive coordinates and batch forms |
| Outputs/BOM/PnP/Gerber/STEP/PDF/OutJob | `proj_export_*`, `proj_run_outjob*`, `proj_generate_fab_package`, PCB exporters | covered; modal paths are labelled modal |
| UI forms, menus, dialogs, cursor selection | `app_click_menu`, dialog tools, `app_run_ui_command`, coordinate replacements | intentionally handled through the GUI layer where no stable scripting API exists |

## Known gaps and deliberate non-tools

| Candidate | Why it is not a public tool | Required next step |
|---|---|---|
| `PCB_FilletCorners` | Live AD26 dry-run wedges the scripting loop because it creates a spatial iterator while a board iterator is active. The handler remains internal and is listed in `KNOWN_UNREACHABLE`. | Rewrite as two passes: snapshot tracks/endpoints, destroy the board iterator, then perform bounded geometry work. Live-test dry-run before exposing. |
| Native `ValidateLayerStack` example | The official examples page names it, but the current public reference does not give a sufficiently reliable AD26 callable contract. | Obtain the current official example or verify an exact interface member in a disposable live project. Do not guess a process id. |
| `RebuildInternalAndSplitPlanes` example | Destructive operation; the legacy example's exact current process/member is not documented reliably. | Verify on a disposable board with planes, prove before/after state, then add rollback guidance. |
| Full PCB query-language execution | The Query Language is separate from the DelphiScript object iterator. A generic raw-query bridge is not present. | Add only with deterministic result extraction and strict read/mutate separation. A Filter-panel dispatch alone is not completion evidence. |
| Every panel/preference/interactive command | Many are GUI-only or undocumented processes. | Prefer menu/dialog tools and capability probing. Add Pascal only when the result can be verified. |

Altium's examples page marks the legacy EnableBasic and Query Script examples
as unusable in current Altium. They are reference material, not missing MCP
features.

## Required implementation trail for every future capability

A capability is not complete until the next agent can trace and verify it:

1. Add a focused handler in `scripts/altium/<Module>.pas`.
2. Add its action to that module's `Handle*Command` dispatcher.
3. Add a typed MCP wrapper under `src/eda_agent/tools/` with units, return
   shape, interaction/safety, limits, and follow-up checks in the docstring.
4. Bump `SCRIPT_VERSION` in `scripts/altium/Main.pas`.
5. Extend `docs/altium-delphiscript/` with the exact members called and traps.
6. Add/update reachability and request-key tests. Never grow an allowlist
   without a concrete reason.
7. Run `python scripts/altium/lint.py scripts/altium` and
   `python scripts/altium/build.py`.
8. Regenerate the catalog with `python scripts/gen_tool_reference.py`.
9. Live-test read-only first. For mutations, use a disposable/checkpointed
   design and verify saved state, connectivity, and DRC/ERC as appropriate.
10. Record the Altium version and observed result here when status changes.

## Latest live verification record

On 2026-08-29, script version `2026.08.29.5` was exercised against Altium
Designer Professional 26.9.1 and the open `modem-stm32-2trx-lr2021` project.
The read-only additions returned real board/schematic data; the final
`application.ping` reported protocol 2 and zero cast errors. No PCB or
schematic mutation from this audit was applied.
