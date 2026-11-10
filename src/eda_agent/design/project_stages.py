# SPDX-License-Identifier: Apache-2.0
"""Which design projects are tracked through the NPI stages, and where each
one stands: the data behind the dashboard's stage pipeline.

The stages, their checks and the scoring are eda_agent.design.phase_gates.
This module adds the one thing the gate did not have: a list of the
projects (.PrjPcb) being tracked, so the dashboard can show every board in
flight rather than one product name at a time.

Everything lives in the server's workspace folder, never in a repository:

    <workspace>/phase_gates/projects.json   which projects, and the board
                                             revision each one is at
    <workspace>/phase_gates/<product>.jsonl the answers, one ledger per
                                             project (phase_gates.Ledger)

A project's ledger is keyed by its file name without the extension, so
``Widget.PrjPcb`` keeps its answers in ``Widget.jsonl`` and an AI calling
``design_phase_answer(product="Widget")`` writes to the same ledger the
dashboard reads. Removing a project from the list never touches its
ledger: putting it back brings every answer back.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import phase_gates as pg

FOLDER = "phase_gates"
REGISTRY = "projects.json"
PROJECT_SUFFIXES = (".prjpcb",)
#: Answers the dashboard records name this as their source, so a ledger
#: says which were ticked by a person and which came from a tool call.
SOURCE = "dashboard"
MAX_DETAIL = 2000


def folder(workspace: Path) -> Path:
    return Path(workspace) / FOLDER


def product_for(project_path: str) -> str:
    """The ledger key of a project: its file name without the extension,
    made safe the way phase_gates.ledger_name makes any product name."""
    stem = Path(str(project_path).replace("\\", "/")).stem
    return pg.ledger_name(stem)[: -len(".jsonl")]


def _read(workspace: Path) -> dict[str, dict]:
    path = folder(workspace) / REGISTRY
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    return {str(k): v for k, v in projects.items() if isinstance(v, dict)}


def _write(workspace: Path, projects: dict[str, dict]) -> None:
    from ..atomicfile import discard, replace_with_retry

    where = folder(workspace)
    where.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".projects.", suffix=".tmp", dir=str(where))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"projects": projects}, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        replace_with_retry(tmp, where / REGISTRY)
    except OSError:
        discard(tmp)
        raise


def short_title(stage: str) -> str:
    """The name a stepper has room for: "EVT" rather than "Engineering
    validation (EVT)", "Prototype" rather than "Prototype build"."""
    if stage in ("evt", "dvt", "pvt"):
        return stage.upper()
    return pg.TITLES[stage].split(" ")[0]


def _ledger(workspace: Path, product: str) -> pg.Ledger:
    return pg.Ledger(folder(workspace) / pg.ledger_name(product))


def _stage_row(review: pg.StageReview, state: str) -> dict[str, Any]:
    tally = review.tally()
    required = [line for line in review.lines if line.check.required]
    return {"stage": review.stage, "title": pg.TITLES[review.stage],
            "short": short_title(review.stage), "state": state,
            "checks": len(review.lines),
            "done": sum(1 for line in required if not line.holding),
            "required": len(required), "tally": tally,
            "stale": sum(1 for line in review.lines if line.ignored_because)}


def pipeline(answers: Iterable[pg.Answer], stages: Iterable[str], revision: str = "",
             today: Optional[datetime] = None) -> dict[str, Any]:
    """Every stage in order, each ``cleared``, ``current``, ``ahead`` or
    ``off`` (not run by this profile), and the stage the project is in.

    The project is in the first stage in force that does not clear, as
    phase_gates.standing decides. A later stage whose checks happen to be
    met still reads ``ahead``: the order is the point of the gates, and a
    pipeline that showed DVT cleared beside an open EVT would be read as
    progress it is not.
    """
    running = tuple(stages)
    given = list(answers)
    current, reviews = pg.standing(given, revision, today, running)
    by_stage = {r.stage: r for r in reviews}
    rows, reached = [], False
    for stage in pg.STAGES:
        if stage not in running:
            rows.append(_stage_row(pg.review(stage, given, revision, today), "off"))
            continue
        if stage == current:
            reached = True
            rows.append(_stage_row(by_stage[stage], "current"))
        else:
            rows.append(_stage_row(by_stage[stage], "ahead" if reached else "cleared"))
    return {"current": current, "title": pg.TITLES[current],
            "in_production": current == pg.DONE, "stages": rows}


