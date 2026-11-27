# SPDX-License-Identifier: Apache-2.0
"""Hertzon, the 3D full-wave FEM solver, driven through its command line.

Hertzon (https://github.com/c8501024/Hertzon) is GPL-2.0-or-later, so it is
never imported here: eda-agent runs its documented command line,
``hertzon-cli`` (``describe`` and ``solve``, JSON contract
``cli_schema`` 1), as a separate program, and reads back the files that
program writes (JSON and Touchstone). No Hertzon code is copied into, or
linked with, this Apache-2.0 package.

Where ``hertzon-cli`` is looked for, first match wins:

1. ``EDA_AGENT_HERTZON_CLI``: the program, or a Hertzon folder holding
   it (directly, or in ``.venv\\Scripts`` / ``.venv/bin``),
2. ``hertzon-cli`` on ``PATH``,
3. the Windows installer's folder,
   ``%LOCALAPPDATA%\\Programs\\Hertzon\\.venv\\Scripts\\hertzon-cli.exe``,
4. the Linux installer's folder,
   ``~/.local/share/hertzon/.venv/bin/hertzon-cli``.

A solve runs in the job's own work folder: the project (a ``.hzn`` file,
or a ``.zip`` of a project folder when the project uses other files such
as STEP, STL or Touchstone models) is unpacked into ``work/project`` and
Hertzon's ``.hznresults`` folder grows there, so the user's own project
and its results are never touched.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

ENV_CLI = "EDA_AGENT_HERTZON_CLI"
CLI_SCHEMA = 1
RUNNER = Path(__file__).with_name("hertzon_runner.py")
SWEEP_TYPES = ("discrete", "interpolating", "fast")
#: ``describe`` loads the project only; the first call after an install
#: can still take a while to import Hertzon's environment.
DESCRIBE_TIMEOUT_S = 180.0
VERSION_TIMEOUT_S = 60.0
#: Unpacked project archives larger than this are refused.
MAX_UNPACKED_BYTES = 2 * 1024 ** 3

#: What each exit code of ``hertzon-cli solve`` means (docs/CLI.md of Hertzon).
EXIT_CODES = {
    0: "done",
    1: "the solver failed (meshing or solve); see solver.log in the results folder",
    2: "wrong command line",
    3: "bad input: project not readable, or unknown design, setup, sweep or variable, "
       "or a bad value",
    4: "the model check failed (validate)",
    5: "adaptive meshing did not converge",
    127: "hertzon-cli could not be started",
    130: "interrupted",
}

_VAR_NAME = re.compile(r"^\$?[A-Za-z_][A-Za-z0-9_]*$")
_exe = ".exe" if sys.platform == "win32" else ""


class HertzonError(ValueError):
    """Something about a Hertzon run, said in words for the user."""


# ------------------------------------------------------------------ finding

def _cli_in(folder: Path) -> Optional[Path]:
    for rel in (f"hertzon-cli{_exe}", f".venv/Scripts/hertzon-cli{_exe}",
                f".venv/bin/hertzon-cli{_exe}", f"Scripts/hertzon-cli{_exe}",
                f"bin/hertzon-cli{_exe}"):
        p = folder / rel
        if p.is_file():
            return p
    return None


def default_locations() -> list[Path]:
    """Where the Hertzon installers put ``hertzon-cli``."""
    out = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        out.append(Path(local) / "Programs" / "Hertzon" / ".venv" / "Scripts" / "hertzon-cli.exe")
    out.append(Path.home() / ".local" / "share" / "hertzon" / ".venv" / "bin" / "hertzon-cli")
    return out


def find_cli() -> dict[str, Any]:
    """``{"available", "cli", "how"}``: where ``hertzon-cli`` is, and how it
    was found (or where it was looked for)."""
    named = os.environ.get(ENV_CLI, "").strip().strip('"')
    if named:
        p = Path(named).expanduser()
        if p.is_file():
            return {"available": True, "cli": str(p), "how": ENV_CLI}
        if p.is_dir():
            found = _cli_in(p)
            if found:
                return {"available": True, "cli": str(found), "how": f"{ENV_CLI} folder"}
        return {"available": False, "cli": "",
                "how": f"{ENV_CLI}={named} is neither hertzon-cli nor a Hertzon folder "
                       "holding it"}
    on_path = shutil.which("hertzon-cli")
    if on_path:
        return {"available": True, "cli": on_path, "how": "PATH"}
    for p in default_locations():
        if p.is_file():
            return {"available": True, "cli": str(p), "how": "installer folder"}
    return {"available": False, "cli": "",
            "how": "not found: set " + ENV_CLI + " to hertzon-cli or to the Hertzon folder; "
                   "looked on PATH and in " + ", ".join(str(p) for p in default_locations())}


def _cli_or_raise(cli: str = "") -> str:
    if cli:
        return cli
    found = find_cli()
    if not found["available"]:
        raise HertzonError("Hertzon is not installed or not found (" + found["how"] + ")")
    return found["cli"]


def child_env() -> dict[str, str]:
    """The environment for ``hertzon-cli``: this one, without what points a
    Python at this process's packages. Hertzon runs its own Python in its
    own environment, which a PYTHONPATH or PYTHONHOME from here would break."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
                                "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV")}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    from ..noconsole import no_window
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, env=child_env(),
                              stdin=subprocess.DEVNULL, **no_window())
    except subprocess.TimeoutExpired as exc:
        raise HertzonError(f"hertzon-cli {cmd[1]} did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise HertzonError(f"hertzon-cli could not be started: {exc}") from exc


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace") if data else ""


def _json_from(text: str) -> Optional[dict]:
    """The JSON object a command printed; a line of other output before it
    (a warning from the environment) is skipped."""
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    if start > 0:
        try:
            data = json.loads(text[start:])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


_version_cache: dict[tuple[str, float], str] = {}


def cli_version(cli: str = "", timeout: float = VERSION_TIMEOUT_S) -> str:
    """Hertzon's version from ``hertzon-cli version --json`` (cached per
    program file); empty when it cannot be told."""
    try:
        cli = _cli_or_raise(cli)
        stamp = Path(cli).stat().st_mtime
    except (HertzonError, OSError):
        return ""
    key = (cli, stamp)
    if key in _version_cache:
        return _version_cache[key]
    try:
        cp = _run([cli, "version", "--json"], timeout)
    except HertzonError:
        return ""
    data = _json_from(_text(cp.stdout)) or {}
    ver = str(data.get("hertzon") or data.get("version") or "").strip()
    if not ver and cp.returncode == 0:
        m = re.search(r"\d+\.\d+(?:\.\d+)?\S*", _text(cp.stdout))
        ver = m.group(0) if m else ""
    _version_cache[key] = ver
    return ver


# ----------------------------------------------------------------- describe

def describe(project: str | Path, design: str = "", cli: str = "",
             timeout: float = DESCRIBE_TIMEOUT_S) -> dict[str, Any]:
    """``hertzon-cli describe``: the designs of a project with their ports,
    variables, setups and sweeps. The project file is not changed."""
    project = Path(project)
    if project.suffix.lower() == ".zip":
        raise HertzonError("describe reads a .hzn file; unpack the archive first")
    if not project.is_file():
        raise HertzonError(f"no project file {project}")
    cli = _cli_or_raise(cli)
    cmd = [cli, "describe", str(project)]
    if design:
        cmd += ["--design", str(design)]
    cp = _run(cmd, timeout)
    if cp.returncode != 0:
        raise HertzonError(_failure(cp.returncode, _text(cp.stderr) or _text(cp.stdout)))
    data = _json_from(_text(cp.stdout))
    if data is None:
        raise HertzonError("hertzon-cli describe printed no JSON: "
                           + _text(cp.stdout)[-400:].strip())
    out = {"ok": True, **data}
    schema = data.get("cli_schema")
    if schema != CLI_SCHEMA:
        out["warning"] = (f"hertzon-cli reports cli_schema {schema!r}; eda-agent is written "
                          f"for {CLI_SCHEMA}, so some fields may be read wrongly")
    return out


def _failure(code: int, text: str) -> str:
    """One line for the user from an exit code and Hertzon's own error."""
    why = EXIT_CODES.get(code, f"exit code {code}")
    detail = ""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        ev = _json_from(line)
        if ev and ev.get("event") == "error":
            detail = str(ev.get("message", "")).strip()
            break
        if not ev and not detail:
            detail = line
            break
    return f"hertzon-cli: {why}" + (f": {detail}" if detail and detail not in why else "")


# -------------------------------------------------------------------- spec

#: The ``hertzon-solve`` spec keys: {key: (type, required, help)}.
SOLVE_SPEC: dict[str, tuple[str, bool, str]] = {
    "design": ("text", False, "the design to solve; empty for the only design (or the "
               "only one with a setup)"),
    "setup": ("text", False, "the analysis setup; empty for the first enabled one"),
    "sweep": ("text", False, "the sweep; empty for the setup's first enabled sweep, "
              "'LastAdaptive' for the adaptive frequency only"),
    "variables": ("object", False, "design or project variables to set for this run, "
                  "e.g. {\"w\": \"1.2mm\", \"$eps\": 4.4}; a number without a unit has "
                  "the design's unit"),
    "f_start_hz": ("number", False, "with f_stop_hz and f_points: a linear band that "
                   "replaces the sweep's frequencies"),
    "f_stop_hz": ("number", False, "top of that band, Hz"),
    "f_points": ("number", False, "frequency points of that band (2 or more)"),
    "frequencies_hz": ("list", False, "single frequencies, Hz, in place of a band"),
    "sweep_type": ("text", False, "discrete, interpolating or fast; empty keeps the "
                   "setup's"),
    "cores": ("number", False, "solver cores; 0 lets Hertzon choose"),
    "validate": ("bool", False, "run Hertzon's model check before solving"),
    "require_convergence": ("bool", False, "count a run whose adaptive mesh did not "
                            "converge as failed (default: done, with a failing "
                            "convergence verdict)"),
    "fields": ("bool", False, "keep the field solution (large) in the results folder"),
    "project_file": ("text", False, "with a .zip project: the .hzn inside it to solve, "
                     "when it holds more than one"),
    "tolerance": ("number", False, "passivity and reciprocity tolerance of the "
                  "S-parameter checks (default 1e-3)"),
}


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_spec(spec: dict[str, Any]) -> list[str]:
    """Problems with a ``hertzon-solve`` spec beyond its types."""
    problems = []
    variables = spec.get("variables") or {}
    if isinstance(variables, dict):
        for name, value in variables.items():
            if not _VAR_NAME.match(str(name)):
                problems.append(f"variable name {name!r} is not a Hertzon variable name")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) \
                    or (isinstance(value, float) and not math.isfinite(value)) \
                    or (isinstance(value, str) and not value.strip()):
                problems.append(f"variable {name!r} needs a number or a value like '1.2mm'")
    band = [k for k in ("f_start_hz", "f_stop_hz", "f_points") if k in spec]
    if band and len(band) != 3:
        problems.append("a band needs all of f_start_hz, f_stop_hz and f_points")
    if len(band) == 3:
        f0, f1, n = spec["f_start_hz"], spec["f_stop_hz"], spec["f_points"]
        if not (_finite(f0) and _finite(f1) and f0 >= 0 and f1 > f0):
            problems.append("the band needs 0 <= f_start_hz < f_stop_hz")
        if not (_finite(n) and float(n) == int(n) and int(n) >= 2):
            problems.append("f_points must be a whole number of 2 or more")
    if "frequencies_hz" in spec:
        fl = spec["frequencies_hz"]
        if band:
            problems.append("give either a band (f_start_hz, f_stop_hz, f_points) or "
                            "frequencies_hz, not both")
        if not isinstance(fl, (list, tuple)) or not fl \
                or not all(_finite(f) and f > 0 for f in fl):
            problems.append("frequencies_hz must be a list of frequencies above 0 Hz")
    st = str(spec.get("sweep_type") or "").lower()
    if st and st not in SWEEP_TYPES:
        problems.append(f"sweep_type must be one of {', '.join(SWEEP_TYPES)}")
    if "cores" in spec:
        c = spec["cores"]
        if not (_finite(c) and float(c) == int(c) and int(c) >= 0):
            problems.append("cores must be a whole number of 0 or more")
    for key in ("design", "setup", "sweep", "project_file"):
        v = spec.get(key)
        if isinstance(v, str) and v != v.strip():
            problems.append(f"{key} has spaces at its ends")
    return problems


