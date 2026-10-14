# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Keep everything on a schematic inside its sheet (offline).

Two questions about a laid-out sheet:

1. Is anything drawn outside the usable area: past the frame, inside the
   border margin, or over the title block?
2. If so, can the drawing be shifted to fit, or is the sheet too small, in
   which case which larger size of the SAME family is the first that fits
   (A4 -> A3 -> A2, ANSI A -> B -> C)? A full sheet is enlarged, never
   spilled onto another sheet.

Frame sizes are Altium's own sheet styles, not paper: Altium's A3 frame is
15500 x 11100 mils where A3 paper is 16540 x 11690. The values are Altium's
SheetStyle sizes in mils (styles 0..17); confirm against a live
``SheetSizeX/SheetSizeY`` read where it matters, and pass the live frame
when one is known, which also covers custom templates.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Optional

#: Altium sheet styles, frame (width, height) in mils, landscape.
ALTIUM_FRAMES: dict[str, tuple[int, int]] = {
    "A4": (11500, 7600), "A3": (15500, 11100), "A2": (22300, 15700),
    "A1": (31500, 22300), "A0": (44600, 31500),
    "A": (9500, 7500), "B": (15000, 9500), "C": (20000, 15000),
    "D": (32000, 20000), "E": (42000, 32000),
    "LETTER": (11000, 8500), "LEGAL": (14000, 8500), "TABLOID": (17000, 11000),
    "ORCADA": (9900, 7900), "ORCADB": (15400, 9900), "ORCADC": (20600, 15600),
    "ORCADD": (32600, 20600), "ORCADE": (42800, 32800),
}

#: Families, smallest first. Enlarging stays within a family so a sheet
#: keeps its look (an ISO template grows into the next ISO size).
FAMILIES: tuple[tuple[str, ...], ...] = (
    ("A4", "A3", "A2", "A1", "A0"),
    ("A", "B", "C", "D", "E"),
    ("LETTER", "TABLOID", "C", "D", "E"),
    ("ORCADA", "ORCADB", "ORCADC", "ORCADD", "ORCADE"),
)

_ALIASES = {"ANSI A": "A", "ANSI B": "B", "ANSI C": "C", "ANSI D": "D",
            "ANSI E": "E", "USLETTER": "LETTER", "USLEGAL": "LEGAL",
            "USLEDGER": "TABLOID", "LEDGER": "TABLOID", "ORCAD A": "ORCADA",
            "ORCAD B": "ORCADB", "ORCAD C": "ORCADC", "ORCAD D": "ORCADD",
            "ORCAD E": "ORCADE"}

#: Altium's default sheet border margin (SheetMarginWidth), mils.
DEFAULT_MARGIN = 200
#: A conservative keep-out for Altium's standard title block, bottom right,
#: (width, height) in mils. Pass the template's measured block when known.
DEFAULT_TITLE_BLOCK = (3500, 1500)


def normalise(size: str) -> str:
    s = str(size or "").strip().upper()
    return _ALIASES.get(s, s.replace(" ", ""))


def altium_frame(size: str) -> Optional[tuple[int, int]]:
    return ALTIUM_FRAMES.get(normalise(size))


def next_size(size: str) -> Optional[str]:
    """The next larger style in ``size``'s family, or None at the top."""
    s = normalise(size)
    for fam in FAMILIES:
        if s in fam:
            i = fam.index(s)
            return fam[i + 1] if i + 1 < len(fam) else None
    return None


@dataclass
class Box:
    kind: str
    ref: str
    x1: float
    y1: float
    x2: float
    y2: float

    def as_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref,
                "bbox": [round(self.x1), round(self.y1), round(self.x2),
                         round(self.y2)]}


@dataclass
class Area:
    """The usable rectangle of a frame, with keep-outs inside it."""

    x1: float
    y1: float
    x2: float
    y2: float
    keepouts: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1


def usable_area(frame: tuple[float, float], margin: float = DEFAULT_MARGIN,
                title_block: Optional[tuple[float, float]] = DEFAULT_TITLE_BLOCK
                ) -> Area:
    w, h = frame
    keep = []
    if title_block:
        tw, th = title_block
        keep.append((w - margin - tw, margin, w - margin, margin + th))
    return Area(margin, margin, w - margin, h - margin, keep)


_LABEL_CHAR_W = 70        # mils per character at the default label font
_PORT_HALF = 150          # a power port glyph's half extent


