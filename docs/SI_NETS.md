# Routed nets and pairs

The second signal-integrity layer models a net, or a differential pair,
the way it is routed on the board. It takes the path from pin to pin
with every trace section, layer change and stub, builds each part from
its own physics, and joins them into one channel.

The channel gives:
- S-parameters (Touchstone file);
- the TDR;
- delay;
- insertion and return loss at the Nyquist frequency;
- for a pair, mode conversion and skew;
- Altium design rules that hold the net to its targets.

It runs as a [simulation job](SIMULATION.md) of kind `net-channel`.

## From the board to a channel

1. **Snapshot.** `si_board_snapshot` reads the board's copper and stackup from Altium once and keeps them in a folder named by their content hash. Every job made from one snapshot is about the same board. Where the stackup carries no dielectric beneath a copper layer, it is taken from Altium's own dielectric layers between the two coppers (several add up, with the thickness-weighted permittivity).
   - A snapshot also records the board file's size and save time. Once the file is saved again, the snapshot, and each result made from it, reports `stale: true`.
   - The rules are not applied from a stale snapshot.
2. **Path.**
   - The net's tracks, arcs, vias and pads are joined into a graph. A track end connects to a pad or via it lands on, to another track's end, or to the middle of another track on the same layer (a tee).
   - The path is the shortest route between the two pins: the ones given, or the two farthest apart.
   - Every branch off the path is a stub, with its length and the pin it reaches.
   - Runs on one layer with one width merge into one section, except at a tee: the run is cut where a stub leaves, so the stub sits at its own place.
3. **Pairing.** For a differential pair both nets are walked, then laid side by side.
   - The path is cut into zones between vias.
   - Inside a zone, each stretch of the positive net is matched to the nearest copper of the negative net on its layer.
   - Stretches with a steady gap become coupled sections.
   - What is left, the breakouts at either end, is uncoupled: each side is its own line with its own length.
4. **Blocks.** Each section uses the RLGC(f) of its own cross-section (layer, width, gap) from the [line models](SI_LINES.md). Each cross-section is solved once and reused.
   - **Vias** use the via model below.
   - **Stubs** are open-ended lines in shunt.
   - All blocks are held as chain (ABCD) matrices and multiplied.
5. **Results.** S follows for any reference impedance. The TDR and step responses use a Gaussian edge of the rise time asked for.

## The models

| Part | Model |
|---|---|
| Trace section | The exact matrix solution of the telegrapher's equations for a uniform length, A = cosh(Gamma l), B = sinh(Gamma l) Gamma^-1 Z, C = Z^-1 Gamma sinh(Gamma l), D = Z^-1 cosh(Gamma l) Z with Gamma = sqrt(ZY). It is the same solution as the coupled-line S-parameters of the line models: a cascade of two lengths equals the whole length to 1e-12. |
| Stub | An open-ended line in shunt, input admittance C A^-1. Its quarter-wave notch falls where it should (tested). |
| Via | The barrel inside the planes' clearance holes is a coaxial line, (60 / sqrt Er) ln(D_antipad / d). The barrel between the entry and exit layers is in series. Any barrel beyond either end is an open stub; it is cut by back-drilling. Each pad adds Johnson and Graham's pad capacitance to the nearest plane. The antipad comes from the board's plane clearance rule (8 mil if none). |
| Return path | When the entry and exit layers have no reference plane in common, the return current loops through the nearest ground via or ground pin. That loop is a series inductance, (mu0 h / pi) acosh(s / 2r), the via-pair formula of the [PDN analysis](PDN.md). With no return via within 250 mil, the result says so. |
| TDR | Step response of S11 (SDD11 for a pair) through a Gaussian edge, Z = Z0 (1 + rho) / (1 - rho). It reads a line's impedance to 1 % (tested). |
| Delay, skew | The 50 % point of the step response through S21. For a pair, each side's own transmission gives the skew. |

## What a run gives

