# SPDX-License-Identifier: Apache-2.0
"""An offline copy of an Altium 365 managed library, kept in the profile.

Altium 365 answers one page of a library connection at a time, and
``totalCount`` stops at 10,000, so a library larger than that has to be
read until ``hasNextPage`` is false. Three things the live service does
shape the code here:

* The component list is sorted by name, and a page cursor is the base64
  of the zero-based row index (``"MA=="`` is row 0). A range can
  therefore be read from any row, and several readers can work on
  different ranges at once. The scheme is checked against the server
  before it is relied on; when it differs, one reader follows the
  cursors it is given.
* A request runs into a 90-second limit on the server's side, and pages
  deep in a large list are slow. A page that times out is asked for
  again at half the size, down to five rows, and the size grows back
  after a success.
* Everything read is appended to files in ``scan/`` as it arrives, so a
  scan stopped by a time budget, a lost connection or a closed laptop
  continues where it stopped.

Where it lives: the database belongs to the profile that works with that
workspace, at ``<profile>/library/<workspace host>/``. The profile names
its workspaces in ``profile.json`` under ``altium365_workspaces``. Nothing
is kept in the repository or in a folder shared between profiles.

Files written there:

``components.json`` / ``.csv``  every component, in the library's order
``symbols.json`` / ``.csv``     every symbol, with how many components use it
``footprints.json`` / ``.csv``  every footprint, likewise
``folders.json`` / ``.csv``     the folder tree
``meta.json``                   when, from where, counts, and how it was read
``design_usage.json``           placements found in the user's schematics
``report.json`` / ``.md``       the library health report

Only reading queries are sent. Nothing in the workspace is changed.
"""
from __future__ import annotations

import base64
import binascii
import csv
import json
import re
import shutil
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .altium365 import Altium365Error, _workspace_key
from .atomicfile import replace_with_retry

#: profile.json key listing the profile's Altium 365 workspace URLs.
PROFILE_KEY = "altium365_workspaces"
LIBRARY_SUBDIR = "library"
SCAN_SUBDIR = "scan"

PAGE_SIZE = 200
MIN_PAGE = 5
CHUNK_ROWS = 1000
MAX_ROWS = 2_000_000

_LIFECYCLE = "lifeCycleState { name }"
COMPONENT_FIELDS = (
    "id name comment description revisionId isManaged createdAt modifiedAt "
    "folder { path } componentType { name path } "
    "revision { details { " + _LIFECYCLE + " releaseDate } } "
    "manufacturerParts { companyName partNumber priority } "
    "model3D { parasolidFile { fileName } } "
    "details { itemInternalId parameters { name value } "
    "symbols { id name revisionInternalId itemInternalId } "
    "footprints { id name revisionInternalId itemInternalId } }")
ITEM_FIELDS = ("id name comment description itemInternalId revisionInternalId "
               + _LIFECYCLE + " folder { path } createdAt releasedAt")

_CONNECTION = ("query($ws: String!, $after: String, $first: Int) { "
               "desLibrary(workspaceUrl: $ws) { %s(first: $first, after: $after) { "
               "pageInfo { hasNextPage endCursor } nodes { %s } } } }")
COMPONENTS_QUERY = _CONNECTION % ("components", COMPONENT_FIELDS)
NAMES_QUERY = _CONNECTION % ("components", "id name")
FOLDERS_QUERY = ("query($ws: String!) { desLibrary(workspaceUrl: $ws) { folders "
                 "{ id name path description folderType createdAt updatedAt } } }")
FOLDERS_QUERY_PLAIN = ("query($ws: String!) { desLibrary(workspaceUrl: $ws) "
                       "{ folders { id name path description } } }")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- where the database lives -------------------------------------------------

def workspace_host(workspace_url: str) -> str:
    """The host of a workspace URL, which names its database folder."""
    text = workspace_url.strip()
    if "://" not in text:
        text = "https://" + text
    host = (urllib.parse.urlparse(text).hostname or "").lower()
    if not host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise Altium365Error(f"{workspace_url!r} is not a workspace URL "
                             "(https://<name>.365.altium.com/)")
    return host


def profile_workspaces(profile_dir: Optional[Path]) -> list[str]:
    """The workspace URLs a profile names in profile.json."""
    if not profile_dir:
        return []
    try:
        meta = json.loads((Path(profile_dir) / "profile.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    listed = meta.get(PROFILE_KEY) if isinstance(meta, dict) else None
    if isinstance(listed, str):
        listed = [listed]
    return [_workspace_key(u) for u in listed or [] if isinstance(u, str) and u.strip()]


def _active_profile_dir() -> Optional[Path]:
    from .profile import resolve_profile_dir
    return resolve_profile_dir()


def resolve_target(workspace_url: str = "", database_dir: str = "",
                   profile_dir: Optional[Path] = None) -> tuple[str, Path]:
    """(workspace URL, database folder) for a call.

    The workspace comes from the call, or from the active profile when it
    names exactly one. The folder is ``database_dir`` when given, else
    ``<profile>/library/<host>/``. Without a profile and without
    ``database_dir`` there is nowhere to keep it, and that is refused
    rather than falling back to a shared folder.
    """
    profile = profile_dir if profile_dir is not None else _active_profile_dir()
    url = workspace_url.strip()
    if not url:
        named = profile_workspaces(profile)
        if len(named) == 1:
            url = named[0]
        elif not named and database_dir:
            meta = read_meta(Path(database_dir))
            url = meta.get("workspace_url", "")
        if not url:
            if named:
                raise Altium365Error(
                    "the profile names several workspaces (" + ", ".join(named)
                    + "); say which with workspace_url")
            raise Altium365Error(
                "no workspace given: pass workspace_url, or list it in the "
                f"profile's profile.json under {PROFILE_KEY!r}")
    url = _workspace_key(url if "://" in url else "https://" + url)
    host = workspace_host(url)
    if database_dir:
        return url, Path(database_dir)
    if not profile:
        raise Altium365Error(
            "no profile is active, so there is no place for this workspace's library "
            "database: activate the profile that works with it (eda-agent profile-use "
            "<name>), or pass database_dir")
    return url, Path(profile) / LIBRARY_SUBDIR / host


def known_databases(profile_dir: Optional[Path] = None) -> list[Path]:
    """Database folders of the active profile that hold a finished scan."""
    profile = profile_dir if profile_dir is not None else _active_profile_dir()
    if not profile:
        return []
    root = Path(profile) / LIBRARY_SUBDIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "components.json").is_file())


