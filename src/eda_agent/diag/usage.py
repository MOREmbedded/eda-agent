# SPDX-License-Identifier: Apache-2.0
"""How many tokens the EDA tools cost: every MCP tool call, counted.

Each tool call the AI makes costs tokens twice: the arguments it writes
(output tokens) and the result it reads back (input tokens on every
later turn of the conversation). Neither number reaches the server, so
both are ESTIMATED from what the server can see: the characters of the
arguments and of the result, at ``CHARS_PER_TOKEN`` characters a token,
and images by their size the way the model counts them. The estimate is
good to tens of percent, which is what it is for: telling which tools
eat the context, not billing.

Where it goes:

* ``<workspace>/.eda-agent/usage/calls-YYYY-MM.jsonl``: one line per
  call, kept per workspace (the folder the AI client started the server
  in, the design workspace). The folder ignores itself for git.
* ``<IPC folder>/tool_usage.txt``: a ready-made table the Stats tab of
  the status window in Altium shows as it is.
* ``~/.eda-agent/usage_workspaces.json``: the workspaces that have
  records, so the dashboard can offer them all whichever folder it was
  started in.

It is a setting, ``usage_stats`` (eda_agent.preferences): On by default,
switched on the Config tab of the status window in Altium or per profile
in the dashboard's Workspace view. Off, nothing is counted or written
(``EDA_AGENT_USAGE_OFF=1`` in the AI client's config does the same).

Recording never breaks a tool: every failure here is swallowed.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("eda_agent.usage")

#: Characters per token. ~4 is the usual figure for English prose; JSON
#: with short keys and numbers runs nearer 3.5, so this errs low.
CHARS_PER_TOKEN = 4.0
#: The model shrinks an image to fit this many pixels on its long edge.
IMAGE_MAX_EDGE = 1568
#: Pixels per token for an image.
IMAGE_PIXELS_PER_TOKEN = 750
#: An image whose size cannot be read is counted as this many tokens.
IMAGE_UNKNOWN_TOKENS = 1600

USAGE_DIRNAME = "usage"
STATUS_FILE = "tool_usage.txt"
ENV_DIR = "EDA_AGENT_USAGE_DIR"
ENV_OFF = "EDA_AGENT_USAGE_OFF"
#: How many tools the status table lists; the rest are summed.
STATUS_TOP = 15


def estimate_tokens(chars: int) -> int:
    return int(math.ceil(max(0, chars) / CHARS_PER_TOKEN))


def image_size(data: bytes) -> Optional[tuple[int, int]]:
    """Width and height of a PNG, JPEG, GIF or WebP, or None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except (struct.error, IndexError, ValueError):
        return None
    return None


def image_tokens(width: int, height: int) -> int:
    """Tokens for an image of this size, after the model shrinks it."""
    if width <= 0 or height <= 0:
        return IMAGE_UNKNOWN_TOKENS
    scale = min(1.0, IMAGE_MAX_EDGE / max(width, height))
    w, h = width * scale, height * scale
    return int(math.ceil(w * h / IMAGE_PIXELS_PER_TOKEN))


def _json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


@dataclass
class Measure:
    """What one tool result holds, as the model would read it."""

    text_chars: int = 0
    structured_chars: int = 0
    images: int = 0
    image_tokens: int = 0
    ok: bool = True

    @property
    def chars(self) -> int:
        # A client reads either copy, not both; they hold the same data,
        # so the larger is the honest upper figure.
        return max(self.text_chars, self.structured_chars)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.chars) + self.image_tokens


def measure_result(result: Any) -> Measure:
    """Size up what FastMCP's call_tool returned: a list of content
    blocks, or (blocks, structured), or anything else."""
    m = Measure()
    blocks, structured = (result if isinstance(result, tuple) and len(result) == 2
                          else (result, None))
    if isinstance(blocks, dict) and structured is None:
        structured, blocks = blocks, []
    if not isinstance(blocks, (list, tuple)):
        blocks = [] if blocks is None else [blocks]
    for block in blocks:
        text = getattr(block, "text", None)
        data = getattr(block, "data", None)
        if isinstance(text, str):
            m.text_chars += len(text)
            if m.ok and text[:1] == "{" and '"ok":false' in text[:200].replace(" ", ""):
                m.ok = False
        elif isinstance(data, str) and str(getattr(block, "type", "")) == "image":
            m.images += 1
            try:
                size = image_size(base64.b64decode(data[:65536] + "=" * (-len(data[:65536]) % 4)))
            except (ValueError, TypeError):
                size = None
            m.image_tokens += image_tokens(*size) if size else IMAGE_UNKNOWN_TOKENS
        elif isinstance(block, str):
            m.text_chars += len(block)
        elif block is not None:
            m.text_chars += _json_chars(getattr(block, "model_dump", lambda: block)())
    if structured is not None:
        m.structured_chars = _json_chars(structured)
        if isinstance(structured, dict) and structured.get("ok") is False:
            m.ok = False
    return m


