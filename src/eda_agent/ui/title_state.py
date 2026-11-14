# SPDX-License-Identifier: Apache-2.0
"""Whether the document in front has unsaved edits, read from Altium's title.

After scripted board edits (tracks, pads, parts, net bindings, a dielectric)
Altium's own ``IServerDocument.Modified`` read False while the window title
showed ``BoosterTest.PcbDoc * - Altium Designer Professional (26.11.1)``.
Measured live on 2026-11-14. So every ``modified`` the bridge reports
could call an edited board clean, and app_close_document's "refuses a
modified document" let it through to Altium's modal save prompt.

The title only describes the FOCUSED document, so this answers for that
one document and says "cannot tell" (None) for any other.
"""
from __future__ import annotations

import ntpath
import re
from typing import Iterable, Optional

#: "<file name>[ *] - Altium Designer<anything>"
_TITLE = re.compile(r"^(?P<name>.+?)(?P<star> \*)? - Altium Designer\b")


def parse_title(title: str) -> Optional[tuple[str, bool]]:
    """``(file name, shows unsaved edits)`` from Altium's main window title,
    or None for a title that is not Altium's document frame."""
    m = _TITLE.match(title or "")
    if not m:
        return None
    return m.group("name").strip(), bool(m.group("star"))


def _titles() -> Iterable[str]:
    from . import windows
    if not windows.available():
        return []
    try:
        return [w.title for w in windows.enumerate_windows(visible_only=True)]
    except Exception:  # noqa: BLE001 - a window list is a hint, never a failure
        return []


def front_document_dirty(file_path_or_name: str, titles: Optional[Iterable[str]] = None
                         ) -> Optional[bool]:
    """True when Altium's title names this document with a ``*``, False when
    it names it without one, None when the title does not name it (another
    document is in front, no Altium window, or not Windows)."""
    want = ntpath.basename(file_path_or_name or "").lower()
    if not want:
        return None
    for title in (titles if titles is not None else _titles()):
        parsed = parse_title(title)
        if parsed and parsed[0].lower() == want:
            return parsed[1]
    return None


def mark_modified(doc: dict, titles: Optional[Iterable[str]] = None) -> dict:
    """``doc`` (a document row of the bridge) with ``modified`` set true when
    the title shows its unsaved edits and the bridge read false. A true is
    never turned false: the title only adds what the API missed."""
    if not isinstance(doc, dict) or doc.get("modified"):
        return doc
    name = doc.get("file_path") or doc.get("file_name") or ""
    if front_document_dirty(name, titles):
        return {**doc, "modified": True, "modified_from": "window title"}
    return doc