| Metric | Meaning |
|---|---|
| `insertion_loss_db` | Insertion loss at Nyquist (half the data rate, or a quarter of the band) |
| `return_loss_db` | Worst return loss up to Nyquist |
| `delay_ps` | Delay, 50 % of the step |
| `tdr_min_ohms`, `tdr_max_ohms` | The TDR inside the channel |
| `line_loss_db` | Loss of the trace sections alone at Nyquist (the odd mode for a pair): what grows with length |
| `stub_resonance_ghz` | The lowest quarter-wave notch of any trace stub or via stub on the path |
| `length_mils`, `vias`, `stubs` | What the path is made of |
| `crosstalk_next_pct`, `crosstalk_fext_pct`, `crosstalk_pct` | Near-end and far-end crosstalk from the copper beside the net, and the worse of the two, in percent of the aggressors' swing |
| `aggressors` | Nets routed beside it |
| `skew_ps`, `mode_conversion_db`, `length_n_mils` | For a pair |

**Checks:**
- passivity;
- reciprocity;
- impedance: the TDR within `z_tol_pct` of `z_target`;
- `il_max_db`, `rl_min_db` and `skew_max_ps` when they are given;
- stub resonance: with a data rate, every stub's first notch must lie at least `stub_margin` (default 2) times above Nyquist. A notch near Nyquist is a fault of its own that no length limit fixes: shorten the stub or back-drill the via.
- crosstalk: the worse of the near and far end within `xtalk_max_pct` (default 5) of the aggressors' swing (below).

When a data rate is given, the skew limit defaults to 5 % of a unit interval.

**Files:**
- `channel.s2p` or `channel.s4p` (ports: near end first, then far end; for a pair P, N, P, N);
- `path.json`, the path as found;
- `sections.json`, every block and the figures of each cross-section;
- `tdr.csv`;
- `crosstalk.json`, every aggressor with its gap, shared length and place;
- `channel.png`.

## Crosstalk

