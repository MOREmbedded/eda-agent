# SPDX-License-Identifier: Apache-2.0
"""Install what features need: a plan first, then the steps.

``plan`` says what would happen and changes nothing. ``execute`` runs a
plan's automatic steps, one process each, and writes everything they
print to one log file. Steps a person has to take (installing openEMS or
LTspice, getting a Rust compiler) are listed, never attempted.

How each kind is installed:

- **python**: one ``python -m pip install`` into this Python, every
  requirement in one call so pip resolves them together. The requirement
  is the one ``pyproject.toml`` gives the feature's extra.
- **local**: ``pip install`` of a folder of this repository (the Rust
  wheels). Needs a source checkout and ``cargo``; otherwise it is a
  manual step with the reason.
- **venv**: a separate virtual environment under the engines folder, so
  copyleft packages never load into this process.
- **program**: manual, with the steps from the catalogue, except a
  program the catalogue says is built with Rust (pcb-toolkit): with
  ``cargo`` on ``PATH`` it is built into ``<engines folder>/<id>``,
  where eda-agent finds it. It stays a program of its own, never linked.
- **core**: the packages every installation has are checked too, so
  ``--all`` repairs an environment that lost one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import detect
from .catalog import LOCAL, PROGRAM, PYTHON, VENV, Dependency, Feature
from .registry import all_features, get_dependency, get_feature


@dataclass
class Step:
    kind: str                      # pip | local | venv | cargo | manual | skip
    deps: list[str]
    title: str
    argv: list[list[str]] = field(default_factory=list)   # commands, in order
    text: str = ""                 # manual instructions, or why skipped
    size_mb: float = 0.0

    @property
    def automatic(self) -> bool:
        return self.kind in ("pip", "local", "venv", "cargo")

    def to_dict(self) -> dict:
        out = {"kind": self.kind, "deps": self.deps, "title": self.title}
        if self.argv:
            out["commands"] = [" ".join(_quote(a) for a in cmd) for cmd in self.argv]
        if self.text:
            out["text"] = self.text
        if self.size_mb:
            out["size_mb"] = round(self.size_mb)
        return out


@dataclass
class Plan:
    features: list[str]
    steps: list[Step]
    already: list[str]             # dependencies that are already present

    @property
    def automatic(self) -> list[Step]:
        return [s for s in self.steps if s.automatic]

    def to_dict(self) -> dict:
        auto = self.automatic
        return {"features": self.features,
                "already_present": self.already,
                "steps": [s.to_dict() for s in self.steps],
                "automatic_steps": len(auto),
                "manual_steps": sum(1 for s in self.steps if s.kind == "manual"),
                "download_mb": round(sum(s.size_mb for s in auto))}


def _quote(a: str) -> str:
    return f'"{a}"' if (not a or any(c in a for c in ' <>|&^')) else a


def repo_root() -> Optional[Path]:
    """The source checkout eda-agent runs from, when it runs from one."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "eda_agent").is_dir():
            return parent
    return None


def select(names: Iterable[str] = (), everything: bool = False,
           include_dev: bool = False) -> list[Feature]:
    """Features by id, or every installable one with ``everything``."""
    # A pip extra's name (``si``, ``web``) stands for its feature, so the
    # names pyproject.toml uses work here too.
    by_extra = {f.extra: f.id for f in all_features(True) if f.extra}
    known = {f.id for f in all_features(True)}
    names = list(dict.fromkeys(
        (n if n in known else by_extra.get(n, n)) for n in names if n))
    if everything:
        # The core features come first: when the environment lost one of
        # their packages, --all puts it back.
        feats = all_features(include_dev)
        chosen = [f for f in feats if f.core] + [f for f in feats if not f.core]
        return chosen + [get_feature(n) for n in names
                         if all(n != f.id for f in chosen)]
    return [get_feature(n) for n in names]


def _wanted(f: Feature, status: dict[str, detect.DepStatus]) -> list[str]:
    """The dependencies a feature still needs installed: its needs, one of
    each group of alternatives (the first one that installs on its own),
    and its additions."""
    # A need only other platforms have (pywin32 off Windows) is no need.
    want = [d for d in f.needs if detect.applies_here(get_dependency(d))]
    for group in f.any_of:
        if any(status[d].state == detect.PRESENT for d in group):
            continue
        auto = [d for d in group if get_dependency(d).kind in (PYTHON, LOCAL, VENV)
                and status[d].state != detect.UNSUPPORTED]
        want.append(auto[0] if auto else group[0])
    want += list(f.adds)
    return want


