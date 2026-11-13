# Thermal analysis

`sim_thermal_analysis` answers the question a layout review keeps asking: how
hot does this board get, and where? You give the power each part dissipates.
It reads the board's real copper and stackup, solves the steady temperature
field in three dimensions, and reports the peak, the hotspots, every powered
part against its limit, and the parts that sit next to the heat. It writes a
heat map of every copper layer.

It runs offline in a few seconds, needs no external program, and changes
nothing on the board.

## A first run

```
sim_thermal_analysis(
    expect_file="C:\\work\\board.PcbDoc",
    powers={"U1": 1.2, "U7": 0.8, "Q3": 0.45, "R1*": 0.1},
    packages={"U1": {"theta_jb": 9.0, "tj_max_c": 125},
              "U7": {"psi_jb": 14.0, "theta_jc": 22.0, "tj_max_c": 125}},
    ambient_c=40,
)
```

`powers` maps designators to watts. A glob such as `R1*` covers a group, and
an exact name wins over a glob. `packages` carries what the datasheet gives
for a part: `theta_jb` (or `psi_jb`) from junction to board, `theta_jc` from
junction to case, `tj_max_c`, and `heatsink_theta_sa` when a heatsink sits on
the case. Without `theta_jb` a part still heats the board, and the reply gives
the board temperature under it, not its junction.

The reply:

| Key | What it holds |
|---|---|
| `t_max_c`, `t_max_layer`, `t_max_at_mils`, `rise_max_k` | The peak and where it is |
| `pass`, `over_tj_max`, `over_limit` | False when a junction is over its `tj_max_c`, or a part's board temperature is over `limit_c`. Null when neither was given: there is nothing to judge against |
| `parts` | Each powered part: pad, junction and case temperature, margin to `tj_max_c`, hottest first |
| `hotspots` | Local maxima of the two outer layers, each with the part whose outline is nearest (distance 0 inside it) |
| `hottest_components` | The warmest parts that dissipate nothing themselves: the electrolytic next to the regulator |
| `layers` | Maximum, mean and minimum of every copper layer, and its copper fraction |
| `balance` | Power in against every way out (convection and radiation from each face, the edges, part cases, heatsinks, clamps). It closes to solver precision, and it is the first check of a run |
| `convection` | The coefficients that came out for each face |
| `grid` | Cell size, cells, unknowns, the solver and its iterations, the pitch the copper was sampled at (`sample_mils`), and the seconds the raster and the solve took |
| `stackup` | Whether it was assumed, the board thickness as solved (copper and laminate) and the laminate alone |
| `refinement` | With `refine=True`: how far each number moved on a grid twice as fine |
| `files`, `run_dir` | One PNG heat map per copper layer on one colour scale, one PNG of each layer's copper as solved, `result.npz` with the fields, `report.json`, `inputs.json` |
| `notes`, `assumptions` | Everything that was assumed or left out |

## What surrounds the board

| Argument | Meaning |
|---|---|
| `ambient_c` | The air and the surroundings. Inside an enclosure, give the air temperature inside it |
| `orientation` | `horizontal` (top side up), `inverted`, or `vertical` |
| `airflow_ms`, `airflow_deg` | Forced air along the board and the direction it moves towards. Parts downstream sit in warmed air and run hotter |
| `altitude_m` | Thinner air convects less: about 12 % less at 3000 m |
| `clamps` | A rectangle of a face held to a chassis at a temperature, through a contact conductance: a mounting rail, a gap pad to the case |
| `heat_areas` | Heat made in the copper of a layer inside a rectangle, each piece of copper taking its share. A rectangle with under 1 % copper heats the bare laminate there |
| `heat_map_npz` | The Joule heat an IR-drop run found, added to the part powers: `.npz` files with one `W_<layer>` array of watts per cell for each copper layer, and `x0`, `y0`, `cell_mils` of their grid. The grid need not match |
| `options` | `emissivity`, `dielectric` (`fr4`, `high_tg_fr4`, `polyimide`, `rogers_4350b`, `rogers_4003c`, `ptfe`, `imsl`) or `k_xy` / `k_z`, `via_fill` (`none`, `epoxy`, `conductive`, `silver`, `copper`), `plating_um`, `solder_mask`, `radiation`, `convection` (`local` or `mean`), fixed `h_top` / `h_bottom`, `up_deg` |

