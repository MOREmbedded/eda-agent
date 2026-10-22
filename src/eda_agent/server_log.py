# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The server's own log file, capped, and every log line redacted.

The server logged to stderr only, which the MCP client keeps or drops as
it likes, so after a problem there was often nothing to read. This adds a
file in the workspace, ``logs/server-<pid>.log``: one per server process,
because several sessions run a server each and Windows will not rename a
file another process has open, which is how a shared rotating log breaks.
Each file rotates at ``MAX_BYTES`` keeping ``BACKUPS`` old copies, and
housekeeping removes the files of servers gone for ``MAX_AGE_DAYS``.

REDACTION is on the record, not the handler, so stderr and the file get
the same treatment: database connection credentials (the redaction the
DbLib tools already use), ``password=`` / ``token=`` / ``api_key=`` style
values, bearer tokens and JWTs. A log line that quotes a request carrying
a secret keeps its shape and loses the secret.

Installed best-effort: a full disk or a read-only workspace never stops a
server from starting.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import time
from pathlib import Path
from typing import Optional

from .library_db import redact_connection_text

MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
MAX_AGE_DAYS = 14
LOG_DIR = "logs"

#: In order: a bearer token and a JWT FIRST, because the key rule below
#: would take "Bearer" as the value of "Authorization:" and leave the
#: token after it in the log.
_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    # A JSON Web Token: three base64url parts, the first starting eyJ.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+"),
     "***jwt***"),
    # JSON or query style: "api_key": "abc", access_token=abc, client_secret
    (re.compile(r'(?i)("?(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?'
                r'token|id[_-]?token|client[_-]?secret|secret|password|passwd|'
                r'pwd)"?\s*[:=]\s*"?)([^"&\s,;}]+)'), r"\1***"),
)


def redact(text: str) -> str:
    """``text`` with every secret it quotes replaced by ``***``."""
    if not isinstance(text, str) or not text:
        return text
    text = redact_connection_text(text)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Redacts the formatted message of every record it passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:                        # noqa: BLE001 - a bad %
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def install(root: logging.Logger, workspace: Path) -> Optional[Path]:
    """Add the redacting filter to ``root``'s handlers and a capped file
    handler in ``workspace/logs``. Returns the file, or None when it could
    not be opened."""
    redactor = RedactingFilter()
    for handler in root.handlers:
        handler.addFilter(redactor)
    try:
        folder = Path(workspace) / LOG_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"server-{os.getpid()}.log"
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8",
            delay=True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        handler.addFilter(redactor)
        root.addHandler(handler)
        return path
    except OSError:
        return None


def prune(workspace: Path, max_age_days: float = MAX_AGE_DAYS) -> int:
    """Remove server logs untouched for ``max_age_days``. Never raises."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for path in (Path(workspace) / LOG_DIR).glob("server-*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
