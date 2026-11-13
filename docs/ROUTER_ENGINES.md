# Autorouter engines

`pcb_autoroute` routes the focused board as a job: the result comes back
judged by our DRC verdict, and nothing reaches the board until
`pcb_autoroute_apply` writes it. Two engines can do the routing.

| Engine | What it is | Used |
|---|---|---|
| `default` | The in-house router: exact clearances on a grid, nets negotiating for contested space, then the stages (differential pairs, pin and bus lanes, fan-out). | Unless you choose otherwise |
| `euan` | Euan Mutch's version of the same router, from his fork ([TechAUmNu/eda-agent](https://github.com/TechAUmNu/eda-agent), branch `carrier-local` at `030575e5a`, 2026-10-27; Apache-2.0). | Only after you turn it on |

## Choosing the engine

The engine is your choice, never the routing call's. A request cannot
switch it, and the job result's `router` field says which engine ran.

| Command or setting | Effect |
|---|---|
| `eda-agent router-use euan` | Use Euan's router for the active profile (writes `router.json` in the profile folder). The next routing run takes it; no restart needed. |
| `eda-agent router-use default` | Back to the default engine. |
| `eda-agent router-show [--json]` | Which engine is in force, and which setting decided it. |
| `EDA_AGENT_ROUTER=euan` or `default` | Overrules the profile for the process that has it set, such as one MCP server. |

The first setting present decides: `EDA_AGENT_ROUTER`, then the
profile's `router.json`. If that setting cannot be used (an unknown
engine name, a `router.json` that is not valid JSON), the default engine
routes, even when a setting further down names Euan's: what you meant is
unknown, and the default engine is the one that needs no asking.
`router-show` then reports the problem and exits with 1, and the job's
notes say what was wrong.

`router-use` keeps any other keys in `router.json`, and replaces the file
in one step, so a routing run never reads half of it. It refuses to
write over a `router.json` it cannot read, which may hold something you
want; fix or delete that file first.

## What Euan's router does differently

From his fork's history, the main differences are these:

- **Pad entries.** Pins are left straight and centred through the pad's
  short edge (`axis_entry`), with a jog outside the pad, a 45 degree
  mid-entry where that fits, and a corner chamfer pass.
- **Via in a pad at its centre only** (`pad_vias="centre"`, written for
  JLC multilayer boards). Only his router has this stage option. The
  routing job takes it in `stages`; `pcb_autoroute` does not offer it.
  His fork's other fab via rules (a gap between vias of the same net,
  stitched vias where a wide net changes layer) are in his package, but
  the job does not turn them on.
- **Neck-down at pads** (`neck_pads`). Euan's router runs the net's full
  width up to the narrow pad's copper and necks down only for the last
  mils. Ours stops the full width clear of the pad. The job's notes say
  so when you ask for it.
- **Layer directions.** It takes a preferred direction per layer by name
  (`layer_dirs`, `dir_strength`), a cost on the outer layers
  (`outer_cost`) and a grid `pitch`. These four are parameters of the
  routing job, given through `design_job_start(kind="layout_route",
  params=...)`; `pcb_autoroute` does not take them. Only the euan engine
  reads them; the default engine ignores them and its notes list them. A
  value out of range fails the job before anything runs:

  | Parameter | Takes |
  |---|---|
  | `pitch` | 1.5 to 25 mil, or 0 for the router's own choice |
  | `dir_strength` | 0 to 10 |
  | `outer_cost` | 0.1 to 100 |
  | `layer_dirs` | copper layer names, each to `h` or `v` |

  Below zero, `dir_strength` and `outer_cost` make some of his search's
  step costs negative, and the shortest path search then ran the server
  process out of memory; a finer pitch grows the grid with the inverse
  square.
- **Old pours.** Before routing, the job takes each polygon's old poured
  copper off the board, which is repoured after. Both engines match the
  copper to its polygon's outline by layer and source. For Euan's, as
  his job did, a pour is also taken off when its net has a polygon
  outline on that layer.
- **Every stage switch off.** With `diff_pairs`, `plane_fanout`,
  `pin_lanes` and `bus_lanes` all off (and no other stage option),
  `pcb_autoroute` asks for the router exactly as it was before the
  stages. The default engine then also skips the via guards below,
  except a BGA fan-out's mask web check. Euan's keeps them all.

Our stage switches are translated for it: `exclude_layers`,
`join_existing`, `reuse_vias` and `neck_pads` become arguments of his
router, and `axis_entry` becomes `pad_entry="axis"`. An option neither
router knows fails the same way on both engines.

## What both engines share

- **One verdict.** Both results go through the same DRC verdict, the
  same check that what is sent is what was judged, and the same gloss:
  ours. Euan's own gloss is still in his package, but the job does not
  use it. In our checks it crossed keep-outs and the board edge, and it
  could drop a track that was already on the board. Ours keeps out of
  keep-outs drawn on the routed layer and on the Keep-Out layer, as
  regions, tracks or arcs (the ring round a mounting hole is often one).
- **Via guards.** His router predates the default router's via guards
  (2026-11-02), so they were ported into it. A new via:
  - keeps the board's hole-to-hole gap from every hole already drilled;
  - clears other nets' copper on layers the router does not route on,
    such as an excluded layer or a pour taken as a plane (without this,
    his router laid a via on another net's copper there, which is a
    short);
  - stays out of keep-outs drawn on those layers, which the via passes
    through as well;
  - leaves the board's solder mask web to every pad and via opening.

  Both engines apply these guards wherever a via is laid: in the
  search, in a BGA's fan-out and in the plane fan-out. A via the search
  puts in a pad is laid at the pad's centre and checked there, not at
  the centre of the nearest grid cell, which can be a few mils away.

`tests/layout/test_router_engines.py` runs every one of these cases on
both engines, and a seeded break of each guard makes it fail.

## How they compare

<!-- BENCH-TABLE -->

## Known limits (both engines)

- New vias are kept from the holes and mask openings already on the
  board. Between two vias the router lays itself, only the copper
  clearance is kept. A hole-to-hole rule larger than that clearance, or
  a mask sliver rule, can still be missed between two new vias. The
  verdict reports any such case.
- At an inward corner of the board outline, a diagonal step can bring a
  track's copper a fraction of a mil past the edge when the board has no
  outline clearance rule.
- The DRC verdict does not check copper against keep-outs. Both routers
  keep out of them; copper placed by other means is not judged against
  them.
- With every stage switch off, the default engine's vias skip the via
  guards (see "Every stage switch off" above).
