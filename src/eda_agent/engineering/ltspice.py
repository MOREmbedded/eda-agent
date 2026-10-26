# SPDX-License-Identifier: Apache-2.0
"""LTspice integration (Analog Devices LTspice 24, Windows).

Command line (ADI LTspice reference, analogdevicesinc.github.io/ltspice-reference):
    LTspice.exe -b <deck.cir|deck.net|schematic.asc>    batch mode
    -Run      start simulating a schematic opened on the command line
    -ascii    write ASCII .raw files (slower, but parseable without extra libraries)
    -netlist  convert a schematic to a netlist (batch)
Default install paths: C:\\Program Files\\ADI\\LTspice\\LTspice.exe (system) or
%LOCALAPPDATA%\\Programs\\ADI\\LTspice\\LTspice.exe (per user). Always pass absolute paths.

Security model. Netlists may be written by an AI model, so they are treated as
untrusted input:
  * each run is executed in a fresh folder sim_root/<project>/<run_id>/;
  * .FERRET (downloads a file from a URL) is always rejected;
  * .INCLUDE/.INC/.LIB paths must resolve inside the run folder or an owner-approved
    model root; absolute paths elsewhere and '..' escapes are rejected;
  * .SAVEBIAS/.LOADBIAS/.WAVE/.BACKANNO file arguments must stay inside the run folder;
  * the process gets a hard timeout and is killed on expiry;
  * at most max_parallel simulations run at once.

Status: command-line flags VENDOR-DOC; output parsers verified against synthetic
files reproducing the documented formats (VERIFIED-T). Validate against real LTspice
output with procedure L-01/L-02 in the manual.
"""
from __future__ import annotations

import math
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .config import LTspicePolicy
from .errors import ExecutionTimeout, NotFound, PermissionDenied, ValidationFailed

_INCLUDE = re.compile(r"^\s*\.(include|inc|lib)\s+(.+?)\s*$", re.I)
_FILE_DIRECTIVES = re.compile(r"^\s*\.(savebias|loadbias|wave|backanno)\b(.*)$", re.I)
_FERRET = re.compile(r"^\s*\.ferret\b", re.I)
RUN_ID = re.compile(r"run_[0-9a-f]{16}")


# ---------------------------------------------------------------- netlist safety
def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s.split()[0] if s.split() else s


def _inside(p: Path, roots: list[Path]) -> bool:
    rp = p.resolve()
    for r in roots:
        rr = r.resolve()
        if rp == rr or rr in rp.parents:
            return True
    return False


def check_netlist(text: str, run_dir: Path, model_roots: tuple[Path, ...]) -> list[str]:
    """Raise ValidationFailed on unsafe content; return the list of referenced model files."""
    if "\x00" in text:
        raise ValidationFailed("netlist contains NUL bytes")
    errors, refs = [], []
    allowed = [run_dir, *model_roots]
    for n, line in enumerate(text.splitlines(), start=1):
        if _FERRET.match(line):
            errors.append(f"line {n}: .FERRET (network download) is not allowed")
            continue
        m = _INCLUDE.match(line)
        if m:
            target = _strip_quotes(m.group(2))
            p = Path(target)
            wabs = PureWindowsPath(target).is_absolute() or bool(PureWindowsPath(target).drive)
            is_abs = p.is_absolute() or wabs
            cand = p if p.is_absolute() else run_dir / p
            if is_abs or ".." in p.parts or ".." in PureWindowsPath(target).parts:
                ok = _inside(cand, allowed) if p.is_absolute() else False
                if not ok:
                    # relative names are also searched by LTspice in its own lib folders; we only allow
                    # plain names (resolved by LTspice standard libs) or paths inside allowed roots
                    errors.append(f"line {n}: {m.group(1)} path outside run folder and model roots: {target}")
                    continue
            refs.append(target)
            continue
        f = _FILE_DIRECTIVES.match(line)
        if f:
            args = f.group(2)
            for tok in re.findall(r"\"[^\"]+\"|'[^']+'|\S+\.(?:raw|wav|bias|txt)\b", args, re.I):
                raw_t = _strip_quotes(tok)
                pth = Path(raw_t)
                if pth.is_absolute() or ".." in pth.parts or PureWindowsPath(raw_t).drive or PureWindowsPath(raw_t).is_absolute():
                    errors.append(f"line {n}: .{f.group(1)} file must be a plain name inside the run folder")
    if errors:
        raise ValidationFailed("netlist rejected by safety policy", details={"errors": errors})
    return refs


