# SPDX-License-Identifier: Apache-2.0
"""Rotating, size-capped, secret-redacting file logging for the "eda_agent" logger tree.

server.py:setup_logging() installs a plain stderr StreamHandler so logging
never touches stdout (the JSON-RPC channel). That handler stays as-is: this
module adds a SECOND handler, a rotating file under
``%USERPROFILE%\\EDA Agent\\logs\\`` (or ``EDA_AGENT_LOG_DIR``), so every
existing ``logging.getLogger("eda_agent...")`` call anywhere in the codebase
gets a persisted, size-bounded, machine-parseable copy for free - no call
site needs to change.

Ported from Altium Booster 3.2's logs.py, adapted to this project's single
root-logger tree (Booster logged each of its own components to a separate
file; eda-agent has one "eda_agent" logger hierarchy, so one capped handler
on its root covers everything through normal propagation) and to this
project's secrets (Nexar/distributor API keys and OAuth tokens, not
Booster's own "aai_"-prefixed keys).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r'(?i)("?(?:\w*api_?key\w*|\w*secret\w*|access_token|refresh_token|'
                r'client_secret|token|passphrase|password)"?\s*[:=]\s*"?)[^",\s}]+'), r"\1***"),
]


def redact(text: str) -> str:
    """Scrub bearer headers, API keys, OAuth tokens and passwords from a log line."""
    for rx, rep in _SECRET:
        text = rx.sub(rep, text)
    return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message - redacted."""

    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": round(record.created, 3), "level": record.levelname,
             "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            d["exc_info"] = self.formatException(record.exc_info)
        return redact(json.dumps(d, default=str, sort_keys=False))


class CappedRotatingHandler(RotatingFileHandler):
    """Size-based rotation, plus a total-directory cap and a free-disk guard.

    Rotation alone bounds ONE file; total_max_bytes bounds the whole
    directory once backups accumulate. min_free_bytes protects the disk
    itself: below that threshold, DEBUG/INFO are dropped and one CRITICAL
    marker is written, so a full disk degrades logging instead of becoming
    another thing that is failing.
    """

    def __init__(self, path: Path, max_bytes: int, backups: int,
                 total_max_bytes: int, min_free_bytes: int):
        super().__init__(path, maxBytes=max_bytes, backupCount=backups,
                         encoding="utf-8", delay=True)
        self.dir = Path(path).parent
        self.total_max = total_max_bytes
        self.min_free = min_free_bytes
        self.low_disk = False
        self._last_disk_check = 0.0

    def _check_disk(self) -> None:
        now = time.monotonic()
        if now - self._last_disk_check < 10:
            return
        self._last_disk_check = now
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            return
        was = self.low_disk
        self.low_disk = free < self.min_free
        if self.low_disk and not was:
            super().emit(logging.makeLogRecord({
                "name": "eda_agent.activity_log", "levelname": "CRITICAL",
                "levelno": logging.CRITICAL, "created": time.time(),
                "msg": f"low disk space ({free // 1048576} MB free); "
                       f"DEBUG/INFO logging suspended"}))

    def emit(self, record: logging.LogRecord) -> None:
        self._check_disk()
        if self.low_disk and record.levelno < logging.WARNING:
            return
        super().emit(record)

    def doRollover(self) -> None:
        super().doRollover()
        enforce_total_cap(self.dir, self.total_max)


def enforce_total_cap(directory: Path, total_max_bytes: int) -> list[str]:
    """Delete oldest rotated files (*.log.N) until the directory fits in total_max_bytes."""
    files = [p for p in Path(directory).glob("*.log*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    removed = []
    rotated = sorted((p for p in files if re.search(r"\.log\.\d+$", p.name)),
                     key=lambda p: p.stat().st_mtime)
    while total > total_max_bytes and rotated:
        p = rotated.pop(0)
        total -= p.stat().st_size
        p.unlink()
        removed.append(p.name)
    return removed


def _env_mb(name: str, default: float) -> int:
    return int(float(os.environ.get(name, default)) * 1_048_576)


def install(logger: logging.Logger | None = None,
            log_dir: Path | None = None) -> Path:
    """Attach a CappedRotatingHandler to ``logger`` (default: the "eda_agent" root).

    Idempotent per logger: calling it again replaces any handler this
    function previously installed rather than stacking duplicates, so
    re-configuring (as tests do) never doubles log output.
    """
    logger = logger or logging.getLogger("eda_agent")
    if log_dir is None:
        override = os.environ.get("EDA_AGENT_LOG_DIR")
        if override:
            log_dir = Path(override)
        else:
            from .config import get_config
            log_dir = get_config().workspace_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    for h in list(logger.handlers):
        if getattr(h, "_eda_agent_activity_log", False):
            logger.removeHandler(h)
            h.close()

    handler = CappedRotatingHandler(
        log_dir / "eda_agent.log",
        _env_mb("EDA_AGENT_LOG_MAX_MB", 10),
        int(os.environ.get("EDA_AGENT_LOG_BACKUPS", 5)),
        _env_mb("EDA_AGENT_LOG_TOTAL_MAX_MB", 200),
        _env_mb("EDA_AGENT_LOG_MIN_FREE_MB", 1024),
    )
    handler.setFormatter(JsonFormatter())
    handler._eda_agent_activity_log = True
    logger.addHandler(handler)
    return log_dir


def tail(log_dir: Path, lines: int = 50) -> list[str]:
    """Last ``lines`` lines of eda_agent.log, without reading the whole file."""
    path = Path(log_dir) / "eda_agent.log"
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", errors="replace").splitlines()
    return data[-lines:]
