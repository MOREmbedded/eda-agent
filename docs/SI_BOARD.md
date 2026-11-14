# Board signal integrity

The fourth signal-integrity layer checks a whole board. It takes the board snapshot once and models each high-speed net or pair it is asked about, as [routed nets and pairs](SI_NETS.md) do. Each one is checked against its own limits, and optionally its [eye](SI_TIME.md) too.

What fails becomes a **finding**: the net, the check, the value against the limit, and where on the board the fault is. The findings go to:
- a report;
- a findings file the dashboard and other tools read;
- Altium, where the failing nets are highlighted and, if asked, each fault is marked.

It runs as a [simulation job](SIMULATION.md) of kind `board-si`, through the `si_board_check` tool.

## What is checked

`targets` is a list. Each entry picks nets and pairs and sets their limits:

| Key | Meaning |
|---|---|
| `match` | A glob on pair and net names (`USB*`, `DDR_DQ?`), case-insensitive |
| `class` | An Altium net class: its pairs (both nets in the class) and its other nets |
| `kind` | `pair` or `net`; both when absent |
| `data_rate_gbps` | The data rate: Nyquist, the default skew limit and the eye follow from it |
| `z_target`, `z_tol_pct`, `il_max_db`, `rl_min_db`, `skew_max_ps`, `stub_margin` | The limits of a [net channel](SI_NETS.md) |
| `xtalk_max_pct`, `xtalk_reach_mils`, `xtalk_quiet`, `xtalk` | Crosstalk from the copper beside it: the limit (5 % of the aggressors' swing), how far a neighbour counts, nets that do not switch, and `false` to leave it out ([Crosstalk](SI_NETS.md#crosstalk)) |
| `eye`, `eye_min_mv`, `eye_min_ui` | Also run the eye, and its limits (open by default) |
| `xtalk_swing_mv` | The neighbours' swing (default the eye's own `amplitude_mv`). Crosstalk times this is noise on both rails of the eye: `eye_min_mv` is held against what is left (`eye_height_with_noise_mv`) |
| `tx_ibis`, `rx_ibis`, ... | The eye's driver and receiver ([Eyes and SPICE models](SI_TIME.md)) |

- A net or pair is taken by the first target that matches it. A broad target last (`{"match": "*", ...}`) catches what the others leave.
- A pair's two nets are checked as the pair, never alone.
- With no targets, every differential pair is checked at the job's `data_rate_gbps`.

Job options:
- `max_items` (40);
- `workers` (4; items solved at once);
- `f_max_ghz` (1.5 times the highest data rate, at least 10);
- `points` (200);
- the laminate settings (`tand`, `foil`, `mask_mils`, `etch_mils`);
- the crosstalk settings (`xtalk`, `xtalk_max_pct`, `xtalk_reach_mils`, `xtalk_quiet`) for targets that give none;
- the copper temperature (below).

A net that cannot be modelled (nothing routed, a pin it cannot reach) is a warning; the rest of the board still runs.

## Where a fault is

