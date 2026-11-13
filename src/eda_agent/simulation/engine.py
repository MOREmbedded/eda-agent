# SPDX-License-Identifier: Apache-2.0
"""What a simulation engine is.

An engine takes a job (a folder with a spec and input files), works on
it, and writes normalised results beside its own native files. Engines
that run in this process (``library``) finish inside ``run``; engines that
run as their own program (``external``: openEMS, anything GPL) start it
in ``run`` and report progress through ``poll``.

Every engine belongs to a feature of the features registry: that is
where its dependencies, its license, its install steps and its on/off
switch live. An engine is offered only when its feature is ready and on.

Normalised results (``results.json`` in the engine's run folder)::

    {"ok": true, "engine": "...", "kind": "...",
     "metrics": {"name": {"value": 1.0, "unit": "dB", "label": "..."}},
     "verdicts": [{"check": "...", "pass": true, "detail": "..."}],
     "files": {"touchstone": "net.s2p", "plot": "sparams.png"},   # in work/
     "notes": ["..."]}

Metrics with the same name from two engines on the same job are what the
Compare view sets side by side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .jobs import Job

LIBRARY = "library"
EXTERNAL = "external"

Progress = Callable[[str, Optional[float]], None]


@dataclass(frozen=True)
class JobKind:
    """A kind of job and what it takes, for validation and for the forms."""
    id: str
    title: str
    summary: str
    #: Input files by role, e.g. {"touchstone": "an .sNp file"}.
    inputs: dict[str, str] = field(default_factory=dict)
    #: Spec keys: {key: (type name, required, help)}.
    spec: dict[str, tuple[str, bool, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineInfo:
    id: str
    title: str
    feature: str
    license: str
    process: str                          # LIBRARY | EXTERNAL
    kinds: tuple[str, ...]
    summary: str = ""
    #: Better-known strengths, shown beside the engine.
    strengths: tuple[str, ...] = ()
    homepage: str = ""


class EngineError(ValueError):
    """A job this engine cannot run, said in words for the user."""


class Engine:
    """Subclass and set ``info``; override ``run`` (and ``poll`` for an
    external engine)."""

    info: EngineInfo

    def check(self, kind: str, spec: dict[str, Any], inputs: dict[str, str]) -> list[str]:
        """Problems with a job before it runs; empty when it can run."""
        return []

    def run(self, job: "Job", folder, progress: Progress) -> dict[str, Any]:
        """Do the work in ``folder``, the engine's own work folder. A
        library engine returns the normalised results; an external one
        starts its program and returns ``{"state": "running"}``."""
        raise NotImplementedError

    def poll(self, job: "Job", folder) -> dict[str, Any]:
        """An external run's state: ``{"state": "running", ...}`` or the
        normalised results once it is done (``state`` "done" or "failed")."""
        return {"state": "done"}

    def cancel(self, job: "Job", folder) -> bool:
        return False


def metric(value: Any, unit: str = "", label: str = "", **extra: Any) -> dict[str, Any]:
    out = {"value": value}
    if unit:
        out["unit"] = unit
    if label:
        out["label"] = label
    out.update(extra)
    return out