def read_meta(db: Path) -> dict:
    try:
        meta = json.loads((Path(db) / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


# --- reading pages --------------------------------------------------------------

def row_cursor(index: int) -> Optional[str]:
    """The ``after`` cursor that makes a page start at row ``index``."""
    if index <= 0:
        return None
    return base64.b64encode(str(index - 1).encode()).decode()


def cursor_row(cursor: Optional[str]) -> Optional[int]:
    """The row a cursor points at, when it is the base64 of a row index."""
    if not cursor:
        return None
    try:
        text = base64.b64decode(cursor, validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() else None


def transient(exc: BaseException) -> bool:
    """A failure that a smaller page or a second try may get past."""
    if isinstance(exc, OSError):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "timed out", "http 5", "http 429",
                                   "execution was cancelled", "took too long"))


class PageReader:
    """One connection of ``desLibrary``, read a page at a time, the page
    halved on a timeout and grown back after a success."""

    def __init__(self, session, workspace_url: str, query: str, key: str,
                 page_size: int = PAGE_SIZE, pause: Callable[[float], None] = time.sleep):
        self.session, self.ws, self.query, self.key = session, workspace_url, query, key
        self.most = max(MIN_PAGE, int(page_size))
        self.size = self.most
        self.pause = pause
        self.halvings = 0

    def page(self, after: Optional[str], want: Optional[int] = None) -> dict:
        """{"nodes", "has_next", "end_cursor"}; ``want`` caps the page."""
        retries = 0
        while True:
            first = min(self.size, want) if want else self.size
            try:
                data = self.session.read(self.query, {"ws": self.ws, "after": after,
                                                      "first": first}, remember=False)
            except Exception as exc:  # noqa: BLE001 - sorted below
                if not transient(exc):
                    raise
                if self.size > MIN_PAGE:
                    self.size = max(MIN_PAGE, self.size // 2)
                    self.halvings += 1
                    continue
                retries += 1
                if retries > 3:
                    raise Altium365Error(
                        f"{self.key}: still failing at {MIN_PAGE} rows a page: {exc}") from exc
                self.pause(2.0 * retries)
                continue
            connection = ((data or {}).get("desLibrary") or {}).get(self.key)
            if connection is None:
                raise Altium365Error(f"the workspace returned no {self.key} list; "
                                     "is the URL a workspace this login reaches?")
            info = connection.get("pageInfo") or {}
            if self.size < self.most:
                self.size = min(self.most, self.size * 2)
            return {"nodes": connection.get("nodes") or [],
                    "has_next": bool(info.get("hasNextPage")),
                    "end_cursor": info.get("endCursor")}


def cursors_are_row_indexes(session, workspace_url: str) -> bool:
    """Whether the server's cursors are base64 row indexes, so that a read
    can start at any row: the first two rows must come back as 0 and 1,
    and the page after row 0 must start at the second row."""
    reader = PageReader(session, workspace_url, NAMES_QUERY, "components", 2)
    first = reader.page(None, 2)
    if len(first["nodes"]) < 2:
        return False
    one = reader.page(row_cursor(1), 1)
    return (cursor_row(first["end_cursor"]) == 1 and len(one["nodes"]) == 1
            and one["nodes"][0].get("id") == first["nodes"][1].get("id"))


# --- the chunk files -------------------------------------------------------------

def _read_lines(path: Path) -> list[dict]:
    """Every whole JSON line of a file; a line cut off mid-write is dropped."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text("utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            break
    return out


def _append(path: Path, record: dict) -> None:
    text = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


class Chunk:
    """Rows ``start`` to ``start + rows`` of the component list, in one file."""

    def __init__(self, scan_dir: Path, start: int, rows: int):
        self.start, self.rows = start, rows
        self.path = scan_dir / f"components.{start:08d}.jsonl"
        lines = _read_lines(self.path)
        self.read = sum(len(r.get("nodes") or []) for r in lines)
        ends = [r for r in lines if "done" in r]
        self.done = bool(ends)
        self.end_of_list = bool(ends and ends[-1].get("end_of_list"))

    def nodes(self) -> list[dict]:
        return [n for r in _read_lines(self.path) for n in r.get("nodes") or []]


def _now() -> float:
    return time.monotonic()


def read_ranges(session, workspace_url: str, scan_dir: Path, *, workers: int = 4,
                chunk_rows: int = CHUNK_ROWS, page_size: int = PAGE_SIZE,
                deadline: Optional[float] = None, max_rows: int = MAX_ROWS,
                progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Read the component list in chunks, ``workers`` at a time.

    A chunk that is done is never read again; one cut short continues from
    its last row. Returns {"complete", "rows", "end_row"}.
    """
    chunk_rows = max(10, int(chunk_rows))
    state = {"end": None, "next": 0, "stopped": False, "halvings": 0}
    lock = threading.Lock()

    for existing in sorted(scan_dir.glob("components.*.jsonl")):
        try:
            start = int(existing.stem.split(".")[1])
        except (IndexError, ValueError):
            continue
        chunk = Chunk(scan_dir, start, chunk_rows)
        if chunk.end_of_list:
            end = start + chunk.read
            state["end"] = end if state["end"] is None else min(state["end"], end)

    def claim() -> Optional[int]:
        with lock:
            if state["stopped"]:
                return None
            start = state["next"]
            if state["end"] is not None and start >= state["end"]:
                return None
            if start >= max_rows:
                return None
            state["next"] = start + chunk_rows
            return start

    def work() -> None:
        reader = PageReader(session, workspace_url, COMPONENTS_QUERY, "components",
                            page_size)
        while True:
            start = claim()
            if start is None:
                break
            chunk = Chunk(scan_dir, start, chunk_rows)
            while not chunk.done:
                if deadline is not None and _now() > deadline:
                    with lock:
                        state["stopped"] = True
                    break
                with lock:
                    end = state["end"]
                at = start + chunk.read
                if end is not None and at >= end:
                    _append(chunk.path, {"done": True, "end_of_list": False})
                    chunk.done = True
                    break
                page = reader.page(row_cursor(at), chunk_rows - chunk.read)
                nodes = page["nodes"]
                if nodes:
                    _append(chunk.path, {"row": at, "nodes": nodes})
                    chunk.read += len(nodes)
                last = not nodes or not page["has_next"]
                if last or chunk.read >= chunk_rows:
                    _append(chunk.path, {"done": True, "end_of_list": last})
                    chunk.done = True
                    if last:
                        with lock:
                            stop = start + chunk.read
                            state["end"] = stop if state["end"] is None else min(
                                state["end"], stop)
                        progress(f"components: the list ends at row {start + chunk.read}")
                    else:
                        progress(f"components: rows {start}-{start + chunk.read - 1} read")
            with lock:
                state["halvings"] += reader.halvings
                reader.halvings = 0

    workers = max(1, min(int(workers), 16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(work) for _ in range(workers)]:
            future.result()
    end = state["end"]
    complete = end is not None and all(
        Chunk(scan_dir, s, chunk_rows).done for s in range(0, end, chunk_rows))
    rows = sum(Chunk(scan_dir, s, chunk_rows).read
               for s in range(0, (end if end is not None else state["next"]), chunk_rows))
    return {"complete": complete, "rows": rows, "end_row": end,
            "page_halvings": state["halvings"]}


def read_sequential(session, workspace_url: str, query: str, key: str, path: Path, *,
                    page_size: int = PAGE_SIZE, deadline: Optional[float] = None,
                    progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Follow the server's own cursors through one connection, keeping each
    page and the cursor after it in ``path`` so a rerun continues."""
    lines = _read_lines(path)
    if lines and lines[-1].get("done"):
        return {"complete": True, "rows": sum(len(r.get("nodes") or []) for r in lines)}
    cursor = lines[-1].get("cursor") if lines else None
    rows = sum(len(r.get("nodes") or []) for r in lines)
    reader = PageReader(session, workspace_url, query, key, page_size)
    while True:
        if deadline is not None and _now() > deadline:
            return {"complete": False, "rows": rows}
        page = reader.page(cursor)
        rows += len(page["nodes"])
        cursor = page["end_cursor"] if page["has_next"] else None
        _append(path, {"cursor": cursor, "nodes": page["nodes"]})
        progress(f"{key}: {rows} read")
        if cursor is None or not page["nodes"]:
            _append(path, {"done": True})
            return {"complete": True, "rows": rows}


def _sequential_nodes(path: Path) -> list[dict]:
    return [n for r in _read_lines(path) for n in r.get("nodes") or []]


# --- turning rows into records -------------------------------------------------

def _get(data: Any, *path: str, default: Any = "") -> Any:
    for step in path:
        data = data.get(step) if isinstance(data, dict) else None
    return default if data in (None, "") else data


def base_item(name: str) -> str:
    """A component name without its revision: ``CMP-0001-3`` -> ``CMP-0001``.
    The revision is the last hyphen-separated part, when it is a number."""
    match = re.fullmatch(r"(.+)-(\d+)", name or "")
    return match.group(1) if match else (name or "")


def normalise_component(node: dict) -> dict:
    details = node.get("details") or {}
    return {
        "id": node.get("id", ""),
        "name": node.get("name", ""),
        "item": base_item(node.get("name", "")),
        "comment": node.get("comment") or "",
        "description": node.get("description") or "",
        "folder": _get(node, "folder", "path"),
        "type": _get(node, "componentType", "path") or _get(node, "componentType", "name"),
        "lifecycle": _get(node, "revision", "details", "lifeCycleState", "name"),
        "revision_id": node.get("revisionId") or "",
        "managed": node.get("isManaged"),
        "created": node.get("createdAt") or "",
        "modified": node.get("modifiedAt") or "",
        "released": _get(node, "revision", "details", "releaseDate"),
        "symbols": [s.get("name") or "" for s in details.get("symbols") or []],
        "footprints": [f.get("name") or "" for f in details.get("footprints") or []],
        "model3d": _get(node, "model3D", "parasolidFile", "fileName"),
        "manufacturer_parts": [
            {"manufacturer": m.get("companyName") or "", "mpn": m.get("partNumber") or "",
             "priority": m.get("priority")}
            for m in node.get("manufacturerParts") or []],
        "parameters": {p.get("name"): p.get("value") or ""
                       for p in details.get("parameters") or [] if p.get("name")},
    }


def normalise_item(node: dict) -> dict:
    return {
        "id": node.get("id", ""),
        "name": node.get("name") or "",
        "comment": node.get("comment") or "",
        "description": node.get("description") or "",
        "folder": _get(node, "folder", "path"),
        "lifecycle": _get(node, "lifeCycleState", "name"),
        "created": node.get("createdAt") or "",
        "released": node.get("releasedAt") or "",
    }


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
    replace_with_retry(tmp, path)


def _write_csv(path: Path, header: list[str], rows: Iterable[list]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    replace_with_retry(tmp, path)


def _joined(values: Iterable[str]) -> str:
    return "; ".join(v for v in values if v)


def _mpns(record: dict) -> str:
    return _joined(f"{m['manufacturer']} {m['mpn']}".strip()
                   for m in record.get("manufacturer_parts") or [])


def write_database(db: Path, components: list[dict], symbols: list[dict],
                   footprints: list[dict], folders: list[dict], meta: dict) -> dict:
    """Write the database files from normalised records; returns the meta."""
    db.mkdir(parents=True, exist_ok=True)
    symbol_use, footprint_use = Counter(), Counter()
    for c in components:
        symbol_use.update(set(filter(None, c["symbols"])))
        footprint_use.update(set(filter(None, c["footprints"])))
    for items, use in ((symbols, symbol_use), (footprints, footprint_use)):
        for item in items:
            item["used_by_components"] = use.get(item["name"], 0)
    usage = load_design_usage(db)
    if usage:
        join_usage(components, usage)
    _write_json(db / "components.json", components)
    _write_json(db / "symbols.json", symbols)
    _write_json(db / "footprints.json", footprints)
    _write_json(db / "folders.json", folders)

    keys = sorted({k for c in components for k in c["parameters"]})
    _write_csv(db / "components.csv",
               ["Name", "Comment", "Description", "Folder", "Type", "Lifecycle",
                "Symbols", "Footprints", "3D model", "Manufacturer / MPN", "Created",
                "Modified", "Used in projects", "Placements",
                "Placed with an older revision", *keys],
               ([c["name"], c["comment"], c["description"], c["folder"], c["type"],
                 c["lifecycle"], _joined(c["symbols"]), _joined(c["footprints"]),
                 c["model3d"], _mpns(c), c["created"][:10], c["modified"][:10],
                 _joined(c.get("used_in_projects") or []), c.get("placements", 0),
                 "yes" if c.get("placed_older_revision") else "",
                 *[c["parameters"].get(k, "") for k in keys]] for c in components))
    item_header = ["Name", "Comment", "Description", "Folder", "Lifecycle",
                   "Used by components", "Created", "Released"]
    for name, items in (("symbols", symbols), ("footprints", footprints)):
        _write_csv(db / f"{name}.csv", item_header,
                   ([i["name"], i["comment"], i["description"], i["folder"],
                     i["lifecycle"], i["used_by_components"], i["created"][:10],
                     i["released"][:10]] for i in items))
    _write_csv(db / "folders.csv", ["Path", "Name", "Type", "Description"],
               ([f.get("path"), f.get("name"), f.get("folderType", ""),
                 f.get("description", "")] for f in folders))
    meta = {**meta, "built_at": now_iso(), "components": len(components),
            "symbols": len(symbols), "footprints": len(footprints),
            "folders": len(folders),
            "lifecycle": dict(Counter(c["lifecycle"] for c in components).most_common())}
    _write_json(db / "meta.json", meta)
    return meta


def _read_folders(session, workspace_url: str) -> list[dict]:
    try:
        data = session.read(FOLDERS_QUERY, {"ws": workspace_url}, remember=False)
    except Altium365Error:
        data = session.read(FOLDERS_QUERY_PLAIN, {"ws": workspace_url}, remember=False)
    return list(((data or {}).get("desLibrary") or {}).get("folders") or [])


def _read_items(session, workspace_url: str, scan: Path, key: str, page_size: int,
                deadline: Optional[float], progress) -> tuple[bool, list[dict]]:
    path = scan / f"{key}.jsonl"
    done = read_sequential(session, workspace_url, _CONNECTION % (key, ITEM_FIELDS), key,
                           path, page_size=page_size, deadline=deadline,
                           progress=progress)
    return done["complete"], _sequential_nodes(path)


# --- the scan ---------------------------------------------------------------------

def scan_library(session, workspace_url: str, db: Path, *, workers: int = 4,
                 page_size: int = PAGE_SIZE, chunk_rows: int = CHUNK_ROWS,
                 max_seconds: Optional[float] = None, restart: bool = False,
                 progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Read the whole library into ``db``, continuing an unfinished scan.

    Returns {"complete": False, ...} when ``max_seconds`` ran out first;
    calling again continues. On completion the database files are
    rewritten and the raw pages removed.
    """
    url = session.permitted(workspace_url)
    db = Path(db)
    scan = db / SCAN_SUBDIR
    if restart and scan.exists():
        shutil.rmtree(scan)
    scan.mkdir(parents=True, exist_ok=True)
    deadline = _now() + max_seconds if max_seconds else None
    started = _read_lines(scan / "state.jsonl")
    state = started[-1] if started else {}
    if state.get("workspace_url") and state["workspace_url"] != url:
        raise Altium365Error(f"{scan} holds an unfinished scan of {state['workspace_url']}; "
                             "pass restart=True to drop it")
    if "row_cursors" not in state:
        state = {"workspace_url": url, "started_at": now_iso(),
                 "row_cursors": cursors_are_row_indexes(session, url),
                 "chunk_rows": max(10, int(chunk_rows))}
        _append(scan / "state.jsonl", state)
    if state["row_cursors"]:
        # The chunk size the scan started with names its files.
        got = read_ranges(session, url, scan, workers=workers,
                          chunk_rows=state.get("chunk_rows", chunk_rows),
                          page_size=page_size, deadline=deadline, progress=progress)
        how = f"row ranges, {max(1, min(int(workers), 16))} readers"
    else:
        got = read_sequential(session, url, COMPONENTS_QUERY, "components",
                              scan / "components.jsonl", page_size=page_size,
                              deadline=deadline, progress=progress)
        how = "one reader following the server's cursors"
    if not got["complete"]:
        return {"complete": False, "database_dir": str(db), "components_read": got["rows"],
                "read_as": how,
                "note": "the time budget ran out; call again to continue from here"}
    sym_done, symbols = _read_items(session, url, scan, "symbols", page_size, deadline,
                                    progress)
    fp_done, footprints = (_read_items(session, url, scan, "footprints", page_size,
                                       deadline, progress) if sym_done else (False, []))
    if not (sym_done and fp_done):
        return {"complete": False, "database_dir": str(db), "components_read": got["rows"],
                "read_as": how,
                "note": "components are read; the time budget ran out on the symbols "
                        "or footprints. Call again to continue."}
    folders = _read_folders(session, url)
    if state["row_cursors"]:
        chunk_files = sorted(scan.glob("components.*.jsonl"))
        nodes = [n for f in chunk_files for r in _read_lines(f) for n in r.get("nodes") or []]
    else:
        nodes = _sequential_nodes(scan / "components.jsonl")
    components, dropped = _dedupe([normalise_component(n) for n in nodes])
    meta = write_database(
        db, components, _dedupe([normalise_item(n) for n in symbols])[0],
        _dedupe([normalise_item(n) for n in footprints])[0], folders,
        {**read_meta(db), "workspace_url": url, "host": workspace_host(url),
         "source": "Altium 365 GraphQL, read-only", "scanned_at": now_iso(),
         "scan_started_at": state.get("started_at"), "read_as": how,
         "duplicate_rows_dropped": dropped, "kind": "full scan"})
    shutil.rmtree(scan, ignore_errors=True)
    return {"complete": True, "database_dir": str(db), **_summary(meta)}


def _dedupe(records: list[dict]) -> tuple[list[dict], int]:
    seen: set[str] = set()
    kept = []
    for record in records:
        key = record.get("id") or record.get("name")
        if key in seen:
            continue
        seen.add(key)
        kept.append(record)
    return kept, len(records) - len(kept)


def _summary(meta: dict) -> dict:
    return {k: meta.get(k) for k in ("workspace_url", "scanned_at", "components",
                                     "symbols", "footprints", "folders",
                                     "duplicate_rows_dropped", "read_as", "kind")
            if k in meta}


# --- the incremental refresh ------------------------------------------------------

def name_family(name: str) -> str:
    """The part of a name before its serial number and revision:
    ``CMP-02-014133-4`` -> ``CMP-02-``, ``CMP-00000123-4`` -> ``CMP-``."""
    parts = (name or "").split("-")
    if len(parts) >= 4:
        return "-".join(parts[:-2]) + "-"
    if len(parts) >= 2:
        return parts[0] + "-"
    return name or ""


def stable_families(components: list[dict]) -> list[str]:
    """Name families of the block at the end of the list that is kept as it
    is between scans: the run of trailing components under the same top
    folder as the last one, provided no component before it shares a
    family with it. Empty when there is no such block."""
    if not components:
        return []
    top = (components[-1].get("folder") or "").split("/")[0]
    start = len(components)
    while start > 0 and (components[start - 1].get("folder") or "").split("/")[0] == top:
        start -= 1
    if start == 0:
        return []
    tail = {name_family(c["name"]) for c in components[start:]}
    head = {name_family(c["name"]) for c in components[:start]}
    if tail & head:
        return []
    return sorted(tail)


def update_library(session, workspace_url: str, db: Path, *,
                   stable_prefixes: Optional[list[str]] = None,
                   page_size: int = PAGE_SIZE,
                   progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Refresh a scanned library without reading all of it again.

    The components before the stable block (by default the bulk folder at
    the end of the list, see :func:`stable_families`) are read again in
    full, with the symbol, footprint and folder lists. Then the row just
    past the last known one is read, to tell whether the stable block
    grew; when it did, or shrank, ``full_scan_needed`` says so.
    """
    url = session.permitted(workspace_url)
    db = Path(db)
    try:
        old = json.loads((db / "components.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise Altium365Error(f"{db} holds no finished scan: run altium365_scan_library "
                             "first") from None
    meta = read_meta(db)
    if meta.get("workspace_url") and meta["workspace_url"] != url:
        raise Altium365Error(f"{db} is the library of {meta['workspace_url']}, not {url}")
    prefixes = list(stable_prefixes if stable_prefixes is not None
                    else meta.get("stable_prefixes") or stable_families(old))
    prefixes = [p for p in prefixes if p]

    def stable(name: str) -> bool:
        return any((name or "").startswith(p) for p in prefixes)

    tail = [c for c in old if stable(c["name"])]
    reader = PageReader(session, url, COMPONENTS_QUERY, "components", page_size)
    head_nodes: list[dict] = []
    cursor = None
    while True:
        page = reader.page(cursor)
        ended = False
        for node in page["nodes"]:
            if prefixes and stable(node.get("name")):
                ended = True
                break
            head_nodes.append(node)
        progress(f"components: {len(head_nodes)} read")
        if ended or not page["has_next"] or not page["nodes"]:
            break
        cursor = page["end_cursor"]
    head = [normalise_component(n) for n in head_nodes]

    growth = "not checked: no stable block" if not prefixes else ""
    full_scan_needed = False
    if prefixes:
        if cursors_are_row_indexes(session, url):
            past = len(head) + len(tail)
            probe = PageReader(session, url, NAMES_QUERY, "components", 2)
            beyond = probe.page(row_cursor(past), 1)["nodes"]
            last = probe.page(row_cursor(past - 1), 1)["nodes"] if past else []
            if beyond:
                growth = f"a row exists past the {past} known: the stable block grew"
                full_scan_needed = True
            elif not last or not stable(last[0].get("name")):
                growth = f"row {past - 1} is not the stable block's last: it shrank"
                full_scan_needed = True
            else:
                growth = "unchanged"
        else:
            growth = "not checked: the server's cursors are not row indexes"

    scan = db / SCAN_SUBDIR / "update"
    if scan.exists():
        shutil.rmtree(scan)
    scan.mkdir(parents=True, exist_ok=True)
    _done, symbols = _read_items(session, url, scan, "symbols", page_size, None, progress)
    _done, footprints = _read_items(session, url, scan, "footprints", page_size, None,
                                    progress)
    folders = _read_folders(session, url)
    shutil.rmtree(db / SCAN_SUBDIR, ignore_errors=True)

    old_head = {c["item"]: c for c in old if not stable(c["name"])}
    new_head = {c["item"]: c for c in head}
    added = sorted(set(new_head) - set(old_head))
    removed = sorted(set(old_head) - set(new_head))
    revised = sorted(k for k in set(new_head) & set(old_head)
                     if new_head[k]["name"] != old_head[k]["name"]
                     or new_head[k]["modified"] != old_head[k]["modified"])
    components, dropped = _dedupe(head + tail)
    meta = write_database(
        db, components, _dedupe([normalise_item(n) for n in symbols])[0],
        _dedupe([normalise_item(n) for n in footprints])[0], folders,
        {**meta, "workspace_url": url, "host": workspace_host(url),
         "updated_at": now_iso(), "stable_prefixes": prefixes,
         "duplicate_rows_dropped": dropped, "kind": "incremental update"})
    return {"database_dir": str(db), "reread_components": len(head),
            "kept_components": len(tail), "stable_prefixes": prefixes,
            "added": added[:200], "removed": removed[:200], "revised": revised[:200],
            "counts": {"added": len(added), "removed": len(removed),
                       "revised": len(revised)},
            "stable_block": growth, "full_scan_needed": full_scan_needed,
            **_summary(meta)}


# --- searching -------------------------------------------------------------------

def load_components(db: Path) -> list[dict]:
    try:
        data = json.loads((Path(db) / "components.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _haystack(c: dict, fields: str) -> list[str]:
    if fields == "name":
        return [c.get("name", ""), c.get("comment", "")]
    if fields == "description":
        return [c.get("description", "")]
    base = [c.get("name", ""), c.get("comment", ""), c.get("description", ""),
            c.get("folder", "")]
    base += [m.get("mpn", "") for m in c.get("manufacturer_parts") or []]
    base += [m.get("manufacturer", "") for m in c.get("manufacturer_parts") or []]
    if fields in ("all", "parameters"):
        base += [str(v) for v in (c.get("parameters") or {}).values()]
    return base


def search_components(databases: Iterable[Path], query: str, *, fields: str = "all",
                      limit: int = 100, folder: str = "", lifecycle: str = "") -> dict:
    """Case-insensitive search over scanned libraries. Every word of the
    query has to appear somewhere in a component's text."""
    words = [w for w in (query or "").lower().split() if w]
    hits: list[dict] = []
    searched = []
    total = 0
    for db in databases:
        meta = read_meta(db)
        components = load_components(db)
        searched.append({"database_dir": str(db), "workspace_url": meta.get("workspace_url", ""),
                         "scanned_at": meta.get("updated_at") or meta.get("scanned_at", ""),
                         "components": len(components)})
        for c in components:
            if folder and folder.lower() not in (c.get("folder") or "").lower():
                continue
            if lifecycle and lifecycle.lower() != (c.get("lifecycle") or "").lower():
                continue
            text = " ".join(_haystack(c, fields)).lower()
            if all(w in text for w in words):
                total += 1
                if len(hits) < limit:
                    hits.append(as_search_result(c, meta.get("workspace_url", "")))
    return {"query": query, "count": len(hits), "matches": total,
            "truncated": total > len(hits), "results": hits, "databases": searched}


def as_search_result(c: dict, workspace_url: str) -> dict:
    """One component in the shape lib_search returns its results, marked
    ``source: "altium365"``."""
    return {
        "name": c.get("name", ""), "alias_name": "",
        "description": c.get("description", ""), "library_path": workspace_url,
        "part_count": None, "source": "altium365", "comment": c.get("comment", ""),
        "folder": c.get("folder", ""), "lifecycle": c.get("lifecycle", ""),
        "symbols": c.get("symbols") or [], "footprints": c.get("footprints") or [],
        "manufacturer_parts": c.get("manufacturer_parts") or [],
        "model3d": c.get("model3d", ""), "placements": c.get("placements", 0),
    }


# --- placements in the user's schematics -----------------------------------------

_SKIPPED_DIRS = ("History", "__Previews", "Project Logs for", "Project Outputs for",
                 ".eda", ".git")


def _skipped(path: Path) -> bool:
    return any(part.startswith(_SKIPPED_DIRS) for part in path.parts)


def _project_of(sheet: Path, roots: list[Path]) -> str:
    for parent in sheet.parents:
        found = [p for p in parent.glob("*.[Pp]rj[Pp][Cc][Bb]")]
        if found:
            return found[0].stem
        if parent in roots:
            break
    return sheet.parent.name


def placements_on_sheet(path: Path) -> list[dict]:
    """The components placed on one .SchDoc, read from the file."""
    from .fileio.altium_sch import read_schdoc_records
    records = read_schdoc_records(path)
    by_index = {pos - 1: r for pos, r in enumerate(records) if pos > 0}
    parts = {i: r for i, r in by_index.items() if r.get("RECORD") == "1"}
    designators: dict[int, str] = {}
    for record in by_index.values():
        owner = record.get("OwnerIndex")
        if record.get("RECORD") == "34" and owner is not None:
            try:
                designators[int(owner)] = record.get("Text", "")
            except ValueError:
                pass
    return [{"designator": designators.get(i, ""),
             "design_item_id": r.get("DesignItemId", ""),
             "lib_reference": r.get("LibReference", ""),
             "source_library": r.get("SourceLibraryName", ""),
             "managed": bool(r.get("VaultGUID")),
             "revision_guid": r.get("RevisionGUID", "")}
            for i, r in parts.items()]


def scan_design_usage(roots: list[Path]) -> dict:
    """Every managed component placed in the schematics under ``roots``,
    read offline (no Altium), with its projects and placement count."""
    roots = [Path(r) for r in roots]
    sheets = sorted({p for r in roots if r.is_dir() for p in r.rglob("*.[Ss]ch[Dd]oc")
                     if not _skipped(p.relative_to(r))})
    found: dict[str, dict] = {}
    errors = []
    for sheet in sheets:
        try:
            placed = placements_on_sheet(sheet)
        except Exception as exc:  # noqa: BLE001 - one bad sheet is reported, not fatal
            errors.append({"sheet": str(sheet), "error": str(exc)[:200]})
            continue
        project = _project_of(sheet, roots)
        for p in placed:
            if not p["managed"] or not p["design_item_id"]:
                continue
            entry = found.setdefault(p["design_item_id"], {
                "design_item_id": p["design_item_id"], "item": base_item(p["design_item_id"]),
                "source_library": p["source_library"], "projects": set(), "placements": 0,
                "designators": set()})
            entry["projects"].add(project)
            entry["placements"] += 1
            if p["designator"]:
                entry["designators"].add(f"{project}:{p['designator']}")
    placed = [{**e, "projects": sorted(e["projects"]),
               "designators": sorted(e["designators"])[:50]}
              for e in sorted(found.values(), key=lambda e: e["design_item_id"])]
    return {"scanned_at": now_iso(), "roots": [str(r) for r in roots],
            "sheets_read": len(sheets) - len(errors), "sheet_errors": errors,
            "placed": placed}


def save_design_usage(db: Path, usage: dict) -> None:
    Path(db).mkdir(parents=True, exist_ok=True)
    _write_json(Path(db) / "design_usage.json", usage)


def load_design_usage(db: Path) -> dict:
    try:
        data = json.loads((Path(db) / "design_usage.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def join_usage(components: list[dict], usage: dict) -> dict:
    """Fill each component's ``used_in_projects``, ``placements``,
    ``placed_revisions`` and ``placed_older_revision`` from the placements,
    joined on the item without its revision. Returns the placements whose
    item is not in the library, and those placed at an older revision."""
    by_item: dict[str, list[dict]] = defaultdict(list)
    for p in usage.get("placed") or []:
        by_item[p["item"]].append(p)
    names = {c["item"] for c in components}
    for c in components:
        placed = by_item.get(c["item"], [])
        c["used_in_projects"] = sorted({proj for p in placed for proj in p["projects"]})
        c["placements"] = sum(p["placements"] for p in placed)
        c["placed_revisions"] = sorted(p["design_item_id"] for p in placed)
        c["placed_older_revision"] = any(p["design_item_id"] != c["name"] for p in placed)
    older = [{"placed": p["design_item_id"], "library": c["name"],
              "projects": p["projects"], "placements": p["placements"],
              "designators": p.get("designators", [])}
             for c in components for p in by_item.get(c["item"], [])
             if p["design_item_id"] != c["name"]]
    return {"older_revision": older,
            "not_in_library": [p for item, ps in by_item.items() if item not in names
                               for p in ps]}


# --- 3D models, read from Explorer -----------------------------------------------

def save_models3d(db: Path, workspace_url: str, got: dict) -> dict:
    """Keep what ui.explorer.scan_models read as models3d.json and .csv."""
    db = Path(db)
    db.mkdir(parents=True, exist_ok=True)
    items = got.get("items") or []
    _write_json(db / "models3d.json", {
        "workspace_url": workspace_url, "read_at": now_iso(),
        "source": "Altium Explorer panel (UI Automation), read-only",
        "folders": got.get("folders") or {}, "items": items})
    _write_csv(db / "models3d.csv",
               ["Item ID", "Revision", "Name", "Description", "State", "Note", "Folder"],
               ([m.get("item_id", ""), m.get("revision", ""), m.get("name", ""),
                 m.get("description", ""), m.get("state", ""), m.get("note", ""),
                 m.get("folder", "")] for m in items))
    unread = [f for f, n in (got.get("folders") or {}).items() if n < 0]
    return {"database_dir": str(db), "models": len(items),
            "folders": len(got.get("folders") or {}), "folders_not_read": unread,
            "by_state": dict(Counter(m.get("state", "") for m in items).most_common())}


def load_models3d(db: Path) -> list[dict]:
    try:
        data = json.loads((Path(db) / "models3d.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return list(data.get("items") or []) if isinstance(data, dict) else []


# --- the health report -----------------------------------------------------------

def health_report(db: Path, design_roots: Optional[list[Path]] = None,
                  sample: int = 50) -> dict:
    """What needs attention in a scanned library, and, with ``design_roots``,
    how it is used in the schematics there. Writes report.json and
    report.md into the database folder."""
    db = Path(db)
    components = load_components(db)
    if not components:
        raise Altium365Error(f"{db} holds no finished scan: run altium365_scan_library first")
    meta = read_meta(db)
    if design_roots:
        usage = scan_design_usage([Path(r) for r in design_roots])
        save_design_usage(db, usage)
        meta["design_roots"] = [str(r) for r in design_roots]
        _write_json(db / "meta.json", meta)
    else:
        usage = load_design_usage(db)
    joined = join_usage(components, usage) if usage else {"older_revision": [],
                                                          "not_in_library": []}

    def load(name: str) -> list[dict]:
        try:
            return json.loads((db / name).read_text("utf-8"))
        except (OSError, ValueError):
            return []
    symbols, footprints = load("symbols.json"), load("footprints.json")

    def names(rows: Iterable[dict]) -> list[str]:
        return [r["name"] for r in rows]

    mpn_owners: dict[str, list[str]] = defaultdict(list)
    for c in components:
        for m in c.get("manufacturer_parts") or []:
            if m.get("mpn"):
                mpn_owners[m["mpn"].strip().upper()].append(c["name"])
    shared_mpn = {k: v for k, v in mpn_owners.items() if len(v) > 1}
    checks = {
        "without_symbol": names(c for c in components if not any(c["symbols"])),
        "without_footprint": names(c for c in components if not any(c["footprints"])),
        "without_manufacturer_part": names(c for c in components
                                           if not c["manufacturer_parts"]),
        "without_3d_model_link": names(c for c in components if not c.get("model3d")),
        "without_description": names(c for c in components if not c["description"].strip()),
        "symbols_used_by_no_component": [s["name"] for s in symbols
                                         if not s.get("used_by_components")],
        "footprints_used_by_no_component": [f["name"] for f in footprints
                                            if not f.get("used_by_components")],
        "mpn_on_several_components": [f"{k}: {', '.join(v[:5])}"
                                      for k, v in sorted(shared_mpn.items())],
    }
    if usage:
        checks["placed_with_an_older_revision"] = [
            f"{o['placed']} (library {o['library']}) in {', '.join(o['projects'])}"
            for o in joined["older_revision"]]
        checks["placed_but_not_in_this_library"] = [
            f"{p['design_item_id']} ({p.get('source_library', '')}) in "
            f"{', '.join(p['projects'])}" for p in joined["not_in_library"]]
    report = {
        "database_dir": str(db), "workspace_url": meta.get("workspace_url", ""),
        "library_read_at": meta.get("updated_at") or meta.get("scanned_at", ""),
        "built_at": now_iso(),
        "counts": {"components": len(components), "symbols": len(symbols),
                   "footprints": len(footprints), "folders": meta.get("folders", 0)},
        "lifecycle": dict(Counter(c["lifecycle"] for c in components).most_common()),
        "largest_folders": dict(Counter(c["folder"] for c in components).most_common(15)),
        "checks": {k: {"count": len(v), "sample": v[:sample]} for k, v in checks.items()},
    }
    models = load_models3d(db)
    if models:
        report["models3d"] = {
            "count": len(models),
            "by_state": dict(Counter(m.get("state", "") for m in models).most_common())}
    if usage:
        report["design_usage"] = {
            "roots": usage.get("roots", []), "sheets_read": usage.get("sheets_read", 0),
            "sheet_errors": len(usage.get("sheet_errors") or []),
            "managed_parts_placed": len(usage.get("placed") or []),
            "library_components_used": sum(1 for c in components if c.get("placements")),
        }
    _write_json(db / "report.json", {**report, "checks": checks})
    (db / "report.md").write_text(_report_markdown(report), "utf-8")
    write_database(db, components, symbols, footprints, load("folders.json"), meta)
    return report


def _report_markdown(report: dict) -> str:
    lines = [f"# Library health: {report['workspace_url']}", "",
             f"Library read {report['library_read_at']}, report built {report['built_at']}.",
             "", "| | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in report["counts"].items()]
    lines += ["", "## Checks", "", "| Check | Count |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {v['count']} |"
              for k, v in report["checks"].items()]
    if "design_usage" in report:
        lines += ["", "## Use in schematics", ""]
        lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in
                  report["design_usage"].items()]
    lines += ["", "Full lists are in report.json next to this file.", ""]
    return "\n".join(lines)