| Check | Placed at |
|---|---|
| Impedance | Where the TDR sees the fault. The fault starts where the TDR first moves half way to its worst value (a tee holds its dip until the stub's own reflection returns, so the worst point is its end). Half that time is the one-way time. The routed path is walked at each trace section's own delay per mil, with vias as instant, and the point reached is the fault. |
| Impedance, further stretches | Every other stretch of the route outside the band, longer than the rise time, is a finding of its own, placed where it starts: a wide section in a line that is itself off target, for example. At most five per net, the furthest from the target first. |
| Stub resonance | The point where the stub leaves the path; for a via stub, the via. |
| Crosstalk | The start of the longest stretch beside the worst aggressor, with the aggressor, the shared length and the gap. |
| Loss, return loss, skew, eye | The driving pin. |

On the test board, the 300 mil test-point stub that leaves the route 600 mil from the driver is placed at 618 mil.

## Copper temperature

Hot copper loses more. Copper's resistivity rises by 0.393 % per kelvin from 20 °C, the temperature of the line solvers' conductivity.
- **At DC** the resistance scales by (1 + alpha dT).
- **In the skin-effect regime** the skin depth grows with the square root of resistivity, so R scales by the square root of that.
- **Between the two**, R(f) is scaled by (1 + alpha dT) to the power 1/2 + R_dc / (2 R(f)).

The internal inductance and the roughness factor are left as they are.

- `temperature_c` puts every trace at one temperature.
- `thermal_npz` takes the `result.npz` of a [thermal analysis](THERMAL.md). Each trace section is sampled along its own copper on its own layer, so a trace under a hot regulator loses more than the same trace at the board edge.
  - A pair's coupled stretch takes the temperature along its positive net.
  - A layer the map does not have stays at 20 °C (or `temperature_c`).

Both also work on a single `net-channel` job.

On the test net, at 85 °C everywhere, the insertion loss at 5 GHz rises from 2.72 to 2.85 dB.

## The report

The job folder holds:
- `report.html`: one self-contained page, readable offline and in light or dark mode. It has the summary, the findings table with each place, a table of every net's figures, and per net its checks, S-parameter and TDR plot, and eye.
- `findings.json`: every item's metrics and verdicts, and the findings with `id`, `severity`, `check`, `detail`, `at_mils`, `layer` and `where`.
- `summary.csv`: one row per net or pair.
- `items/<kind>_<name>/`: each net's own channel files (Touchstone, TDR, path, plots).

## In Altium

`si_board_check(..., mark=...)`:
- `none` (default): results only.
- `highlight`: the failing nets are highlighted on the board (the first clears earlier highlights).
- `text`: also writes each error's id and check (`SI F3 impedance`) as text on `marker_layer` (default `Mechanical13`) at its place. This changes the board, so it is used only when the user asked for it.
  - The free texts of an earlier run (those that start with `SI F`) are removed first, so a second run replaces the markers.
  - It is refused when the snapshot is stale, because the board has changed since and the places may no longer be right.
  - An edit that is not saved yet leaves the file as it was. So the board is read again before anything is written, and the markers are refused if it differs from the snapshot or if another document is in front.

## The job graph

Each job records where its inputs came from: a board snapshot, or a file another job wrote (a `net-channel` job's channel, for example, when an eye job uses it).

On the Simulation page, each job shows:
- what it was made from, with the snapshot marked **stale** once the board has been saved since;
- what used it.

Each link opens that job. A stale job says to take a new snapshot and run it again.

## Verified

- **Placement:** on a board with a pair and a stubbed net:
  - the tee is placed within 45 mil of its true point (618 mil against 600 mil);
  - the stub is placed at its tee;
  - the skew fault is placed at the driving pin.
- **Crosstalk:** a neighbour 4 mil from a net for 600 mil is placed at the start of that stretch, with its name, length and gap.
- **Selection:** first-target-wins, net classes, and a pair taken as a pair are each tested.
- **Thermal link:**
  - DC resistance scales by exactly (1 + alpha dT), and a skin-effect R by its square root.
  - 20 °C changes nothing.
  - A map that heats only part of a trace adds less loss than heating all of it.
- **Report:** it lists every finding and embeds the plots, with no script.
- **Job graph:** snapshot to channel to eye are linked both ways, and saving the board marks the chain stale.
- **Altium:** failing nets are highlighted in order (the first clears), there is one text per error, and no text is written from a stale snapshot.
- **Seeded breaks:** 11 seeded breaks are each caught by a test.

## Limits

- **Coupling between nets:** crosstalk is worked out from each neighbour's gap and shared length ([Crosstalk](SI_NETS.md#crosstalk)), on the same layer only. The channel itself (loss, TDR) is still each net on its own. The eye takes crosstalk as its peak on both rails whenever it arrives, which is the worst case; the neighbours' waveforms are not simulated.
- **Fault placement** is as good as the delay per mil of each section. Vias count as instant, and a fault within about a rise time of another merges with it. A stretch that follows a large step is placed late, because the step's own reflections blur the TDR behind it.
- **Altium markers:** marking uses highlights and text. Altium's violation list is not written.
