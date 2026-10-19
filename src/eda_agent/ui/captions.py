# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Caption text as a person types it.

Dialog captions arrive with typographic punctuation. MEASURED on AD
26.10.1.6: the WPF "Unsaved Changes" prompt names its button "Don't
Save" with a right single quotation mark (U+2019) where a caller types
an ASCII apostrophe. Compared raw the two never matched, the press by
name found nothing, and the fallback key that followed pressed the
dialog's default button, Save, while reporting the button asked for.

Every matcher that compares a caller's caption with a control's caption
folds both through ``plain`` first.
"""

from __future__ import annotations

_PLAIN = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2026": "...",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
})


def plain(text) -> str:
    """Typographic quotes, ellipsis, dashes and hard spaces as ASCII."""
    return str(text or "").translate(_PLAIN)