def canvas_boxes(canvas, sheet: str) -> list[Box]:
    """Every drawn object on ``sheet`` as an axis-aligned box."""
    boxes: list[Box] = []
    for inst in canvas.instances:
        if inst.sheet != sheet:
            continue
        bb = inst.world_bbox()
        xs = [bb.x_min, bb.x_max] + [p.x for p in inst.all_pin_endpoints()]
        ys = [bb.y_min, bb.y_max] + [p.y for p in inst.all_pin_endpoints()]
        for pos in (inst.designator_pos, inst.value_pos):
            if pos:
                xs += [pos[0], pos[0] + 600]
                ys += [pos[1], pos[1] + 100]
        boxes.append(Box("part", inst.refdes, min(xs), min(ys), max(xs), max(ys)))
    for w in canvas.wires:
        if w.sheet == sheet:
            boxes.append(Box("wire", w.net, min(w.x1, w.x2), min(w.y1, w.y2),
                             max(w.x1, w.x2), max(w.y1, w.y2)))
    for lb in canvas.labels:
        if lb.sheet != sheet:
            continue
        length = len(lb.text) * _LABEL_CHAR_W
        x1 = lb.x - length if getattr(lb, "justification", 0) == 2 else lb.x
        boxes.append(Box("label", lb.text, x1, lb.y, x1 + length, lb.y + 100))
    for pp in canvas.power_ports:
        if pp.sheet == sheet:
            boxes.append(Box("power_port", pp.text, pp.x - _PORT_HALF,
                             pp.y - _PORT_HALF, pp.x + _PORT_HALF,
                             pp.y + _PORT_HALF))
    for j in canvas.junctions:
        if j.sheet == sheet:
            boxes.append(Box("junction", "", j.x, j.y, j.x, j.y))
    return boxes


def _overlaps(b: Box, k: tuple[float, float, float, float]) -> bool:
    return not (b.x2 <= k[0] or b.x1 >= k[2] or b.y2 <= k[1] or b.y1 >= k[3])


def outside(boxes: list[Box], area: Area) -> list[dict]:
    """Objects not wholly inside the usable area, or over a keep-out."""
    out = []
    for b in boxes:
        past = max(area.x1 - b.x1, area.y1 - b.y1, b.x2 - area.x2,
                   b.y2 - area.y2, 0)
        over = [k for k in area.keepouts if _overlaps(b, k)]
        if past > 0 or over:
            d = b.as_dict()
            d["past_border_mils"] = round(past)
            if over:
                d["over_title_block"] = True
            out.append(d)
    return out


