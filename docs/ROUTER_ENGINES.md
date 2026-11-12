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

A setting that cannot be used (an unknown engine name, a `router.json`
that is not valid JSON) is ignored: the default engine routes, and the
job's notes say what was wrong.

## What Euan's router does differently

From his fork's history, the main differences are these:

- **Pad entries.** Pins are left straight and centred through the pad's
  short edge (`axis_entry`), with a jog outside the pad, a 45 degree
  mid-entry where that fits, and a corner chamfer pass.
- **Fab via rules** (written for JLC multilayer boards): a via in a pad
  only at its centre (`pad_vias="centre"`), a gap between vias of the
  same net, the net's existing vias reused as free layer changes
  (`reuse_vias`), and stitched vias where a wide net changes layer.
- **Neck-down at pads** (`neck_pads`). Euan's router runs the net's full
  width up to the narrow pad's copper and necks down only for the last
  mils. Ours stops the full width clear of the pad. The job's notes say
  so when you ask for it.
- **Layer directions.** It takes a preferred direction per layer by name
  (`layer_dirs`, `dir_strength`), a cost on the outer layers
  (`outer_cost`) and a grid `pitch`. These four are routing job
  parameters that only the euan engine reads. The default engine ignores
  them and its notes list them.

Our stage switches are translated for it: `exclude_layers`,
`join_existing`, `reuse_vias` and `neck_pads` become arguments of his
router, and `axis_entry` becomes `pad_entry="axis"`. An option neither
router knows fails the same way on both engines.

## What both engines share

- **One verdict.** Both results go through the same DRC verdict, the
  same check that what is sent is what was judged, and the same gloss:
  ours. Euan's own gloss is still in his package, but the job does not
  use it. In our checks it crossed keep-outs and the board edge, and it
  could drop a track that was already on the board. Ours now also keeps
  out of keep-outs drawn on the Keep-Out layer, which it used to miss.
- **Via guards.** His router predates the default router's via guards
  (2026-11-02), so they were ported into it. A new via keeps the board's
  hole-to-hole gap from every hole already drilled. It clears other
  nets' copper on layers the router does not route on, such as an
  excluded layer or a pour taken as a plane (without this, his router
  laid a via on another net's copper there, which is a short). It also
  leaves the board's solder mask web to every pad and via opening. Both
  engines apply these guards wherever a via is laid: in the search, in a
  BGA's fan-out and in the plane fan-out.
- **Old pours** are taken off before routing per layer and net, as his
  router expects.

`tests/layout/test_router_engines.py` runs every one of these cases on
both engines, and a seeded break of each guard makes it fail.

## How they compare

Synthetic boards, each routed by both engines with the same settings.
`python -m eda_agent.layout.bench_run --router euan` runs the benchmark
on your own finished boards with Euan's router (`default` is the
default).

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
