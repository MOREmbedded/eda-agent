# Simulation engines

Every simulator eda-agent drives is an **engine**: this project's own
solvers, scikit-rf, openEMS, and any engine another package adds. Every
problem is a **job**. A job is solved by one engine or several, and each
engine's answer is kept beside the others so they can be compared. Two
independent engines that agree are a second opinion; two that disagree
are a warning no single tool can give.

This is the base the signal-integrity work builds on. Transmission-line
models and the stackup planner ([Transmission lines](SI_LINES.md)) are
the first job kinds on it; net S-parameters, IBIS time-domain simulation
and eye diagrams follow as new job kinds and engines on the same model.

## Engines

| Engine | License | Runs | Job kinds | Feature |
|---|---|---|---|---|
| `eda-agent` | Apache-2.0 | in this process | `touchstone-check`, `xsection-2d`, `line-model`, `stackup-plan`, `net-channel`, `channel-eye`, `board-si` | always installed |
| `scikit-rf` | BSD-3-Clause | in this process | `touchstone-check`, `line-model` (a single microstrip without mask) | `scikit-rf` (`si` extra) |
| `spice` | ngspice: BSD-3-Clause; LTspice: freeware | as its own program | `channel-eye` (the channel as a SPICE subcircuit) | `spice` |
| `openems` | GPL-3.0 | as its own program | `em-3d` | `openems` |

An engine is offered only when its feature is installed and switched on
([Features and dependencies](FEATURES_AND_DEPENDENCIES.md)). Copyleft
engines never load into the eda-agent process: they run as their own
program, files in and files out, as openEMS does.

## Job kinds

| Kind | Inputs | Spec | What comes back |
|---|---|---|---|
| `touchstone-check` | `touchstone`: an `.sNp` file | `tolerance` (default 1e-3) | Passivity (largest singular value of S over the band, and where), reciprocity (largest \|Sij - Sji\|), worst return loss, S21 at the top frequency and at its worst, verdicts. |
| `xsection-2d` | none | `calculator`: a PCB toolkit impedance calculator; `inputs`: its inputs | Z0, Er_eff, delay, L and C per length, losses: every number the [calculator](PCB_TOOLKIT.md) gives, from the 2D field solver. |
| `line-model` | `stackup` (optional): a saved stackup reply, with `layer` | the line: `structure` or `layer`, `width_mils`, `traces`, `gap_mils`, heights, Er, tan d, mask, roughness, `length_mm`, band | Impedances (lossless and lossy), delay, loss, crosstalk, Touchstone file, RLGC matrices: see [Transmission lines](SI_LINES.md). |
| `stackup-plan` | `stackup`: a saved stackup reply | `se_ohms`, `diff_ohms`, `layers`, laminate and mask settings, `loss_ghz`, `altium_profiles` | Per layer the width for the single-ended target and the pair geometry for each differential target; comparison with Altium's profiles. |
| `net-channel` | `board`: a saved LayoutBoard; `stackup`: its stackup reply (both from `si_board_snapshot`) | `net` or `pair`, `from_pin`, `to_pin`, `data_rate_gbps`, band, `rise_ps`, `z_target`, limits | The routed net or pair as one channel: S-parameters, TDR, delay, loss at Nyquist, skew, mode conversion, and Altium rules: see [Routed nets and pairs](SI_NETS.md). |
| `channel-eye` | `touchstone`: a 2-port or 4-port channel | `data_rate_gbps`, `rise_ps`, `amplitude_mv`, `prbs`, driver and receiver (`tx_ibis`, `rx_ibis`, resistances, capacitances), `simulator`, `fit_tol` | The eye by convolution with the worst-case eye, or in SPICE through a passive macromodel kept as a subcircuit: see [Eyes and SPICE models](SI_TIME.md). |
| `board-si` | `board`, `stackup` (as `net-channel`) | `targets` (nets and pairs by name glob or Altium net class, each with its limits and optionally the eye), `data_rate_gbps`, `max_items`, `workers`, `temperature_c` or `thermal_npz` | Every target net and pair checked, each failure placed on the board; `report.html`, `findings.json`, `summary.csv`: see [Board signal integrity](SI_BOARD.md). |
| `em-3d` | `board`: a saved LayoutBoard; `stackup` (optional): a saved stackup reply | `nets`, `ports`, `f_max_mhz`, `f_min_mhz`, `margin_mils`, `port_ohms`, `max_cells_m`, `threads` | The Touchstone file, plots, and the `touchstone-check` figures of the result. |

