# SPDX-License-Identifier: Apache-2.0
"""What a downloaded file really is, judged from its bytes.

A manufacturer's "datasheet" link that lands on a sign-in page or a bot
check still often says ``application/pdf``, so the Content-Type is never
trusted: a PDF must carry ``%PDF`` near its start, a STEP model the
ISO 10303-21 header, and a ZIP is opened and only its STEP and PDF
members are kept.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

__all__ = ["is_pdf", "is_step", "is_zip", "looks_like_html", "safe_name",
           "step_members", "pdf_title"]

_HEAD = 2048


def is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024] and b"%%EOF" in data[-2048:]


def is_step(data: bytes) -> bool:
    return b"ISO-10303-21" in data[:_HEAD].upper()


def is_zip(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith((b"<!doctype", b"<html", b"<head", b"<?xml"))


def step_members(data: bytes, max_total: int = 200 * 1024 * 1024) -> list[tuple[str, bytes]]:
    """The STEP files inside a ZIP, by base name, each checked by header.

    Member names are reduced to their base name, so a crafted path
    (``../../x.step``) cannot place a file outside the cache.
    """
    out: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                name = PurePosixPath(info.filename.replace("\\", "/")).name
                if not name.lower().endswith((".step", ".stp")):
                    continue
                total += info.file_size
                if total > max_total:
                    break
                body = zf.read(info)
                if is_step(body):
                    out.append((name, body))
    except (zipfile.BadZipFile, OSError, ValueError):
        return []
    return out


def safe_name(text: str, fallback: str = "document", limit: int = 80) -> str:
    """A file name made from free text: letters, digits, ``.-_`` only."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "")).strip("._-")
    name = re.sub(r"_+", "_", name)[:limit].strip("._-")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or not name:
        return fallback
    return name


def pdf_title(data: bytes) -> str:
    """The document's /Title when it is stored as plain text, else ''."""
    m = re.search(rb"/Title\s*\(([^)]{1,200})\)", data[:200000])
    if not m:
        return ""
    raw = m.group(1)
    if raw.startswith(b"\xfe\xff"):
        try:
            return raw[2:].decode("utf-16-be", "replace").strip()
        except Exception:                               # noqa: BLE001
            return ""
    return raw.decode("latin-1", "replace").strip()
