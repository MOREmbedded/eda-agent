# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Render a constructed PCB placement to a PNG image.

A developer/validation illustration: it turns a placement into a picture
so a human can see what the placer produced, without going through
Altium. Matplotlib (Agg backend) is imported lazily so importing this
module is cheap and headless-safe.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence


def placement_png(
    comps: Sequence,
    positions: Mapping[str, Sequence[float]],
    region,
    nets: Sequence,
    path: str,
    *,
    title: str = "",
    rotations: Optional[Mapping[str, float]] = None,
    sides: Optional[Mapping[str, int]] = None,
    report=None,
) -> str:
    """Draw a PCB placement (board outline, courtyards, net stars).

    ``comps`` are PlaceComp; ``positions`` maps ref -> (x, y) centroid;
    ``region`` is a BoardRegion; ``nets`` are PlaceNet. Returns the path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    rotations = rotations or {}
    sides = sides or {}
    by_ref = {c.ref: c for c in comps}

    fig, ax = plt.subplots(figsize=(8, 8))

    # Board outline.
    bx = min(region.x1, region.x2)
    by = min(region.y1, region.y2)
    ax.add_patch(Rectangle((bx, by), region.width, region.height,
                           fill=False, edgecolor="#1b5e20", linewidth=1.6))

    # Net stars: connect each net's member centroids to the net centroid.
    for net in nets:
        pts = [positions[r] for r in dict.fromkeys(net.refs) if r in positions]
        if len(pts) < 2:
            continue
        ncx = sum(p[0] for p in pts) / len(pts)
        ncy = sum(p[1] for p in pts) / len(pts)
        for (x, y) in pts:
            ax.plot([ncx, x], [ncy, y], color="#90a4ae", linewidth=0.5,
                    zorder=1)

    # Component courtyards (effective w/h at the chosen rotation).
    for ref in sorted(positions):
        c = by_ref.get(ref)
        if c is None:
            continue
        x, y = positions[ref][0], positions[ref][1]
        rot = float(rotations.get(ref, getattr(c, "rotation", 0.0)))
        w, h = (c.h, c.w) if int(round(rot)) % 180 == 90 else (c.w, c.h)
        side = int(sides.get(ref, 1))
        face = "#e3f2fd" if side >= 0 else "#fff3e0"
        ax.add_patch(Rectangle((x - w / 2.0, y - h / 2.0), w, h, fill=True,
                               facecolor=face, edgecolor="#0d47a1",
                               linewidth=1.1, zorder=2))
        ax.text(x, y, ref, ha="center", va="center", fontsize=8,
                color="#0d47a1", weight="bold", zorder=3)

    sub = ""
    if report is not None:
        sub = (f"HPWL={report.hpwl:.0f}  via={report.via:.2f}  "
               f"cong={report.cong:.1f}  legal={report.legal}  "
               f"util={report.utilization:.2f}")
    ax.set_title((title + "\n" if title else "") + sub, fontsize=9)
    ax.set_aspect("equal")
    ax.margins(0.08)
    ax.grid(True, linewidth=0.3, color="#cfd8dc")
    ax.set_xlabel("mils")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
