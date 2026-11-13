# Features and dependencies

eda-agent's core install needs nothing beyond `python -m pip install -e .`.
Everything else is optional: pictures and plots, STEP models, the Rust
wheels, the RF toolbox, the field solvers and other programs some tools
drive. One registry describes all of it, and three views read it: a
command, a dashboard page and an MCP tool.

## In one command

```bash
eda-agent features                 # what is installed, with versions
eda-agent install-deps --all       # install every optional feature
eda-agent install-deps render si   # or only some
eda-agent install-deps --all --dry-run   # the plan, changing nothing
```

`install-deps` first prints its plan, then asks before it runs anything
(`--yes` skips the question). It installs:

| Kind | How |
|---|---|
| Python package | `python -m pip install` into the Python eda-agent runs in, one call per feature, with the version range `pyproject.toml` gives that feature's extra. A feature that fails does not stop the others. |
| Built from this repository | The Rust wheels (`eda-agent-compute`, the path tracer) are built with `pip install <folder>` when this is a source checkout and `cargo` is on `PATH`. Without Rust it says how to get it, or to use the wheel the CI builds ([Rust compute](RUST_COMPUTE.md)). |
| Own environment | Engines under a copyleft license get a virtual environment of their own under `EDA_AGENT_ENGINES_DIR` (default `%USERPROFILE%\EDA Agent\engines`), so their code never loads into the Apache-2.0 eda-agent process. |
| Program | openEMS, LTspice, ngspice, KiCad, FreeCAD and pcb-toolkit are applications, not pip packages. The plan lists each one's steps and download link, and `eda-agent features` finds them once they are installed. |

Every run writes its full output to a log under
`<engines folder>\..\install-logs\`. `--dev` adds the test suite and the
website tools to `--all`.

## The features

| Feature | Offers | Needs |
|---|---|---|
| `core` | Every MCP tool not listed below | nothing more |
| `dashboard` | The local web dashboard | Flask (`web` extra) |
| `render` | PNG previews and result plots | Matplotlib (`render` extra) |
| `render3d` | `pcb_render_3d` with the path tracer | eda-agent-render, built from `render-core/` |
| `gerber` | `pcb_check_fab_output` | gerbonara (`gerber` extra; Python 3.12+) |
| `pdf` | Pin tables from datasheet PDFs | pdfplumber (`pdf` extra) |
| `models3d` | Real STEP solids for generated footprints | CadQuery (`models3d` extra) or FreeCAD |
| `compute` | Faster autorouting | eda-agent-compute, built from `rust/eda_agent_compute` |
| `spice` | SPICE runs and the LTspice round trip | ngspice or LTspice |
| `openems` | 3D full-wave S-parameters and antennas; simulation engine `openems` | openEMS |
| `scikit-rf` | Simulation engine `scikit-rf` | scikit-rf (`si` extra) |
| `pcb-toolkit` | `calc-cross-check` | pcb-toolkit |
| `kicad` | The KiCad backend | KiCad |
| `dev`, `site` | The test suite, the website | pytest and plugins; MkDocs |

The authoritative list, with licenses, is `eda-agent features`: it is
read from the registry the installer uses, so it cannot be out of date.

A feature is **ready** when everything it needs is present, **partly
ready** when an optional addition is missing, **not installed** when
something it needs is missing, **not for this machine** when it cannot
exist here (LTspice on Linux, gerbonara on Python 3.11), and **switched
off** when you turned it off.

## Switching engines off

Simulation engines can be switched off without uninstalling them:

```bash
eda-agent features --off openems
eda-agent features --on openems
```

An engine that is off is not offered for simulation jobs. The switches
live in `%USERPROFILE%\.eda-agent\features.json`
(`EDA_AGENT_FEATURES_FILE` names another file).

## The dashboard page

**Features** in the dashboard's top bar shows the same registry as cards,
one per feature, grouped by what they are for: the state, what the
feature offers, each dependency with its status, version and license,
and for a program that is missing, how to install it. **Install** shows
the plan first and runs it on a second click, with pip's output streaming
into the page; **Install everything missing** does the same for all of
them. **Check thoroughly** also asks openEMS and the programs for their
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
