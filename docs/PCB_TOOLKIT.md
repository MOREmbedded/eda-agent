# PCB toolkit: calculators with a field solver behind them

The **Calculators** tab of the dashboard (key `0`) and the `pcb_calc`
tool family carry 44 calculators for the questions a board
designer asks every day: what impedance is this trace, how wide for 100
ohm differential, how hot does this trace get at 5 A, what does this via
cost, how much crosstalk, where do these planes resonate.

They cover the ground of the well-known desktop PCB toolkits. Everything
here is written from published physics and standards; no code, data
tables or artwork come from any other tool.

Two things set them apart from a page of formulas:

- **Impedance, differential pairs and crosstalk are solved, not looked
  up.** A 2D field solver works on the real cross-section: trapezoidal
  traces, solder mask, a prepreg with a different Er above the trace,
  offset stripline, coplanar grounds of finite width. The published
  closed form is shown beside the result as a cross-check.
- **Trace and via current come from a thermal model of your board**: its
  thickness, laminate, copper planes and their distance, air flow and
  orientation. That is the physics IPC-2152 measured and IPC-2221's
  1950s chart left out.

Every result names its model, its status (`exact`, `solver`,
`published` or `estimate`), its expected accuracy and its sources.

## Using it

**Dashboard.** Open the dashboard, press `0` or click *Calculators*.
Pick a calculator on the left, type values (each length takes mil, mm,
um, in or oz), and the result updates as you type, with a drawing of the
solved cross-section. *Fill from the board* reads the layer stack of the
board shown and fills copper thickness, dielectric height and Er for
the layer you pick; filled fields are marked green.

**AI / MCP.** The same calculators, by name:

```text
pcb_calc kind=toolkit                           # every calculator, by group
pcb_calc kind=toolkit args={"calculator": "edge_coupled_stripline"}
                                                # that calculator's inputs, defaults and units
pcb_calc kind=toolkit args={"calculator": "edge_coupled_stripline",
                            "inputs": {"w": "4 mil", "s": "5 mil",
                                       "h1": "5 mil", "h2": "5 mil",
                                       "er": 3.9, "frequency": "5 GHz"}}
                                                # the result ("inputs": {} runs the defaults)
pcb_calc kind=toolkit_board                     # the open board's stack as inputs
```

A value is a number in the input's default unit, a string with a unit
(`"0.2 mm"`, `"2.4GHz"`, `"1 oz"`), or `{"value": 5, "unit": "mil"}`.
Units are case-tolerant except where case changes the meaning: `MA` is
refused rather than read as `mA`.

**Python.**

```python
from eda_agent.engineering import toolkit
r = toolkit.run("microstrip", {"w": "6 mil", "h": "4 mil", "er": 4.2})
r["outputs"][0]["display"]      # '53.85 Ω'
```

## The field solver

`engineering/toolkit/fdsolver.py`. Finite differences on a graded
tensor grid, fine at every conductor edge and dielectric interface. It
solves Laplace's equation once per conductor with the dielectrics and
once in vacuum, giving the capacitance matrices C and C0, then

- L = mu0 eps0 C0^-1, Z0 = 1 / (c sqrt(C C0)), Er_eff = C / C0;
- odd, even, differential and common-mode impedance from the 2 x 2
  matrices (for unequal lines, from the full characteristic impedance
  matrix);
- dielectric loss from each region's loss tangent;
- conductor loss from the surface current on every metal face, return
  planes included, so current crowding at trace edges is in the answer;
- near- and far-end crosstalk coefficients from L and C.

Each structure is solved on two grids, the second twice as fine
everywhere, and extrapolated to zero cell size with the convergence
order of the edge singularity (h at a knife edge, h^(4/3) at a square
corner, h^(1/3) for the loss integral). A solve takes 0.1 to 0.5 s.

Around the solver:

- **Er(f) and tan d(f)**: the causal wideband Debye model of
  Djordjevic and Sarkar carries the laminate's data-sheet point to the
  analysis frequency.
- **Microstrip dispersion**: Kirschning and Jansen.
- **Copper roughness**: Hammerstad, Groisse or Huray (snowball).

### Measured accuracy

These are the checks in `tests/toolkit/`, against results that do not
depend on the solver.

