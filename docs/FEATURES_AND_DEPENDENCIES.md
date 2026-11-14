# Features and dependencies

eda-agent's core install needs nothing beyond `python -m pip install -e .`.
It already carries the analysis tools: signal and power integrity, thermal,
the EMI prescan, the placer and router and the calculators. Everything else
is optional: pictures and plots, STEP models, the Rust kernels, the RF
toolbox, the field solvers and other programs some tools drive. One
registry describes all of it, built in or optional, and three views read
it: a command, a dashboard page and an MCP tool.

## In one command

```bash
git clone https://github.com/MOREmbedded/eda-agent && cd eda-agent
python -m pip install -e .
eda-agent install-deps --all       # every optional feature, in one go
```

```bash
eda-agent features                       # what is installed, with versions
eda-agent install-deps render scikit-rf  # or only some
eda-agent install-deps --all --dry-run   # the plan, changing nothing
```

`install-deps` first prints its plan, then asks before it runs anything
(`--yes` skips the question). It installs:

| Kind | How |
|---|---|
| Python package | `python -m pip install` into the Python eda-agent runs in, one call per feature, with the version range `pyproject.toml` gives that feature's extra. A feature that fails does not stop the others. |
| Built from this repository | The Rust wheels (`eda-agent-compute`, the path tracer) are built with `pip install <folder>` when this is a source checkout and `cargo` is on `PATH`. Without Rust it says how to get it, or to use the wheel the CI builds ([Rust compute](RUST_COMPUTE.md)). |
| Own environment | Engines under a copyleft license get a virtual environment of their own under `EDA_AGENT_ENGINES_DIR` (default `%USERPROFILE%\EDA Agent\engines`), so their code never loads into the Apache-2.0 eda-agent process. |
| Built with Rust | pcb-toolkit is a program of its own. With `cargo` on `PATH` it is built from its repository into `<engines folder>\pcb-toolkit`, where eda-agent finds it; without Rust it is a step by hand. |
| Program | openEMS, LTspice, ngspice, KiCad and FreeCAD are applications, not pip packages. The plan lists each one's steps and download link, and `eda-agent features` finds them once they are installed: on `PATH`, in their usual install folders, or wherever you point it (see [Program locations](#program-locations)). |
| Core | The packages every installation has are checked too. When the environment lost one (a broken upgrade, a package removed by hand), `--all` puts it back with the version range `pyproject.toml` gives. |