# --- aggregation ------------------------------------------------------------

@dataclass
class Tally:
    calls: int = 0
    failed: int = 0
    args_tokens: int = 0
    result_tokens: int = 0
    images: int = 0
    ms: int = 0
    max_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.args_tokens + self.result_tokens

    def add(self, rec: dict) -> None:
        self.calls += 1
        self.failed += 0 if rec.get("ok", True) else 1
        a, r = int(rec.get("args_tokens") or 0), int(rec.get("result_tokens") or 0)
        self.args_tokens += a
        self.result_tokens += r
        self.images += int(rec.get("images") or 0)
        self.ms += int(rec.get("ms") or 0)
        self.max_tokens = max(self.max_tokens, a + r)

    def to_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "failed": self.failed,
                "args_tokens": self.args_tokens, "result_tokens": self.result_tokens,
                "tokens": self.tokens, "images": self.images, "ms": self.ms,
                "avg_tokens": round(self.tokens / self.calls) if self.calls else 0,
                "max_tokens": self.max_tokens}


@dataclass
class Summary:
    total: Tally = field(default_factory=Tally)
    by_tool: dict[str, Tally] = field(default_factory=dict)
    by_day: dict[str, Tally] = field(default_factory=dict)
    by_session: dict[str, Tally] = field(default_factory=dict)
    by_profile: dict[str, Tally] = field(default_factory=dict)
    session_span: dict[str, list[float]] = field(default_factory=dict)

    def add(self, rec: dict) -> None:
        self.total.add(rec)
        ts = float(rec.get("ts") or 0)
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
        for table, key in ((self.by_tool, rec.get("tool") or "?"),
                           (self.by_day, day),
                           (self.by_session, rec.get("session") or "?"),
                           (self.by_profile, rec.get("profile") or "(none)")):
            table.setdefault(str(key), Tally()).add(rec)
        span = self.session_span.setdefault(str(rec.get("session") or "?"), [ts, ts])
        span[0], span[1] = min(span[0], ts), max(span[1], ts)

    def to_dict(self, top: Optional[int] = None) -> dict[str, Any]:
        def rows(table: dict[str, Tally], key: str, sort_by_tokens=True) -> list[dict]:
            items = sorted(table.items(),
                           key=(lambda kv: -kv[1].tokens) if sort_by_tokens else (lambda kv: kv[0]),
                           reverse=False)
            return [{key: k, **t.to_dict()} for k, t in items]

        tools = rows(self.by_tool, "tool")
        sessions = [{"session": k, "first": self.session_span[k][0],
                     "last": self.session_span[k][1], **t.to_dict()}
                    for k, t in self.by_session.items()]
        sessions.sort(key=lambda r: -r["last"])
        return {"total": self.total.to_dict(),
                "by_tool": tools[:top] if top else tools,
                "by_day": sorted(rows(self.by_day, "day", False), key=lambda r: r["day"]),
                "by_session": sessions,
                "by_profile": rows(self.by_profile, "profile")}


def summarize(records: Iterable[dict]) -> Summary:
    s = Summary()
    for rec in records:
        if isinstance(rec, dict):
            s.add(rec)
    return s


# --- where records live -----------------------------------------------------

def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def default_workspace() -> Optional[Path]:
    """The design workspace: EDA_AGENT_USAGE_DIR, else the folder the
    server was started in. None under pytest unless the variable is set,
    so a test run never writes into whatever folder it ran from."""
    named = os.environ.get(ENV_DIR)
    if named:
        return Path(named)
    if _under_pytest():
        return None
    try:
        return Path.cwd()
    except OSError:
        return None


def usage_dir(workspace: Path) -> Path:
    from ..profile import PROJECT_PROFILE_DIRNAME
    return Path(workspace) / PROJECT_PROFILE_DIRNAME / USAGE_DIRNAME