def _hz(v: float) -> str:
    """A frequency as hertzon-cli takes it: in Hz, without a unit."""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def solve_args(project: str | Path, spec: dict[str, Any], out: str | Path) -> list[str]:
    """The arguments after ``hertzon-cli`` for one solve of ``spec``."""
    problems = check_spec(spec)
    if problems:
        raise HertzonError("; ".join(problems))
    args = ["solve", str(project)]
    for key in ("design", "setup", "sweep"):
        if spec.get(key):
            args += [f"--{key}", str(spec[key])]
    for name, value in (spec.get("variables") or {}).items():
        text = repr(value) if isinstance(value, float) else str(value).strip()
        args += ["--set", f"{name}={text}"]
    if "f_start_hz" in spec:
        args += ["--freq", f"{_hz(spec['f_start_hz'])}:{_hz(spec['f_stop_hz'])}:"
                           f"{int(spec['f_points'])}"]
    elif spec.get("frequencies_hz"):
        args += ["--points", ",".join(_hz(f) for f in spec["frequencies_hz"])]
    if spec.get("sweep_type"):
        args += ["--sweep-type", str(spec["sweep_type"]).lower()]
    if int(spec.get("cores") or 0) > 0:
        args += ["--cores", str(int(spec["cores"]))]
    if spec.get("validate"):
        args.append("--validate")
    if spec.get("require_convergence"):
        args.append("--require-convergence")
    if spec.get("fields"):
        args.append("--fields")
    # The run folder is fresh, so --force changes nothing; a re-run of the
    # same job is decided by eda-agent's own job cache.
    args += ["-o", str(out), "--progress", "json", "--json"]
    return args


