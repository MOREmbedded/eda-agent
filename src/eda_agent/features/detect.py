# SPDX-License-Identifier: Apache-2.0
"""Is each dependency here, which version, and is each feature ready.

Detection is cheap by default: package metadata is read without importing
anything, and programs are looked for on PATH, in their usual install
folders and in the environment variables that name them. ``deep=True``
also runs the slower probes (asking openEMS's Python to import openEMS),
which can take seconds.
"""
from __future__ import annotations

import importlib.metadata as md
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import catalog as cat
from .catalog import LOCAL, PROGRAM, PYTHON, VENV, Dependency, Feature

#: Dependency states.
PRESENT = "present"
MISSING = "missing"
OUTDATED = "outdated"          # installed, outside the version the extra asks for
UNSUPPORTED = "unsupported"    # cannot exist here (platform, Python version)

#: Feature states.
READY = "ready"
PARTIAL = "partial"            # works, some additions missing
NEEDS = "missing"              # something it needs is missing
OFF = "off"                    # switched off by the user
UNAVAILABLE = "unavailable"    # cannot work on this machine


@dataclass
class DepStatus:
    id: str
    state: str
    version: str = ""
    where: str = ""
    wanted: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


@dataclass
class FeatureStatus:
    id: str
    state: str
    missing: list[str] = field(default_factory=list)
    detail: str = ""


def engines_dir() -> Path:
    """Where engine environments and downloads live:
    ``EDA_AGENT_ENGINES_DIR``, else ``~/EDA Agent/engines``."""
    named = os.environ.get("EDA_AGENT_ENGINES_DIR", "").strip()
    return Path(named) if named else Path.home() / "EDA Agent" / "engines"


def venv_python(venv: str) -> Path:
    root = engines_dir() / venv / "venv"
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


# ------------------------------------------------------------ requirements

def extra_requirements(extra: str, dist: str = "eda-agent") -> list[str]:
    """The pip requirement strings an extra of this package adds, from the
    installed metadata, with the ``extra == ...`` marker removed. Entries
    whose other markers do not hold here are left out."""
    try:
        reqs = md.requires(dist) or []
    except md.PackageNotFoundError:
        return []
    out = []
    for r in reqs:
        req = _parse(r)
        if req is None:
            continue
        name, spec, marker = req
        if not marker or f"extra == '{extra}'" not in marker.replace('"', "'"):
            continue
        rest = _strip_extra(marker, extra)
        if rest and not _marker_holds(rest):
            continue
        out.append(name + spec)
    return out


def _parse(text: str) -> Optional[tuple[str, str, str]]:
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - pip always brings packaging
        head, _, marker = text.partition(";")
        name = head.strip()
        return name, "", marker.strip()
    try:
        r = Requirement(text)
    except Exception:  # noqa: BLE001 - a malformed line is skipped
        return None
    return r.name, str(r.specifier), str(r.marker) if r.marker else ""


def _strip_extra(marker: str, extra: str) -> str:
    m = marker.replace('"', "'")
    for form in (f" and extra == '{extra}'", f"extra == '{extra}' and ", f"extra == '{extra}'"):
        m = m.replace(form, "")
    m = m.strip()
    while m.startswith("(") and m.endswith(")"):
        m = m[1:-1].strip()
    return m


def _marker_holds(marker: str) -> bool:
    try:
        from packaging.markers import Marker
        return bool(Marker(marker).evaluate())
    except Exception:  # noqa: BLE001 - unknown: let pip decide
        return True


def requirement_for(dep: Dependency, feature: Optional[Feature] = None) -> str:
    """The pip requirement for a Python dependency: the one its feature's
    extra declares, else the catalogue's, else the bare name."""
    extras = [feature.extra] if feature and feature.extra else [
        f.extra for f in cat.FEATURES if f.extra and dep.id in f.all_deps()]
    for extra in extras:
        for r in extra_requirements(extra):
            parsed = _parse(r)
            if parsed and _norm(parsed[0]) == _norm(dep.dist or dep.id):
                return r
    return dep.requirement or (dep.dist or dep.id)


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _satisfies(version: str, requirement: str) -> bool:
    parsed = _parse(requirement)
    if not parsed or not parsed[1] or not version:
        return True
    try:
        from packaging.specifiers import SpecifierSet
        return SpecifierSet(parsed[1]).contains(version, prereleases=True)
    except Exception:  # noqa: BLE001 - an odd version string is not an error
        return True


# ---------------------------------------------------------------- programs

def _which_any(*names: str) -> str:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def _program_ngspice(deep: bool) -> tuple[str, str]:
    from ..sim.ngspice import find_ngspice, ngspice_version
    path = find_ngspice() or ""
    return path, (ngspice_version(path) if path and deep else "")


def _program_ltspice(deep: bool) -> tuple[str, str]:
    from ..sim.ngspice import find_ltspice
    return find_ltspice() or "", ""


def _program_freecad(deep: bool) -> tuple[str, str]:
    return _which_any("freecadcmd", "FreeCADCmd"), ""


def _program_pcb_toolkit(deep: bool) -> tuple[str, str]:
    from ..engineering.pcb_toolkit import find_binary
    return find_binary() or "", ""


def _program_kicad(deep: bool) -> tuple[str, str]:
    path = _which_any("kicad-cli")
    if not path and sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            root = Path(base) / "KiCad"
            if root.is_dir():
                found = sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True)
                if found:
                    path = str(found[0])
    return path, ""