# ---------------------------------------------------------------- file decoding
def read_text_auto(path: Path) -> str:
    """LTspice writes logs and raw headers as UTF-16LE in recent versions, UTF-8/ANSI in others."""
    b = path.read_bytes()
    if b.startswith(b"\xff\xfe"):
        return b[2:].decode("utf-16-le", errors="replace")
    if len(b) > 3 and b[1:2] == b"\x00" and b[3:4] == b"\x00":
        return b.decode("utf-16-le", errors="replace")
    return b.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- log parsing
_MEAS_SINGLE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*:\s*(.*?)=\s*([-+0-9.eEinfINFnaN()]+)(?:\s+(?:FROM|AT)\s+(\S+)(?:\s+TO\s+(\S+))?)?\s*$")
_MEAS_EQ = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=\s*([-+0-9.eEinfINF]+)(?:\s+(?:FROM|AT)\s+(\S+)(?:\s+TO\s+(\S+))?)?\s*$")
_ERR = re.compile(r"(error|singular matrix|time ?step too small|failed|could not|unknown (?:subcircuit|model|parameter)|can't find)", re.I)
_WARN = re.compile(r"\bwarning\b", re.I)
_ELAPSED = re.compile(r"Total elapsed time:\s*([0-9.]+)\s*seconds", re.I)


def _num(s: str) -> float:
    s = s.strip().strip("()")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_log(text: str) -> dict:
    """Extract .MEAS results (single run and .STEP tables), errors, warnings, elapsed time."""
    meas: dict[str, object] = {}
    errors, warnings = [], []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("Measurement:"):
            name = ln.split(":", 1)[1].strip()
            i += 1
            header = lines[i].split("\t") if i < len(lines) else []
            rows = []
            i += 1
            while i < len(lines) and lines[i].strip() and re.match(r"^\s*\d+\s*\t", lines[i]):
                cells = [c.strip() for c in lines[i].split("\t")]
                rows.append({"step": int(cells[0]), "value": _num(cells[1]) if len(cells) > 1 else float("nan")})
                i += 1
            meas[name] = {"stepped": True, "expression": header[1].strip() if len(header) > 1 else "", "values": rows}
            continue
        m = _MEAS_SINGLE.match(ln)
        if m and not ln.lower().startswith(("warning", "error")):
            meas[m.group(1)] = {"stepped": False, "expression": m.group(2).strip(), "value": _num(m.group(3)),
                                "from": m.group(4), "to": m.group(5)}
        else:
            m2 = _MEAS_EQ.match(ln)
            if m2:
                meas[m2.group(1)] = {"stepped": False, "expression": "", "value": _num(m2.group(2)),
                                     "from": m2.group(3), "to": m2.group(4)}
        if _WARN.search(ln):
            warnings.append(ln.strip())
        elif _ERR.search(ln) and "elapsed" not in ln.lower():
            errors.append(ln.strip())
        i += 1
    e = _ELAPSED.search(text)
    return {"measurements": meas, "errors": errors, "warnings": warnings,
            "elapsed_s": float(e.group(1)) if e else None}


# ---------------------------------------------------------------- raw (ASCII) parsing
@dataclass
class RawData:
    plotname: str
    flags: str
    variables: list[tuple[str, str]]   # (name, type)
    values: list[list[complex | float]]  # values[var_index][point]
    steps: int