def plan(features: list[Feature], deep: bool = False,
         python: str = "") -> Plan:
    python = python or sys.executable
    deps: list[Dependency] = []
    owner: dict[str, Feature] = {}
    status: dict[str, detect.DepStatus] = {}
    for f in features:
        for d in f.all_deps():
            if d not in status:
                status[d] = detect.dependency_status(get_dependency(d), deep, f)
        for d in _wanted(f, status):
            if d not in owner:
                owner[d] = f
                deps.append(get_dependency(d))
    already = [d.id for d in deps if status[d.id].state == detect.PRESENT]
    todo = [d for d in deps if status[d.id].state != detect.PRESENT]
    # The features this plan is about: the ones asked for by name, and
    # the built-in ones only when they have something to repair.
    touched = {owner[d.id].id for d in todo}
    feature_ids = [f.id for f in features if not f.core or f.id in touched]
    steps: list[Step] = []

    # One pip call per feature: pip resolves a feature's packages
    # together, and a package that will not install (CadQuery is large
    # and picky) does not take the other features down with it.
    by_feature: dict[str, list[Dependency]] = {}
    for d in todo:
        st = status[d.id]
        if st.state == detect.UNSUPPORTED:
            steps.append(Step("skip", [d.id], d.title, text=st.detail))
            continue
        if d.kind == PYTHON:
            by_feature.setdefault(owner[d.id].id, []).append(d)
    for fid, group in by_feature.items():
        f = owner[group[0].id]
        reqs = [detect.requirement_for(d, f) for d in group]
        steps.append(Step("pip", [d.id for d in group],
                          f"{f.title}: " + ", ".join(d.title for d in group),
                          argv=[[python, "-m", "pip", "install", *reqs]],
                          size_mb=sum(d.size_mb for d in group)))
    root = repo_root()
    for d in todo:
        if status[d.id].state == detect.UNSUPPORTED or d.kind == PYTHON:
            continue
        if d.kind == LOCAL:
            src = root / d.source if root else None
            if src is None or not src.is_dir():
                steps.append(Step("manual", [d.id], d.title, text=(
                    f"Built from {d.source}/ of the source repository, which this "
                    "installation does not have. Install the wheel the CI builds "
                    "(see docs/RUST_COMPUTE.md).")))
            elif not shutil.which("cargo"):
                steps.append(Step("manual", [d.id], d.title, text=(
                    f"Building {d.source}/ needs Rust: install it from rustup.rs "
                    "(on Windows also the Visual Studio C++ build tools), then run "
                    "this again. Or install the wheel the CI builds "
                    "(docs/RUST_COMPUTE.md).")))
            else:
                steps.append(Step("local", [d.id], d.title,
                                  argv=[[python, "-m", "pip", "install", str(src)]],
                                  size_mb=d.size_mb))
        elif d.kind == VENV:
            vpy = detect.venv_python(d.venv or d.id)
            cmds = []
            if not vpy.is_file():
                cmds.append([python, "-m", "venv", str(vpy.parent.parent)])
            cmds.append([str(vpy), "-m", "pip", "install", d.requirement or d.dist or d.id])
            steps.append(Step("venv", [d.id], f"{d.title} in its own environment",
                              argv=cmds, size_mb=d.size_mb,
                              text=f"Kept apart from eda-agent ({d.license})."))
        elif d.kind == PROGRAM and d.cargo and shutil.which("cargo"):
            target = detect.engines_dir() / d.id
            steps.append(Step("cargo", [d.id], f"{d.title}, built with Rust",
                              argv=[["cargo", "install", "--locked", "--root",
                                     str(target), *d.cargo]],
                              size_mb=d.size_mb,
                              text=f"Into {target}; a program of its own ({d.license})."))
        elif d.kind == PROGRAM:
            steps.append(Step("manual", [d.id], d.title,
                              text=(d.manual or f"Install {d.title}.")
                              + (f" Download: {d.url}" if d.url else "")))
    return Plan(feature_ids, steps, already)


@dataclass
class StepResult:
    title: str
    ok: bool
    seconds: float
    returncode: Optional[int] = None
    tail: str = ""

    def to_dict(self) -> dict:
        out = {"title": self.title, "ok": self.ok, "seconds": round(self.seconds, 1)}
        if self.returncode not in (None, 0):
            out["returncode"] = self.returncode
        if self.tail and not self.ok:
            out["tail"] = self.tail
        return out


def log_dir() -> Path:
    return detect.engines_dir().parent / "install-logs"


def execute(p: Plan, on_line: Optional[Callable[[str], None]] = None,
            log_path: Optional[Path] = None,
            runner: Optional[Callable[..., subprocess.Popen]] = None) -> dict:
    """Run the plan's automatic steps in order. A failed step stops the
    steps that depend on it (the commands of one step) but not the other
    steps. Returns per-step results and the log file."""
    from ..noconsole import no_window
    runner = runner or subprocess.Popen
    if log_path is None:
        log_path = log_dir() / time.strftime("install-%Y%m%d-%H%M%S.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results: list[StepResult] = []
    env = dict(os.environ)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    with open(log_path, "a", encoding="utf-8") as log:
        def say(line: str) -> None:
            log.write(line + "\n")
            log.flush()
            if on_line:
                on_line(line)
        for step in p.automatic:
            t0 = time.monotonic()
            say(f"== {step.title}")
            ok, rc, tail = True, 0, []
            for cmd in step.argv:
                say("$ " + " ".join(_quote(a) for a in cmd))
                try:
                    proc = runner(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace",
                                  env=env, **no_window())
                except OSError as exc:
                    ok, rc = False, None
                    tail.append(str(exc))
                    say(f"could not start: {exc}")
                    break
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    tail = (tail + [line])[-25:]
                    say(line)
                rc = proc.wait()
                if rc != 0:
                    ok = False
                    say(f"exit code {rc}")
                    break
            results.append(StepResult(step.title, ok, time.monotonic() - t0, rc,
                                      "\n".join(tail)))
    import importlib
    importlib.invalidate_caches()
    from .registry import load
    load(refresh=True)
    return {"ok": all(r.ok for r in results), "log": str(log_path),
            "steps": [r.to_dict() for r in results],
            "manual": [s.to_dict() for s in p.steps if s.kind == "manual"]}