All of it can live in a JSON file (`inputs_json_path`) with the keys `powers`,
`packages`, `heat_areas`, `clamps` and `options`. Arguments given in the call
win over the file. Every run writes the inputs it used as `inputs.json`, so a
run can be repeated.

## The model

**Conduction.** The board is cut into square cells (about 40,000 by default,
or `cell_mils`) and through its thickness into planes: one for each copper
layer and two or more for each dielectric. Every cell of every plane is an
unknown, so a four-layer board is several hundred thousand of them.

- Copper is taken from the board as it is: pours with their voids and
  thermal-relief cuts, split planes with an antipad round every hole of another
  net, tracks, pads. Tracks, round shapes and rectangles enter with their exact
  area, so a track conducts with its real width whatever the cell size.
- Between two cells of a copper layer, heat flows through the copper that
  really joins them. A track along the way conducts with its width. A
  clearance across it conducts only through the resin, even when it is far
  narrower than a cell.
- The copper is sampled every 2 mil whatever the cell size. The web of
  copper between two antipads under a BGA is 10 mil or less: on a coarser
  sample it reads as a cut, and the plane under the BGA as open.
- The laminate conducts three times better along the glass than through it
  (0.81 against 0.29 W/m/K for FR-4). Both values are used.
- Every plated hole is modelled on its own: the barrel's copper wall from
  layer to layer, joined to each plane it passes by radial conduction through
  that plane. On a plane of its own net it joins at the wall. On a plane of
  another net the antipad's resin comes first, so a signal via does not leak
  heat into a ground plane it does not touch. Via fill and plating thickness
  are options.
- A pad smaller than the cells around it is not smeared over them: heat
  leaves through the area under the copper and a fringe beside each edge,
  from the conformal-mapping result for a strip over a plane, and crosses
  to the next layer in a tube of that width.
- The laminate is one body from face to face. Heat on its way through the
  board passes an inner layer through the laminate between its tracks, and
  a plane through its antipads, also in a cell that holds copper and bare
  laminate.
- A via's land is at its barrel's temperature, on cells larger and smaller
  than the land.
- Copper that does not touch is never joined by the grid. A cell that holds
  a pad and the pour beyond its clearance, two tracks side by side, or a
  via's land in another net's pour, has one node for each of those pieces of
  copper. Each piece conducts to its own island in the next cell, to the
  laminate under its own area, to its own barrel and to its own pads. So a
  gull-wing part in a pour reads the same on 50 mil cells as on cells that
  resolve its 6 mil clearance. The heat map shows the hottest copper of each
  cell.

**Surfaces.** Both faces, the board's edges and part cases lose heat to the
air and to the surroundings.

- Natural convection: Lloyd and Moran for a face looking up, Raithby and
  Hollands for a face looking down, Churchill and Chu for a vertical board.
  Each cell uses its own temperature. On a vertical board it also uses its
  own height, so the top of the board, in air the bottom has warmed, is cooled
  less.
- Forced convection: the flat plate in parallel flow, laminar and turbulent,
  local from the leading edge. It combines with natural convection by
  Churchill's rule.
- Radiation: grey-body exchange with the surroundings, to the fourth power of
  temperature with no linearisation left in the result.
- Air properties follow temperature (Sutherland) and pressure.

**Solution.** The surface laws are nonlinear, so the system is solved by
Newton's method. Each step is a sparse symmetric system, solved by conjugate
gradients with an algebraic-multigrid preconditioner. Two million unknowns
take well under a minute.

## How far to trust it

The solver is tested against exact solutions (`tests/thermal`):