def load(workspace: Path, since: float = 0.0) -> list[dict]:
    """Every record kept for ``workspace``, oldest first. A torn or
    foreign line is skipped."""
    out: list[dict] = []
    folder = usage_dir(workspace)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("calls-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and float(rec.get("ts") or 0) >= since:
                out.append(rec)
    return out


def _registry_path() -> Path:
    from ..profile import PROJECT_PROFILE_DIRNAME, _home
    return _home() / PROJECT_PROFILE_DIRNAME / "usage_workspaces.json"


def known_workspaces() -> list[dict]:
    """Workspaces with records, most recently used first."""
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = [r for r in (data if isinstance(data, list) else [])
            if isinstance(r, dict) and r.get("path")]
    rows.sort(key=lambda r: -float(r.get("last") or 0))
    return rows


def _remember_workspace(workspace: Path) -> None:
    path = _registry_path()
    rows = [r for r in known_workspaces() if r.get("path") != str(workspace)]
    rows.insert(0, {"path": str(workspace), "last": time.time()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[:50], indent=1), encoding="utf-8")
    from ..atomicfile import replace_with_retry
    replace_with_retry(tmp, path)


def _profile_name() -> str:
    try:
        from .. import profile as profile_mod
        directory = profile_mod.resolve_profile_dir()
    except Exception:  # noqa: BLE001
        return ""
    if directory is None:
        return ""
    if directory.name == profile_mod.PROJECT_PROFILE_SUBDIR:
        return f"project:{directory.parent.parent.name}"
    return directory.name


def setting_on() -> bool:
    """The ``usage_stats`` setting, and the environment switch."""
    if os.environ.get(ENV_OFF):
        return False
    try:
        from .. import preferences
        return preferences.usage_stats_on()
    except Exception:  # noqa: BLE001 - a broken settings file keeps the default
        return True


def _session_tag() -> str:
    try:
        from ..bridge import altium_bridge
        return str(altium_bridge._SESSION_TAG)
    except Exception:  # noqa: BLE001
        return ""


# --- the recorder -----------------------------------------------------------

class Recorder:
    """Counts this server's tool calls and keeps them for its workspace."""

    def __init__(self, workspace: Optional[Path] = None, status_dir: Optional[Path] = None,
                 session: Optional[str] = None, remember: bool = True,
                 switch=None) -> None:
        self.workspace = workspace
        self.status_dir = status_dir
        self.session = session if session is not None else _session_tag()
        self.remember = remember
        self.started = time.time()
        self._lock = threading.Lock()
        self._session = Summary()
        self._all: Optional[Summary] = None
        self._remembered = False
        #: Asked before each call: is counting on? (the usage_stats setting)
        self.switch = switch if switch is not None else setting_on
        self._off_written = False

    @property
    def enabled(self) -> bool:
        """Whether records are kept on disk (there is a workspace)."""
        return self.workspace is not None

    def _workspace_summary(self) -> Summary:
        if self._all is None:
            self._all = summarize(load(self.workspace)) if self.workspace else Summary()
        return self._all

    def record(self, tool: str, arguments: Any, result: Any = None, ms: int = 0,
               error: Optional[BaseException] = None) -> Optional[dict]:
        try:
            if not self.switch():
                self._switched_off()
                return None
            self._off_written = False
            m = measure_result(result) if error is None else Measure(
                text_chars=len(str(error)), ok=False)
            args_chars = _json_chars(arguments or {})
            rec = {"ts": round(time.time(), 3), "session": self.session,
                   "profile": _profile_name(), "tool": tool or "?",
                   "ms": int(ms), "ok": m.ok,
                   "args_chars": args_chars, "args_tokens": estimate_tokens(args_chars),
                   "result_chars": m.chars, "images": m.images,
                   "result_tokens": m.tokens}
            with self._lock:
                self._session.add(rec)
                if self.enabled:
                    self._workspace_summary().add(rec)
                    self._append(rec)
                self._write_status()
            return rec
        except Exception as exc:  # noqa: BLE001 - counting never breaks a tool
            logger.debug("usage record failed: %s", exc)
            return None

    def _switched_off(self) -> None:
        """Say so on the Stats tab, once per switch-off."""
        if self._off_written or self.status_dir is None:
            return
        self._off_written = True
        self._put_status("Token usage statistics are off.\n\n"
                         "Switch them on with Token stats on the Config tab,\n"
                         "or usage_stats in the profile (dashboard, Workspace).\n")

    def _append(self, rec: dict) -> None:
        folder = usage_dir(self.workspace)
        folder.mkdir(parents=True, exist_ok=True)
        ignore = folder / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# Token usage records, per machine; see eda_agent.diag.usage\n*\n",
                              encoding="utf-8")
        path = folder / f"calls-{datetime.fromtimestamp(rec['ts']).strftime('%Y-%m')}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if self.remember and not self._remembered:
            self._remembered = True
            try:
                _remember_workspace(self.workspace)
            except Exception as exc:  # noqa: BLE001
                logger.debug("usage registry not written: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            whole = self._workspace_summary() if self.enabled else Summary()
            return {"ok": True, "on": bool(self.switch()),
                    "workspace": str(self.workspace or ""),
                    "session": self.session, "started": self.started,
                    "chars_per_token": CHARS_PER_TOKEN,
                    "this_session": self._session.to_dict(),
                    "workspace_totals": whole.to_dict()}

    def _write_status(self) -> None:
        if self.status_dir is None:
            return
        whole = self._all if self.enabled else None
        self._put_status(status_text(self._session, whole, str(self.workspace or ""),
                                     _profile_name()))

    def _put_status(self, text: str) -> None:
        if self.status_dir is None:
            return
        target = Path(self.status_dir) / STATUS_FILE
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            from ..atomicfile import replace_with_retry
            replace_with_retry(tmp, target)
        except OSError as exc:
            logger.debug("usage status not written: %s", exc)


def fmt_tokens(n: int) -> str:
    if n >= 10_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_ms(ms: int) -> str:
    if ms >= 60_000:
        return f"{ms // 60_000}m{(ms % 60_000) // 1000:02d}s"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def status_text(session: Summary, whole: Optional[Summary], workspace: str = "",
                profile: str = "") -> str:
    """The table the status window's Stats tab shows. Plain text, about
    52 columns, so it fits the panel in Consolas."""
    lines = [f"Token use, estimated (~{CHARS_PER_TOKEN:g} chars a token)",
             f"{'':16}{'calls':>7}{'tokens':>9}{'args':>8}{'result':>9}"]

    def total_row(label: str, t: Tally) -> str:
        return (f"{label[:16]:16}{t.calls:>7}{fmt_tokens(t.tokens):>9}"
                f"{fmt_tokens(t.args_tokens):>8}{fmt_tokens(t.result_tokens):>9}")

    lines.append(total_row("This session", session.total))
    if whole is not None:
        today = datetime.now().strftime("%Y-%m-%d")
        lines.append(total_row("Today", whole.by_day.get(today, Tally())))
        lines.append(total_row("This workspace", whole.total))
        if profile and profile in whole.by_profile:
            lines.append(total_row(f"Profile {profile}", whole.by_profile[profile]))
    lines.append("")
    lines.append(f"{'This session, by tool':26}{'N':>5}{'tokens':>8}{'avg':>7}{'time':>7}")
    lines.append("-" * 53)
    tools = sorted(session.by_tool.items(), key=lambda kv: -kv[1].tokens)
    for name, t in tools[:STATUS_TOP]:
        avg = round(t.tokens / t.calls) if t.calls else 0
        label = name if len(name) <= 26 else name[:25] + "~"
        lines.append(f"{label:26}{t.calls:>5}{fmt_tokens(t.tokens):>8}"
                     f"{fmt_tokens(avg):>7}{_fmt_ms(t.ms):>7}")
    rest = tools[STATUS_TOP:]
    if rest:
        calls = sum(t.calls for _, t in rest)
        tokens = sum(t.tokens for _, t in rest)
        lines.append(f"{f'({len(rest)} more tools)':26}{calls:>5}{fmt_tokens(tokens):>8}")
    if not tools:
        lines.append("No tool calls yet in this session.")
    if workspace:
        lines.append("")
        lines.append(f"Kept in {workspace}")
        lines.append("Dashboard: Usage tab")
    return "\n".join(lines) + "\n"


_RECORDER: Optional[Recorder] = None
_RECORDER_LOCK = threading.Lock()


def get_recorder() -> Recorder:
    global _RECORDER
    with _RECORDER_LOCK:
        if _RECORDER is None:
            status_dir = None
            if not _under_pytest():
                try:
                    from ..config import get_config
                    status_dir = get_config().workspace_dir
                except Exception:  # noqa: BLE001
                    status_dir = None
            _RECORDER = Recorder(default_workspace(), status_dir)
        return _RECORDER


def set_recorder(recorder: Optional[Recorder]) -> None:
    """Swap the process recorder (tests)."""
    global _RECORDER
    with _RECORDER_LOCK:
        _RECORDER = recorder


def workspace_report(workspace: Path, days: int = 0) -> dict[str, Any]:
    """Everything kept for one workspace, summed; ``days`` > 0 limits it
    to the last so many days."""
    since = time.time() - days * 86400 if days > 0 else 0.0
    s = summarize(load(workspace, since))
    return {"ok": True, "workspace": str(workspace), "days": days,
            "chars_per_token": CHARS_PER_TOKEN, **s.to_dict()}