def _program_openems(deep: bool) -> tuple[str, str]:
    """openEMS without the slow probe: the folder or Python the user
    named. ``deep`` asks that Python to import openEMS (up to a minute)."""
    if deep:
        from ..emc.openems import find_openems_python
        found = find_openems_python()
        return (found["python"] if found.get("available") else ""), ""
    home = os.environ.get("EDA_AGENT_OPENEMS_DIR", "").strip()
    if home:
        for exe in ("openEMS.exe", "openEMS", "bin/openEMS"):
            if (Path(home) / exe).is_file():
                return str(Path(home) / exe), ""
    py = os.environ.get("EDA_AGENT_OPENEMS_PYTHON", "").strip()
    if py and Path(py).is_file():
        return py, ""
    return _which_any("openEMS"), ""


PROGRAMS: dict[str, Callable[[bool], tuple[str, str]]] = {
    "ngspice": _program_ngspice,
    "ltspice": _program_ltspice,
    "freecad": _program_freecad,
    "pcb-toolkit": _program_pcb_toolkit,
    "kicad": _program_kicad,
    "openems": _program_openems,
}


# ------------------------------------------------------------------ status

def _version(dist: str) -> str:
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        return ""


def _venv_version(venv: str, dist: str) -> tuple[str, str]:
    """(version, python) of ``dist`` inside an engine environment, from
    its dist-info folder: no process is started."""
    py = venv_python(venv)
    if not py.is_file():
        return "", ""
    root = engines_dir() / venv / "venv"
    want = _norm(dist).replace("-", "_")
    for site in list(root.glob("Lib/site-packages")) + list(root.glob("lib/python*/site-packages")):
        for info in site.glob("*.dist-info"):
            name, _, ver = info.name[:-len(".dist-info")].rpartition("-")
            if _norm(name).replace("-", "_") == want:
                return ver, str(py)
    return "", str(py)


def unsupported_reason(dep: Dependency) -> str:
    if dep.platforms and sys.platform not in dep.platforms:
        return f"not made for {sys.platform}"
    if dep.python_min and sys.version_info[:2] < tuple(dep.python_min):
        need = ".".join(map(str, dep.python_min))
        return f"needs Python {need} or later (this is {sys.version_info.major}.{sys.version_info.minor})"
    return ""


def dependency_status(dep: Dependency, deep: bool = False,
                      feature: Optional[Feature] = None) -> DepStatus:
    why = unsupported_reason(dep)
    if dep.kind in (PYTHON, LOCAL):
        ver = _version(dep.dist or dep.id)
        wanted = requirement_for(dep, feature) if dep.kind == PYTHON else ""
        if ver:
            if wanted and not _satisfies(ver, wanted):
                return DepStatus(dep.id, OUTDATED, ver, wanted=wanted,
                                 detail=f"{ver} is installed, the feature asks for {wanted}")
            return DepStatus(dep.id, PRESENT, ver, wanted=wanted)
        return DepStatus(dep.id, UNSUPPORTED if why else MISSING, wanted=wanted, detail=why)
    if dep.kind == VENV:
        ver, py = _venv_version(dep.venv or dep.id, dep.dist or dep.id)
        if ver:
            return DepStatus(dep.id, PRESENT, ver, where=py, wanted=dep.requirement)
        return DepStatus(dep.id, UNSUPPORTED if why else MISSING, where=py,
                         wanted=dep.requirement, detail=why)
    if dep.kind == PROGRAM:
        finder = PROGRAMS.get(dep.id)
        path, ver = ("", "")
        if finder is not None:
            try:
                path, ver = finder(deep)
            except Exception as exc:  # noqa: BLE001 - a broken finder is "not found"
                return DepStatus(dep.id, MISSING, detail=f"the check failed: {exc}")
        if path:
            return DepStatus(dep.id, PRESENT, ver, where=path)
        return DepStatus(dep.id, UNSUPPORTED if why else MISSING, detail=why)
    raise ValueError(f"unknown dependency kind {dep.kind!r}")


def feature_status(f: Feature, deps: dict[str, DepStatus], switched_off: bool) -> FeatureStatus:
    if f.core:
        return FeatureStatus(f.id, READY)
    ok = {d for d, s in deps.items() if s.state == PRESENT}
    missing = [d for d in f.needs if d not in ok]
    for group in f.any_of:
        if not any(d in ok for d in group):
            missing.append(" or ".join(group))
    impossible = [d for d in f.needs if deps.get(d) and deps[d].state == UNSUPPORTED]
    impossible += [" or ".join(g) for g in f.any_of
                   if all(deps.get(d) and deps[d].state == UNSUPPORTED for d in g)]
    if switched_off:
        return FeatureStatus(f.id, OFF, missing)
    if impossible:
        return FeatureStatus(f.id, UNAVAILABLE, missing,
                             detail="; ".join(f"{d}: {deps[d.split(' or ')[0]].detail}"
                                              for d in impossible))
    if missing:
        return FeatureStatus(f.id, NEEDS, missing)
    extra_missing = [d for d in f.adds if d not in ok]
    if extra_missing:
        return FeatureStatus(f.id, PARTIAL, extra_missing)
    return FeatureStatus(f.id, READY)
