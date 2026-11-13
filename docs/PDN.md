# Power distribution network (PDN) analysis

Two solvers answer the two questions a power rail raises on a routed board.
Both run offline on the board's real copper and stackup, need no external
program, and change nothing on the board.

| Tool | Question | Method |
|---|---|---|
| `sim_pdn_ir_drop` | How much voltage does each load lose at DC, where does the current crowd, and which tracks and vias carry too much? | A sparse resistor network of the net's copper, solved directly |
| `sim_pdn_impedance` | Does the rail stay under its target impedance from the regulator's bandwidth up to where the package takes over? | A field solve of the power/ground plane pair, with every capacitor and its mounting |

Both read the focused board live (`expect_file`) or a saved board
(`board_json_path`), as `sim_emc_prescan` does, and both read the real layer
stackup. Each run writes its files to a new folder of its own under
`runs_dir`, else `EDA_AGENT_PDN_RUNS`, else `~/EDA Agent/pdn`, so two runs
never overwrite each other. Both read the whole board, so ask before running
them on a very large one.

## DC IR drop: `sim_pdn_ir_drop`

```text
sim_pdn_ir_drop(
    net="+3V3", return_net="GND",
    sources=[{"ref": "U1", "volts": 3.3}],
    sinks=[{"ref": "U7", "amps": 1.8}, {"ref": "U9", "amps": 0.4}],
    max_drop_pct=2.0,
    expect_file=r"D:\proj\board.PcbDoc")
```

A **source** is a regulator output or a connector: every pad of `ref` on the
net is held at `volts`. A **sink** is a load and its DC current. With
`return_net`, the ground rise is solved the same way and added, so the rail
each load sees is the source voltage less both drops. A source or sink given
by `pads` names its return pins with `return_pads`.

`volts` and `amps` are required and must be above zero. A missing one, or a
key the tool does not know (`voltage`, `current`), is an error. It is never
read as zero.

### The model

- **Area copper** (pours, split and solid planes, pads) sits on a square
  grid. Neighbouring cells are joined by the sheet conductance σt times the
  harmonic mean of their copper fill. This is the five-point finite-volume
  form of ∇·(σt∇V) = 0 and is second-order accurate in the cell size.
  Plane layers are negative: antipads are cut round the holes of other nets.
  A polygon's outline is not copper: only its poured shapes are.
- **Tracks and arcs** are exact resistors, R = ρL/(wt). An arc is a bent
  strip, (r/w)·ln((r + w/2)/(r − w/2)) squares per radian, which matters
  when the radius is close to the width. A track is cut at the edge of a
  pour of its own net and at the edge of a pad, found to a fraction of a
  cell, so no copper is counted twice.
- **Connectivity follows the copper**, as Altium's does. Tracks that cross
  are joined where they cross, a via on an unbroken track taps it, ends
  whose copper overlaps are joined, and ends that are apart by more than
  their width are not.
- **Via barrels and plated through-hole pads** join the layers with
  R = ρ·Δz / (π((r+t)² − r²)), using the real dielectric heights and a
  plating thickness you can set (0.79 mil, the IPC-6012 class 2 minimum, by
  default).
- **Pads** are equipotential, because a soldered lead is far thicker than
  the foil. A pad meets the area copper at its real edge, not at the
  nearest cell centre (Shortley-Weller links), so the result does not move
  with the grid.
- **A barrel meets a plane at its hole wall**, which is much narrower than
  its land. A contact smaller than a few cells is given the resistance of
  the last cell analytically, (Rs/2π)·ln(r_grid/a), with r_grid taken from
  the lattice Green's function of the grid (a generalised Peaceman well
  model). A through-hole pin of a load is one conductor on every layer.
- **Copper resistivity** is 1.7241·10⁻⁸ Ω·m at 20 °C with 0.393 %/K, at the
  `temp_c` you give.
- The network is solved by a sparse direct solve, not by iteration, so
  Kirchhoff's laws hold to machine precision. The tests check that the
  Joule loss equals the power the loads lose.

`cell_mils=0` picks a cell of a quarter of the net's smallest pad, within a
budget of 800,000 nodes of the rail's own copper and the grid size the
board allows. When either limit makes the cell coarser than that, a note
says so. The tool then solves again on a grid twice as coarse and reports
`convergence.estimated_error_mv`, the Richardson estimate of what the grid
still costs. It is an estimate of the grid error only, not of the
assumptions below. Pass a smaller `cell_mils` when it is larger than you can
accept.

### What comes back

- `sinks`: for every load the worst pin, the drop in mV and percent, the pin
  count and the `limit` worst pins. Every pin is in the pins CSV. `rails`
  adds the return rise.
- `layers`: the peak current density (A/mm²) on each layer with its place,
  the voltage span, and the loss in watts.