The board is searched for what runs beside the net. For each stretch of the route, tracks and arcs of other nets on the same layer count when they run within 10 degrees of it and within reach, and the net shares at least 10 mil with the route in all (a curve is many short chords, so it is the net's whole shared length that counts): four heights above the reference plane edge to edge, and at least three trace widths (`xtalk_reach_mils` sets it).

Each neighbour's coupling comes from the field solver's cross-section of the two traces at their gap:

| | |
|---|---|
| Kc = \|C12\| / C11 | Capacitive coupling |
| Kl = L12 / L11 | Inductive coupling |
| Near end = (Kc + Kl) / 4 | Reached once the shared length is longer than half the edge; 2 TD / tr of it below that |
| Far end = (Kc - Kl) / 2 x TD / tr | Grows with the shared length and with a faster edge. Zero in a stripline (one dielectric), negative on a microstrip |

TD is the delay of the shared length and tr the edge (`rise_ps`, taken for the aggressor too). Gaps are solved on a grid and read between its points, so a board with many neighbours costs a few fast solves per layer and width.

- One aggressor's stretches add up; its near end stops at the ceiling of its tightest gap however the route is cut into segments.
- A differential aggressor counts as the difference between its two sides, and a differential victim hears the difference between what reaches its two sides. A pair routed beside a net therefore disturbs it far less than one of its sides alone.
- Aggressors add up as magnitudes: all switching together, the worst case.
- Grounds, plane nets and supply rails (by name: `GND*`, `VCC*`, `VDD*`, `+3V3`, `1V8`, ...) do not switch and are left out. `xtalk_quiet` names more, as globs (`["LED_*", "RESET_N"]`).
- `xtalk: false` leaves the check out.

The figures are fractions of the aggressor's swing. A 3.3 V neighbour at 4 % puts 132 mV on the net, whatever the net's own swing is.

## Rules back to Altium

Each run works out `pcb_create_design_rule` calls:

| Rule | From |
|---|---|
| Width (net or pair) | The field solver's width for `z_target` on the layer the net mostly runs on, +/- 10 %. A net-scoped width rule covers every layer; the run says which other layers then need their own width, which `si_stackup_plan` gives. |
| Differential pair gap | The gap that goes with that width. |
| Max uncoupled length | The skew budget divided by the routed delay per mil. |
| Length | The routed length plus the loss headroom (`il_max_db` less the insertion loss) spent at the lines' own loss per mil. Vias, stubs and mismatch are a fixed cost that does not grow with the trace, so they are not scaled by length; the rule's reason names a stub whose notch falls near Nyquist. |

`apply_rules: true` creates them through the bridge, with the usual rule-edit log. Do it only when the user asks.

## Tools

**`si_board_snapshot(expect_file | board_json_path, stackup_json_path, list_only)`**
- Takes or lists snapshots.
- Returns the `id`, the counts, the board's differential pairs and `stale`.

**`si_net_channel(net | pair, snapshot | expect_file | board_json_path, options, apply_rules)`**
- Runs the channel.
- `options` takes these keys:
  - `from_pin`, `to_pin`;
  - `data_rate_gbps`, `f_max_ghz`, `points`, `rise_ps`;
  - `z_ref`, `z_target`, `z_tol_pct`;
  - `il_max_db`, `rl_min_db`, `skew_max_ps`, `stub_margin`;
  - `temperature_c` or `thermal_npz` (hot copper loses more: see [Board signal integrity](SI_BOARD.md#copper-temperature));
  - `tand`, `foil`, `mask_mils`, `etch_mils`;
  - `rules`.

## Accuracy, as tested

- **Cascade:** a cascade of line blocks equals the coupled-line solution of the whole length to 1e-12, for single lines and pairs.
- **Matched line:** a matched lossless line is transparent to 1e-9, and its step delay is l sqrt(Er) / c within 1 %.
- **TDR:** the TDR of a 65 ohm line between 50 ohm ports reads 65 ohm within 1 %.
- **Stubs:**
  - An open stub notches at its quarter-wave frequency within 1 %.
  - On a routed net, the notch of a 300 mil test-point stub falls within 8 % of c / (4 l sqrt(Er_eff)).
  - The TDR at its tee drops to about half the line impedance.
- **Via stub:** the via stub's notch falls within 5 % of c / (4 l sqrt Er), and back-drilling removes it.
- **Line loss:** the lines' loss equals the sum of each cross-section's own loss per inch to 0.1 %; a pair's is the odd mode's exactly.
- **Length budget:** the same stubbed net routed 2000 and 5000 mil long gets the same length limit within 3 % (scaling the whole loss by the routed length moved it by over 30 %).
- **Curved routes:** an arc following the net's own arc 4 mil away for a quarter circle of 500 mil radius couples as much as the same length side by side on a straight run, within 2 %.
- **Crosstalk:** for two traces 4 mil apart over 800 mil of microstrip and a 100 ps edge, the near end is within 5 % and the far end within 15 % of the peaks of the coupled line solved in full (the four-port from the eddy-current solver, between 50 ohm ports). A stripline's far end is under 0.3 %. A pair with one side each side of the net cancels to zero.
- **Pair skew:** the skew of a pair with a 49 mil length mismatch matches the mismatch times the line's delay per mil within 1.5 ps.

## Limits

- **Crosstalk:**
  - The weak-coupling results hold for matched ends and coupling of a few percent to about 15 %. Reflections at mismatched ends add to them.
  - Only tracks and arcs on the same layer count. Copper on the next signal layer with no plane between (broadside coupling) and vias beside vias are not searched.
  - The aggressor's edge is taken as fast as the victim's `rise_ps`. Beside a slower neighbour the far end is overstated.

- **Vias:**
  - The via model is an engineering model: right in shape to the first stub resonance, and good to a few GHz for a typical via. Above that, or where a via field matters, model the via in 3D (`em-3d`, openEMS) and use its S-parameters.
  - The two vias of a pair are modelled uncoupled. Their coupling lowers the differential via impedance a little.
- **Pads and stubs on pairs:**
  - Pad capacitance at the pins and the pins' package are not included.
  - Stubs on a pair's nets are listed but not modelled.
- **Copper:**
  - Planes are taken as solid under the path. A split or gap under a trace is not seen; phase 5 checks for it.
  - Copper pours on signal layers used as a reference count as the reference only where the stackup's layer order makes them one.
