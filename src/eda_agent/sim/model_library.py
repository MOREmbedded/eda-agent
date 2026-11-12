# SPDX-License-Identifier: Apache-2.0
"""A searchable index of the SPICE models on this computer.

Two kinds of source are read:

* **Model files** (.lib, .sub, .mod, .cir, .ckt, .mdl, .sp, .spi, .inc,
  .txt holding SPICE): every ``.SUBCKT`` header (name and node list,
  continuation lines included) and every ``.MODEL`` card (name and device
  type).
* **LTspice symbols** (.asy): the attributes that tie a symbol to its
  model (``Prefix``, ``SpiceModel``, ``Value``, ``ModelFile``,
  ``Description``) and every pin's name with its ``SpiceOrder``. For the
  models LTspice ships, many of which are encrypted, the symbol is the
  only readable statement of which node is which, so it is indexed even
  when the model file itself cannot be parsed.

The index is plain JSON and belongs to a design workspace, never to the
repository: ``<workspace>/.eda-agent/spice-models/index.json``. A file
whose size and modification time have not changed is not read again.

Nothing here talks to Altium or runs a simulator.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

#: Extensions read as SPICE text. ``.txt`` is included because vendors ship
#: models as .txt surprisingly often; a .txt with no .SUBCKT/.MODEL simply
#: contributes nothing.
MODEL_EXTENSIONS = frozenset({".lib", ".sub", ".mod", ".cir", ".ckt", ".mdl",
                              ".sp", ".spi", ".spice", ".inc", ".txt", ".bjt",
                              ".dio", ".jft", ".mos", ".ind", ".cap", ".res"})
SYMBOL_EXTENSIONS = frozenset({".asy"})

#: A model file larger than this is skipped (LTspice's standard.* files are
#: well under it; a 50 MB "model" is a dump, not a library).
MAX_FILE_BYTES = 20 * 1024 * 1024
#: Walking stops after this many files per folder, so pointing the scan at a
#: drive root fails quickly instead of reading everything.
MAX_FILES_PER_ROOT = 60_000

INDEX_VERSION = 1

_SUBCKT = re.compile(r"^\s*\.subckt\s+(\S+)(.*)$", re.I)
_MODEL = re.compile(r"^\s*\.model\s+(\S+)\s+([A-Za-z]+)", re.I)
_ENDS = re.compile(r"^\s*\.ends\b", re.I)


@dataclass
class ModelEntry:
    """One model or one symbol, as found on disk."""

    name: str
    kind: str                      # "subckt" | "model" | "symbol"
    file: str
    nodes: list[str] = field(default_factory=list)
    device: str = ""               # .MODEL type (D, NPN, NMOS, VDMOS, ...)
    description: str = ""
    params: list[str] = field(default_factory=list)
    # symbol-only
    prefix: str = ""
    model_file: str = ""
    pins: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in ("", [], None)}


# ---------------------------------------------------------------- reading files
def read_text(path: Path) -> str:
    """Model files come as ASCII, UTF-8, Latin-1 and (LTspice 24) UTF-16."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if len(raw) > 3 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """SPICE lines with '+' continuations joined; (first line number, text)."""
    out: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("+") and out:
            num, prev = out[-1]
            out[-1] = (num, prev + " " + stripped[1:])
        else:
            out.append((n, line))
    return out


def _strip_inline_comment(line: str) -> str:
    # ';' starts a comment in LTspice and PSpice; '$' in some vendor files.
    for mark in (";", " $ "):
        if mark in line:
            line = line.split(mark, 1)[0]
    return line


def _header_nodes(rest: str) -> tuple[list[str], list[str]]:
    """Split what follows ``.SUBCKT name`` into nodes and parameters."""
    rest = _strip_inline_comment(rest)
    nodes: list[str] = []
    params: list[str] = []
    in_params = False
    for tok in rest.replace(",", " ").split():
        low = tok.lower()
        if low in ("params:", "param:"):
            in_params = True
            continue
        if low.startswith(("params:", "param:")):
            in_params = True
            tok = tok.split(":", 1)[1]
            if not tok:
                continue
        if in_params or "=" in tok:
            in_params = True
            key = tok.split("=", 1)[0]
            if key:
                params.append(key)
            continue
        nodes.append(tok)
    return nodes, params


def _comment_above(lines: list[tuple[int, str]], idx: int) -> str:
    """The comment block right above a header, as a one-line description."""
    texts: list[str] = []
    j = idx - 1
    while j >= 0 and len(texts) < 4:
        s = lines[j][1].strip()
        if not s.startswith("*"):
            break
        body = s.lstrip("*").strip()
        if body and not set(body) <= set("-=*#_ "):
            texts.append(body)
        j -= 1
    return " ".join(reversed(texts))[:200]


def parse_model_text(text: str, file: str = "") -> list[ModelEntry]:
    """Every .SUBCKT and top-level .MODEL in SPICE text."""
    lines = _logical_lines(text)
    found: list[ModelEntry] = []
    depth = 0
    for i, (_, line) in enumerate(lines):
        m = _SUBCKT.match(line)
        if m:
            depth += 1
            nodes, params = _header_nodes(m.group(2))
            found.append(ModelEntry(name=m.group(1), kind="subckt", file=file,
                                    nodes=nodes, params=params,
                                    description=_comment_above(lines, i)))
            continue
        if _ENDS.match(line):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            m = _MODEL.match(line)
            if m:
                found.append(ModelEntry(name=m.group(1), kind="model", file=file,
                                        device=m.group(2).upper(),
                                        description=_comment_above(lines, i)))
    return found


def parse_asy(text: str, file: str = "") -> Optional[ModelEntry]:
    """An LTspice symbol's model attributes and pins (in SpiceOrder)."""
    attrs: dict[str, str] = {}
    pins: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("SYMATTR "):
            parts = s.split(None, 2)
            if len(parts) >= 2:
                attrs[parts[1]] = parts[2].strip() if len(parts) > 2 else ""
        elif s.startswith("PIN "):
            current = {"name": "", "order": None}
            pins.append(current)
        elif s.startswith("PINATTR ") and current is not None:
            parts = s.split(None, 2)
            if len(parts) < 3:
                continue
            if parts[1] == "PinName":
                current["name"] = parts[2].strip()
            elif parts[1] == "SpiceOrder":
                try:
                    current["order"] = int(parts[2])
                except ValueError:
                    pass
    if not pins and not attrs:
        return None
    ordered = sorted((p for p in pins if p["order"] is not None),
                     key=lambda p: p["order"])
    spice_model = attrs.get("SpiceModel", "")
    value = attrs.get("Value", "")
    model_file = attrs.get("ModelFile", "")
    # ADI's own symbols put the FILE in SpiceModel and the subcircuit in
    # Value; third-party symbols usually put the subcircuit in SpiceModel.
    if not model_file and Path(spice_model).suffix.lower() in MODEL_EXTENSIONS:
        model_file, spice_model = spice_model, ""
    name = spice_model or attrs.get("Value2", "") or value or Path(file).stem
    return ModelEntry(
        name=name, kind="symbol", file=file,
        nodes=[p["name"] for p in ordered],
        prefix=attrs.get("Prefix", "").upper(),
        model_file=model_file,
        description=attrs.get("Description", "")[:200],
        pins=[{"name": p["name"], "spice_order": p["order"]} for p in ordered])