# ------------------------------------------------------------------ project

def prepare_project(source: str | Path, folder: str | Path, project_file: str = "",
                    name: str = "") -> Path:
    """Copy (``.hzn``) or unpack (``.zip``) a project into ``folder/project``
    and return the ``.hzn`` to solve. ``name`` is the project's own file
    name, kept for the copy (the job keeps its inputs under role names)."""
    source, dest = Path(source), Path(folder) / "project"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    suffix = source.suffix.lower()
    if suffix == ".hzn":
        if project_file:
            raise HertzonError("project_file is for a .zip project")
        own = Path(name).name if name and Path(name).suffix.lower() == ".hzn" else ""
        target = dest / (own or source.name)
        shutil.copyfile(source, target)
        return target
    if suffix != ".zip":
        raise HertzonError(f"{source.name}: a Hertzon project is a .hzn file or a .zip "
                           "of its folder")
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise HertzonError(f"{source.name} is not a readable zip archive") from exc
    with zf:
        total = 0
        root = dest.resolve()
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            target = (dest / name).resolve()
            if not (target == root or root in target.parents) or name.startswith("/"):
                raise HertzonError(f"{source.name}: entry {info.filename!r} points outside "
                                   "the archive")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise HertzonError(f"{source.name} unpacks to more than "
                                   f"{MAX_UNPACKED_BYTES // 1024 ** 3} GB")
        zf.extractall(dest)
    found = sorted(p for p in dest.rglob("*.hzn")
                   if not any(part.endswith(".hznresults") for part in p.parts))
    if project_file:
        want = project_file.replace("\\", "/").strip("/")
        match = [p for p in found
                 if p.relative_to(dest).as_posix() == want or p.name == want]
        if len(match) != 1:
            raise HertzonError(f"no single {project_file!r} in {source.name}; it holds "
                               + (", ".join(p.relative_to(dest).as_posix() for p in found)
                                  or "no .hzn file"))
        return match[0]
    if len(found) != 1:
        raise HertzonError(f"{source.name} holds "
                           + (f"{len(found)} .hzn files ("
                              + ", ".join(p.relative_to(dest).as_posix() for p in found)
                              + "): name one with project_file" if found
                              else "no .hzn file"))
    return found[0]


