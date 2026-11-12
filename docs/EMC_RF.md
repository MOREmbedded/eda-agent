# EMI/EMC pre-scan and RF field solving

Two layers of help with emissions and RF, used together:

1. **The EMI pre-scan** (`sim_emc_prescan`) judges a routed board in seconds
   with no external program. It finds the usual emitters while they are
   still a layout change.
2. **openEMS** (`sim_em_build_model`, `sim_em_run`, `sim_em_results`)
   solves Maxwell's equations on a few nets cut out of the board: S-parameters,
   return loss, insertion loss, and for an antenna its resonance,
   directivity, efficiency and pattern. The result comes back as a
   Touchstone file plus a summary, and as plots when matplotlib is
   installed.

`sim_read_touchstone` summarises any `.sNp` file, so you can also check a
vendor's antenna or filter data.

## The pre-scan

| Check | What it finds |
|---|---|
| `reference_gaps` | A fast track that crosses a split or slot in its reference plane, or one with no plane on an adjacent layer. The return current detours round the gap and draws a large loop. |
| `edge_proximity` | A fast track nearer the board edge or a cutout than `edge_mils` (100 mil by default). |
| `return_vias` | A fast net's via with no return via within `return_via_mils` (40 mil by default). This is the same check `pcb_layout_audit` runs. |
| `emission` | A differential-mode radiated-emission estimate for each net whose drive you give: frequency, rise time, swing, duty cycle and impedance. It is judged against a limit line. |

**Fast nets** are the differential pairs, the high-speed net classes, and
nets named like clocks, fast buses or switching nodes (`CLK`, `SCK`, `USB`,
`RGMII`, `DDR`, `DQ`, `SW`, `LX` and so on). The ground and plane nets are
never fast nets. You can pass `nets` to choose them yourself.

**The emission estimate** follows Ott, *Electromagnetic Compatibility
Engineering*, eq. 12-3, a small loop over a reflecting ground plane. The
loop area is the track length times its height to the reference plane, from
the real stackup. The current is the trapezoid wave's harmonic at each
multiple of the clock. Limit lines: `cispr32_b_3m` (default),
`cispr32_b_10m`, `cispr32_a_3m`, `cispr32_a_10m`, `fcc15_b_3m` and
`fcc15_a_10m`. Use it to rank nets, not to predict a chamber scan. Common-mode
current on cables usually dominates a real scan, and board geometry alone
cannot give it. When a net has no plane beside it, its loop is marked as a
lower bound.

Example:

```
sim_emc_prescan(expect_file="C:\\work\\board.PcbDoc",
                drives={"MCU_CLK": {"freq_mhz": 25, "rise_ns": 1.5, "volts": 3.3},
                        "ETH_RXCLK": {"freq_mhz": 125, "rise_ns": 0.7}})
```

A live read takes the whole board, so on a very large board, ask before
reading it. A saved LayoutBoard (`board_json_path`) works offline. A saved
`pcb_get_layer_stackup` reply (`stackup_json_path`) supplies the stackup
when you have no live session. Without either, a 1.6 mm FR-4 stack is
assumed and the result says so.

## openEMS

[openEMS](https://www.openems.de/) is a free, open-source FDTD field solver
(GPL-3). eda-agent never links to it. It writes a model file and runs
openEMS's own Python as a separate process, then reads the files that run
leaves behind.

### Pipeline

1. **Build.** `sim_em_build_model(nets=[...], ports=["J1.1", "U3.7"])`
   cuts a box round the nets (`margin_mils`, or `area_mils` for an exact
   box). Inside the box it takes:
   - the nets' copper;
   - the return copper (planes, pours, ground tracks, pads and vias);
   - the real dielectric stack.

   It puts a lumped port on each named pad, from the pad to the nearest
   return copper. It writes `model.json` and the runner into a new run
   folder. It also estimates the mesh size, and refuses a model above
   `max_cells_m` (20 M cells by default) with what to change.
2. **Run.** `sim_em_run(folder)` starts the solve and returns at once. Each
   port is excited in turn, so N ports take N solves.
3. **Results.** `sim_em_results(folder, wait_s=60)` reports progress. When
   the run is done, it writes `<run>.sNp` and returns:
   - return loss per port, and the S11 dip with Zin there;
   - S21;
   - for `mode="antenna"`: directivity, efficiency and two pattern cuts;
   - `sparams.png` and `pattern.png`, when matplotlib (the `render` extra)
     is installed.

`mode="antenna"` adds a quarter wavelength of air round the board, PML
boundaries and a near-to-far-field box. It also includes copper with no net,
which is how a PCB antenna is usually drawn.

### Modelling choices

- Copper is a zero-thickness perfect conductor at its layer's interface.
  Dielectric loss is included, from the loss tangent (0.02 assumed, since
  Altium's stackup does not carry it); conductor loss is not.
- The mesh is 20 cells per wavelength at `f_max` in the densest dielectric,
  graded at a ratio of 1.4, with lines on the signal copper's edges and the
  ports.
- Each port is a 50 ohm lumped port on a vertical line from the pad's
  centre to the return copper under it, with mesh lines through that point.
- A run is judged by its port signals. When every port's voltage has died
  down 40 dB from its peak, the result is marked `settled`. An open board
  model often leaves a little energy riding the absorbing boundary long
  after that, so the solver's own energy criterion is not the test. The
  step cap is set from the band and the box size. An unsettled run returns
  a warning with its S-parameters.
- Nets other than the chosen ones and the return nets are left out. If a
  neighbour couples strongly (a diff-pair partner, a guard trace), name it
  in `nets`.
- Antipads in a plane layer that has no split-plane objects are not cut.
  Pour and split-plane holes are cut.

### Installing openEMS (Windows)

Installing is your decision; eda-agent never installs it. Download the
Windows build from the
[openEMS-Project releases](https://github.com/thliebig/openEMS-Project/releases),
unzip it to a drive with room, and set:

| Variable | Value |
|---|---|
| `EDA_AGENT_OPENEMS_DIR` | the unzipped openEMS folder |
| `EDA_AGENT_OPENEMS_PYTHON` | a Python that can `import openEMS` (the version the build's bindings were made for) |
| `EDA_AGENT_EM_RUNS` | where run folders go. Runs can be gigabytes, so use a drive with room. |

`sim_em_run` checks that the Python can import openEMS before it starts
anything, and says what is missing when it cannot.

## Tool choice and licences

| Tool | Use | Licence | How eda-agent uses it |
|---|---|---|---|
| Built-in pre-scan | Rule checks, emission estimate | Apache-2.0 (this repository) | In process |
| openEMS | 3D FDTD: S-parameters, radiation, antennas | GPL-3 | Separate process, file exchange |
| scikit-rf | Touchstone, de-embedding, matching | BSD-3 | Optional; the built-in reader covers Touchstone 1.x |
| NEC2 (nec2c) | Wire antennas (whips, dipoles, loops) | Public domain (NEC-2), GPL-2 (nec2c) | Planned, separate process |
| Qucs-S | RF circuit simulation with S-parameter blocks | GPL-2 | Planned, separate process |
| Elmer FEM | Low-frequency fields, thermal | GPL-2 / LGPL | Considered |

Meep and gprMax were considered and left out. Both are general FDTD
codes, and neither handles a PCB stackup as directly as openEMS does.