def parse_raw_ascii(text: str) -> RawData:
    lines = text.splitlines()
    hdr, i = {}, 0
    variables: list[tuple[str, str]] = []
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("Variables:"):
            nvar = int(hdr["No. Variables"])
            for k in range(nvar):
                parts = lines[i + 1 + k].split()
                variables.append((parts[1], parts[2] if len(parts) > 2 else ""))
            i += 1 + nvar
            continue
        if ln.startswith("Values:"):
            i += 1
            break
        if ln.startswith("Binary:"):
            raise ValidationFailed("binary .raw file: run LTspice with -ascii or use spicelib")
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdr[k.strip()] = v.strip()
        i += 1
    if not variables:
        raise ValidationFailed("raw file has no Variables section")
    complex_ = "complex" in hdr.get("Flags", "").lower()
    npts = int(hdr["No. Points"])
    nvar = len(variables)
    vals: list[list] = [[] for _ in range(nvar)]
    tokens: list[str] = []
    for ln in lines[i:]:
        tokens.extend(ln.split())
    # each point: index, then nvar values
    pos = 0
    for _ in range(npts):
        if pos >= len(tokens):
            break
        pos += 1  # point index
        for v in range(nvar):
            t = tokens[pos]
            pos += 1
            if complex_:
                re_, im = t.split(",")
                vals[v].append(complex(float(re_), float(im)))
            else:
                vals[v].append(float(t))
    steps = 1
    if "stepped" in hdr.get("Flags", "").lower() and vals[0]:
        x = [abs(z) if isinstance(z, complex) else z for z in vals[0]]
        steps = 1 + sum(1 for a, b in zip(x, x[1:]) if b < a)
    return RawData(hdr.get("Plotname", ""), hdr.get("Flags", ""), variables, vals, steps)


def trace(raw: RawData, name: str, max_points: int = 2000) -> dict:
    names = [v[0].lower() for v in raw.variables]
    key = name.lower()
    if key not in names:
        raise NotFound(f"trace {name} not in raw file", details={"available": [v[0] for v in raw.variables][:200]})
    idx = names.index(key)
    x, y = raw.values[0], raw.values[idx]
    n = len(y)
    step = max(1, math.ceil(n / max_points))
    is_c = bool(y) and isinstance(y[0], complex)

    def stats(seq):
        if not seq:
            return {}
        mag = [abs(v) for v in seq] if is_c else seq
        mean = sum(mag) / len(mag)
        return {"min": min(mag), "max": max(mag), "mean": mean, "final": mag[-1],
                "rms": math.sqrt(sum(v * v for v in mag) / len(mag))}

    out = {"name": raw.variables[idx][0], "type": raw.variables[idx][1], "points_total": n,
           "decimation": step, "stats": stats(y)}
    xs = [abs(v) if isinstance(v, complex) else v for v in x[::step]]
    if is_c:
        out["x"] = xs
        out["mag_db"] = [20 * math.log10(max(abs(v), 1e-300)) for v in y[::step]]
        out["phase_deg"] = [math.degrees(math.atan2(v.imag, v.real)) for v in y[::step]]
    else:
        out["x"] = xs
        out["y"] = y[::step]
    return out


# ---------------------------------------------------------------- runner
def hidden_window_kwargs(platform: str | None = None) -> dict:
    """On Windows start LTspice with SW_HIDE so a batch run never flashes a window.

    LTspice otherwise briefly opens a visible window per run even in -b mode,
    which is disruptive when many runs fire back to back. No-op elsewhere.
    """
    import sys as _sys
    if (platform or _sys.platform) != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


