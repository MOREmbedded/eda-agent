# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Author KiCad footprint (.kicad_mod) files.

KiCad exposes no API or CLI for *creating* library content, but footprint files
are text s-expressions, so eda-agent generates them directly. This closes the
one genuine library-authoring gap versus Altium's ``lib_create_footprint``. The
output can be validated offline by rendering it with ``kicad-cli fp export svg``.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional


def standard_footprint_dirs(cli_path: Optional[str] = None) -> list[str]:
    """Directories that hold KiCad ``.pretty`` footprint libraries.

    Checks the ``KICAD*_FOOTPRINT_DIR`` environment variables, then derives the
    bundled library path from the kicad-cli location (``<root>/bin/kicad-cli``
    -> ``<root>/share/kicad/footprints``).
    """
    dirs: list[str] = []
    for key in ("KICAD9_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR",
                "KICAD8_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"):
        v = os.environ.get(key)
        if v and os.path.isdir(v):
            dirs.append(v)
    if cli_path:
        root = os.path.dirname(os.path.dirname(cli_path))
        cand = os.path.join(root, "share", "kicad", "footprints")
        if os.path.isdir(cand):
            dirs.append(cand)
    return dirs


#: Characters that turn a name into a path. A library nickname and a
#: symbol or footprint name are IDENTIFIERS, and these functions treat
#: them as one path component each, so anything that could make a
#: component mean somewhere else is refused outright.
#:
#: The colon matters as much as the separators, and only on Windows:
#: ``os.path.join(d, "C:evil")`` returns ``"C:evil"``, because a drive
#: prefix makes the second argument absolute and the first is discarded
#: entirely. That is not traversal out of the search directory, it is
#: ignoring the search directory, and a rule written only against ``..``
#: would not have caught it.
_PATHISH = ("/", "\\", ":", "\x00")


def is_plain_name(part: str) -> bool:
    """Whether ``part`` is usable as a single path component.

    Deliberately a rejection of path syntax rather than an allow-list of
    characters. KiCad names carry ``+``, ``-``, ``.``, ``~`` and ``#``
    (``+3V3``, ``C_Polarized``, ``74HC00``), so enumerating what is
    permitted would be a guess about somebody else's naming rules and
    would refuse parts that exist.
    """
    if not part or part in (".", ".."):
        return False
    return not any(ch in part for ch in _PATHISH)


def is_inside(base: str, candidate: str) -> bool:
    """Whether ``candidate`` resolves to something under ``base``.

    The second half of the check, and the one that does not rely on
    having thought of every dangerous spelling. ``is_plain_name`` refuses
    the input; this confirms the OUTPUT landed where it was supposed to,
    after symlinks and ``..`` have been resolved.
    """
    try:
        b = os.path.realpath(base)
        c = os.path.realpath(candidate)
    except (OSError, ValueError):
        return False
    return c == b or c.startswith(b + os.sep)


def find_footprint_file(lib_id: str, search_dirs: list[str]) -> Optional[str]:
    """Resolve a footprint lib_id ("Lib:Name") to its .kicad_mod path, or None.

    A bare name (no ``:``) is looked up across every library.

    A lib_id that is not two plain names resolves to nothing. This
    function's contract is that it searches ``search_dirs``, and both
    halves of the id land in the path it builds, so without this a
    caller-supplied id could name a file outside them and still be
    answered as though it had been found in a library.
    """
    if not lib_id:
        return None
    if ":" in lib_id:
        lib, name = lib_id.split(":", 1)
        if not (is_plain_name(lib) and is_plain_name(name)):
            return None
        for d in search_dirs:
            cand = os.path.join(d, lib + ".pretty", name + ".kicad_mod")
            if os.path.isfile(cand) and is_inside(d, cand):
                return cand
        return None
    if not is_plain_name(lib_id):
        return None
    for d in search_dirs:
        try:
            for entry in os.listdir(d):
                if entry.endswith(".pretty"):
                    cand = os.path.join(d, entry, lib_id + ".kicad_mod")
                    if os.path.isfile(cand) and is_inside(d, cand):
                        return cand
        except OSError:
            continue
    return None


