# SPDX-License-Identifier: Apache-2.0
"""Simulation jobs: one folder per problem, one subfolder per engine.

    <jobs dir>/<kind>-<hash>/
        job.json            kind, title, spec, inputs (name, sha256), created
        inputs/             copies of the input files
        runs/<engine>/      status.json, results.json (normalised)
        runs/<engine>/work/ the engine's own files

A job's id is a hash of its kind, spec and the content of its inputs: the
same problem is the same job, so asking twice does not solve twice, and a
second engine on it lands beside the first, ready to compare.

The folder is ``EDA_AGENT_SIM_JOBS``, else ``~/EDA Agent/jobs``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..atomicfile import replace_with_retry

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


def jobs_dir() -> Path:
    named = os.environ.get("EDA_AGENT_SIM_JOBS", "").strip()
    return Path(named) if named else Path.home() / "EDA Agent" / "jobs"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    replace_with_retry(tmp, path)


def read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _safe(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return keep.strip("._") or "file"


@dataclass
class Job:
    id: str
    kind: str
    title: str
    spec: dict[str, Any]
    inputs: dict[str, dict[str, str]] = field(default_factory=dict)   # role -> {file, sha256, name}
    created: float = 0.0
    folder: Path = Path()

    def input_path(self, role: str) -> Path:
        return self.folder / "inputs" / self.inputs[role]["file"]

    def run_folder(self, engine: str) -> Path:
        """status.json and results.json (ours) for one engine."""
        return self.folder / "runs" / _safe(engine)

    def work_folder(self, engine: str) -> Path:
        """The engine's own files, kept apart so that an engine that writes
        a status.json or results.json of its own (openEMS does) never
        collides with ours."""
        return self.run_folder(engine) / "work"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "spec": self.spec,
                "inputs": self.inputs, "created": self.created}


def make_id(kind: str, spec: dict[str, Any], input_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256(_canonical({"kind": kind, "spec": spec,
                                        "inputs": input_hashes}).encode()).hexdigest()
    return f"{_safe(kind)}-{digest[:12]}"


def create(kind: str, spec: dict[str, Any], inputs: Optional[dict[str, str]] = None,
           title: str = "", base: Optional[Path] = None) -> Job:
    """The job for this problem: made when new, found when it exists."""
    inputs = dict(inputs or {})
    files: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for role, p in inputs.items():
        path = Path(p)
        if not path.is_file():
            raise FileNotFoundError(f"input {role!r}: no such file {p}")
        files[role] = path
        hashes[role] = _sha256(path)
    spec = json.loads(_canonical(spec or {}))
    jid = make_id(kind, spec, hashes)
    root = (base or jobs_dir()) / jid
    existing = load(root)
    if existing is not None:
        return existing
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    meta_inputs = {}
    for role, path in files.items():
        name = f"{_safe(role)}{''.join(path.suffixes[-1:]) or ''}"
        if role in ("touchstone",) or path.suffix.lower().startswith(".s"):
            name = _safe(path.name)
        shutil.copyfile(path, root / "inputs" / name)
        meta_inputs[role] = {"file": name, "sha256": hashes[role], "name": path.name}
    job = Job(jid, kind, title or kind, spec, meta_inputs, time.time(), root)
    write_json(root / "job.json", job.to_dict())
    return job


def load(folder: Path) -> Optional[Job]:
    meta = read_json(Path(folder) / "job.json")
    if not meta:
        return None
    return Job(meta["id"], meta["kind"], meta.get("title", meta["kind"]), meta.get("spec", {}),
               meta.get("inputs", {}), float(meta.get("created", 0.0)), Path(folder))


def get(job_id: str, base: Optional[Path] = None) -> Job:
    if not job_id or _safe(job_id) != job_id:
        raise KeyError(f"no job {job_id!r}")
    job = load((base or jobs_dir()) / job_id)
    if job is None:
        raise KeyError(f"no job {job_id!r} in {base or jobs_dir()}")
    return job


def runs(job: Job) -> dict[str, dict[str, Any]]:
    """Each engine's status (and results when done) for a job."""
    out = {}
    root = job.folder / "runs"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        st = read_json(d / "status.json") or {"state": QUEUED}
        res = read_json(d / "results.json")
        if res is not None:
            st = {**st, "results": res}
        out[d.name] = st
    return out


def list_jobs(base: Optional[Path] = None, limit: int = 100) -> list[dict[str, Any]]:
    """Newest first: id, kind, title, created, and each engine's state."""
    root = base or jobs_dir()
    if not root.is_dir():
        return []
    found = []
    for d in root.iterdir():
        job = load(d) if d.is_dir() else None
        if job is None:
            continue
        found.append(job)
    found.sort(key=lambda j: j.created, reverse=True)
    out = []
    for job in found[:max(1, limit)]:
        states = {}
        for engine, st in runs(job).items():
            res = st.get("results") or {}
            states[engine] = {"state": st.get("state"), "ok": res.get("ok"),
                              "seconds": st.get("seconds")}
        out.append({**job.to_dict(), "folder": str(job.folder), "runs": states})
    return out


def set_status(folder: Path, state: str, **extra: Any) -> dict[str, Any]:
    old = read_json(folder / "status.json") or {}
    now = time.time()
    st = {**old, "state": state, "updated": now, **extra}
    if state == RUNNING and "started" not in extra:
        st["started"] = old.get("started", now) if old.get("state") == RUNNING else now
    if state in (DONE, FAILED, CANCELLED) and st.get("started"):
        st["seconds"] = round(now - float(st["started"]), 3)
    write_json(folder / "status.json", st)
    return st


def delete(job: Job) -> None:
    """Remove a job folder. Only ever a folder this module made: it must
    hold a job.json naming the same id."""
    meta = read_json(job.folder / "job.json")
    if not meta or meta.get("id") != job.id:
        raise ValueError(f"{job.folder} is not a job folder")
    shutil.rmtree(job.folder)
