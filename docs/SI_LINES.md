# Transmission lines and the stackup planner

The first signal-integrity layer: a wideband model of a trace or of
coupled traces, built from the real stackup, and a planner that finds
the widths and pair gaps for target impedances on every layer. Both run
as [simulation jobs](SIMULATION.md) (`line-model`, `stackup-plan`), so
their answers are kept, shown on the dashboard's Simulation page and can
be set beside another engine's.

## What a line model gives

| Figure | Single trace | Pair |
|---|---|---|
| Impedance, lossless (the number fabs and Altium's Layer Stack Manager quote) | `z0_ohms` | `z_diff_ohms`, `z_common_ohms`, `z_odd_ohms`, `z_even_ohms` |
| Impedance with the copper's internal inductance and loss | `z0_lossy_ohms` | `z_diff_lossy_ohms` |
| Effective Er (C / C0) | `er_eff` | |
| Delay | `delay_ps_per_in` | odd and even mode |
| Loss per inch at the report frequency and at the top of the band | `loss_db_per_in` | differential |
| Crosstalk over the band, for the length given | | `next_db_max`, `fext_db_max` |
| Insertion loss of the length at the top frequency | `s21_db_at_f_max` | SDD21 |
| DC resistance with the return | `r_dc_ohm_per_m` | |

Files in the job's folder: the Touchstone file of the line (`.s2p` for
one trace, `.s4p` for a pair, ports 1..n at the near end and n+1..2n at
the far end), `rlgc.json` (R, L, G, C matrices per metre at every
frequency), `section.json` (the cross-section as solved) and a plot.

## The physics

| Part | Model |
|---|---|
| R(f), L(f) | A 2D eddy-current (magneto-quasistatic) solve over every conductor, the reference planes included as copper slabs of their real thickness. Skin effect, proximity effect between traces and the crowding of the return current under the trace come out of the field equation, from DC through the skin-effect transition. Above the frequency where the grid resolves the skin depth, the exact form of a good conductor's surface impedance (R = a sqrt f + b, internal reactance a sqrt f) is fitted to the last solved points. |
| C(f), G(f) | The 2D field solver of the [PCB toolkit](PCB_TOOLKIT.md) with complex permittivity: each dielectric (core, prepreg, solder mask) follows the causal Djordjevic-Sarkar model through its data-sheet Er and tan d, and the loss is weighted by the field that is really in each material. Solved on two grids and extrapolated to zero cell size. |
| Roughness | Huray snowball model, or Hammerstad-Bekkadal and Groisse from the RMS roughness, applied to the frequency-dependent part of the copper impedance. Foil presets `std`, `rtf`, `vlp`, `hvlp` carry typical published Huray fits; a fab's measured values are better. |
| Coupled lines | The exact modal solution of the telegrapher's equations (ZY = T g^2 T^-1), giving the 2n-port admittance and S-parameters of a uniform length at every frequency. Mixed-mode parameters for a pair. |

The high-frequency inductance from the eddy-current solve and from the
electrostatic solve (L = mu0 eps0 C0^-1) are two independent answers to
one question; every run reports how far apart they are (typically under
0.5 %) and fails its "solvers agree" check above 2 %.

## Accuracy, as tested

- DC resistance: exact (rho / area of the trace plus the plane's share).
- High-frequency inductance: the eddy-current and electrostatic solves
  agree within 0.5 %.
- High-frequency conductor loss: within 3 % of Wheeler's
  incremental-inductance rule computed independently from the
  electrostatic solution's surface current.
- Lossless impedance and Er_eff of a microstrip: within 0.2 % of
  scikit-rf's Hammerstad-Jensen model and of the toolkit's calculator.
- S-parameters: identical (1e-13) to scikit-rf's transmission line built
  from the same propagation constant and impedance; passive and
  reciprocal by construction.

## Stackup planner

For every signal layer of the board's stackup (or a saved
`pcb_get_layer_stackup` reply) the planner builds the real cross-section
and solves the field for:

- the width that gives the single-ended target (default 50 ohm);
- for each differential target (default 100 ohm), the width with either
  a fixed gap (`diff_gap_mils`), a fixed gap-to-width ratio
  (`diff_gap_ratio`), or else the tightest ratio from 1 to 4 that
  reaches the target.

Outer layers are microstrip over the next copper, with solder mask
(0.6 mil, Er 3.5 by default); inner layers are stripline between the
nearest copper above and below. When an adjacent layer is a signal layer
it is taken as the reference and the result says so. A target the
layer cannot reach within the width limits is reported with the range it
can reach.

With `compare_altium` the board's impedance profiles are read and each
profile's width is solved here too: our impedance at Altium's width, the
difference in percent, and our width for its target. With `apply` the
plan is written as impedance profiles (one per target, widths and gaps
locked); `altium_steps` lists those calls without making them.

## Tools

| Tool | Does |
|---|---|
| `si_line_model` | The wideband model of a trace or pair, from its numbers or from a stackup layer |
| `si_stackup_plan` | Widths and pair geometry for targets on every layer; compare with or write Altium's impedance profiles |

Both are simulation jobs: `sim_job_run(kind="line-model", ...)` does the
same as `si_line_model`, and adding `"scikit-rf"` to the engines gives a
closed-form second opinion on a single microstrip without mask.

Example:

```json
si_line_model(spec={"structure": "stripline", "traces": 2, "width_mils": 4,
                    "gap_mils": 6, "h_below_mils": 5, "h_above_mils": 5,
                    "copper_mils": 0.7, "laminate": "low-loss", "foil": "vlp",
                    "length_mm": 150, "f_max_ghz": 28})
```

## The dashboard

The Simulation page has a **Transmission line** form (structure, single
or pair, widths, heights, Er, loss tangent, mask, roughness, length and
band). Each job shows its figures in a table, the S-parameter plot and
links to the Touchstone, RLGC and section files.

## Limits

- 2D: the line is uniform along its length. Vias, bends, breakouts and
  plane gaps: see [Routed nets and pairs](SI_NETS.md) for nets as routed; plane gaps are checked in a later phase.
- The reference planes are solid and wide (25 dielectric heights each
  side). A plane split or a slot under the trace is not modelled here.
- Microstrip dispersion beyond the quasi-TEM model is not added; it
  matters above roughly 20 GHz for thick outer dielectrics.
