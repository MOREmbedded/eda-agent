"""The fill and outline a new symbol body is drawn with.

Bodies were always Altium's block yellow (8454143) with a black outline.
A library with its own convention (SkyComm's symbols are #FFFFB0 with a
dark-red outline) came out with one part that matched nothing else in it.
So the colours are, in order: the ones the call gives, the ones the
library's existing symbols use, and only then the old yellow and black.

Altium stores a colour as a BGR integer: #RRGGBB is B*65536 + G*256 + R.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

#: Altium's block yellow, BGR (#FFFF80).
DEFAULT_FILL = 8454143
#: Black.
DEFAULT_OUTLINE = 0


def to_bgr(value: Any) -> Optional[int]:
    """A colour as Altium stores it, from "#RRGGBB", "RRGGBB" or a BGR
    integer; None for empty. Raises ValueError for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a colour: {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise ValueError(f"a BGR colour is 0..16777215, not {value}")
        return value
    text = str(value).strip()
    if text.isdigit():
        return to_bgr(int(text))
    hexpart = text[1:] if text.startswith("#") else text
    if len(hexpart) != 6 or any(c not in "0123456789abcdefABCDEF" for c in hexpart):
        raise ValueError(f"a colour is #RRGGBB or a BGR integer, not {value!r}")
    r, g, b = (int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
    return b * 65536 + g * 256 + r


def to_hex(bgr: int) -> str:
    """#RRGGBB for a BGR integer."""
    r, g, b = bgr & 0xFF, (bgr >> 8) & 0xFF, (bgr >> 16) & 0xFF
    return f"#{r:02X}{g:02X}{b:02X}"


def body_colours_in(geometries: Iterable[Any]) -> Optional[tuple[int, int]]:
    """The (fill, outline) most of these symbols' solid rectangles use, or
    None when none has one. ``geometries`` are library.get_symbol_geometry
    replies."""
    seen: Counter = Counter()
    for geo in geometries:
        if not isinstance(geo, dict):
            continue
        for prim in geo.get("primitives") or []:
            if not isinstance(prim, dict) or prim.get("kind") != "rect":
                continue
            if not prim.get("is_solid"):
                continue
            try:
                seen[(int(prim["area_color"]), int(prim["color"]))] += 1
            except (KeyError, TypeError, ValueError):
                continue
    if not seen:
        return None
    return seen.most_common(1)[0][0]


def choose(fill: Any = None, outline: Any = None,
           learned: Optional[tuple[int, int]] = None) -> dict[str, Any]:
    """``{fill, outline, source}``: what the call gives wins, then what the
    library uses, then the defaults. Raises ValueError on a bad colour."""
    f, o = to_bgr(fill), to_bgr(outline)
    source = "given" if f is not None or o is not None else ""
    if learned is not None:
        f = learned[0] if f is None else f
        o = learned[1] if o is None else o
        source = source or "library"
    if f is None or o is None:
        source = source or "default"
    f = DEFAULT_FILL if f is None else f
    o = DEFAULT_OUTLINE if o is None else o
    return {"fill": f, "outline": o, "source": source}