def fit_report(boxes: list[Box], size: str,
               frame: Optional[tuple[float, float]] = None,
               margin: float = DEFAULT_MARGIN,
               title_block: Optional[tuple[float, float]] = DEFAULT_TITLE_BLOCK,
               grid: float = 100.0) -> dict:
    """Does the drawing fit ``size``; if not, shift it or name the first
    larger size of the same family that holds it.

    A shift is a whole number of ``grid`` steps (rounded away from zero), so
    pins and wires stay on the snap grid; 0 leaves it unrounded.

    The extent must fit the usable area; the title block is cleared by
    requiring the extent to fit either above it or to its left. Returns the
    verdict ``fits`` / ``shift`` (with dx, dy) / ``enlarge`` (with the size)
    / ``too_big`` (nothing in the family holds it)."""
    frame = frame or altium_frame(size) or ALTIUM_FRAMES["A4"]
    area = usable_area(frame, margin, title_block)
    found = outside(boxes, area)
    if not boxes:
        return {"verdict": "fits", "size": normalise(size), "outside": []}
    ex1 = min(b.x1 for b in boxes)
    ey1 = min(b.y1 for b in boxes)
    ex2 = max(b.x2 for b in boxes)
    ey2 = max(b.y2 for b in boxes)
    ew, eh = ex2 - ex1, ey2 - ey1
    report = {"size": normalise(size), "frame": list(frame),
              "extent": [round(ex1), round(ey1), round(ex2), round(ey2)],
              "outside": found}
    if not found:
        report["verdict"] = "fits"
        return report

    def minimal_move(a: Area) -> Optional[tuple[float, float]]:
        """The SMALLEST (dx, dy) that puts every box inside ``a`` and off
        its keep-outs, or None when no translation can. Smallest, because
        a drawing that is a little over the edge should move a little:
        centring it would move every part on a sheet re-run for the sake
        of one."""
        if ew > a.w or eh > a.h:
            return None

        def clamp(lo, hi, e_lo, e_hi):
            if e_lo < lo:
                return lo - e_lo
            if e_hi > hi:
                return hi - e_hi
            return 0.0

        dx = clamp(a.x1, a.x2, ex1, ex2)
        dy = clamp(a.y1, a.y2, ey1, ey2)

        def clear(dx, dy):
            return not any(_overlaps(Box("", "", b.x1 + dx, b.y1 + dy,
                                         b.x2 + dx, b.y2 + dy), k)
                           for b in boxes for k in a.keepouts)

        def on_grid(move):
            if not grid:
                return move
            mdx, mdy = (math.copysign(math.ceil(abs(v) / grid - 1e-9) * grid, v)
                        if v else 0.0 for v in move)
            if (ex1 + mdx >= a.x1 - 1e-6 and ex2 + mdx <= a.x2 + 1e-6 and
                    ey1 + mdy >= a.y1 - 1e-6 and ey2 + mdy <= a.y2 + 1e-6
                    and clear(mdx, mdy)):
                return mdx, mdy
            return None

        if clear(dx, dy):
            return on_grid((dx, dy))
        options = []
        for k in a.keepouts:
            # Up, over the block: the lowest box that hits it ends above it.
            hits = [b for b in boxes if _overlaps(
                Box("", "", b.x1 + dx, b.y1 + dy, b.x2 + dx, b.y2 + dy), k)]
            up = k[3] - min(b.y1 + dy for b in hits)
            left = (max(b.x2 + dx for b in hits)) - k[0]
            for cand in ((dx, dy + up), (dx - left, dy)):
                cdx, cdy = cand
                if (ex1 + cdx >= a.x1 - 1e-6 and ex2 + cdx <= a.x2 + 1e-6 and
                        ey1 + cdy >= a.y1 - 1e-6 and ey2 + cdy <= a.y2 + 1e-6
                        and clear(cdx, cdy)):
                    snapped = on_grid(cand)
                    if snapped:
                        options.append(snapped)
        if not options:
            return None
        return min(options, key=lambda d: abs(d[0]) + abs(d[1]))

    move = minimal_move(area)
    if move:
        report.update(verdict="shift", dx=round(move[0]), dy=round(move[1]))
        return report
    nxt = next_size(size)
    while nxt:
        if minimal_move(usable_area(ALTIUM_FRAMES[nxt], margin, title_block)):
            report.update(verdict="enlarge", to_size=nxt,
                          to_frame=list(ALTIUM_FRAMES[nxt]))
            return report
        nxt = next_size(nxt)
    report["verdict"] = "too_big"
    return report


def check_canvas(canvas, sheet: Optional[str] = None,
                 frames: Optional[dict[str, tuple[float, float]]] = None,
                 **kw) -> list[dict]:
    """fit_report for every sheet of a canvas (or one)."""
    out = []
    for s in canvas.sheets:
        if sheet and s.name != sheet:
            continue
        frame = (frames or {}).get(s.name)
        rep = fit_report(canvas_boxes(canvas, s.name), s.size, frame, **kw)
        rep["sheet"] = s.name
        out.append(rep)
    return out


__all__ = ["ALTIUM_FRAMES", "FAMILIES", "altium_frame", "next_size",
           "usable_area", "canvas_boxes", "outside", "fit_report",
           "check_canvas", "normalise", "template_size_token",
           "template_for_size", "fit_canvas", "translate_sheet"]


# ---------------------------------------------------------------------------
# Template families
# ---------------------------------------------------------------------------

import re as _re
from pathlib import Path as _Path

#: Size tokens as they appear in template file names, longest first so
#: "ORCAD_A" is not read as "A".
_TOKEN = _re.compile(
    r"(?<![A-Za-z0-9])(ORCAD[ _-]?[A-E]|ANSI[ _-]?[A-E]|A[0-4]|LETTER|LEGAL|"
    r"TABLOID|LEDGER|[A-E])(?![A-Za-z0-9])", _re.I)


def template_size_token(path: str) -> Optional[str]:
    """The sheet size a template's file name declares (``Co_A3.SchDot`` ->
    A3), or None when the name carries no size."""
    stem = _Path(path).stem
    found = _TOKEN.findall(stem)
    return normalise(found[-1].replace("_", " ").replace("-", " ")) if found else None