def _block_end(text: str, start: int) -> int:
    """Index just past the ')' that closes the '(' at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def embed_footprint(mod_text: str, refdes: str, value: str,
                    x_mm: float, y_mm: float, pad_nets: dict[str, str],
                    net_num: dict[str, int], uid) -> Optional[str]:
    """Adapt a .kicad_mod footprint into a placed board footprint.

    Sets the position and uuid, overrides the Reference/Value, and injects
    ``(net N "NAME")`` into each pad whose number is in ``pad_nets``. Returns
    None if the text is not a footprint.
    """
    import re
    t = mod_text.strip()
    m = re.match(r'\(footprint\s+"[^"]*"', t)
    if not m:
        return None
    t = (t[:m.end()]
         + f'\n\t(at {_num(x_mm)} {_num(y_mm)})\n\t(uuid "{uid()}")'
         + t[m.end():])
    t = re.sub(r'(\(property\s+"Reference"\s+")[^"]*(")',
               lambda mm: mm.group(1) + _esc(refdes) + mm.group(2), t, count=1)
    t = re.sub(r'(\(property\s+"Value"\s+")[^"]*(")',
               lambda mm: mm.group(1) + _esc(value) + mm.group(2), t, count=1)

    out: list[str] = []
    i = 0
    while True:
        j = t.find('(pad "', i)
        if j < 0:
            out.append(t[i:])
            break
        nm = re.match(r'\(pad\s+"([^"]*)"', t[j:])
        num = nm.group(1) if nm else ""
        end = _block_end(t, j)
        if end < 0:
            out.append(t[i:])
            break
        block = t[j:end]
        name = pad_nets.get(num)
        if name and net_num.get(name):
            block = (block[:-1].rstrip()
                     + f'\n\t\t(net {net_num[name]} "{_esc(name)}")\n\t)')
        out.append(t[i:j])
        out.append(block)
        i = end
    return "".join(out)


def _esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _num(v: Any) -> str:
    # Compact numeric formatting (no trailing ".0" clutter) as KiCad emits.
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def _pad_sexpr(p: dict[str, Any]) -> str:
    number = _esc(p.get("number", ""))
    ptype = str(p.get("type", "smd")).lower()
    shape = str(p.get("shape", "rect")).lower()
    x, y = _num(p.get("x_mm", 0)), _num(p.get("y_mm", 0))
    w, h = _num(p.get("w_mm", 1)), _num(p.get("h_mm", 1))
    if ptype in ("thru_hole", "th", "pth"):
        drill = _num(p.get("drill_mm", 0.8))
        return (f'\t(pad "{number}" thru_hole {shape}\n'
                f'\t\t(at {x} {y})\n\t\t(size {w} {h})\n'
                f'\t\t(drill {drill})\n\t\t(layers "*.Cu" "*.Mask")\n\t)')
    return (f'\t(pad "{number}" smd {shape}\n'
            f'\t\t(at {x} {y})\n\t\t(size {w} {h})\n'
            f'\t\t(layers "F.Cu" "F.Paste" "F.Mask")\n\t)')


def build_footprint(name: str, pads: list[dict[str, Any]],
                    descr: str = "", tags: str = "") -> str:
    """Build a valid .kicad_mod s-expression for a footprint with pads.

    Pads only: no silkscreen, courtyard or fabrication outline. A complete
    footprint from a package drawing is :func:`build_footprint_from_model`.
    """
    lines = [
        f'(footprint "{_esc(name)}"',
        "\t(version 20240108)",
        '\t(generator "eda-agent")',
        '\t(layer "F.Cu")',
    ]
    if descr:
        lines.append(f'\t(descr "{_esc(descr)}")')
    if tags:
        lines.append(f'\t(tags "{_esc(tags)}")')
    lines.append(
        '\t(property "Reference" "REF**"\n\t\t(at 0 -1 0)\n'
        '\t\t(layer "F.SilkS")\n'
        '\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    lines.append(
        f'\t(property "Value" "{_esc(name)}"\n\t\t(at 0 1 0)\n'
        '\t\t(layer "F.Fab")\n'
        '\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    for p in pads:
        lines.append(_pad_sexpr(p))
    lines.append(")")
    return "\n".join(lines) + "\n"


# --- a complete footprint from a land pattern model ---------------------------

#: Graphic roles of the land pattern model -> KiCad layers, by the KiCad
#: library convention: silkscreen, fabrication (assembly) and courtyard.
KICAD_ROLE_LAYERS = {"silk": "F.SilkS", "assembly": "F.Fab",
                     "courtyard": "F.CrtYd"}

#: Model pad shape -> KiCad pad shape.
_KICAD_SHAPE = {"roundrect": "roundrect", "rect": "rect", "rectangle": "rect",
                "rectangular": "rect", "circle": "circle", "round": "circle",
                "oval": "oval", "ellipse": "oval"}


def _mm(v: Any) -> str:
    # Fixed point, never an exponent: the same formatter the bridge uses.
    from ..units import format_length
    return format_length(round(float(v), 6))


def _xy(x: float, y: float) -> str:
    # KiCad's Y axis points down; the model's points up.
    return f"{_mm(x)} {_mm(-y)}"


def _stroke(width: float) -> str:
    return f"(stroke (width {_mm(width)}) (type solid))"


def _kicad_pad(p) -> str:
    shape = _KICAD_SHAPE.get(str(p.shape or "").strip().lower())
    if shape is None:
        raise ValueError(f"pad {p.number} has shape {p.shape!r}, which KiCad "
                         f"cannot draw")
    ratio = float(p.corner_ratio or 0.0)
    if shape == "roundrect" and ratio <= 0:
        shape = "rect"
    rot = float(p.rotation or 0.0) % 360.0
    at = f"(at {_xy(p.x, p.y)}{' ' + _mm(rot) if rot else ''})"
    size = f"(size {_mm(p.width)} {_mm(p.height)})"
    extra: list[str] = []
    if p.kind == "smd":
        kind = "smd"
        layers = ['"F.Cu"', '"F.Mask"']
        if p.paste and not p.paste_apertures:
            layers.insert(1, '"F.Paste"')
        drill = ""
    else:
        kind = "np_thru_hole" if (p.kind == "npth" or not p.plated) else "thru_hole"
        layers = ['"*.Cu"', '"*.Mask"']
        if p.hole_length > p.hole > 0:
            drill = f"(drill oval {_mm(p.hole_length)} {_mm(p.hole)})"
        else:
            drill = f"(drill {_mm(p.hole)})"
    if shape == "roundrect":
        extra.append(f"(roundrect_rratio {_mm(min(ratio, 0.5))})")
    if p.mask_expansion:
        extra.append(f"(solder_mask_margin {_mm(p.mask_expansion)})")
    if p.kind == "smd" and p.paste_expansion and not p.paste_apertures:
        extra.append(f"(solder_paste_margin {_mm(p.paste_expansion)})")
    parts = [f'(pad "{_esc(p.number)}" {kind} {shape}', at, size]
    if drill:
        parts.append(drill)
    parts.append(f"(layers {' '.join(layers)})")
    parts.extend(extra)
    return "\t" + "\n\t\t".join(parts) + "\n\t)"


def _kicad_graphic(g, layer: str) -> list[str]:
    from ..design.landpattern.model import Arc, Circle, Line, Polygon, Text
    if isinstance(g, Line):
        return [f"\t(fp_line (start {_xy(g.x1, g.y1)}) (end {_xy(g.x2, g.y2)}) "
                f'{_stroke(g.width)} (layer "{layer}"))']
    if isinstance(g, Circle):
        fill = "solid" if g.filled else "none"
        return [f"\t(fp_circle (center {_xy(g.x, g.y)}) "
                f"(end {_xy(g.x + g.r, g.y)}) "
                f'{_stroke(g.width)} (fill {fill}) (layer "{layer}"))']
    if isinstance(g, Arc):
        a0 = math.radians(g.start)
        am = math.radians(g.start + g.sweep / 2.0)
        a1 = math.radians(g.start + g.sweep)
        pts = [(g.x + g.r * math.cos(a), g.y + g.r * math.sin(a))
               for a in (a0, am, a1)]
        return [f"\t(fp_arc (start {_xy(*pts[0])}) (mid {_xy(*pts[1])}) "
                f'(end {_xy(*pts[2])}) {_stroke(g.width)} (layer "{layer}"))']
    if isinstance(g, Polygon):
        if g.closed or g.filled:
            pts = " ".join(f"(xy {_xy(x, y)})" for x, y in g.points)
            fill = "solid" if g.filled else "none"
            return [f"\t(fp_poly (pts {pts}) {_stroke(g.width)} (fill {fill}) "
                    f'(layer "{layer}"))']
        out = []
        for (x1, y1), (x2, y2) in zip(g.points, g.points[1:]):
            out.append(f"\t(fp_line (start {_xy(x1, y1)}) (end {_xy(x2, y2)}) "
                       f'{_stroke(g.width)} (layer "{layer}"))')
        return out
    if isinstance(g, Text):
        return [f'\t(fp_text user "{_esc(g.text)}" (at {_xy(g.x, g.y)} 0) '
                f'(layer "{layer}") (effects (font (size {_mm(g.height)} '
                f"{_mm(g.height)}) (thickness {_mm(g.thickness)}))))"]
    raise ValueError(f"no KiCad form for {type(g).__name__}")


def build_footprint_from_model(model, name: str = "", descr: str = "",
                               tags: str = "") -> str:
    """A complete .kicad_mod from a land pattern :class:`FootprintModel`.

    Pads with their shape, corner ratio, rotation, drill or slot and
    plating; paste windows over exposed pads as unnumbered F.Paste pads
    (the KiCad library's own convention), with the thermal pad's full
    paste opening left out; silkscreen with the pin 1 dot on F.SilkS; the
    body outline and a ``${REFERENCE}`` text on F.Fab; the courtyard on
    F.CrtYd. Origin at the model's origin, the package centroid. Y flips,
    because KiCad's axis points down.

    No 3D body: KiCad draws one only from a model file, which a package
    drawing does not supply. Manual mask and paste margins are written
    only when non-zero, so a 1:1 opening leaves KiCad's own setting in
    charge.
    """
    from ..design.landpattern.model import Text

    fp_name = name or model.name
    description = (descr or model.description or "").replace("\n", " ")
    x0, y0, x1, y1 = model.courtyard_bbox()
    cx = (x0 + x1) / 2.0
    lines = [
        f'(footprint "{_esc(fp_name)}"',
        "\t(version 20240108)",
        '\t(generator "eda-agent")',
        '\t(layer "F.Cu")',
    ]
    if description:
        lines.append(f'\t(descr "{_esc(description)}")')
    if tags:
        lines.append(f'\t(tags "{_esc(tags)}")')
    lines.append(
        f'\t(property "Reference" "REF**"\n\t\t(at {_xy(cx, y1 + 1.0)} 0)\n'
        '\t\t(layer "F.SilkS")\n'
        '\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    lines.append(
        f'\t(property "Value" "{_esc(fp_name)}"\n'
        f'\t\t(at {_xy(cx, y0 - 1.0)} 0)\n'
        '\t\t(layer "F.Fab")\n'
        '\t\t(effects (font (size 1 1) (thickness 0.15)))\n\t)')
    kinds = {p.kind for p in model.pads}
    if "tht" in kinds:
        lines.append("\t(attr through_hole)")
    elif "smd" in kinds:
        lines.append("\t(attr smd)")
    for g in model.graphics:
        lines.extend(_kicad_graphic(g, KICAD_ROLE_LAYERS[g.layer]))
    lines.extend(_kicad_graphic(model.courtyard, KICAD_ROLE_LAYERS["courtyard"]))
    ref = model.ref
    lines.extend(_kicad_graphic(
        Text(ref.layer, "${REFERENCE}", ref.x, ref.y, ref.height, ref.thickness),
        KICAD_ROLE_LAYERS[ref.layer]))
    for p in model.pads:
        lines.append(_kicad_pad(p))
    for p in model.pads:
        for a in p.paste_apertures:
            lines.append(
                f'\t(pad "" smd rect\n\t\t(at {_xy(a.x, a.y)})\n'
                f"\t\t(size {_mm(a.width)} {_mm(a.height)})\n"
                '\t\t(layers "F.Paste")\n\t)')
    lines.append(")")
    return "\n".join(lines) + "\n"


def footprint_pads_mm(text: str) -> list[dict[str, Any]]:
    """The pads of a .kicad_mod, read back with the library reader's parser.

    Each: number, type, shape, x and y in the Y-UP frame (flipped back),
    rotation, w, h, drill (diameter), slot (length, 0 when round), layers,
    roundrect_rratio and the mask and paste margins (0 when absent).
    """
    from ..libimport.kicad.sexpr import loads

    root = loads(text)
    if not root or root[0] != "footprint":
        raise ValueError("not a footprint")

    def child(node: list, tag: str):
        for c in node[1:]:
            if isinstance(c, list) and c and c[0] == tag:
                return c
        return None

    out: list[dict[str, Any]] = []
    for node in root[1:]:
        if not (isinstance(node, list) and node and node[0] == "pad"):
            continue
        at = child(node, "at") or ["at", "0", "0"]
        size = child(node, "size") or ["size", "0", "0"]
        drill = child(node, "drill")
        layers = child(node, "layers") or ["layers"]
        d, slot = 0.0, 0.0
        if drill:
            vals = [v for v in drill[1:] if not isinstance(v, list)]
            if vals and vals[0] == "oval":
                slot, d = float(vals[1]), float(vals[2])
            elif vals:
                d = float(vals[0])

        def num(tag: str, node: list = node) -> float:
            c = child(node, tag)
            return float(c[1]) if c and len(c) > 1 else 0.0

        out.append({
            "number": node[1], "type": node[2], "shape": node[3],
            "x": float(at[1]), "y": -float(at[2]),
            "rotation": float(at[3]) if len(at) > 3 else 0.0,
            "w": float(size[1]), "h": float(size[2]),
            "drill": d, "slot": slot, "layers": list(layers[1:]),
            "roundrect_rratio": num("roundrect_rratio"),
            "solder_mask_margin": num("solder_mask_margin"),
            "solder_paste_margin": num("solder_paste_margin"),
        })
    return out
