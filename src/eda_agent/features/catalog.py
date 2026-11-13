# SPDX-License-Identifier: Apache-2.0
"""What eda-agent can do and what each part of it needs.

One table for everything optional: the pip extras in ``pyproject.toml``,
the programs some tools drive (openEMS, LTspice, ngspice, KiCad, FreeCAD,
pcb-toolkit), the compiled wheels built from this repository, and the
simulation engines that run in their own environment. The installer
(``eda-agent install-deps``), the status report (``eda-agent features``),
the dashboard's Features page and the ``sys_features`` tool all read it,
so a dependency is described once.

A **feature** is something a user would turn on: "3D STEP models", "EMI
and RF field solving". It lists the **dependencies** it needs. A
dependency is one installable thing: a Python distribution, a program, a
wheel built from a folder of this repository, or a package installed into
a separate virtual environment (how GPL tools are kept out of this
Apache-2.0 process).

Pip requirement strings are not repeated here for the extras: they come
from the installed package's own metadata (``Requires-Dist``), which is
generated from ``pyproject.toml``. A test checks that every extra there
belongs to a feature here, so the two cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Dependency kinds.
PYTHON = "python"      # a distribution installed into this Python
LOCAL = "local"        # a wheel built from a folder of this repository
VENV = "venv"          # packages in a separate virtual environment
PROGRAM = "program"    # an application found on this machine

KINDS = (PYTHON, LOCAL, VENV, PROGRAM)


@dataclass(frozen=True)
class Dependency:
    id: str
    title: str
    kind: str
    license: str
    url: str = ""
    #: Distribution name (PYTHON, LOCAL, VENV), as pip and
    #: importlib.metadata know it.
    dist: str = ""
    #: pip requirement when no extra carries one (VENV and new packages).
    requirement: str = ""
    #: LOCAL: the folder in this repository that pip builds from.
    source: str = ""
    #: VENV: the environment's folder name under the engines folder.
    venv: str = ""
    #: Lowest Python that can install it, as (major, minor).
    python_min: tuple[int, int] = ()
    #: Platforms it exists on (``sys.platform`` values); empty means any.
    platforms: tuple[str, ...] = ()
    #: Rough download, for the installer's summary.
    size_mb: float = 0.0
    #: Steps for a person, when the installer cannot do it alone.
    manual: str = ""
    #: Environment variables that point eda-agent at it.
    env: tuple[str, ...] = ()
    #: One line on why it is needed or how it is used.
    note: str = ""

    @property
    def runs_apart(self) -> bool:
        """Copyleft code never loads into this process."""
        return self.kind in (VENV, PROGRAM)


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    group: str
    summary: str
    #: What a designer gets, one line each.
    offers: tuple[str, ...] = ()
    #: Dependencies that must all be present.
    needs: tuple[str, ...] = ()
    #: Groups of alternatives: one of each group is enough.
    any_of: tuple[tuple[str, ...], ...] = ()
    #: Dependencies that add to it but are not needed.
    adds: tuple[str, ...] = ()
    #: The pip extra that installs ``needs`` (pyproject.toml).
    extra: str = ""
    #: MCP tools or commands that depend on it.
    tools: tuple[str, ...] = ()
    docs: str = ""
    #: Always on, nothing to install or switch.
    core: bool = False
    #: Development only; left out of ``--all`` unless asked.
    dev: bool = False
    #: The user may switch it off (an engine that should not be offered).
    switchable: bool = False

    def all_deps(self) -> tuple[str, ...]:
        seen: list[str] = []
        for d in (*self.needs, *(x for g in self.any_of for x in g), *self.adds):
            if d not in seen:
                seen.append(d)
        return tuple(seen)


GROUPS = (
    "Core",
    "Views and pictures",
    "Manufacturing files",
    "3D models",
    "Speed",
    "Circuit simulation",
    "Field solvers and signal integrity",
    "Other EDA programs",
    "Development",
)


DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("flask", "Flask", PYTHON, "BSD-3-Clause",
               "https://flask.palletsprojects.com", dist="flask",
               size_mb=1, note="the local web server behind the dashboard"),
    Dependency("matplotlib", "Matplotlib", PYTHON, "Matplotlib (PSF-style, permissive)",
               "https://matplotlib.org", dist="matplotlib", size_mb=40,
               note="draws the PNG previews and result plots"),
    Dependency("eda-agent-render", "eda-agent path tracer", LOCAL, "Apache-2.0",
               dist="eda-agent-render", source="render-core", size_mb=5,
               note="compiled from render-core/ with Rust"),
    Dependency("gerbonara", "gerbonara", PYTHON, "Apache-2.0",
               "https://gitlab.com/gerbolyze/gerbonara", dist="gerbonara",
               python_min=(3, 12), size_mb=10),
    Dependency("pdfplumber", "pdfplumber", PYTHON, "MIT",
               "https://github.com/jsvine/pdfplumber", dist="pdfplumber", size_mb=5),
    Dependency("cadquery", "CadQuery", PYTHON, "Apache-2.0",
               "https://cadquery.readthedocs.io", dist="cadquery", size_mb=400,
               note="large: it brings the OpenCascade kernel"),
    Dependency("freecad", "FreeCAD", PROGRAM, "LGPL-2.1+",
               "https://www.freecad.org/downloads.php",
               manual="Install FreeCAD from freecad.org. eda-agent finds "
                      "freecadcmd on PATH.",
               note="used for STEP models when CadQuery is not installed"),
    Dependency("eda-agent-compute", "eda-agent Rust compute", LOCAL, "Apache-2.0",
               dist="eda-agent-compute", source="rust/eda_agent_compute", size_mb=2,
               env=("EDA_AGENT_COMPUTE",),
               note="compiled from rust/eda_agent_compute with Rust; turn it on "
                    "with EDA_AGENT_COMPUTE=auto"),
    Dependency("ngspice", "ngspice", PROGRAM, "BSD-3-Clause",
               "https://ngspice.sourceforge.io/download.html",
               manual="Windows: download the ngspice zip from "
                      "ngspice.sourceforge.io and put its bin folder on PATH "
                      "(or `scoop install ngspice`). Linux: install the ngspice "
                      "package."),
    Dependency("ltspice", "LTspice", PROGRAM, "Freeware (Analog Devices)",
               "https://www.analog.com/ltspice", platforms=("win32", "darwin"),
               env=("EDA_AGENT_LTSPICE_EXE",),
               manual="Install LTspice from analog.com/ltspice. eda-agent "
                      "finds the usual install folders, or set "
                      "EDA_AGENT_LTSPICE_EXE."),
    Dependency("openems", "openEMS", PROGRAM, "GPL-3.0 (runs as its own process)",
               "https://github.com/thliebig/openEMS-Project/releases",
               env=("EDA_AGENT_OPENEMS_DIR", "EDA_AGENT_OPENEMS_PYTHON"),
               manual="Windows: unzip the openEMS release (it carries the "
                      "Python bindings) and set EDA_AGENT_OPENEMS_DIR to that "
                      "folder; set EDA_AGENT_OPENEMS_PYTHON to a Python that "
                      "can `import openEMS`. Linux: build openEMS-Project with "
                      "its Python bindings."),
    Dependency("scikit-rf", "scikit-rf", PYTHON, "BSD-3-Clause",
               "https://scikit-rf.readthedocs.io", dist="scikit-rf",
               requirement="scikit-rf>=1.0,<3", size_mb=5,
               note="network algebra, Touchstone, vector fitting, a second "
                    "opinion on S-parameters"),
    Dependency("pcb-toolkit", "pcb-toolkit", PROGRAM, "MIT OR Apache-2.0",
               "https://github.com/akiselev/pcb-toolkit",
               env=("EDA_AGENT_PCB_TOOLKIT",),
               manual="Build it with Rust: git clone "
                      "https://github.com/akiselev/pcb-toolkit, cargo build "
                      "--release --workspace, then put the binary on PATH or "
                      "point EDA_AGENT_PCB_TOOLKIT at it."),
    Dependency("kicad", "KiCad", PROGRAM, "GPL-3.0 (runs as its own program)",
               "https://www.kicad.org/download/",
               manual="Install KiCad 8 or later from kicad.org. eda-agent "
                      "drives kicad-cli and KiCad's API server."),
    Dependency("pytest", "pytest and plugins", PYTHON, "MIT", "https://pytest.org",
               dist="pytest", size_mb=5),
    Dependency("pytest-asyncio", "pytest-asyncio", PYTHON, "Apache-2.0",
               dist="pytest-asyncio"),
    Dependency("pytest-xdist", "pytest-xdist", PYTHON, "MIT", dist="pytest-xdist"),
    Dependency("mkdocs", "MkDocs", PYTHON, "BSD-2-Clause", dist="mkdocs", size_mb=5),
    Dependency("mkdocs-material", "Material for MkDocs", PYTHON, "MIT",
               dist="mkdocs-material", size_mb=15),
)


FEATURES: tuple[Feature, ...] = (
    Feature("core", "Altium bridge and the design tools", "Core",
            "Everything the MCP server does with no extra installed: reading and "
            "editing schematics and boards in Altium, reviews, rules, libraries, "
            "the placer and router, the calculators, PDN and EMI checks.",
            offers=("All MCP tools that are not listed under another feature",),
            core=True, docs="docs/FEATURES.md"),
    Feature("dashboard", "Dashboard", "Views and pictures",
            "The local web dashboard: board view, review, library, workspace, "
            "calculators, features and simulation.",
            offers=("eda-agent dashboard",), needs=("flask",), extra="web",
            tools=("dashboard",), docs="docs/FEATURES.md"),
    Feature("render", "Pictures and plots", "Views and pictures",
            "PNG previews of design steps and the plots of PDN, thermal, EMI "
            "and simulation results.",
            offers=("render_png previews", "result plots (S-parameters, maps, curves)"),
            needs=("matplotlib",), extra="render"),
    Feature("render3d", "Raytraced board pictures", "Views and pictures",
            "pcb_render_3d draws the board with eda-agent's own path tracer "
            "instead of flat shading.",
            offers=("pcb_render_3d, raytraced",), needs=("eda-agent-render",),
            extra="render3d", tools=("pcb_render_3d",)),
    Feature("gerber", "Fab output read-back", "Manufacturing files",
            "Reads an exported Gerber and drill package back and checks it "
            "against the board.",
            offers=("pcb_check_fab_output",), needs=("gerbonara",), extra="gerber",
            tools=("pcb_check_fab_output",)),
    Feature("pdf", "Datasheet pin tables", "Manufacturing files",
            "Pulls pin tables out of datasheet PDFs for symbol checks.",
            offers=("pin tables from a datasheet PDF",), needs=("pdfplumber",),
            extra="pdf"),
    Feature("models3d", "STEP models from drawings", "3D models",
            "Builds a STEP body from the package drawing. Without either "
            "kernel the body is a plain extrusion and the reply says so.",
            offers=("real STEP solids for generated footprints",),
            any_of=(("cadquery", "freecad"),), extra="models3d"),
    Feature("compute", "Rust compute", "Speed",
            "Compiled kernels: the routing search runs about 15 times faster. "
            "Python stays the default and the reference.",
            offers=("faster autorouting (EDA_AGENT_COMPUTE=auto)",),
            needs=("eda-agent-compute",), tools=("compute-status",),
            docs="docs/RUST_COMPUTE.md"),
    Feature("spice", "SPICE simulation", "Circuit simulation",
            "Runs SPICE decks built from the schematic, and the LTspice round "
            "trip.",
            offers=("sim_run_* with ngspice", "eng_ltspice_* with LTspice"),
            any_of=(("ngspice", "ltspice"),), docs="docs/SPICE_MODELS.md"),
    Feature("openems", "3D field solver (openEMS)", "Field solvers and signal integrity",
            "Full-wave FDTD: S-parameters of nets and vias, antenna patterns. "
            "Runs as its own process.",
            offers=("sim_em_build_model / sim_em_run / sim_em_results",
                    "simulation engine `openems`"),
            needs=("openems",), tools=("sim_em_run",), docs="docs/EMC_RF.md",
            switchable=True),
    Feature("scikit-rf", "RF network toolbox (scikit-rf)", "Field solvers and signal integrity",
            "Touchstone and network algebra from the scikit-rf project; a "
            "second opinion on every S-parameter check.",
            offers=("simulation engine `scikit-rf`",), needs=("scikit-rf",), extra="si",
            docs="docs/SIMULATION.md", switchable=True),
    Feature("pcb-toolkit", "Calculator cross-check (pcb-toolkit)", "Other EDA programs",
            "A second, independent implementation of the PCB calculators.",
            offers=("calc-cross-check",), needs=("pcb-toolkit",),
            tools=("calc-cross-check",)),
    Feature("kicad", "KiCad backend", "Other EDA programs",
            "Runs the same tools against KiCad instead of Altium.",
            offers=("--backend kicad", "kicad-cli exports"), needs=("kicad",),
            extra="kicad", docs="docs/BACKENDS.md"),
    Feature("dev", "Test suite", "Development",
            "What the test suite needs.", needs=("pytest", "pytest-asyncio",
                                                 "pytest-xdist", "matplotlib", "flask",
                                                 "scikit-rf"),
            extra="dev", dev=True),
    Feature("site", "Product website", "Development",
            "Builds the documentation website locally.",
            needs=("mkdocs", "mkdocs-material"), extra="site", dev=True),
)


_DEPS = {d.id: d for d in DEPENDENCIES}
_FEATURES = {f.id: f for f in FEATURES}


def dependency(dep_id: str) -> Dependency:
    try:
        return _DEPS[dep_id]
    except KeyError:
        raise KeyError(f"unknown dependency {dep_id!r}") from None


def feature(feature_id: str) -> Feature:
    try:
        return _FEATURES[feature_id]
    except KeyError:
        known = ", ".join(sorted(_FEATURES))
        raise KeyError(f"unknown feature {feature_id!r}; known: {known}") from None


def features(include_dev: bool = True) -> list[Feature]:
    return [f for f in FEATURES if include_dev or not f.dev]


def users_of(dep_id: str) -> list[str]:
    return [f.id for f in FEATURES if dep_id in f.all_deps()]


@dataclass
class Plugin:
    """A feature and its dependencies contributed by another package
    through the ``eda_agent.features`` entry-point group."""
    feature: Feature
    dependencies: list[Dependency] = field(default_factory=list)
    origin: str = ""