| Quantity | Reference | Agreement |
|---|---|---|
| Stripline Z0, zero thickness | Cohn 1954, exact conformal map | within 0.1 % |
| Edge-coupled stripline, odd and even mode | Cohn 1955, exact | within 0.15 % |
| Coplanar waveguide in air | exact conformal map | within 0.2 % |
| Microstrip Z0 and Er_eff, w/h 0.2 to 5, Er 2.2 to 9.8 | Hammerstad-Jensen 1980 (its authors: 0.2 %) | within 0.4 % |
| Stripline Z0, finite thickness | Wheeler 1978 (0.5 %) | within 0.6 % |
| Coplanar waveguide on a substrate | Veyres-Fouad Hanna formula (1 to 2 %) | within 1.5 % |
| Dielectric loss, homogeneous line | pi f sqrt(Er) tan d / c, exact | within 0.2 % |
| Conductor loss, stripline, smooth copper | Wheeler incremental-inductance rule (Pozar eq. 3.181) | within 3 % |

What the solver does not do: it is quasi-static (valid while the
cross-section is small against a wavelength, which holds for PCB traces
to tens of GHz), it treats copper loss as a surface effect (below about
10 MHz on 1 oz copper the current fills the trace and the DC resistance
applies instead), and it knows nothing about glass-weave skew or how
your fab's resin flows. Real boards also carry a +/-10 % impedance
tolerance from etching and pressing, which no calculation removes: for
a controlled-impedance order, your fab's stack-up report rules.

## The trace thermal model

`engineering/toolkit/thermal_fv.py`. A finite-volume solve of the
board's cross-section through the trace:

- Joule heat I^2 rho(T) / A, with copper's resistivity following its own
  temperature;
- conduction through an anisotropic laminate (FR-4: 0.81 W/mK in plane,
  0.29 through), through copper planes at their depth, and through
  solder mask;
- natural or forced convection from both faces (Lloyd-Moran,
  Raithby-Hollands, Churchill-Chu) and radiation, each at the surface
  temperature it produces, iterated to convergence.

Checks: a full-width heater matches the one-dimensional heat balance by
hand within 2 %; the grid changes the answer by less than 1 %; the
inverse solves (current for a rise, width for a current) round-trip
within 1 %; and a close plane cuts the rise by more than 40 %, as
IPC-2152 found.

How to read it against the charts: IPC-2152's main chart is a
conservative envelope (polyimide board, no planes, still air), and
IPC-2221's is older still. Each result shows the IPC-2221 current for
the same rise so the difference is visible. The model's own
uncertainty is the board around the trace: other heat sources, copper
pours, enclosure and air path are not in a cross-section; those need
a board-level thermal analysis.

## Calculators

### Conductor

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Trace current and temperature rise | `trace_current` | Current, width or temperature rise of a trace, solved for your board's thickness, planes, laminate and air (the physics IPC-2152 measured). | 2D thermal cross-section of the board (finite volume), rho(T), convection + radiation | solver |
| Conductor resistance, skin effect, voltage drop | `conductor_properties` | DC and AC resistance of a trace at a temperature and frequency. | R = rho(T) L / A; skin effect from the bar's surface | exact |
| Fusing current (Onderdonk) | `fusing_current` | The current that melts a trace in a given time. A destruction limit, not a rating. | Onderdonk's equation (adiabatic) | published |
| Minimum conductor spacing (IPC-2221) | `conductor_spacing` | Electrical clearance for a peak voltage between two conductors. | IPC-2221 Table 6-1 (A/B revision classes) | published |

### Impedance

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Microstrip | `microstrip` | Trace on an outer layer over one reference plane, with optional solder mask. | Field solver + Kirschning-Jansen dispersion | solver |
| Embedded microstrip | `embedded_microstrip` | Outer-layer style trace buried under more dielectric (coated, or under a prepreg with no plane above). | Field solver | solver |
| Stripline (symmetric or offset) | `stripline` | Inner-layer trace between two planes. Unequal heights give the offset (asymmetric) case. | Field solver | solver |
| Dual stripline | `dual_stripline` | Two signal layers between two planes; the trace is on one of them. | Dual stripline as offset stripline: Field solver | solver |
| Coplanar waveguide | `coplanar` | Trace with ground copper either side on the same layer, with or without a plane below. | Field solver | solver |
| Effective dielectric constant | `er_effective` | Er_eff of a microstrip at a frequency, Hammerstad-Jensen with Kirschning-Jansen dispersion. | Hammerstad-Jensen 1980, Kirschning-Jansen 1982 | published |
| Laminate Er(f) and tan d(f) | `laminate_er` | Causal frequency dependence of a laminate from one data-sheet point (Djordjevic-Sarkar). | Wideband Debye (Djordjevic-Sarkar 2001) | published |
| Width for a target impedance | `width_for_impedance` | Trace width that gives a target Z0 or Zdiff, for any structure, solved with the field solver. | Field solver, Microstrip, bisection on width | solver |