# -------------------------------------------------------------------- runs

def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def start_solve(project: str | Path, spec: dict[str, Any], folder: str | Path,
                cli: str = "", python: str = "") -> dict[str, Any]:
    """Start ``hertzon-cli solve`` on ``project`` as a detached process
    watched by ``hertzon_runner.py``; the Touchstone lands in ``folder``."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cli = _cli_or_raise(cli)
    args = solve_args(project, spec, folder)
    for stale in ("exit.json", "summary.json", "progress.jsonl", "run.json"):
        (folder / stale).unlink(missing_ok=True)
    from ..noconsole import no_window
    cmd = [python or sys.executable, "-I", str(RUNNER), str(folder), "--", cli, *args]
    with open(folder / "runner.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=str(folder), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, env=child_env(),
                                **no_window())
    (folder / "run.json").write_text(json.dumps(
        {"pid": proc.pid, "started": time.time(), "cli": cli, "args": args}, indent=1),
        encoding="utf-8")
    return {"ok": True, "pid": proc.pid, "cli": cli, "args": args}


def _alive(pid: int) -> bool:
    import psutil
    if pid <= 0 or not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except Exception:  # noqa: BLE001 - cannot inspect it: count it as running
        return True


def progress_events(folder: str | Path) -> dict[str, Any]:
    """What Hertzon has said so far: the last progress percentage and
    message, the adaptive passes, warnings, errors, and plain-text lines."""
    path = Path(folder) / "progress.jsonl"
    out: dict[str, Any] = {"percent": None, "message": "", "passes": [], "warnings": [],
                           "errors": [], "text": [], "done": None}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        ev = _json_from(line) if line.startswith("{") else None
        if not ev:
            out["text"].append(line)
            continue
        kind = ev.get("event")
        if kind == "progress":
            if _finite(ev.get("percent")):
                out["percent"] = float(ev["percent"])
            out["message"] = str(ev.get("message", ""))
        elif kind == "pass":
            out["passes"].append({k: ev.get(k) for k in ("pass", "tets", "delta_s")})
        elif kind == "log":
            if str(ev.get("level", "")).lower() in ("warning", "warn"):
                out["warnings"].append(str(ev.get("message", "")))
            elif str(ev.get("level", "")).lower() == "error":
                out["errors"].append(str(ev.get("message", "")))
        elif kind == "error":
            out["errors"].append(str(ev.get("message", "")))
        elif kind == "done":
            out["done"] = ev
    out["text"] = out["text"][-20:]
    out["warnings"] = out["warnings"][-20:]
    return out


def solve_status(folder: str | Path) -> dict[str, Any]:
    """``running`` (with progress), ``done``, ``failed`` or ``not_started``."""
    folder = Path(folder)
    run = _read(folder / "run.json")
    if not run:
        return {"state": "not_started"}
    ev = progress_events(folder)
    ended = _read(folder / "exit.json")
    if ended is None:
        if _alive(int(run.get("pid") or 0)):
            msg = ev["message"] or "starting Hertzon"
            if ev["passes"]:
                p = ev["passes"][-1]
                msg += f" (adaptive pass {p.get('pass')}, {p.get('tets')} tetrahedra"
                msg += f", delta S {p['delta_s']:.3g})" if _finite(p.get("delta_s")) else ")"
            out: dict[str, Any] = {"state": "running", "message": msg,
                                   "elapsed_s": round(time.time() - float(run.get("started")
                                                                          or time.time()))}
            if ev["percent"] is not None:
                out["progress"] = max(0.0, min(1.0, ev["percent"] / 100.0))
            return out
        # Read once more: the runner may have finished between the two reads.
        ended = _read(folder / "exit.json")
        if ended is None:
            return {"state": "failed", "error": "the Hertzon run stopped without saying how "
                                                "it ended", "log_tail": _tail(folder)}
    code = int(ended.get("code", -1))
    if code in (0, 5):
        return {"state": "done", "code": code}
    text = "\n".join(ev["text"])
    if ev["errors"]:
        text += "\n" + json.dumps({"event": "error", "message": ev["errors"][-1]})
    error = ended.get("error") or _failure(code, text)
    return {"state": "failed", "code": code, "error": error, "log_tail": _tail(folder)}


def _tail(folder: Path, size: int = 1500) -> str:
    parts = []
    for name in ("progress.jsonl", "runner.log"):
        try:
            t = (folder / name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if t:
            parts.append(f"--- {name}\n{t[-size:]}")
    return "\n".join(parts)


def solve_results(folder: str | Path) -> dict[str, Any]:
    """The summary and Touchstone file of a finished solve."""
    folder = Path(folder)
    ended = _read(folder / "exit.json") or {}
    summary = _read(folder / "summary.json")
    if summary is None:
        try:
            summary = _json_from((folder / "summary.json").read_text(
                encoding="utf-8", errors="replace")) or {}
        except OSError:
            summary = {}
    ts = None
    named = str(summary.get("touchstone") or "")
    if named and named != "-":
        p = Path(named)
        if not p.is_absolute():
            p = folder / p
        if p.is_file():
            ts = p
    if ts is None:
        found = sorted(folder.glob("*.s*p"), key=lambda p: p.stat().st_mtime)
        ts = found[-1] if found else None
    if ts is None:
        return {"ok": False, "error": "Hertzon finished but wrote no Touchstone file",
                "log_tail": _tail(folder)}
    ev = progress_events(folder)
    return {"ok": True, "touchstone": ts, "summary": summary, "code": int(ended.get("code", 0)),
            "warnings": ev["warnings"], "passes": ev["passes"]}


def cancel_solve(folder: str | Path) -> bool:
    """Stop the runner and the Hertzon processes under it."""
    run = _read(Path(folder) / "run.json") or {}
    pid = int(run.get("pid") or 0)
    if pid <= 0:
        return False
    import psutil
    try:
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return False
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    return True