- `tracks` and `vias`: the heaviest by current, each with its IPC-2221
  current capacity for `temp_rise_c` and an `over_capacity` flag. IPC-2221
  is conservative, so treat the flag as "look here".
- `files`: a voltage map and a current-density map per layer (PNG), the
  voltage of every sink pin (CSV), and an `.npz` with the arrays, including
  `W_<layer>`, the Joule heat per cell.
- `worst_drop_pct`, and `pass` when you give `max_drop_pct`. If the files
  cannot be written the numbers still come back, with `files_error`.

### What it assumes

Thermal reliefs are taken as fully connected. Copper is at its nominal
thickness, with no etch-back or plating build-up. The copper is at one
temperature. The result lists these each time.

### Accuracy, as tested

| Case | Reference | Error |
|---|---|---|
| Straight track | ρL/(wt) | under 0.3 % |
| Pour strip between two bars | squares × sheet resistance | under 1 % |
| Two discs on a large sheet | (Rs/π)·acosh(D/2a) | under 5 % at a 20 mil cell |
| Round pad on a pour, cells from 2.5 to 20 mil | same formula | the same answer within 1 % on every grid |
| Through-hole pin on a plane, holes 8 to 60 mil | spreading resistance from the hole wall | under 1 % |
| Tight arc between two pads | bent-strip formula | under 1 % |
| Track, via, track | tracks + barrel formula | under 2 % |
| Parallel tracks, T junction | current divider | under 1 % |
| 5 A through a 4-layer board | energy balance | 0.01 % |

## AC impedance: `sim_pdn_impedance`

```text
sim_pdn_impedance(
    net="+1V0", ground="GND", load={"ref": "U7"},
    volts=1.0, ripple_pct=3, step_amps=2.0,
    expect_file=r"D:\proj\board.PcbDoc")
```

The **target impedance** is Z = V·ripple / I_step, or pass `target_mohm`.
The tool reports every band where |Z| at the load is above it.

### The model

- **The plane pair.** The tool finds the power and ground layers that face
  each other over the largest area and solves them as a two-dimensional
  cavity on their real shape, by finite differences in the frequency domain.
  Each cell holds its plate capacitance; each link holds the series
  impedance per square, jωμ₀d + Zs(top) + Zs(bottom). Plane resonances,
  spreading inductance and the position of every capacitor are therefore in
  the curve. When the copper falls into separate islands, the island under
  the load is the one solved.
- **Perforated planes.** Under a BGA the antipads take copper away. A cell's
  capacitance follows the copper both planes really have there, and its
  links follow the Maxwell-Garnett rule for round holes, (1 − v)/(1 + v)
  for a void fraction v. A slot that cuts a cell through is found on a finer
  raster and opens the link. The inductance of a via field therefore rises
  as it should.
- **Copper loss** uses the exact surface impedance of a slab,
  Zs = (γ/σ)·coth(γt), which is the DC sheet resistance at low frequency and
  the skin-effect value at high frequency.
- **The laminate** follows the Djordjevic-Sarkar wideband Debye model, fitted
  to the stackup's εr and a loss tangent at 1 GHz. It is causal: εr falls
  slowly with frequency as the loss requires.
- **Capacitors** are found on the board: parts with pads on both nets whose
  designator is a capacitor's (`C1`, `CB3`, not `CN1` or `CR2`) or whose
  comment reads as a capacitance. A part number such as `1N4148` is not a
  value. Each is a series R-L-C. The package and the kind come from the
  footprint name and the comment; see the defaults below.
- **Mounting inductance** is worked out for each pin from the copper that
  really joins it to the planes, never from the nearest via. The tool
  follows the pin's own tracks to its vias, takes a via in the pad or a
  through-hole pin as they are, and goes through a pour when that is what
  joins them. Each via adds half the loop of a via pair down to the plane
  pair, (μ₀h/2π)·acosh(s/2r); each fan-out adds μ₀·h·l/(w + 2h) from the
  pad's edge; a via that goes on to the far plane adds its inductance
  inside the cavity. The legs of one net are in parallel and the two nets
  are in series. The fan-out and barrel resistance is added to the ESR. A
  blind via that stops short of the plane is not counted. When no copper
  path is found, 1 nH is assumed and a note names the parts.
- **The load** is treated the same way: every power pin has its own way
  down, and the current is shared among the pins. A load that is not over
  the plane pair is joined to its edge by a track of its distance, with a
  note, instead of being placed on the edge silently.
- **The regulator** is R + jωL. By default R is half the target and L gives
  a 50 kHz loop. Pass `vrm={"r_mohm": .., "l_nh": .., "ref": "U1"}` for the
  real one.
- With no plane pair, or with `mode="lumped"`, everything is in parallel at
  one node and the result says so.

### Capacitor defaults