def overview(workspace: Path, stages: Iterable[str],
             open_paths: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Every tracked project with its pipeline, the open ones marked."""
    running = tuple(stages)
    opened = {str(p).upper() for p in open_paths}
    out = []
    for product, entry in sorted(_read(workspace).items(), key=lambda kv: kv[0].lower()):
        path = str(entry.get("path") or "")
        revision = str(entry.get("revision") or "")
        answers = _ledger(workspace, product).answers()
        out.append({"product": product, "path": path, "name": Path(path).name or product,
                    "revision": revision, "exists": bool(path) and Path(path).is_file(),
                    "open": path.upper() in opened, "answers": len(answers),
                    "last_answer": max((a.when for a in answers), default=None),
                    **pipeline(answers, running, revision)})
    for row in out:
        if row["last_answer"] is not None:
            row["last_answer"] = row["last_answer"].isoformat(timespec="minutes")
    return out


def add(workspace: Path, project_path: str) -> dict[str, Any]:
    """Start tracking a project. Its answers so far, if any, come with it."""
    path = str(project_path or "").strip().strip('"')
    if not path:
        return {"ok": False, "reason": "give the path of a .PrjPcb project file"}
    if Path(path).suffix.lower() not in PROJECT_SUFFIXES:
        return {"ok": False, "reason": f"{Path(path).name} is not a .PrjPcb project file"}
    if not Path(path).is_file():
        return {"ok": False, "reason": f"there is no file at {path}"}
    product = product_for(path)
    projects = _read(workspace)
    held = projects.get(product)
    if held and str(held.get("path") or "").upper() != path.upper():
        return {"ok": False, "reason": (
            f"another project with the same name, {held.get('path')}, is already "
            f"tracked, and the two would share one ledger. Remove that one first.")}
    if held:
        return {"ok": True, "product": product, "already": True}
    projects[product] = {"path": path, "revision": "",
                         "added": datetime.now().isoformat(timespec="seconds")}
    _write(workspace, projects)
    return {"ok": True, "product": product}


def remove(workspace: Path, product: str) -> dict[str, Any]:
    """Stop tracking a project. Its ledger stays where it is."""
    projects = _read(workspace)
    if product not in projects:
        return {"ok": False, "reason": f"{product} is not tracked"}
    del projects[product]
    _write(workspace, projects)
    return {"ok": True, "product": product, "ledger_kept": True}


def set_revision(workspace: Path, product: str, revision: str) -> dict[str, Any]:
    """The board revision a project is at. Answers recorded for another
    revision stop counting, and each check says which revision it was for."""
    projects = _read(workspace)
    if product not in projects:
        return {"ok": False, "reason": f"{product} is not tracked"}
    revision = str(revision or "").strip()[:40]
    projects[product]["revision"] = revision
    _write(workspace, projects)
    return {"ok": True, "product": product, "revision": revision}


def stage_checks(workspace: Path, product: str, stage: str) -> dict[str, Any]:
    """One stage of one project: every check, its outcome and its evidence."""
    projects = _read(workspace)
    if product not in projects:
        return {"ok": False, "reason": f"{product} is not tracked"}
    try:
        stage = pg.stage_from(stage)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    revision = str(projects[product].get("revision") or "")
    review = pg.review(stage, _ledger(workspace, product).answers(), revision)
    from ..tools.folds import call_form

    lines = []
    for line in review.lines:
        c, a = line.check, line.answer
        lines.append({"key": c.key, "asks": c.asks, "why_now": c.reason,
                      "done_when": c.done_when, "required": c.required,
                      "tools": [call_form(t) for t in c.tools],
                      "good_for_days": c.good_for_days, "outcome": line.outcome,
                      "recorded": a.outcome if a else "",
                      "detail": a.detail if a else "", "source": a.source if a else "",
                      "when": a.when.isoformat(timespec="minutes") if a else "",
                      "revision": a.revision if a else "",
                      "ignored_because": line.ignored_because})
    return {"ok": True, "product": product, "stage": stage, "title": pg.TITLES[stage],
            "revision": revision, "cleared": review.cleared, "checks": lines}


def answer(workspace: Path, product: str, key: str, outcome: str, detail: str = "",
           stages: Iterable[str] = pg.STAGES) -> dict[str, Any]:
    """Record one outcome for one check, against the project's revision.

    A check of a stage the profile does not run is refused, as
    design_phase_answer refuses it: evidence that nothing reviews is worse
    than none. A waiver needs a reason (phase_gates.Ledger refuses one
    without).
    """
    projects = _read(workspace)
    if product not in projects:
        return {"ok": False, "reason": f"{product} is not tracked"}
    check = pg.CHECKS.get(str(key))
    if check is None:
        return {"ok": False, "reason": f"no check {key!r}"}
    if check.stage not in tuple(stages):
        return {"ok": False, "reason": f"{pg.TITLES[check.stage]} is not a stage this "
                                       f"profile runs, so nothing would review the answer"}
    detail = str(detail or "").strip()[:MAX_DETAIL]
    try:
        _ledger(workspace, product).add(
            check.key, str(outcome or ""), source=SOURCE, detail=detail,
            revision=str(projects[product].get("revision") or ""))
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "product": product, "key": check.key, "outcome": outcome}