| Case | Reference | Agreement |
|---|---|---|
| Heat straight through a stack of layers | Series resistances | 1 part in 10⁹ |
| Point heat source on a convecting copper sheet | Modified Bessel function K₀ | 0.6 % |
| Square source on a laminate slab with anisotropic conduction | Fourier-series spreading resistance | 0.5 % at the peak on 10 mil cells |
| Board in still air with radiation | The scalar balance of the same correlations | 0.002 K |
| Vertical board and forced flow | Churchill-Chu and flat-plate averages | Matches the average, with the expected gradient along the board |
| Via arrays | Barrels in parallel | 2 % |
| A plane perforated by a BGA's antipads | Rayleigh's conductivity of a sheet with a square array of round holes | 4 % on 16 mil cells, 15 % on 40 mil |
| A track as the only path | Its own length over conductivity, width and thickness | 3 % at any cell size |
| A part on thermal vias, cells from 40 mil down to 5 | The same answer | Within 0.5 K on a 62 K rise |
| Heat through an inner layer of tracks, 25 mil cells against 5 | The same answer | 4 % |
| A 20 mil pad in 50 mil cells: on bare laminate, over a plane, over tracks | The same pad on 2.5 mil cells | 8 to 11 % |
| A BGA's via field, 20 mil cells against 10 | The same answer | 1 % |
| A gull-wing part in a pour, 8 mil clearance, cells of 50, 25 and 20 mil | The same part on 6.25 mil cells | 4 % (46 % low before islands were split) |
| A copy of a real 16-pin part in a pour, 6.5 mil clearance, cells from 25 mil down to 5 | The same answer | 1 K on a 47 K rise |
| Vias of one net through another net's pour, cells of 30 and 20 mil | The same on 5 mil cells | 2 % |

`refine=True` solves the board a second time with cells half the size and
lists how far every temperature moved. That move is the estimate of the
error left in the finer solution. A part on thermal vias over planes is
settled to a fraction of a kelvin on the default grid. A part whose pads
are smaller than a few cells is held to about a tenth of its rise by the
sub-cell models. The reply flags it as `under_resolved`, and a smaller
`cell_mils` or `refine` settles it. A fine-pitch part (pads under 20 mil wide,
under 10 mil apart) with no thermal pad moves by up to a tenth of its rise
between 25 and 8 mil cells for the same reason: on a real six-layer board
a 24-pin part at 25.6 mil pitch read 16.2 K over ambient at 25 mil cells and
17.5 K at 8 mil, while a 16-pin part at 50 mil pitch beside it stayed
within 1 K on a 35 K rise.

The grid is rarely what limits the answer. These matter more:

- **The powers.** A regulator's dissipation at the real load and input
  voltage, not the datasheet's maximum. Temperature rise is close to
  proportional to power.
- **The air.** The correlations are for an open board in still or evenly
  moving air. In a sealed box the air inside is hotter than the room: give
  that temperature as `ambient_c`. Neighbouring boards, ducts and fans close
  to a part are not modelled.
- **The package numbers.** `theta_jb` and `psi_jb` are measured on a JEDEC
  test board. They transfer well to a board with planes, less well to a
  two-layer board.

Expect the result to be within a few kelvin of a careful measurement when
the powers and the air are known, and to rank design changes (more vias, a
bigger pour, a different orientation) reliably.

## What is not modelled

- Transients: the result is the temperature the board settles at.
- Thermal-relief spokes on plane layers: a hole on its own net's plane is
  taken as fully connected. On signal layers the poured copper is used as
  Altium poured it, reliefs included.
- Temperature dependence of the laminate and copper conductivity.
- Heat carried by wires, connectors and mounting screws, unless you describe
  it as a clamp.
- A polygon pour that has not been poured has no copper to count. The reply
  names it.
- A stackup with no dielectric thickness or no copper thickness set: the
  missing numbers are filled from a standard 1.6 mm build and 1 oz copper,
  and the reply says which. Set the layer stack in the PCB editor for real
  numbers.

## Files

Each run gets a folder under `EDA_AGENT_THERMAL_RUNS` (default
`%USERPROFILE%\EDA Agent\thermal`), or under `runs_dir`:

| File | Content |
|---|---|
| `thermal_<layer>.png` | Heat map of the layer, part outlines, a label on each powered part, a cross on the peak, a scale in degrees C. All layers share one scale |
| `copper_<layer>.png` | The copper the solver saw on the layer: each cell shaded by the share of it that is copper, with the board outline and the parts. Compare it with the layer in the PCB editor: a pour that is missing here was not read |
| `result.npz` | `T_<layer>` arrays (degrees C, NaN outside the board) with `x0`, `y0`, `cell_mils` |
| `report.json` | The full reply |
| `inputs.json` | The inputs as used |

## From Python

```python
from eda_agent.layout.model import LayoutBoard
from eda_agent.emc.stack import assumed_stack
from eda_agent.thermal import run

board = LayoutBoard.load("board.json")
report, result, used = run.run(board, assumed_stack(board.layers),
                               powers={"U1": 1.2}, ambient_c=40, refine=True)
print(report["t_max_c"], report["refinement"]["estimated_error_k"])
run.write_files(report, result, board, "out", used)
```
