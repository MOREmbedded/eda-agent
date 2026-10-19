# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A caller's layer name is resolved against the document's own stack.

GetLayerFromString answers eTopLayer for any name it does not know, so a
user-named layer such as "Top Assembly", or Mechanical17, became top
copper: lib_delete_footprint_primitives would have deleted copper, and a
3D body landed on the top layer. ResolveLayerId walks the stack and
answers eNoLayer for a name it cannot place. Only Utils.pas, which builds
ResolveLayerId on it, may call it.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_pas_constants_declared import _build_files, _code


def test_only_the_resolver_calls_getlayerfromstring():
    callers = [p.name for p in _build_files()
               if p.name != "Utils.pas"
               and re.search(r"\bGetLayerFromString\s*\(",
                             _code(p.read_text(encoding="utf-8-sig",
                                               errors="replace")))]
    assert callers == [], (
        f"{callers} resolve a layer with GetLayerFromString; use "
        f"ResolveLayerId so an unknown name is refused, not made top copper")