### Differential pairs

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Edge-coupled microstrip | `edge_coupled_microstrip` | Differential pair on an outer layer over a plane, with optional solder mask. | Field solver, edge-coupled microstrip (quasi-static) | solver |
| Edge-coupled embedded microstrip | `edge_coupled_embedded` | Differential pair buried under a dielectric with no plane above. | Field solver, embedded edge-coupled microstrip | solver |
| Edge-coupled stripline | `edge_coupled_stripline` | Differential pair on an inner layer between two planes, centred or offset. | Field solver, edge-coupled stripline | solver |
| Broadside-coupled stripline | `broadside_coupled_stripline` | Pair stacked on two adjacent layers, with planes above and below (shielded) or without. | Field solver, broadside-coupled shielded stripline | solver |
| Coplanar differential pair | `coplanar_diff` | Differential pair with ground copper either side, with or without a plane below. | Field solver, coplanar differential pair | solver |

### Via

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Via current and properties | `via_properties` | Barrel resistance, current for a temperature rise, thermal resistance, inductance, capacitance, impedance and resonance of a plated via. | Barrel as an unrolled conductor on the board (IPC-2152 equivalence), parasitics from Johnson & Graham | solver |
| Differential via pair | `via_pair` | Differential and single impedance of a via pair through a plane stack (two-wire and coaxial models). | Two-wire line (exact for parallel cylinders in a uniform dielectric) | estimate |

### Planes and PDN

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Plane pair: capacitance, resonances, impedance | `plane_pair` | Capacitance of a power/ground plane pair, its cavity resonances, and the impedance seen at a port across frequency. | Parallel-plate capacitance; cavity modal sum (Lei et al. 1999) | exact |
| PDN target impedance | `pdn_target` | Target impedance of a supply from its voltage, ripple budget and transient current. | Z_target = V * ripple / I_step (Smith, Novak) | published |

### Crosstalk

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Crosstalk between two traces | `crosstalk` | Near- and far-end crosstalk between two parallel traces, from the field solver's L and C matrices. | Field-solver L/C matrices, weak-coupling crosstalk | solver |

### Signal and timing

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Wavelength | `wavelength` | Wavelength of a signal on a line with a given effective Er, and the usual fractions. | lambda = c / (f sqrt(Er_eff)) | exact |
| Bandwidth, rise time and critical length | `rise_time_bandwidth` | Signal bandwidth from rise time, and the trace length beyond which it needs to be treated as a transmission line. | BW = 0.35/tr (Gaussian edge); knee 0.5/tr; l_crit = frac*tr/tpd | published |
| PPM and frequency tolerance | `ppm` | Frequency error of a crystal or oscillator in ppm and Hz, both directions. | df = f0 * ppm / 1e6 | exact |

### Thermal

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Junction temperature | `junction_temperature` | Device junction temperature from power and thermal resistances, and the power allowed for a junction limit. | Tj = Ta + P * theta (series thermal resistances) | exact |

### Ohm's law and passives

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Ohm's law | `ohms_law` | Any two of voltage, current, resistance and power give the other two. | V = I R, P = V I | exact |
| Reactance and resonance | `reactance` | Reactance of a capacitor and an inductor at a frequency, and their resonance. | Xc = 1/(2 pi f C), Xl = 2 pi f L, f0 = 1/(2 pi sqrt(LC)) | exact |
| Series and parallel R, L, C | `series_parallel` | Combine resistors, inductors or capacitors in series and in parallel. | Series adds, parallel adds reciprocals (C the other way round) | exact |
| Voltage divider | `voltage_divider` | Output voltage, current and resistor power of a two-resistor divider, loaded or not. | Vout = Vin R2' / (R1 + R2'), R2' = R2 || Rload | exact |
| LED series resistor | `led_resistor` | Series resistor for an LED, its power, and the nearest E-series values. | R = (Vs - n Vf) / I | exact |
| Pi and T attenuator pads | `attenuator` | Resistor values for a matched Pi or T attenuator. | Matched symmetric pads, K = 10^(dB/20) | exact |
| Crystal load capacitors | `crystal_load` | Load capacitor values for a Pierce oscillator from the crystal's specified load. | C1 = C2 = 2 (CL - Cstray) | exact |
| RC / RL time constant and cutoff | `rc_time` | Time constant and -3 dB frequency of a first-order RC or RL network. | tau = RC or L/R, fc = 1/(2 pi tau) | exact |
| Planar spiral inductor | `planar_inductor` | Inductance of a square, hexagonal, octagonal or circular spiral on a PCB. | Mohan et al. 1999: current-sheet and modified Wheeler | published |