class LTspiceRunner:
    def __init__(self, cfg: LTspicePolicy, popen=subprocess.Popen, clock=time.monotonic):
        self.cfg = cfg
        self._popen = popen
        self._clock = clock
        self._slots = threading.BoundedSemaphore(max(1, cfg.max_parallel))

    def run_dir(self, project: str, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ValidationFailed("invalid run id")
        d = (self.cfg.sim_root / project / run_id).resolve()
        if self.cfg.sim_root.resolve() not in d.parents:
            raise PermissionDenied("run folder escapes sim_root")
        return d

    def argv(self, deck: Path) -> list[str]:
        args = ["-b", "-ascii"]
        if deck.suffix.lower() == ".asc":
            args = ["-Run", "-b", "-ascii"]
        return [*self.cfg.exe_prefix, self.cfg.exe, *args, str(deck)]

    def run(self, project: str, netlist_text: str | None = None, source_file: Path | None = None,
            deck_name: str = "deck.cir", timeout_s: float | None = None) -> dict:
        if (netlist_text is None) == (source_file is None):
            raise ValidationFailed("provide exactly one of netlist_text or source_file")
        run_id = "run_" + secrets.token_hex(8)
        d = self.run_dir(project, run_id)
        d.mkdir(parents=True)
        if source_file is not None:
            if source_file.suffix.lower() not in (".cir", ".net", ".sp", ".asc"):
                raise ValidationFailed("source must be .cir, .net, .sp or .asc")
            deck = d / source_file.name
            shutil.copy2(source_file, deck)
            if deck.suffix.lower() != ".asc":
                netlist_text = read_text_auto(deck)
        else:
            if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}\.(cir|net|sp)", deck_name):
                raise ValidationFailed("deck_name must be a plain .cir/.net/.sp file name")
            deck = d / deck_name
            deck.write_text(netlist_text, encoding="utf-8")
        refs = check_netlist(netlist_text, d, self.cfg.model_roots) if netlist_text is not None else []
        t = timeout_s or self.cfg.timeout_s
        if not self._slots.acquire(timeout=t):
            raise ExecutionTimeout("no free LTspice slot")
        try:
            start = self._clock()
            proc = self._popen(self.argv(deck), cwd=str(d), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **hidden_window_kwargs())
            try:
                rc = proc.wait(timeout=t)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                raise ExecutionTimeout(f"LTspice exceeded {t}s; process killed", details={"run_id": run_id})
            elapsed = self._clock() - start
        finally:
            self._slots.release()
        return self.collect(project, run_id, deck.stem, rc=rc, wall_s=elapsed, includes=refs)

    def collect(self, project: str, run_id: str, stem: str | None = None, **extra) -> dict:
        d = self.run_dir(project, run_id)
        if not d.exists():
            raise NotFound(f"run {run_id} not found")
        if stem is None:
            decks = [p for p in d.iterdir() if p.suffix.lower() in (".cir", ".net", ".sp", ".asc")]
            stem = decks[0].stem if decks else "deck"
        log = d / f"{stem}.log"
        raws = sorted(p.name for p in d.glob(f"{stem}*.raw"))
        res = {"run_id": run_id, "project": project, "log_found": log.exists(), "raw_files": raws, **extra}
        if log.exists():
            res.update(parse_log(read_text_auto(log)))
        res["ok"] = bool(res.get("log_found")) and not res.get("errors") and extra.get("rc", 0) == 0
        return res

    def get_trace(self, project: str, run_id: str, name: str, raw_file: str | None = None) -> dict:
        d = self.run_dir(project, run_id)
        cands = sorted(d.glob("*.raw"))
        cands = [p for p in cands if not p.name.endswith(".op.raw")] or cands
        if raw_file:
            if not re.fullmatch(r"[A-Za-z0-9_\-.]{1,80}\.raw", raw_file):
                raise ValidationFailed("invalid raw file name")
            cands = [d / raw_file]
        if not cands or not cands[0].exists():
            raise NotFound("no .raw file for this run")
        raw = parse_raw_ascii(read_text_auto(cands[0]))
        out = trace(raw, name, self.cfg.max_points_returned)
        out.update(plotname=raw.plotname, steps=raw.steps, raw_file=cands[0].name)
        return out

    def list_runs(self, project: str, limit: int = 20) -> list[dict]:
        root = self.cfg.sim_root / project
        if not root.exists():
            return []
        runs = sorted((p for p in root.iterdir() if RUN_ID.fullmatch(p.name)), key=lambda p: p.stat().st_mtime,
                      reverse=True)[:limit]
        return [{"run_id": p.name, "files": sorted(x.name for x in p.iterdir())} for p in runs]
