# Eyes and SPICE models

The third signal-integrity layer turns a channel into what a receiver
sees: an eye diagram at a data rate, with the real driver and receiver at
its ends. The channel is any 2-port line or 4-port pair: a Touchstone file
from a measurement or a field solver, or the channel of a
[routed net or pair](SI_NETS.md).

Two independent routes give the eye, and the job sets them side by side:

- **Convolution** (`eda-agent` engine): the channel's pulse response is
  convolved with a PRBS pattern. The worst-case eye from peak distortion
  analysis comes with it.
- **Circuit simulation** (`spice` engine): the channel is fitted as a
  passive rational macromodel and written as a SPICE subcircuit. The same
  pattern then runs through it in ngspice or LTspice.

When both routes agree, the eye is a property of the channel and not of
one method. The subcircuit is kept, with an LTspice symbol, so the
channel can go straight into a schematic beside vendor driver models.

It runs as a [simulation job](SIMULATION.md) of kind `channel-eye`.

## The convolution eye

1. **Transfer.** S21 for a line, or SDD21 for a pair (ports 1 and 2 near P and N, 3 and 4 far). With real terminations, the transfer comes from the whole circuit; see [Driver and receiver](#driver-and-receiver).
2. **Edge.** The transmitter's edge is a linear ramp of the given 10-90 % rise time, the shape a SPICE PWL source makes. Its spectrum multiplies the transfer. The top tenth of the band is tapered by a half cosine, so the band edge does not ring.
3. **Pulse response.** The step response is the inverse FFT of the edged transfer, integrated. One unit interval of it, less the same delayed by one UI, is the pulse response.
4. **Pattern.** A PRBS of order 7, 9, 11 or 15 from the ITU-T O.150 generators is played as NRZ symbols of plus and minus half the amplitude. The pattern settles for one channel length and then runs `repeats` more times.
5. **Eye.** The waveform is folded onto one UI, with the pulse peak as the sampling reference.
   - Eye height is the lowest one less the highest zero at the best phase.
   - Eye width is the open span around that phase.
6. **Worst case.** Peak distortion analysis (Casper et al., VLSI Circuits 2002) takes, at each phase, the main cursor less the sum of every other cursor's magnitude. No pattern closes the eye further, so this is the guaranteed opening.

Amplitude is the incident swing into a matched load, peak to peak, and differential for a pair. A line driven by a source of plus and minus A through z0 receives S21 times A/2. A pair driven by plus and minus A/2 on each side receives SDD21 times A/2 differentially.

## The SPICE eye

1. **Fit.** Relaxed vector fitting (Gustavsen and Semlyen, with the relaxation of Gustavsen 2006) puts one set of common poles under every S-parameter.
   - Frequencies are normalised and the fast QR form is used for each element.
   - Unstable poles are flipped into the left half plane.
   - The order grows in steps of 8 until the RMS error is under `fit_tol`.
   - Reciprocal data is symmetrised first and stays reciprocal.
2. **Passivity.** The largest singular value of S must stay at most 1 at every frequency: in band, a decade and more below it, and up to four times above it.
   - Where it goes over 1, the residues and D are perturbed by least squares. The change is held small in band and kept reciprocal.
   - Both singular values, before and after, are reported.
3. **Subcircuit.** The model becomes a subcircuit of resistors, capacitors and linear controlled sources only. Every SPICE reads these, and they simulate exactly as the model says.
   - Each port is z0 to a source of twice its outgoing wave.
   - Each pole is a state node: a capacitor, a conductance and a current from the incoming wave.
   - The outputs are sums of currents into a 1 ohm node.
   - An ngspice AC run of the subcircuit gives back the model's S to 1e-6.
4. **Transient.** PWL sources play the same pattern with the same linear edges, through the same terminations. The simulator steps four times finer than the convolution's grid (`spice_oversample`), and its waveform is resampled onto that grid and folded the same way.
5. **Agreement.** The RMS difference between the SPICE waveform and the convolution of the same macromodel is reported. The check passes under 2 % of the amplitude. Typical runs agree to under 1 mV on 800 mV.

The `spice` engine needs ngspice or LTspice; `simulator` picks one (`auto` takes what is installed).

### The subcircuit in a schematic

`channel.sub` and `channel.asy` are written into the job folder. `si_spice_model` writes them for any Touchstone file, without an eye.

- Pins are `port1`, `port2` (or `p_near`, `n_near`, `p_far`, `n_far` for a pair), then `ref`.
- Place `channel.asy` in an LTspice schematic with `channel.sub` beside it. Then drive it with a vendor's IBIS-to-SPICE or transistor-level buffer model.
- `macromodel.json` keeps the poles and residues.

## Driver and receiver

By default, both ends are the reference impedance. Real ends change the eye: a 40 ohm driver into a high-impedance CMOS receiver nearly doubles the swing, and pad capacitance slows the edge.

| Option | Meaning |
|---|---|
| `tx_ibis`, `tx_model` | An IBIS driver. Its output resistance, its C_comp, its 10-90 % edge (8/6 of the 20-80 % [Ramp] time) and its swing (rail to rail) replace the defaults. The resistance is read from the pull-down and pull-up V-I tables between the rail and 30 % of the supply into the driving range: the side the transistor works on, not the clamp diode below the rail. Where one of the two barely conducts there (an n-channel pull-up), the stronger is used and a note says so. An input, terminator or series model is refused as a driver. |
| `rx_ibis`, `rx_model` | An IBIS receiver: its C_comp at the far end. Where its clamp tables carry an on-die termination (current between the rails), that resistance loads the line; otherwise it is high impedance. `rx_r_ohms` overrides either. |
| `tx_r_ohms`, `tx_c_pf` | Driver resistance and pad capacitance per side. |
| `rx_r_ohms`, `rx_c_pf` | Receiver termination per side to ground (a number, or `open`) and pad capacitance. |

Explicit values win over an IBIS file's, and `rise_ps` and `amplitude_mv` win over its ramp and supply.

- **In the convolution**, each port is closed by its Norton equivalent. With waves of reference z0, (G+ - G- S) a = I_n with G+/- = 1/z0 +/- Y_t, and v = (I + S) a. The transfer is normalised so that it equals S21 (SDD21) when both ends are matched.
- **In the SPICE deck**, the same resistors and capacitors are elements beside the subcircuit.

The two routes therefore see the same circuit.

`si_ibis_models` lists the models of an IBIS file with these figures, so the right `tx_model` and `rx_model` can be picked.

The IBIS reduction is linear: the driver is its resistance at the typical corner. That is right for a reflection and loss budget. The full nonlinear buffer, with its V-I curve through the whole swing and its V-t waveforms, needs a full IBIS simulation; the subcircuit is there for that.

## Equalisation

A long channel closes the plain eye well before the link stops working, because both ends equalise. The convolution engine models the three usual stages; all are off by default.

| Option | Meaning |
|---|---|
| `tx_ffe` | Transmitter feed-forward taps, one unit interval apart, for example `[-0.1, 0.7, -0.2]` (one pre-cursor, the main tap, one post-cursor). The largest tap is the main one. The taps are scaled so that their magnitudes add up to 1: the driver's peak swing stays `amplitude_mv`, and what the equaliser gains at the edge it pays for in level. Up to 8 taps |
| `ctle_db` | Receiver continuous-time linear equaliser: its boost over DC in dB (0 to 30). It is passive in shape: the gain at DC is lowered by the boost and the peak is at most 1, H(f) = g (1 + j f / fz) / ((1 + j f / fp)(1 + j f / 4 fp)) with g = 10^(-boost / 20) and fz = g fp |
| `ctle_peak_ghz` | The CTLE's first pole fp (default: Nyquist). The second pole is at four times it |
| `dfe_taps` | Receiver decision-feedback taps (0 to 32). Tap k takes off the pulse's post-cursor k bits after the main one, over the whole interval around it |
| `dfe_max` | The most one tap can take off, as a part of the main cursor (default 0.5). A post-cursor above it is only partly cancelled |

The CTLE multiplies the channel's transfer, the FFE adds shifted copies of the pulse and the DFE subtracts from it; the eye, the worst-case eye, the waveform and the cursors are then those after equalisation. The edge figures and the insertion loss stay the channel's own. `dfe_tap1_mv` gives the first DFE tap, and the notes list all taps, so they can be compared with what the receiver allows.

**Chosen for you.** `tx_ffe: "auto"` works out the taps, with `tx_ffe_pre` taps before the main one (default 1, up to 3) and `tx_ffe_post` after it (default 1, up to 4). At the sampling point where the eye without them is tallest, the worst-case opening, the main cursor less the magnitudes of all the others, is piecewise linear in the taps, and the taps' magnitudes add up to 1: the best taps are the solution of a linear programme, exact and not a search. Cursors a DFE takes off are left out of it. No taps at all is one of the choices, and the run keeps it whenever the eye over the whole bit is no larger with the taps: an automatic equaliser never leaves a smaller worst-case eye than none. `ctle_db: "auto"` tries every whole dB from 0 to `ctle_max_db` (default 12), with the taps worked out again at each, and keeps the one with the largest worst-case eye; between equal eyes the least equalisation wins, so a clean channel gets none. The run reports what it chose: `ffe_main_tap` (1 means no taps were needed), `ctle_db`, and the taps in the notes, which can be given back by hand.

Otherwise the taps are the ones given. The DFE takes every decision as right, so error propagation is not in the eye. The SPICE engine refuses equalisation; run it without to compare the two routes on the plain channel.

## The eye at a bit error rate

The worst-case eye asks whether any pattern can close the eye; on a long channel one usually can, once in more bits than the link will ever send. The statistical eye asks what is left at a bit error rate. It is worked out when one of these is given:

| Option | Meaning |
|---|---|
| `ber` | The bit error rate the eye is drawn at (default 1e-12; 1e-18 to 0.1) |
| `noise_rms_mv` | Random (Gaussian) noise at the receiver, RMS |
| `rj_rms_ps`, `dj_pp_ps` | Jitter of the sampling instant against the data: random (Gaussian, RMS) and deterministic (peak to peak, as two points: the dual-Dirac model) |
| `eye_at_ber_min_mv`, `eye_at_ber_min_ui` | The height and width needed at that rate (default: open) |

At each sampling phase the level of a one is its main cursor plus every other cursor with the sign of an independent random bit. The probability density of that sum is the transform of its characteristic function, the product of cos(w a_k) over the cursors times the noise's Gaussian: every pattern is in it, with its probability, without running one. Jitter mixes the densities of the phases it reaches, each with its probability. The inner contour is the level a one falls below with probability `ber`; a zero mirrors it, and the eye is what lies between.

The run then gives `eye_height_at_ber_mv`, `eye_width_at_ber_ps` and `eye_width_at_ber_ui`, the check "eye at the bit error rate", and `bit_error_rate`: the error rate at the best sampling instant with the threshold in the middle (1e-19 stands for that or less). Peak noise (`noise_mv`, the crosstalk of a board check) comes off both rails as it does for the pattern's eye. Equalisation is applied before it.

What it assumes: bits independent and equally likely (scrambled data, not a coded pattern), a linear channel, and jitter in steps of one time sample, so `samples_per_ui` sets how finely a small jitter is resolved. The levels are worked out on 32768 voltage steps and always carry a Gaussian width of at least 2.8 steps, which costs an eye of ISI alone under 2 mV in 500.

## What a run gives

| Metric | Engine | Meaning |
|---|---|---|
| `eye_height_mv`, `eye_width_ps`, `eye_width_ui` | both | The eye of the pattern |
| `worst_case_eye_height_mv`, `worst_case_eye_width_ps` | `eda-agent` | Peak distortion analysis |
| `main_cursor_mv`, `isi_mv` | `eda-agent` | The main cursor and the sum of the others |
| `overshoot_pct`, `ringback_pct` | `eda-agent` | One edge at the receiver: how far it goes above the level it settles to, and how far it falls back below it afterwards |
| `edge_reversal_pct` | `eda-agent` | The deepest fall on the way up, between 10 % and 90 % of the level. A reflection that arrives inside the edge turns it back, and a clock input can see the edge twice |
| `settling_ps` | `eda-agent` | From half the level until the edge stays within 5 % of it |
| `eye_height_at_ber_mv`, `eye_width_at_ber_ps`, `eye_width_at_ber_ui`, `bit_error_rate` | `eda-agent` | With `ber`, noise or jitter given: [the eye at a bit error rate](#the-eye-at-a-bit-error-rate) |
| `ffe_main_tap`, `ctle_db` | `eda-agent` | With `tx_ffe` or `ctle_db` set to `auto`: the main tap and the boost chosen |
| `dfe_tap1_mv` | `eda-agent` | With `dfe_taps`: the first tap, as the voltage it takes off |
| `series_r_ohms` | `eda-agent` | When the overshoot is over its limit: the resistor in series with the driver (each side of a pair) that brings it within, to the whole ohm above. It is worked out as more source resistance with everything else as it is, up to four reference impedances; when that is not enough the overshoot comes from the line and the check says so |
| `insertion_loss_db_at_nyquist` | `eda-agent` | Of the transfer used |
| `spice_vs_convolution_rms_mv` | `spice` | The two routes' difference |
| `macromodel_poles`, `macromodel_rms_error` | `spice` | The fit |

**Checks:**
- eye open;
- worst case open;
- overshoot, ringback and edge reversal, each when its limit is given (`overshoot_max_pct`, `ringback_max_pct`, `edge_reversal_max_pct`, in percent of the settled level);
- macromodel passive;
- macromodel fit within twice `fit_tol`;
- SPICE agrees with convolution.

**Files:**
- `eye.png`;
- `waveform.csv`;
- `channel.sub`, `channel.asy` and `macromodel.json` (spice).

## Tools

- `si_channel_eye(data_rate_gbps, touchstone_file or net_job, engines, options)`.
  - `net_job` takes the channel of a `net-channel` job, so a routed net goes from the board to its eye in two calls.
  - `options`: `rise_ps` (0.3 UI), `amplitude_mv` (800), `noise_mv` (0: noise at the receiver from crosstalk or the supply, peak; the eye loses twice this in height, reported as `eye_height_with_noise_mv` with the check `eye open with noise`), `prbs` (7), `repeats` (2), `samples_per_ui` (32), `spice_oversample` (4), `simulator`, `fit_tol` (0.002), and the driver and receiver options above.
- `si_spice_model(touchstone_file or net_job, name, out_folder, fit_tol)` writes the subcircuit and symbol.
- `si_ibis_models(ibis_file)` lists an IBIS file's buffers.

## Verified

- **PRBS:** each generator has its full period (2^n - 1) and no shorter one, with one more one than zeros.
- **Edge:** the step of a flat channel is the ramp, with the 10-90 % time asked for within 3 %.
- **The single edge:**
  - A damped ringing step gives back its first peak as overshoot, the dip after it as ringback and its decay as the settling time; a shelf that dips 15 % on the way up reads 15 % of reversal.
  - A 15 ohm driver into an open end of a 50 ohm line overshoots by 2 x 50 / 65 - 1 = 54 % (within 4 points on a lossy line) and rings back as the source reflection says; a matched or a weak driver does not overshoot.
  - The level a step settles to is worked out from the S-matrix at 0 Hz and the terminations. Each transmission's magnitude is read back to 0 Hz in a straight line from its first two points (the magnitude does not turn with the delay, as the real part does), and each port reflects what it does not pass on, as a channel of conductors alone does at DC: 1 between matched ends and 2 into an open end within 1 %, also behind a 215 ohm driver whose transfer has its corner near the frequency step. A pure delay then stays flat to 1 % where the first point taken as DC slopes. A 0.8 m line swept in 50 MHz steps (over 1 rad of delay per step) still gives its level within 1 %, and reflections inside the band do not change it.
  - The series resistor named for a 15 ohm driver into an open end and a 10 % limit is 2 Z / 1.1 - Z - 15 = 26 ohm within 3; with it in the edge passes, and 1.5 ohm less it does not.
- **Eyes:**
  - The pulse settles to the DC level worked out from the S-matrix at 0 Hz. A matched lossless line then gives the full swing with under 4 mV of ISI at 800 mV (the first point taken as DC left more than twice that, as offsets of all the bits adding up), and a 150 ohm driver into an open end runs where it used to be refused as still ringing.
  - A flat channel's eye and worst-case eye are the full swing.
  - On a lossy line, the worst case lies below the PRBS eye.
  - The worst case is reached by the pattern that peak distortion analysis names.
- **The eye at a bit error rate:**
  - With noise alone it is the Gaussian quantile, main cursor + sigma x Phi^-1(ber), within 2 parts in a million from 1e-3 down to 1e-18; the error rate at the threshold is the Gaussian tail within 0.1 % at 6e-16.
  - With ISI alone it steps through the patterns: four cursors give the worst case at any rate below 1 in 16 and the second-worst pattern at 0.1. Sixteen cursors of 0.07 close the worst-case eye; all sixteen line up once in 65536 bits, which is the error rate reported, and at 1e-3 the eye is that of fourteen against the bit.
  - Deterministic jitter of 3 samples either way gives the level 3 samples off the centre; random jitter of half a sample gives the level 4 samples off at 1e-12 and 2 samples off at 1e-3, as its tail probabilities say.
  - 2000 cursors of 15 millionths, each a fifth of a voltage step, widen the levels as the Gaussian of their sum does (within 5 %), and the series they go through gives the eye that multiplying them out one by one gives, within a microvolt.
  - On a 0.8 m line at 5 Gb/s it lies between the worst-case eye and the PRBS eye; noise and jitter each lower it, a looser rate raises it, and peak noise takes twice its value off.
- **Equalisation:**
  - The CTLE is 1 / boost at DC, and at its first pole it is the formula's value by hand; with no boost it is 1 everywhere.
  - On a flat channel, FFE taps [0.75, -0.25] give the de-emphasis they should: the eye is 0.75 - 0.25 = 0.5 of the swing. A tap that mirrors a channel's post-cursor cancels it.
  - A DFE with enough taps and range takes the post-cursors off to zero and leaves the main cursor and the pre-cursors alone; a tap at its limit takes off the limit and no more.
  - A 0.8 m lossy line at 10 Gb/s has a closed worst-case eye. FFE taps [-0.1, 0.7, -0.2] open it, and so do four DFE taps, each above 80 mV at 800 mV; 6 dB of CTLE opens it too and widens the eye.
  - Chosen taps: for a pulse with cursors 0.2, 1, 0.4, 0.1 the programme's three taps give a worst-case opening no grid of other taps (steps of 0.05) and no random step around them beats; one post-cursor alone is left unequalised, as a tap would cost the main cursor what it gains. On the 0.8 m line the chosen taps open the worst-case eye 36 mV further than [-0.1, 0.7, -0.2], and the chosen CTLE boost is the best of the thirteen run by hand. A short clean line gets no equaliser, and an 85 ohm line between 50 ohm ends, whose ISI is reflections, is never left with a smaller eye than without.
- **Vector fitting:** known poles come back to 1e-5 with an RMS error under 1e-8. Fits stay stable and reciprocal.
- **Passivity:** a model with gain above 1 is brought to at most 1 everywhere on the check grid.
- **Subcircuit:** ngspice AC of a 2-port and a 4-port subcircuit equals the model's S to 1e-6.
- **Terminations:**
  - Matched ends give S21 and SDD21 exactly.
  - An open end doubles the received voltage, and a 25 ohm source gives 4/3.
  - A capacitive load matches the circuit formula to 1e-12.
- **IBIS:** a test buffer's 40 ohm V-I slope, 80 ps edge, C_comp and supply are read back.
- **Agreement:** with IBIS ends, the ngspice and convolution eyes agree within the 2 % check, and the job's compare marks them as agreeing.

## Limits

- The eye is NRZ. The statistical eye takes bits as independent, the channel as linear and jitter as that of the sampling instant; transmitter duty-cycle distortion, sinusoidal jitter and the clock recovery's tracking are not modelled.
- Equalisation is not adaptive: taps and boost are given, or chosen once for the largest worst-case eye at the nominal sampling point (not for the eye at a bit error rate). No DFE error propagation, and not in the SPICE engine.
- The DC level takes the channel as conductors alone. A Touchstone file with a shunt element or a series capacitor in it gets an approximate level.
- The IBIS reduction is linear at the typical corner. Package parasitics ([Package]) are not added; include them in the channel if they matter.
- The convolution needs the channel on a uniform grid from df. Another grid is resampled through a fitted model, and the job says so.
- The channel's frequency step sets the time its response covers (1/df). The eye is refused, with the step it needs, when that time is under three bits or the response is still ringing at its end: the response would wrap round onto itself. A board check gives a slow net with an eye more points so its sweep covers eight bits.
- The linear IBIS buffer has no clamp diodes, so overshoot past the rails is not clipped: the overshoot figure is the unclamped one, the worst case.
- The edge figures are those of one isolated rising edge with the terminations given; a falling edge is the same in this linear model.
- A channel with very long delay needs many poles (about one per 50 ps of delay per 10 GHz of band). Fitting time grows with them.
