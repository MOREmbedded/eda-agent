# SPDX-License-Identifier: Apache-2.0
"""What eda-agent can do and what each part of it needs.

One table for everything eda-agent can do, built in or optional: the
core packages and the pip extras in ``pyproject.toml``,
the programs some tools drive (openEMS, LTspice, ngspice, KiCad, FreeCAD,
pcb-toolkit), the compiled wheels built from this repository, and the
simulation engines that run in their own environment. The installer
(``eda-agent install-deps``), the status report (``eda-agent features``),
the dashboard's Features page and the ``app_features`` tool all read it,
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
    #: Environment variables that point eda-agent at it. The first one is
    #: the one the Features page and ``eda-agent features --set-path``
    #: save, so it names the program or its folder.
    env: tuple[str, ...] = ()
    #: PROGRAM built with Rust: the arguments after ``cargo install``
    #: (a crate, or ``--git URL crate``). The installer builds it into
    #: ``<engines folder>/<id>`` when cargo is present.
    cargo: tuple[str, ...] = ()
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
    #: Whether a switchable feature is on until the user says otherwise.
    #: The Rust kernels are off by default: Python stays the reference.
    default_on: bool = True
    #: A switch that works through an environment variable:
    #: (variable, value when on, value when off). Switching on sets the
    #: variable for this process and every later one (see state.apply);
    #: a value the user set in the environment decides instead.
    switch_env: tuple[str, ...] = ()
    #: Other features that extend this one (an engine for its jobs, a
    #: faster kernel): shown beside it, never required.
    related: tuple[str, ...] = ()

    def all_deps(self) -> tuple[str, ...]:
        seen: list[str] = []
        for d in (*self.needs, *(x for g in self.any_of for x in g), *self.adds):
            if d not in seen:
                seen.append(d)
        return tuple(seen)


GROUPS = (
    "Core",
    "Analysis",
    "Views and pictures",
    "Manufacturing files",
    "3D models",
    "Speed",
    "Circuit simulation",
    "Field solvers and signal integrity",
    "Other EDA programs",
    "Development",
)


#: The packages every installation has: ``[project] dependencies`` in
#: pyproject.toml. Listed so the Features page shows their versions and
#: licenses, and so ``install-deps`` can repair an environment that lost
#: one. A test keeps this list equal to pyproject.toml.
CORE_DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("mcp", "MCP Python SDK", PYTHON, "MIT",
               "https://github.com/modelcontextprotocol/python-sdk", dist="mcp",
               note="the protocol the assistant talks to eda-agent in"),
    Dependency("anyio", "AnyIO", PYTHON, "MIT", "https://anyio.readthedocs.io",
               dist="anyio"),
    Dependency("pydantic", "Pydantic", PYTHON, "MIT", "https://docs.pydantic.dev",
               dist="pydantic", note="tool arguments and replies"),
    Dependency("psutil", "psutil", PYTHON, "BSD-3-Clause",
               "https://github.com/giampaolo/psutil", dist="psutil",
               note="finds Altium and watches the solver processes"),
    Dependency("pywin32", "pywin32", PYTHON, "PSF-2.0",
               "https://github.com/mhammond/pywin32", dist="pywin32",
               platforms=("win32",), note="windows, the clipboard and COM on Windows"),
    Dependency("comtypes", "comtypes", PYTHON, "MIT",
               "https://github.com/enthought/comtypes", dist="comtypes",
               platforms=("win32",), note="reads Altium's dialogs (MSAA, UI Automation)"),
    Dependency("pillow", "Pillow", PYTHON, "MIT-CMU", "https://python-pillow.org",
               dist="pillow", note="screen captures and footprint pictures"),
    Dependency("networkx", "NetworkX", PYTHON, "BSD-3-Clause", "https://networkx.org",
               dist="networkx", note="net graphs for the placer, router and reviews"),
    Dependency("numpy", "NumPy", PYTHON, "BSD-3-Clause", "https://numpy.org",
               dist="numpy", note="every solver's arrays"),
    Dependency("scipy", "SciPy", PYTHON, "BSD-3-Clause", "https://scipy.org",
               dist="scipy", note="sparse solves for layout, PDN, thermal and SI"),
    Dependency("pyamg", "PyAMG", PYTHON, "MIT", "https://github.com/pyamg/pyamg",
               dist="pyamg", note="the multigrid preconditioner of the thermal solver"),
    Dependency("threadpoolctl", "threadpoolctl", PYTHON, "BSD-3-Clause",
               "https://github.com/joblib/threadpoolctl", dist="threadpoolctl",
               note="keeps parallel SI solves from oversubscribing the cores"),
    Dependency("pypdf", "pypdf", PYTHON, "BSD-3-Clause", "https://pypdf.readthedocs.io",
               dist="pypdf", note="text search in datasheets"),
    Dependency("pypdfium2", "pypdfium2", PYTHON, "Apache-2.0 OR BSD-3-Clause",
               "https://github.com/pypdfium2-team/pypdfium2", dist="pypdfium2",
               note="renders datasheet figures"),
    Dependency("olefile", "olefile", PYTHON, "BSD-2-Clause",
               "https://github.com/decalage2/olefile", dist="olefile",
               note="reads Altium files without Altium running"),
    Dependency("xlrd", "xlrd", PYTHON, "BSD-3-Clause", "https://github.com/python-excel/xlrd",
               dist="xlrd", note="Altium's IPC footprint batch templates (.xls)"),
    Dependency("certifi", "certifi", PYTHON, "MPL-2.0",
               "https://github.com/certifi/python-certifi", dist="certifi",
               note="certificates for downloading fab data"),
    Dependency("packaging", "packaging", PYTHON, "Apache-2.0 OR BSD-2-Clause",
               "https://packaging.pypa.io", dist="packaging",
               note="version checks of the optional parts"),
)


DEPENDENCIES: tuple[Dependency, ...] = CORE_DEPENDENCIES + (
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
               env=("EDA_AGENT_FREECADCMD",),
               manual="Install FreeCAD from freecad.org. eda-agent finds "
                      "freecadcmd on PATH and in the usual install folders, or "
                      "set EDA_AGENT_FREECADCMD to it.",
               note="used for STEP models when CadQuery is not installed"),
    Dependency("eda-agent-compute", "eda-agent Rust compute", LOCAL, "Apache-2.0",
               dist="eda-agent-compute", source="rust/eda_agent_compute", size_mb=2,
               env=("EDA_AGENT_COMPUTE",),
               note="compiled from rust/eda_agent_compute with Rust; turn it on "
                    "with EDA_AGENT_COMPUTE=auto"),
    Dependency("ngspice", "ngspice", PROGRAM, "BSD-3-Clause",
               "https://ngspice.sourceforge.io/download.html",
               env=("EDA_AGENT_NGSPICE",),
               manual="eda-agent needs ngspice 30 or later; the copy EAGLE ships "
                      "(ngspice 26) is too old. Windows: unzip the ngspice release from "
                      "ngspice.sourceforge.io (to C:\\Spice64 or Program Files, "
                      "where eda-agent looks), or `scoop install ngspice`, or "
                      "set EDA_AGENT_NGSPICE to ngspice_con.exe. Linux: install "
                      "the ngspice package."),
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
                      "can `import openEMS`. Save both once on the Features "
                      "page or with `eda-agent features --set-path`. Linux: "
                      "build openEMS-Project with its Python bindings."),
    Dependency("scikit-rf", "scikit-rf", PYTHON, "BSD-3-Clause",
               "https://scikit-rf.readthedocs.io", dist="scikit-rf",
               requirement="scikit-rf>=1.0,<3", size_mb=5,
               note="network algebra, Touchstone, vector fitting, a second "
                    "opinion on S-parameters"),
    Dependency("pcb-toolkit", "pcb-toolkit", PROGRAM, "MIT OR Apache-2.0",
               "https://github.com/akiselev/pcb-toolkit",
               env=("EDA_AGENT_PCB_TOOLKIT",),
               cargo=("--git", "https://github.com/akiselev/pcb-toolkit", "pcb-toolkit-cli"),
               size_mb=1,
               manual="Built from source with Rust: install Rust from rustup.rs "
                      "(on Windows also the Visual Studio C++ build tools) and "
                      "run the installer again, which builds it into the engines "
                      "folder. Or build it yourself and point "
                      "EDA_AGENT_PCB_TOOLKIT at the binary.",
               note="a separate Rust program; built with cargo, never linked"),
    Dependency("kicad", "KiCad", PROGRAM, "GPL-3.0 (runs as its own program)",
               "https://www.kicad.org/download/",
               env=("EDA_AGENT_KICAD_CLI",),
               manual="Install KiCad 8 or later from kicad.org. eda-agent "
                      "drives kicad-cli and KiCad's API server, and finds "
                      "kicad-cli on PATH and in Program Files\\KiCad, or set "
                      "EDA_AGENT_KICAD_CLI to it."),
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
            "What every installation does: reading and editing schematics and "
            "boards in Altium, reviews, ERC and DRC, rules, libraries and parts, "
            "and every tool not listed under another feature.",
            offers=("Schematic and board reading and editing in Altium",
                    "Design review, ERC and DRC, rule checks",
                    "Libraries, part search and footprint generation"),
            needs=("mcp", "anyio", "pydantic", "psutil", "pywin32", "comtypes",
                   "pillow", "networkx", "numpy", "olefile", "xlrd", "pypdf",
                   "pypdfium2", "certifi", "packaging"),
            tools=("review_design", "run_drc", "run_erc"),
            core=True, docs="docs/FEATURES.md"),
    Feature("layout", "Placer and router", "Analysis",
            "Automatic placement and routing of the board, with the result "
            "checked before it is written to Altium. Two routing engines: this "
            "project's own, and Euan's alternative router, switched with "
            "`eda-agent router-use`.",
            offers=("pcb_autoplace with incremental scoring",
                    "pcb_autoroute, default or Euan's engine",
                    "Route plans and repair plans"),
            needs=("numpy", "scipy", "networkx"),
            tools=("pcb_autoplace", "pcb_autoroute", "route_plan"),
            related=("compute",), core=True, docs="docs/ROUTER_ENGINES.md"),
    Feature("signal-integrity", "Signal integrity", "Analysis",
            "Lines, nets and channels from the stackup to the eye: RLGC with "
            "surface roughness, routed-net S-parameters and TDR, IBIS eyes, and "
            "a whole-board check whose findings are marked in Altium.",
            offers=("si_line_model and si_stackup_plan: impedance and loss per layer",
                    "si_net_channel: S-parameters and TDR of a routed net",
                    "si_channel_eye: eye diagrams, with IBIS drivers",
                    "si_board_check: findings placed on the board"),
            needs=("numpy", "scipy", "threadpoolctl"),
            tools=("si_line_model", "si_stackup_plan", "si_board_snapshot",
                   "si_net_channel", "si_channel_eye", "si_spice_model",
                   "si_ibis_models", "si_board_check"),
            related=("scikit-rf", "spice", "openems"), core=True,
            docs="docs/SI_BOARD.md"),
    Feature("power-integrity", "Power integrity (PDN)", "Analysis",
            "DC IR drop over the copper of each rail and the PDN impedance "
            "against its target, with the decoupling read from the design.",
            offers=("sim_pdn_ir_drop: voltage drop and current density maps",
                    "sim_pdn_impedance: impedance of the rail against its target"),
            needs=("numpy", "scipy"),
            tools=("sim_pdn_ir_drop", "sim_pdn_impedance", "pcb_calc_pdn_target"),
            related=("render",), core=True, docs="docs/PDN.md"),
    Feature("thermal", "Thermal analysis", "Analysis",
            "Steady-state board temperatures: 3D conduction through copper and "
            "laminate, convection and radiation, junction temperatures of the "
            "parts.",
            offers=("sim_thermal_analysis: temperature map and Tj per part",),
            needs=("numpy", "scipy", "pyamg"),
            tools=("sim_thermal_analysis",), related=("render",), core=True,
            docs="docs/THERMAL.md"),
    Feature("emi", "EMI prescan", "Analysis",
            "Fast EMC checks of the routed board with no external program: "
            "tracks over plane splits, fast nets near the edge, layer changes "
            "without a return via, and radiated estimates against CISPR and FCC "
            "limits. openEMS gives the full-wave answer.",
            offers=("sim_emc_prescan: ranked EMC risks of the fast nets",
                    "Touchstone reading and checks (sim_read_touchstone)"),
            needs=("numpy",),
            tools=("sim_emc_prescan", "sim_read_touchstone"),
            related=("openems", "scikit-rf"), core=True, docs="docs/EMC_RF.md"),
    Feature("calculators", "PCB calculators", "Analysis",
            "Impedance, current capacity, vias, fusing, spacing, padstacks and "
            "more, each with its model and accuracy, also on the dashboard's "
            "Calculators page.",
            offers=("pcb_calc_* and the pcb_calc_toolkit family",),
            needs=("numpy", "scipy"),
            tools=("pcb_calc", "pcb_calc_toolkit", "pcb_calc_impedance"),
            related=("pcb-toolkit",), core=True, docs="docs/PCB_TOOLKIT.md"),
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
            "Python stays the default and the reference; switch it on here or "
            "with EDA_AGENT_COMPUTE=auto.",
            offers=("faster autorouting: the grid search in compiled code",
                    "the same answers as Python, checked by a parity test"),
            needs=("eda-agent-compute",), tools=("compute-status",),
            docs="docs/RUST_COMPUTE.md", switchable=True, default_on=False,
            switch_env=("EDA_AGENT_COMPUTE", "auto", "python")),
    Feature("spice", "SPICE simulation", "Circuit simulation",
            "Runs SPICE decks built from the schematic, the LTspice round trip, "
            "and the signal-integrity eye through a circuit simulator. Runs as "
            "its own process.",
            offers=("sim_run and sim_run_deck with ngspice or LTspice",
                    "eng_ltspice_*: export, run and compare with LTspice",
                    "simulation engine `spice`: channel eyes in a circuit simulator"),
            any_of=(("ngspice", "ltspice"),),
            tools=("sim_run", "sim_run_deck", "eng_ltspice_run", "si_spice_model"),
            docs="docs/SPICE_MODELS.md", switchable=True),
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
            offers=("pcb_calc_cross_check: every calculator against a second implementation",),
            needs=("pcb-toolkit",), tools=("pcb_calc_cross_check", "calc-cross-check"),
            docs="docs/PCB_TOOLKIT.md"),
    Feature("kicad", "KiCad backend", "Other EDA programs",
            "Runs the same tools against KiCad instead of Altium.",
            offers=("--backend kicad", "kicad-cli exports",
                    "Altium to KiCad conversion, checked with KiCad's own ERC and DRC"),
            needs=("kicad",),
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