### Padstack and layout

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Padstack from drill | `padstack` | Pad diameters for a plated hole from its drill, the required annular ring and the fab's registration allowance (IPC-2221 / IPC-2222). | Pad = hole + 2 x annular ring + fab allowance (IPC-2221 9.1.1) | exact |
| BGA land and breakout | `bga_land` | Land size for a BGA ball (IPC-7351 reduction), and the widest trace that fits between two lands. | IPC-7351B BGA land reduction; breakout geometry | published |
| Traces between pads | `conductor_to_pad` | Widest trace that fits between two pads, or how many traces of a width fit. | Free gap = pitch - pad - (n + 1) spacing, shared by n traces | exact |
| Corner-to-corner (diagonal) | `corner_to_corner` | Diagonal of a rectangular or square pad, aperture or keep-out. | Pythagoras, rounded corners pulled in | exact |
| Embedded (planar) resistor | `embedded_resistor` | Resistance of a printed or buried thin-film resistor from its sheet resistance and shape. | R = Rs x squares x (1 + TCR dT) | exact |

### Conversions and reference

| Calculator | Name | What it gives | Model | Status |
|---|---|---|---|---|
| Wire gauge (AWG) | `wire_gauge` | Diameter, area and resistance of an AWG wire. | ASTM B258 diameter law; copper resistivity at T | exact |
| Drill sizes | `drill_chart` | Nearest standard metric and fractional-inch drills for a hole. | Metric drills in 0.05 mm steps; fractional inch in 1/64 | exact |
| Unit conversion | `unit_convert` | Convert a value between units of the same kind. | Exact unit definitions | exact |
| dB, dBm, volts and watts | `db_convert` | Convert between dBm, watts and RMS volts in a system impedance, and dB ratios. | P = 1 mW x 10^(dBm/10), V = sqrt(P Z) | exact |

## Limits worth knowing

- **Via inductance and capacitance** are Johnson and Graham's rules of
  thumb (+/-20 to 30 %). The barrel's resistance, thermal resistance and
  current are computed properly. For signal-integrity sign-off of a via,
  use a 3D solver (`sim_openems_*`, see [EMI/EMC and RF](EMC_RF.md)).
- **Conductor spacing** values are transcribed from secondary sources
  of IPC-2221 A/B, the larger value taken where sources disagree. Safety
  standards usually ask for more.
- **Fusing current** is Onderdonk's adiabatic equation: a destruction
  limit, trustworthy below about one second.
- **PDN target impedance** is the flat first-pass target. The plane
  pair's impedance is the exact rectangular-cavity sum, without
  decoupling capacitors.

## Not the same thing as pcb-toolkit

`engineering/pcb_toolkit.py` and `eda-agent calc-cross-check` compare
the older closed-form calculators with
[pcb-toolkit](https://github.com/akiselev/pcb-toolkit), an optional
Rust program you build yourself (see
[Fab rules and calculators](FAB_AND_CALCULATORS.md)). The toolkit on
this page is native Python, needs nothing installed, and carries its
own cross-checks in every result.

## Adding a calculator

One declaration in `engineering/toolkit/` puts a calculator on the
dashboard, in the MCP tool and under test:

```python
@calculator("my_calc", "My calculator", "signal", "One line on what it gives.",
            [Input("f", "Frequency", "frequency", 1.0, "GHz", min=0.0)])
def my_calc(a):
    r = Result(model="the formula or method", status="exact")
    r.add("x", "Result", 2 * a["f"], "frequency", primary=True)
    return r
```

Inputs arrive in SI. `tests/toolkit/test_calculators.py` runs every
registered calculator on its defaults and checks that it names its
model and accuracy.
