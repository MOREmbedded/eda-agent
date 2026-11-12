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
stackup. Result files go to `EDA_AGENT_PDN_RUNS`, or to
`~/EDA Agent/pdn` when it is not set.

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
each load sees is the source voltage less both drops.

### The model

- **Area copper** (pours, split and solid planes, pads) sits on a square
  grid. Neighbouring cells are joined by the sheet conductance σt times the
  harmonic mean of their copper fill. This is the five-point finite-volume
  form of ∇·(σt∇V) = 0 and is second-order accurate in the cell size.
  Plane layers are negative: antipads are cut round the holes of other nets.
- **Tracks and arcs** are exact resistors, R = ρL/(wt). A track is cut
  where it enters a pour of its own net, so no copper is counted twice. T
  junctions split the trunk.
- **Via barrels and plated through-hole pads** join the layers with
  R = ρ·Δz / (π((r+t)² − r²)), using the real dielectric heights and a
  plating thickness you can set (0.79 mil, the IPC-6012 class 2 minimum, by
  default).
- **Pads** are equipotential, because a soldered lead is far thicker than
  the foil.
- **Copper resistivity** is 1.7241·10⁻⁸ Ω·m at 20 °C with 0.393 %/K, at the
  `temp_c` you give.
- The network is solved by a sparse direct solve, not by iteration, so
  Kirchhoff's laws hold to machine precision. The tests check that the
  Joule loss equals the power the loads lose.

`cell_mils=0` picks a cell of a quarter of the net's smallest pad, within a
budget of 800,000 nodes. The tool then solves again on a grid twice as
coarse and reports `convergence.estimated_error_mv`, the Richardson estimate
of what the grid still costs. Pass a smaller `cell_mils` when that is larger
than you can accept.

### What comes back

- `sinks`: the voltage at every pin of every load, the worst pin and the
  drop in mV and percent. `rails` adds the return rise.
- `layers`: the peak current density (A/mm²) on each layer with its place,
  the voltage span, and the loss in watts.
- `tracks` and `vias`: the heaviest by current, each with its IPC-2221
  current capacity for `temp_rise_c` and an `over_capacity` flag. IPC-2221
  is conservative, so treat the flag as "look here".
- `files`: a voltage map and a current-density map per layer (PNG), and an
  `.npz` with the arrays, including `W_<layer>`, the Joule heat per cell.
  The thermal solver takes that heat map as a source.
- `pass`, when you give `max_drop_pct`.

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
  the curve.
- **Copper loss** uses the exact surface impedance of a slab,
  Zs = (γ/σ)·coth(γt), which is the DC sheet resistance at low frequency and
  the skin-effect value at high frequency.
- **The laminate** follows the Djordjevic-Sarkar wideband Debye model, fitted
  to the stackup's εr and a loss tangent at 1 GHz. It is causal: εr falls
  slowly with frequency as the loss requires.
- **Capacitors** are found on the board: parts with pads on both nets whose
  comment reads as a capacitance. Each is a series R-L-C. The package comes
  from the footprint name, and ESR and ESL default to typical values for the
  value and package.
- **Mounting inductance** is worked out for each capacitor from its own
  copper: the loop of its power and ground vias down to the plane pair,
  (μ₀h/π)·acosh(s/2r); its fan-out traces over their reference plane; and
  the via's own inductance inside the cavity. The result lists the three
  parts per capacitor.
- **The regulator** is R + jωL. By default R is half the target and L gives
  a 50 kHz loop. Pass `vrm={"r_mohm": .., "l_nh": .., "ref": "U1"}` for the
  real one.
- With no plane pair, or with `mode="lumped"`, everything is in parallel at
  one node and the result says so.

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
board adds capacitors for a what-if. `exclude` removes parts.

### What comes back

- `pass`, `worst`, and `violations`: each band over the target with its peak.
- `anti_resonances`: the peaks between capacitor groups and the plane.
- `suggestions`: for each failing band, the capacitance it needs below it
  and the inductance it needs across it, as a count of mounted capacitors.
- `decaps`: every capacitor with its model, mounted resonance and mounting
  inductance.
- `cavity`: the plane pair, its capacitance and its first resonance.
- `files`: the curve as CSV, a plot, and a lumped SPICE deck
  (`*_pdn_lumped.cir`) that runs in LTspice or ngspice, where V(load) reads
  in ohms. Use `eng_ltspice_run` on it to add your regulator's own model.

### Accuracy, as tested

| Case | Reference | Error |
|---|---|---|
| Transfer impedance of a rectangular plane pair, 1 MHz to 1.5 GHz | double cosine series (Okoshi; Lei et al.) | median 0.0001 %, 90 % of points under 0.5 % |
| First plane resonances | c / (2L√εr) | under 0.3 % |
| Low-frequency limit | plate capacitance | 0.2 % |
| Grid independence of a via port | μ₀d/(2π)·ln 3 for a 3× finer grid | 0.1 % |
| Resonance damping | Q = d/δ with dielectric loss | within 25 % |
| Two-capacitor anti-resonance | 1 / (2π√(L·C_series)) | under 1 % |

### What it does not do

- The die and the package are not modelled. Above a few hundred MHz they
  decide the supply noise, so read the top of the curve as the board's
  share.
- A rail routed as tracks only has no cavity; it is solved lumped, without
  the tracks' inductance.
- One plane pair is solved. A rail on several plane pairs is modelled on
  the largest one.

## Feeding the thermal solver

`eda_agent.pdn.ir_drop.power_density(result)` gives the Joule heat per grid
cell and layer in watts, on the same grid `layout.copper_raster` uses. The
`.npz` file holds the same arrays as `W_<layer>`.
