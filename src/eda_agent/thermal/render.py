# SPDX-License-Identifier: Apache-2.0
"""Heat maps of a solved board as PNG, drawn with Pillow alone.

One image per layer: the temperature field in a dark-to-bright palette,
the board outline and cutouts, the outlines of the parts on that side, a
label on each powered part and on the peak, and a scale in degrees C.
Board +y is up, as in the PCB editor (the bottom layer is shown from the
top, not mirrored, so it lines up with the top layer's image).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from ..layout.model import BOTTOM, LayoutBoard
from .solver import Result

#: Palette anchors, cold to hot (position 0..1, RGB): navy, purple,
#: magenta, red, orange, pale yellow. Brightness rises all the way, so
#: the map still reads in greyscale.
_ANCHORS = (
    (0.00, (12, 12, 64)),
    (0.18, (62, 22, 132)),
    (0.38, (150, 30, 130)),
    (0.58, (222, 62, 62)),
    (0.78, (248, 150, 32)),
    (0.92, (252, 222, 90)),
    (1.00, (255, 252, 214)),
)
_BG = (250, 250, 250)
_INK = (30, 30, 30)
_OUTLINE = (20, 20, 20)
_PART = (235, 235, 235)
_PART_ON_COPPER = (40, 110, 200)
_COPPER_NONE = np.array([255.0, 255.0, 255.0])
_COPPER_FULL = np.array([150.0, 75.0, 20.0])


def palette(n: int = 256) -> np.ndarray:
    """(n, 3) uint8 colours, cold to hot."""
    pos = np.array([a[0] for a in _ANCHORS])
    rgb = np.array([a[1] for a in _ANCHORS], dtype=float)
    x = np.linspace(0.0, 1.0, n)
    return np.stack([np.interp(x, pos, rgb[:, k]) for k in range(3)], axis=1).round().astype(np.uint8)


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has one fixed bitmap font
        return ImageFont.load_default()


def _nice_ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    span = max(hi - lo, 1e-9)
    raw = span / max(n - 1, 1)
    mag = 10 ** np.floor(np.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    first = np.ceil(lo / step) * step
    ticks = [float(first + k * step) for k in range(int((hi - first) / step + 1e-9) + 1)]
    return ticks or [lo, hi]


def render_layer(result: Result, board: LayoutBoard, layer: str, path: str | Path,
                 t_range: Optional[tuple[float, float]] = None, width_px: int = 1400,
                 title: str = "", labels: Optional[list[str]] = None) -> Path:
    """Write one layer's heat map to ``path`` and return it."""
    from PIL import Image, ImageDraw

    raster = result.raster
    t = result.temps[layer]
    ny, nx = t.shape
    lo, hi = t_range if t_range else (float(np.nanmin(t)), float(np.nanmax(t)))
    if hi - lo < 1e-6:
        hi = lo + 1.0
    pal = palette()
    inside = np.isfinite(t)
    norm = np.clip((np.where(inside, t, lo) - lo) / (hi - lo), 0.0, 1.0)
    rgb = pal[(norm * 255).round().astype(int)]
    rgb[~inside] = _BG

    bar_w, margin, head = 150, 24, 64
    scale = max(1.0, (width_px - bar_w - 2 * margin) / nx)
    map_w, map_h = int(round(nx * scale)), int(round(ny * scale))
    field = Image.fromarray(rgb[::-1], "RGB")          # +y up
    mask = Image.fromarray((inside[::-1] * 255).astype(np.uint8), "L")
    smooth = Image.BILINEAR if scale >= 2 else Image.NEAREST
    field = field.resize((map_w, map_h), smooth)
    mask = mask.resize((map_w, map_h), Image.NEAREST)
    img = Image.new("RGB", (map_w + bar_w + 2 * margin, map_h + head + margin), _BG)
    img.paste(field, (margin, head), mask)
    d = ImageDraw.Draw(img)
    f_small, f_big = _font(15), _font(22)

    def px(x: float, y: float) -> tuple[float, float]:
        return (margin + (x - raster.x0) / raster.cell_mils * scale,
                head + map_h - (y - raster.y0) / raster.cell_mils * scale)

    def ring(points, colour, width=1):
        pts = [px(x, y) for x, y in points]
        if len(pts) >= 2:
            d.line(pts + [pts[0]], fill=colour, width=width)

    side = BOTTOM if (layer == raster.layers[-1] and len(raster.layers) > 1) else "top"
    outer = layer in (raster.layers[0], raster.layers[-1])
    if outer:
        for c in board.components:
            if c.side == side and len(c.courtyard) >= 3:
                ring(c.courtyard, _PART)
    if len(board.outline) >= 3:
        ring(board.outline, _OUTLINE, 2)
    for cut in board.cutouts:
        if len(cut) >= 3:
            ring(cut, _OUTLINE, 2)

    def tag(x: float, y: float, text: str):
        cx, cy = px(x, y)
        box = d.textbbox((0, 0), text, font=f_small)
        w, h = box[2] - box[0], box[3] - box[1]
        tx = min(max(cx + 6, margin), margin + map_w - w - 4)
        ty = min(max(cy - h - 8, head), head + map_h - h - 4)
        d.rectangle([tx - 3, ty - 2, tx + w + 3, ty + h + 4], fill=(255, 255, 255),
                    outline=_INK)
        d.text((tx, ty), text, fill=_INK, font=f_small)

    if outer:
        want = set(labels) if labels is not None else {p["ref"] for p in result.parts
                                                       if p["power_w"] > 0}
        by_ref = {p["ref"]: p for p in result.parts}
        for c in board.components:
            if c.side != side or c.ref not in want or c.ref not in by_ref:
                continue
            p = by_ref[c.ref]
            temp = p.get("t_junction_c") or p["t_pads_c"]
            tag(c.x, c.y, f"{c.ref} {temp:.1f}")
    iy, ix = np.unravel_index(np.nanargmax(t), t.shape)
    mx, my = raster.centre(int(ix), int(iy))
    cx, cy = px(mx, my)
    d.line([cx - 9, cy, cx + 9, cy], fill=(255, 255, 255), width=3)
    d.line([cx, cy - 9, cx, cy + 9], fill=(255, 255, 255), width=3)
    d.line([cx - 9, cy, cx + 9, cy], fill=_INK, width=1)
    d.line([cx, cy - 9, cx, cy + 9], fill=_INK, width=1)

    head_text = title or f"{board.name or 'board'}  {layer}"
    d.text((margin, 10), head_text, fill=_INK, font=f_big)
    d.text((margin, 38), f"max {np.nanmax(t):.1f} C   mean {np.nanmean(t):.1f} C   "
                         f"ambient {result.ambient_c:g} C   cell {raster.cell_mils:g} mil",
           fill=_INK, font=f_small)

    bx0 = margin + map_w + 28
    bar_h = max(map_h - 20, 60)
    by0 = head + 10
    column = pal[::-1][np.linspace(0, 255, bar_h).round().astype(int)][:, None, :]
    img.paste(Image.fromarray(np.repeat(column, 26, axis=1), "RGB"), (bx0, by0))
    d.rectangle([bx0, by0, bx0 + 26, by0 + bar_h], outline=_INK)
    for v in _nice_ticks(lo, hi):
        y = by0 + bar_h - (v - lo) / (hi - lo) * bar_h
        d.line([bx0 + 26, y, bx0 + 32, y], fill=_INK)
        d.text((bx0 + 36, y - 8), f"{v:g}", fill=_INK, font=f_small)
    d.text((bx0, by0 - 22), "deg C", fill=_INK, font=f_small)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def render_all(result: Result, board: LayoutBoard, folder: str | Path,
               layers: Optional[list[str]] = None, common_scale: bool = True) -> dict[str, str]:
    """Heat maps of ``layers`` (default: every copper layer) into
    ``folder``; returns {layer: path}. With ``common_scale`` every image
    shares one temperature range, so they compare at a glance."""
    names = layers or list(result.raster.layers)
    rng = None
    if common_scale:
        rng = (min(float(np.nanmin(result.temps[n])) for n in names),
               max(float(np.nanmax(result.temps[n])) for n in names))
    out = {}
    for n in names:
        safe = "".join(ch if ch.isalnum() else "_" for ch in n)
        out[n] = str(render_layer(result, board, n, Path(folder) / f"thermal_{safe}.png", rng))
    return out