Unknown spec keys and inputs are errors, so a misspelt option is never
silently ignored.

## Jobs

A job is a folder under `EDA_AGENT_SIM_JOBS` (default
`%USERPROFILE%\EDA Agent\jobs`):

```
touchstone-check-3f9a1c0b2d4e/
  job.json              kind, title, spec, inputs with their SHA-256
  inputs/               copies of the input files
  runs/eda-agent/       status.json, results.json
  runs/scikit-rf/       status.json, results.json
  runs/openems/work/    the engine's own files (model, Touchstone, plots)
```

The folder name is a hash of the kind, the spec and the content of the
inputs. The same problem is therefore the same job: asking again returns
the finished answer without solving (unless forced), and a second engine
lands beside the first. A changed input file is a new job. A finished
run also records the code that computed it: after an update, asking again
solves the same job again instead of returning the older code's answer.

Every engine writes the same `results.json` shape: named metrics with
units, pass/fail verdicts, notes and files. Metrics with the same name
from two engines are what the comparison sets side by side, with the
relative spread and whether it is within the tolerance (2 % by default);
a check the engines disagree on is reported on its own.

Engines that run in this process finish before the call returns. An
external engine (openEMS) is started and keeps running on its own;
asking for the job's status polls it. A run whose eda-agent process
stopped is marked failed rather than left running.

## Tools

| Tool | Does |
|---|---|
| `sim_engines` | The engines with their state, and the job kinds with their spec |
| `sim_job_run` | Run a problem on one or more engines; with two or more, the comparison comes back too |
| `sim_job_status` | A job and each engine's state and results; polls external engines |
| `sim_job_compare` | The engines' answers side by side |
| `sim_jobs` | Recent jobs |

Example:

```json
sim_job_run(kind="touchstone-check", engines=["eda-agent", "scikit-rf"],
            inputs={"touchstone": "C:/sparams/usb_channel.s4p"})
```

## The dashboard page

**Simulation** in the dashboard's top bar shows the engines as cards
(state, license, where they run, job kinds, versions, and a link to the
Features page for one that is missing), a form that checks a Touchstone
file on the engines you tick, the jobs newest first, and for the chosen
job each engine's results in one table with the spread per row, its
verdicts, and a button to run it on another engine.

## Writing an engine

An engine subclasses `eda_agent.simulation.Engine`, sets `info`, and
implements `run` (and `poll` for an external program). Register it
through the `eda_agent.engines` entry-point group, and declare its
feature (dependencies, license, install steps) through
`eda_agent.features` ([how](FEATURES_AND_DEPENDENCIES.md#adding-a-feature-from-another-package)):

```python
from eda_agent.simulation import Engine, EngineInfo, metric

class MySolver(Engine):
    info = EngineInfo("my-solver", "My solver", feature="my-solver",
                      license="MIT", process="library", kinds=("xsection-2d",))

    def run(self, job, folder, progress):
        progress("solving", 0.5)
        z0 = ...   # read job.spec, write files into folder
        return {"metrics": {"z0": metric(z0, "ohm", "Characteristic impedance Z0")}}

def engines():
    return MySolver()
```

```toml
[project.entry-points."eda_agent.engines"]
my_solver = "my_package.eda:engines"
```

Use the metric names the built-in engines use for the same quantity, so
the comparison lines them up. An engine whose feature is not registered,
whose id is taken or whose job kinds are unknown is not loaded, and the
reason is shown on the Simulation page and by `sim_engines`.

An engine that runs code under a copyleft license must run it as a
separate process (`process="external"`), never by importing it.