Every run writes its full output to a log under
`<engines folder>\..\install-logs\`. `--dev` adds the test suite and the
website tools to `--all`.

## The features

Built in, always on, nothing to install:

| Feature | Offers | Guide |
|---|---|---|
| `core` | Altium reading and editing, reviews, ERC and DRC, libraries and parts: every tool not listed elsewhere | [Features](FEATURES.md) |
| `layout` | `pcb_autoplace`, `pcb_autoroute` with the default engine or Euan's (`eda-agent router-use`) | [Router engines](ROUTER_ENGINES.md) |
| `signal-integrity` | `si_line_model`, `si_stackup_plan`, `si_net_channel`, `si_channel_eye`, `si_board_check` and the rest of `si_*` | [Board SI](SI_BOARD.md) |
| `power-integrity` | `sim_pdn_ir_drop`, `sim_pdn_impedance` | [PDN](PDN.md) |
| `thermal` | `sim_thermal_analysis` | [Thermal](THERMAL.md) |
| `emi` | `sim_emc_prescan`, `sim_read_touchstone` | [EMC and RF](EMC_RF.md) |
| `calculators` | `pcb_calc_*`, the `pcb_calc_toolkit` family | [PCB toolkit](PCB_TOOLKIT.md) |

Each card lists the packages it rests on (NumPy, SciPy, PyAMG and so on)
with their versions and licenses, and the optional features that extend
it: the thermal and PDN maps are drawn by `render`, the router runs
faster with `compute`, the SI channels get a second opinion from
`scikit-rf`, `spice` and `openems`.

Optional:

| Feature | Offers | Needs |
|---|---|---|
| `dashboard` | The local web dashboard | Flask (`web` extra) |
| `render` | PNG previews and result plots | Matplotlib (`render` extra) |
| `render3d` | `pcb_render_3d` with the path tracer | eda-agent-render, built from `render-core/` |
| `gerber` | `pcb_check_fab_output` | gerbonara (`gerber` extra; Python 3.12+) |
| `pdf` | Pin tables from datasheet PDFs | pdfplumber (`pdf` extra) |
| `models3d` | Real STEP solids for generated footprints | CadQuery (`models3d` extra) or FreeCAD |
| `compute` | Faster autorouting; off until switched on | eda-agent-compute, built from `rust/eda_agent_compute` |
| `spice` | SPICE runs, the LTspice round trip, simulation engine `spice` (channel eyes) | ngspice or LTspice |
| `openems` | 3D full-wave S-parameters and antennas; simulation engine `openems` | openEMS |
| `scikit-rf` | Simulation engine `scikit-rf` | scikit-rf (`si` extra) |
| `pcb-toolkit` | `pcb_calc_cross_check`, `calc-cross-check` | pcb-toolkit (built with Rust) |
| `kicad` | The KiCad backend, the Altium to KiCad conversion | KiCad |
| `dev`, `site` | The test suite, the website | pytest and plugins; MkDocs |

The authoritative list, with licenses, is `eda-agent features`: it is
read from the registry the installer uses, so it cannot be out of date.

A feature is **ready** when everything it needs is present, **partly
ready** when an optional addition is missing, **not installed** when
something it needs is missing, **not for this machine** when it cannot
exist here (LTspice on Linux, gerbonara on Python 3.11), and **switched
off** when you turned it off.

## Switches

Simulation engines (`openems`, `scikit-rf`, `spice`) can be switched off
without uninstalling them, and the Rust kernels (`compute`), which are off
until asked for, can be switched on:

```bash
eda-agent features --off openems
eda-agent features --on openems
eda-agent features --on compute      # same as EDA_AGENT_COMPUTE=auto, for good
```

An engine that is off is not offered for simulation jobs. Switching
`compute` on sets `EDA_AGENT_COMPUTE=auto` for every later session; a value
you set in the environment yourself still decides, and the Features page
shows which one is in force.

## Program locations

eda-agent looks for each program on `PATH` and in its usual install
folders. When it is somewhere else, point eda-agent at it once and every
later session uses it:

```bash
eda-agent features --set-path EDA_AGENT_OPENEMS_DIR=F:\openEMS\openEMS
eda-agent features --set-path EDA_AGENT_OPENEMS_PYTHON=F:\openEMS\pyenv\Scripts\python.exe
eda-agent features --set-path EDA_AGENT_NGSPICE=C:\Tools\Spice64
eda-agent features --clear-path EDA_AGENT_NGSPICE
```

The Features page has the same fields under each program's **How to
install** (or **Location**, once found). The variables are the ones in
[Options](OPTIONS.md): `EDA_AGENT_OPENEMS_DIR`, `EDA_AGENT_OPENEMS_PYTHON`,
`EDA_AGENT_NGSPICE`, `EDA_AGENT_LTSPICE_EXE`, `EDA_AGENT_KICAD_CLI`,
`EDA_AGENT_FREECADCMD` and `EDA_AGENT_PCB_TOOLKIT`. A saved location is a
default: the same variable set in the environment wins. A path that does
not exist is refused, and when a variable names a place where nothing
usable is found, the report says so instead of only "missing".

A program that is found but too old to work is reported as **wrong
version** with the reason: eda-agent needs ngspice 30 or later, and the
ngspice 26 that EAGLE ships ignores a deck's `.control` block in batch
mode, so its runs do nothing.

Switches and locations live in `%USERPROFILE%\.eda-agent\features.json`
(`EDA_AGENT_FEATURES_FILE` names another file).

## The dashboard page

**Features** in the dashboard's top bar shows the same registry as cards,
one per feature, grouped by what they are for: the state, what the
feature offers, its tools, the features that extend it, each dependency
with its status, version and license, and for a program that is missing,
how to install it and where to point eda-agent at it.

- **Filters** show everything, the built-in features, what is installed,
  what is still to install, or what is switched off.
- **Install** on a card shows the plan first and runs it on a second
  click, with pip's output streaming into the page. Tick several cards and
  **Install selected** runs them as one plan; **Install everything
  missing** does the same for all of them.
- **On** switches an engine or the Rust kernels.
- **All dependencies**, at the foot of the page, lists every package and
  program in one table, with the features that use it.
- **Check thoroughly** also asks openEMS and the programs for their
  versions, which takes a few seconds.

Installing into a running eda-agent is safe for new packages. A package
that was already loaded (an upgrade) is picked up after eda-agent
restarts.

## For the AI

`app_features` returns the registry with every state and version, so an
assistant that meets a missing package can say which feature it belongs
to. `app_install_features` returns the plan, and installs only when
called again with `confirm=True` after the user agreed.

## Adding a feature from another package

A package adds features through the `eda_agent.features` entry-point
group. The entry point names a callable that returns a
`eda_agent.features.catalog.Plugin`, or a list of them:

```toml
[project.entry-points."eda_agent.features"]
my_solver = "my_package.eda:features"
```

```python
from eda_agent.features.catalog import Dependency, Feature, Plugin, PYTHON

def features():
    dep = Dependency("my-solver-lib", "My solver", PYTHON, "MIT", dist="my-solver-lib")
    feat = Feature("my-solver", "My solver", "Field solvers and signal integrity",
                   "What it does, in one sentence.", needs=("my-solver-lib",),
                   switchable=True)
    return Plugin(feat, [dep])
```

Ids are lower case. A plugin that reuses an id, names a dependency
nobody declares, or leaves out a license is not loaded, and the reason is
shown on the Features page and by `eda-agent features`. A plugin that
raises is reported the same way; it never hides the rest.