def render_copper(result: Result, board: LayoutBoard, layer: str, path: str | Path,
                  width_px: int = 1400) -> Path:
    """Write the copper the solver saw on ``layer`` to ``path``: each
    cell shaded by the share of it that is copper (white none, dark
    full), with the board outline and, on the outer layers, the parts.
    It is there to be compared by eye with the layer in the PCB editor."""
    from PIL import Image, ImageDraw

    raster = result.raster
    fill = np.clip(raster.fill[layer], 0.0, 1.0)
    inside = raster.mask > 0
    ny, nx = fill.shape
    shade = _COPPER_NONE + (_COPPER_FULL - _COPPER_NONE) * fill[..., None]
    rgb = shade.round().astype(np.uint8)
    rgb[~inside] = _BG

    margin, head = 24, 64
    scale = max(1.0, (width_px - 2 * margin) / nx)
    map_w, map_h = int(round(nx * scale)), int(round(ny * scale))
    field = Image.fromarray(rgb[::-1], "RGB").resize((map_w, map_h), Image.NEAREST)
    img = Image.new("RGB", (map_w + 2 * margin, map_h + head + margin), _BG)
    img.paste(field, (margin, head))
    d = ImageDraw.Draw(img)

    def ring(points, colour, width=1):
        pts = [(margin + (x - raster.x0) / raster.cell_mils * scale,
                head + map_h - (y - raster.y0) / raster.cell_mils * scale)
               for x, y in points]
        if len(pts) >= 2:
            d.line(pts + [pts[0]], fill=colour, width=width)

    side = BOTTOM if (layer == raster.layers[-1] and len(raster.layers) > 1) else "top"
    if layer in (raster.layers[0], raster.layers[-1]):
        for c in board.components:
            if c.side == side and len(c.courtyard) >= 3:
                ring(c.courtyard, _PART_ON_COPPER)
    if len(board.outline) >= 3:
        ring(board.outline, _OUTLINE, 2)
    for cut in board.cutouts:
        if len(cut) >= 3:
            ring(cut, _OUTLINE, 2)
    share = float(fill[inside].sum() / max(inside.sum(), 1))
    d.text((margin, 10), f"{board.name or 'board'}  {layer}  copper as solved",
           fill=_INK, font=_font(22))
    d.text((margin, 38), f"copper {100 * share:.1f} % of the board   "
                         f"cell {raster.cell_mils:g} mil   sampled at "
                         f"{raster.cell_mils / raster.supersample:.2g} mil",
           fill=_INK, font=_font(15))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def render_copper_all(result: Result, board: LayoutBoard, folder: str | Path,
                      layers: Optional[list[str]] = None) -> dict[str, str]:
    """``render_copper`` of ``layers`` (default: every copper layer) into
    ``folder``; returns {layer: path}."""
    out = {}
    for n in layers or list(result.raster.layers):
        safe = "".join(ch if ch.isalnum() else "_" for ch in n)
        out[n] = str(render_copper(result, board, n, Path(folder) / f"copper_{safe}.png"))
    return out


def save_npz(result: Result, path: str | Path) -> Path:
    """The temperature fields and their grid, for other tools to read:
    ``x0``, ``y0``, ``cell_mils``, ``layers`` and one ``T_<layer>`` array
    per copper layer, shaped (ny, nx), NaN outside the board."""
    raster = result.raster
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {f"T_{n}": result.temps[n].astype(np.float32) for n in raster.layers}
    np.savez_compressed(path, x0=raster.x0, y0=raster.y0, cell_mils=raster.cell_mils,
                        layers=np.array(raster.layers), ambient_c=result.ambient_c, **arrays)
    return path