| Kind | Recognised by | ESR | ESL |
|---|---|---|---|
| MLCC (default) | chip case in the footprint name (`0402`, `CAPC1005`) | fitted to makers' class II curves on log axes: 30 mΩ at 100 nF, 12 mΩ at 1 µF, 5 mΩ at 10 µF | by case: 0.35 nH for 0402, 0.5 nH for 0603, 0.6 nH for 0805 |
| Polymer | `POLY`, `POSCAP`, `SP-Cap`, `OS-CON` | 12 to 50 mΩ by value | 2 nH |
| Tantalum | `CAPMP`, `TANT`, `EIA-7343`, `CASE-D`, `TAJ` | 0.2 to 1.5 Ω by value | 2 nH |
| Aluminium electrolytic | `CAPAE`, `CAPPR`, `RB5-10.5`, `CP_Elec`, or a through-hole part of 10 µF or more, or 47 µF or more in no known chip case | 0.05 to 1 Ω by value | 5 nH, 10 nH through-hole |

The MLCC fit is good to about a factor of two. Bulk parts vary several times
between series, so each one on defaults carries a note asking for the data
sheet's numbers. Say the kind yourself with `kind` when the footprint name
does not: `{"ref": "C20", "kind": "polymer"}`.

A part with more than three pads is taken when all its pads are on the rail
and its ground (a low-inductance part with doubled terminals). A capacitor
array with other nets on it is not counted, and a note names it; pass its
`ref` in `decaps` to count its share. A `ref` given in `decaps` is always
counted, whatever it is called.

### Giving it real data

Defaults use the nominal capacitance. An X5R or X7R capacitor in a small case
loses 30 to 80 % of it at its working voltage, so for a sign-off number pass
the derated values from the maker's tool:

```text
decaps=[{"ref": "C12", "c_uf": 5.9, "esr_ohms": 0.004, "esl_nh": 0.45},
        {"ref": "C40", "touchstone": r"D:\models\GRM155.s2p"},
        {"ref": "NEW", "c_uf": 1.0, "count": 4, "x": 3100, "y": 1650}]
```

A Touchstone file of the part (shunt-through by default, `config="series"`
for a series fixture) replaces the R-L-C. An entry whose `ref` is not on the
board adds capacitors for a what-if. `exclude` removes parts. A key the tool
does not know is named in a note, and a wrong or negative number is an
error.

### What comes back

- `pass`, `worst`, and `violations`: each band over the target with its peak.
- `anti_resonances`: the peaks between capacitor groups and the plane.
- `suggestions`: for each failing band, the capacitance it needs below it
  and the inductance it needs across it, as a count of mounted capacitors.
- `decaps`: every capacitor with its kind, model, mounted resonance and how
  it is mounted. Above 30 capacitors the reply lists them by group
  (`decap_groups`) and the CSV has every one.
- `cavity`: the plane pair, its capacitance, its extent and its first
  resonance, found as the first mode of the real shape.
- `load`: its pins, how they are mounted, and how far it is from the plane
  pair when it is not over it.
- `files`: the curve as CSV, the capacitor table as CSV, a plot, and a
  lumped SPICE deck (`*_pdn_lumped.cir`) that runs in LTspice or ngspice,
  where V(load) reads in ohms. Use `eng_ltspice_run` on it to add your
  regulator's own model. The plot needs matplotlib
  (`pip install eda-agent[render]`); without it a note says so and the CSV
  has the curve. If the files cannot be written the numbers still come
  back, with `files_error`.

### Accuracy, as tested

| Case | Reference | Error |
|---|---|---|
| Transfer impedance of a rectangular plane pair, 1 MHz to 1.5 GHz | double cosine series (Okoshi; Lei et al.) | median 0.0001 %, 90 % of points under 0.5 % |
| First plane resonances | c / (2L√εr) | under 0.3 % |
| Low-frequency limit | plate capacitance | 0.2 % |
| Grid independence of a via port | μ₀d/(2π)·ln 3 for a 3× finer grid | 0.1 % |
| Resonance damping | Q = d/δ with dielectric loss | within 25 % |
| Two-capacitor anti-resonance | 1 / (2π√(L·C_series)) | under 1 % |
| N dog-bone vias in parallel | 1/N of one, by hand | under 2 % |
| First mode of a rectangular pair | c / (2L√εr), from the eigen-solve | under 1 % |

### What it does not do

- The die and the package are not modelled. Above a few hundred MHz they
  decide the supply noise, so read the top of the curve as the board's
  share.
- A rail routed as tracks only has no cavity; it is solved lumped, without
  the tracks' inductance.
- One plane pair is solved. A rail on several plane pairs is modelled on
  the largest one.

## The Joule heat map

`eda_agent.pdn.ir_drop.power_density(result)` gives the Joule heat per grid
cell and layer in watts, on the same grid `layout.copper_raster` uses. The
`.npz` file holds the same arrays as `W_<layer>`, with the grid's origin and
cell size, for a thermal solve that wants the copper's own heating as a
source.
