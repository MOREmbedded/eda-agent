# Hertzon 3D FEM

[Hertzon](https://github.com/c8501024/Hertzon) is an open-source 3D
full-wave electromagnetic solver: finite elements in the frequency domain
with adaptive meshing, built on the EMerge core. It models antennas,
filters, waveguides and PCB structures with wave, lumped and Floquet
ports, and has its own window for drawing a model or importing a board
from Gerber files.

eda-agent drives Hertzon as one more [simulation engine](SIMULATION.md),
`hertzon`. A Hertzon project is solved through Hertzon's own command line
(`hertzon-cli`), its S-parameters come back as Touchstone, and they are
checked like every other engine's: passivity, reciprocity, return loss,
transmission, plus the adaptive mesh's convergence. The result feeds the
rest of the toolchain (eyes, SPICE macromodels, Touchstone checks).

## License boundary

Hertzon is GPL-2.0-or-later; eda-agent is Apache-2.0. eda-agent never
imports Hertzon and contains none of its code. It runs `hertzon-cli` as
a separate program and reads the files it writes (JSON and Touchstone),
exactly as it runs openEMS. Hertzon is installed by the user, from
Hertzon's own releases.

## Install

1. Install Hertzon 0.4 or later from its
   [releases page](https://github.com/c8501024/Hertzon/releases/latest):
   on Windows `Hertzon-Setup-<version>.exe` (about 1 GB with its Python
   environment), on Linux the `linux.tar.gz` and `./install.sh`.
2. Check that eda-agent finds it:

   ```bash
   eda-agent features --deep
   ```

   The `hertzon` feature shows **ready** with Hertzon's version.

eda-agent looks for `hertzon-cli` in this order, first match wins:

| Where | Path |
|---|---|
| `EDA_AGENT_HERTZON_CLI` | `hertzon-cli` itself, or a Hertzon folder holding it (directly, or in `.venv\Scripts` / `.venv/bin`) |
| `PATH` | `hertzon-cli` |
| Windows installer | `%LOCALAPPDATA%\Programs\Hertzon\.venv\Scripts\hertzon-cli.exe` |
| Linux installer | `~/.local/share/hertzon/.venv/bin/hertzon-cli` |

For a portable copy or a source checkout, save its location once:

```bash
eda-agent features --set-path EDA_AGENT_HERTZON_CLI=D:\Tools\Hertzon
```

The Features page has the same field. The engine can be switched off
without uninstalling Hertzon (`eda-agent features --off hertzon`).

## Use

Build or open the model in Hertzon's window and save the project
(`.hzn`). Then, from the assistant:

1. **`sim_hertzon_describe(project)`** lists the designs, each with its
   ports (number, kind, reference impedance), its variables (name,
   expression, SI value) and its setups and sweeps.
2. **`sim_hertzon_solve(project, design=..., variables=..., ...)`** starts
   the solve and returns a job id at once.
3. **`sim_job_status(job)`** follows the adaptive passes and returns the
   results when Hertzon has finished.

Example: a hairpin filter with two dimensions changed and a wider band:

```json
sim_hertzon_solve(project="D:/rf/Filter.hzn", design="Hairpin",
                  variables={"L1": "11.2mm", "gap": "0.25mm"},
                  f_start_hz=1.5e9, f_stop_hz=3.5e9, f_points=401)
```

The same problem through the generic job tool:

```json
sim_job_run(kind="hertzon-solve", engines=["hertzon"],
            inputs={"project": "D:/rf/Filter.hzn"},
            spec={"design": "Hairpin", "variables": {"L1": "11.2mm"},
                  "f_start_hz": 1.5e9, "f_stop_hz": 3.5e9, "f_points": 401})
```

### Options

| Spec key (tool argument) | Hertzon option | Meaning |
|---|---|---|
| `design` | `--design` | The design; empty for the only design (or the only one with a setup). |
| `setup` | `--setup` | The analysis setup; empty for the first enabled one. |
| `sweep` | `--sweep` | The sweep; empty for the setup's first enabled sweep, `LastAdaptive` for the adaptive frequency alone. |
| `variables` | `--set NAME=VALUE` | Design or project (`$name`) variables for this run. A value without a unit has the design's unit (`1.2` is 1.2 mm in a design in mm). An unknown variable fails the run before anything is solved. |
| `f_start_hz`, `f_stop_hz`, `f_points` | `--freq START:STOP:COUNT` | A linear band in place of the sweep's frequencies. All three or none. |
| `frequencies_hz` | `--points F1,F2,...` | Single frequencies in place of a band. Not together with a band. |
| `sweep_type` | `--sweep-type` | `discrete`, `interpolating` or `fast`; empty keeps the setup's. |
| `cores` | `--cores` | Solver cores; 0 lets Hertzon choose. |
| `validate` | `--validate` | Run Hertzon's model check (it builds the geometry) before solving. |
| `require_convergence` | `--require-convergence` | Ask Hertzon to treat an unconverged adaptive mesh as an error (exit code 5). The Touchstone file is still written and kept; the convergence verdict fails. |
| `fields` | `--fields` | Keep the field solution in the results folder (large). |
| `project_file` | | With a `.zip` project holding several `.hzn` files: the one to solve. |
| `tolerance` | | Passivity and reciprocity tolerance of the S-parameter checks (default 1e-3). |

Every option is checked before Hertzon starts: an unknown key, a band
with a missing end, a sweep type Hertzon does not have or a variable
name it would not accept is refused with the reason.

### Projects that use other files

A project that refers to other files (an imported STEP or STL body,
Touchstone models of lumped parts, other `.hzn` designs used as blocks)
must travel with them. Zip the project folder and pass the `.zip` as the
project; eda-agent unpacks it in the job's folder and solves the `.hzn`
inside (`project_file` picks one when there are several). A `.hzn` given
alone is copied on its own.

## What comes back

The run lives in the job's folder (see [Jobs](SIMULATION.md#jobs)):

```
hertzon-solve-<hash>/
  inputs/project.hzn                 the project as given
  runs/hertzon/results.json          the normalised results
  runs/hertzon/work/
    project/Filter.hzn               the copy Hertzon solves
    project/Filter.hzn.hznresults/   Hertzon's own results and solver.log
    Hairpin_Setup1_Sweep.s2p         the S-parameters (Touchstone 1.0)
    summary.json                     Hertzon's summary of the run
    progress.jsonl                   Hertzon's progress events
```

The user's own project and its `.hznresults` folder are never touched.

| Metric / verdict | From |
|---|---|
| `ports`, `points`, `f_min_hz`, `f_max_hz` | the Touchstone file |
| `passivity_max_sv`, `passivity_worst_hz`, `reciprocity_max_err`, `worst_return_loss_db`, `s21_at_f_max_db`, `s21_worst_db` | the same S-parameter checks as `touchstone-check` |
| `z0_ohms` | the Touchstone reference impedance |
| `adaptive_passes`, `tetrahedra`, `solve_seconds` | Hertzon's summary |
| verdicts `passivity`, `reciprocity` | the S-parameter checks |
| verdict `convergence` | Hertzon's summary; a failing verdict names the last delta S |

Hertzon's warnings are kept in the notes. A failed run says why in
Hertzon's own words, with the meaning of its exit code (solver failure,
bad input, failed model check), and the end of its log.

The Touchstone file can go straight on to the other tools:
`si_channel_eye` (an eye through the structure), `si_spice_model` (a
passive SPICE macromodel), `sim_read_touchstone`, or a `touchstone-check`
job on the `scikit-rf` engine for an independent second opinion.

## Limits

- eda-agent solves projects made in Hertzon. It does not yet build a
  Hertzon model from an Altium board: export Gerber X2 files with a job
  file (`.gbrjob`) from Altium and use Hertzon's **File > Import PCB**,
  which reads the stackup from the job file.
- Hertzon's command line returns S-parameters. Far fields, fields and
  losses stay in Hertzon's results folder for its window.
- The integration follows Hertzon's documented command-line contract
  (`cli_schema` 1). A different schema is reported as a warning by
  `sim_hertzon_describe`.