# ---------------------------------------------------------------- scanning
def _walk(root: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
        for fn in filenames:
            count += 1
            if count > MAX_FILES_PER_ROOT:
                return
            yield Path(dirpath) / fn


def _stamp(path: Path) -> list[float]:
    st = path.stat()
    return [st.st_size, round(st.st_mtime, 3)]


def scan(roots: Iterable[Path], previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Index every model file and symbol under ``roots``.

    ``previous`` is an earlier index: files whose size and mtime are
    unchanged keep their entries without being read again.
    """
    old_files: dict[str, Any] = (previous or {}).get("files", {}) \
        if (previous or {}).get("version") == INDEX_VERSION else {}
    files: dict[str, Any] = {}
    reused = read = skipped = 0
    seen_roots: list[str] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        seen_roots.append(str(root))
        for path in _walk(root):
            ext = path.suffix.lower()
            if ext not in MODEL_EXTENSIONS and ext not in SYMBOL_EXTENSIONS:
                continue
            key = str(path)
            if key in files:
                continue
            try:
                stamp = _stamp(path)
            except OSError:
                continue
            if stamp[0] > MAX_FILE_BYTES:
                skipped += 1
                continue
            old = old_files.get(key)
            if old and old.get("stamp") == stamp:
                files[key] = old
                reused += 1
                continue
            try:
                text = read_text(path)
            except OSError:
                skipped += 1
                continue
            if ext in SYMBOL_EXTENSIONS:
                sym = parse_asy(text, key)
                entries = [sym.to_dict()] if sym and (sym.model_file or sym.prefix == "X"
                                                      or sym.nodes) else []
            else:
                entries = [e.to_dict() for e in parse_model_text(text, key)]
            files[key] = {"stamp": stamp, "entries": entries}
            read += 1
    return {"version": INDEX_VERSION, "roots": seen_roots, "files": files,
            "stats": {"files": len(files), "read": read, "reused": reused,
                      "skipped": skipped,
                      "models": sum(1 for f in files.values() for e in f["entries"]
                                    if e["kind"] != "symbol"),
                      "symbols": sum(1 for f in files.values() for e in f["entries"]
                                     if e["kind"] == "symbol")}}


def entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for f in (index.get("files") or {}).values() for e in f.get("entries", [])]


# ---------------------------------------------------------------- searching
_PACKAGE_TAIL = re.compile(
    r"(#?(TR|PBF|TRPBF|G4|CT|ND|-?REEL|R7|R13|/TR))+$")


def normalize_part(text: str) -> str:
    """Upper-case alphanumerics only, with reel/lead-free tails dropped."""
    s = re.sub(r"[^A-Z0-9#/]", "", str(text).upper())
    s = _PACKAGE_TAIL.sub("", s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _score(query: str, name: str) -> int:
    """How well a model name answers a part-number query (0 = not at all)."""
    if not query or not name:
        return 0
    q, n = normalize_part(query), normalize_part(name)
    if not q or not n:
        return 0
    if q == n:
        return 100
    # The MPN carries a package/grade suffix the model name lacks:
    # LT3080EDD -> LT3080, TPS7A4701RGWR -> TPS7A4701, ADA4898-1YRDZ -> ADA4898-1.
    if q.startswith(n) and len(n) >= 4:
        return 60 + min(30, int(30 * len(n) / len(q)))
    if n.startswith(q) and len(q) >= 4:
        return 50 + min(20, int(20 * len(q) / len(n)))
    if len(q) >= 5 and q in n:
        return 30
    return 0


def find(index: dict[str, Any], query: str, limit: int = 10,
         kinds: tuple[str, ...] = ("subckt", "model", "symbol")) -> list[dict[str, Any]]:
    """Best matches for a model name or manufacturer part number."""
    scored = []
    for e in entries(index):
        if e.get("kind") not in kinds:
            continue
        s = max(_score(query, e.get("name", "")),
                _score(query, Path(e.get("model_file", "")).stem) - 5
                if e.get("model_file") else 0)
        if s:
            scored.append((s, e))
    scored.sort(key=lambda t: (-t[0], t[1].get("kind") != "subckt", t[1].get("name", "")))
    return [{"score": s, **e} for s, e in scored[:max(1, limit)]]


def resolve(index: dict[str, Any], name: str) -> dict[str, Any]:
    """Everything known about one model name: its definition(s) and symbol(s).

    Returns ``definitions`` (subckt/model entries, exact name match,
    case-insensitive), ``symbols`` (LTspice symbols naming it) and
    ``model_file`` (the single file to include, when unambiguous).
    """
    key = name.strip().lower()
    defs = [e for e in entries(index)
            if e.get("kind") in ("subckt", "model") and e.get("name", "").lower() == key]
    syms = [e for e in entries(index)
            if e.get("kind") == "symbol" and (
                e.get("name", "").lower() == key
                or Path(e.get("model_file", "")).stem.lower() == key)]
    files = sorted({d["file"] for d in defs})
    model_file = files[0] if len(files) == 1 else ""
    if not model_file and not defs and syms:
        # An encrypted LTspice model: the symbol names the file; find it.
        wanted = {Path(s["model_file"]).name.lower() for s in syms if s.get("model_file")}
        hits = sorted({f for f in (index.get("files") or {})
                       if Path(f).name.lower() in wanted})
        model_file = hits[0] if len(hits) == 1 else ""
        files = hits
    return {"name": name, "definitions": defs, "symbols": syms,
            "model_file": model_file, "candidate_files": files}


# ---------------------------------------------------------------- storage
DATA_DIR = Path(".eda-agent") / "spice-models"
INDEX_FILE = "index.json"
PIN_MAPS_FILE = "pin_maps.json"


def data_dir(workspace: Optional[Path] = None) -> Path:
    """Where this workspace keeps its model index and pin maps.

    The design workspace (the folder ``eda-agent start`` ran in, found by
    its .eda-agent/workspace.json) when there is one, else the active
    profile's folder, else ~/.eda-agent. Never the repository.
    """
    if workspace is not None:
        return Path(workspace) / DATA_DIR
    try:
        from ..workspace_start import find_workspace
        ws = find_workspace(Path.cwd())
    except Exception:                                   # noqa: BLE001
        ws = None
    if ws is not None:
        return ws / DATA_DIR
    try:
        from ..preferences import profile_directory
        prof = profile_directory()
    except Exception:                                   # noqa: BLE001
        prof = None
    if prof is not None:
        return Path(prof) / "spice-models"
    return Path.home() / ".eda-agent" / "spice-models"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data: Any) -> None:
    from ..atomicfile import discard, replace_with_retry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        replace_with_retry(tmp, path)
    except BaseException:
        discard(tmp)
        raise


def ltspice_library_dirs() -> list[Path]:
    """LTspice's own model and symbol folders, where they exist.

    LTspice 24 keeps them in %LOCALAPPDATA%\\LTspice\\lib; LTspice XVII in
    Documents\\LTspiceXVII\\lib. Only sub/, cmp/ and sym/ are useful.
    """
    bases = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        bases.append(Path(local) / "LTspice" / "lib")
    home = Path.home()
    bases += [home / "Documents" / "LTspiceXVII" / "lib",
              home / "Documents" / "LTspice" / "lib"]
    out = []
    for b in bases:
        for sub in ("sub", "cmp", "sym"):
            p = b / sub
            if p.is_dir():
                out.append(p)
    return out


def default_roots(workspace: Optional[Path] = None) -> list[Path]:
    """The folders scanned when none are named: the workspace's own
    ``models``/``spice`` folders, EDA_AGENT_SPICE_MODEL_DIRS, and LTspice's
    library."""
    roots: list[Path] = []
    ws = workspace
    if ws is None:
        try:
            from ..workspace_start import find_workspace
            ws = find_workspace(Path.cwd())
        except Exception:                               # noqa: BLE001
            ws = None
    if ws is not None:
        for name in ("models", "spice", "spice_models", "Simulation"):
            p = Path(ws) / name
            if p.is_dir():
                roots.append(p)
    listed = os.environ.get("EDA_AGENT_SPICE_MODEL_DIRS", "")
    roots += [Path(p) for p in listed.split(os.pathsep) if p.strip()]
    roots += ltspice_library_dirs()
    return roots