def template_for_size(template_path: str, size: str,
                      search: Optional[list[str]] = None) -> Optional[str]:
    """The template of the same family for ``size``: the file whose name is
    ``template_path``'s with its size token swapped, looked for beside it
    and then in ``search`` folders. Lists file names only; reads nothing."""
    if not template_path:
        return None
    src = _Path(template_path)
    token_match = list(_TOKEN.finditer(src.stem))
    if not token_match:
        return None
    m = token_match[-1]
    want = normalise(size)
    folders = [src.parent, *[_Path(f) for f in (search or [])]]
    for folder in folders:
        try:
            entries = list(folder.glob(f"*{src.suffix or '.SchDot'}"))
        except OSError:
            continue
        for entry in entries:
            em = list(_TOKEN.finditer(entry.stem))
            if not em:
                continue
            e = em[-1]
            same_family = (entry.stem[:e.start()].lower() == src.stem[:m.start()].lower()
                           and entry.stem[e.end():].lower() == src.stem[m.end():].lower())
            if same_family and template_size_token(str(entry)) == want:
                return str(entry)
    return None


# ---------------------------------------------------------------------------
# Fixing the engine's own canvas
# ---------------------------------------------------------------------------

def translate_sheet(canvas, sheet: str, dx: int, dy: int) -> None:
    """Move everything on ``sheet`` by (dx, dy) together, wiring included,
    so connectivity is untouched."""
    from dataclasses import replace

    if not dx and not dy:
        return

    def mv(obj, *pairs):
        upd = {}
        for fx, fy in pairs:
            upd[fx] = getattr(obj, fx) + dx
            upd[fy] = getattr(obj, fy) + dy
        return replace(obj, **upd)

    insts = []
    for i in canvas.instances:
        if i.sheet != sheet:
            insts.append(i)
            continue
        upd = {"x": i.x + dx, "y": i.y + dy}
        for f in ("designator_pos", "value_pos"):
            pos = getattr(i, f)
            if pos:
                upd[f] = (pos[0] + dx, pos[1] + dy)
        insts.append(replace(i, **upd))
    canvas.instances[:] = insts
    for name, pairs in (("wires", (("x1", "y1"), ("x2", "y2"))),
                        ("buses", (("x1", "y1"), ("x2", "y2"))),
                        ("bus_entries", (("x1", "y1"), ("x2", "y2"))),
                        ("labels", (("x", "y"),)),
                        ("power_ports", (("x", "y"),)),
                        ("junctions", (("x", "y"),))):
        items = getattr(canvas, name)
        items[:] = [mv(o, *pairs) if o.sheet == sheet else o for o in items]


def fit_canvas(canvas, frames: Optional[dict[str, tuple[float, float]]] = None,
               grid: int = 100) -> list[dict]:
    """Bring every sheet of the engine's canvas inside its frame.

    ``shift`` moves the whole drawing (snapped to ``grid``); ``enlarge``
    sets the sheet to the first larger size of its family that holds the
    drawing, then shifts it onto that frame. A full sheet is never spilled
    onto another. Returns what was done per sheet."""
    actions = []
    for s in canvas.sheets:
        frame = (frames or {}).get(s.name)
        rep = fit_report(canvas_boxes(canvas, s.name), s.size, frame,
                         grid=grid)
        action = {"sheet": s.name, "verdict": rep["verdict"],
                  "size": rep["size"]}
        if rep["verdict"] == "enlarge":
            s.size = rep["to_size"]
            s.width_mils, s.height_mils = ALTIUM_FRAMES[rep["to_size"]]
            action["enlarged_to"] = rep["to_size"]
            rep = fit_report(canvas_boxes(canvas, s.name), s.size, grid=grid)
        if rep["verdict"] == "shift":
            # fit_report already moves in whole grid steps, away from zero:
            # rounding to the NEAREST step left a drawing up to half a step
            # past the margin where it was.
            dx, dy = int(rep["dx"]), int(rep["dy"])
            translate_sheet(canvas, s.name, dx, dy)
            action.update(shifted=[dx, dy])
        left = outside(canvas_boxes(canvas, s.name),
                       usable_area(frame or altium_frame(s.size)
                                   or ALTIUM_FRAMES["A4"]))
        action["still_outside"] = len(left)
        actions.append(action)
    return actions
