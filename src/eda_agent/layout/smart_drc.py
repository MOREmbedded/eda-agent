# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Design checks built from temporary rules, run offline (no document edits).

The board's own DRC answers "does anything break the rules the designer
wrote". This asks the questions those rules usually leave out, with rules
made for the occasion and never written into the board:

* **Loop spacing.** Sensitive nets (regulator and op-amp feedback, crystal
  nets) kept at least ``spacing_mils`` from noisy ones (switch nodes,
  bootstrap and gate-drive nets), both found by design/loops.
* **Tightest pairs.** A probe: the actual edge-to-edge gap between every
  sensitive and noisy net pair within ``probe_mils``, smallest first. It
  ranks how close a board runs to trouble, not only where it fails.
* **Hot-loop width.** Tracks on hot-loop nets narrower than the IPC-2221
  width for the current given in ``currents`` (design/trace_sizing).

Distances use the DRC's own geometry (exact edge to edge, arcs on 0.05 mil
chords), so a gap here agrees with layout/drc.py.

``spacing_mils`` is a starting value, not a standard: take it from the
regulator's or oscillator's datasheet layout section where one is given.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from eda_agent.design.loops import find_loops, graph_from_board
from eda_agent.layout import geom
from eda_agent.layout.drc import _Distances, conductive_items
from eda_agent.layout.model import LayoutBoard

SENSITIVE_KINDS = ("feedback", "crystal")
NOISY_KINDS = ("hot_loop", "bootstrap", "gate_drive")


def loop_net_sets(board: LayoutBoard,
                  pin_names: Optional[dict[str, dict[str, str]]] = None
                  ) -> tuple[set[str], set[str], set[str], list]:
    """(sensitive nets, noisy nets, hot-loop nets, loops) from the loops."""
    graph = graph_from_board(board, pin_names)
    loops = find_loops(graph)
    sensitive: set[str] = set()
    noisy: set[str] = set()
    hot: set[str] = set()
    for l in loops:
        nets = {n for n in l.nets if n and not graph.is_ground(n)
                and not graph.is_rail(n)}
        if l.kind in SENSITIVE_KINDS:
            sensitive |= nets
        if l.kind in NOISY_KINDS:
            noisy |= nets
        if l.kind == "hot_loop":
            hot |= {n for n in l.nets if n}
    return sensitive - noisy, noisy, hot, loops


def net_pair_gaps(board: LayoutBoard, nets_a: set[str], nets_b: set[str],
                  probe_mils: float = 100.0) -> list[dict]:
    """Smallest edge-to-edge gap per (net_a, net_b, layer) within the probe."""
    items = [it for it in conductive_items(board)
             if it.net in nets_a or it.net in nets_b]
    dist = _Distances(items)
    best: dict[tuple, dict] = {}
    for i, a in enumerate(items):
        if a.net not in nets_a:
            continue
        for j, b in enumerate(items):
            if b.net not in nets_b or b.net == a.net or a.layer != b.layer:
                continue
            if not geom.bboxes_near(a.shape, b.shape, probe_mils):
                continue
            gap = dist.gap(i, a.shape, j, b.shape, probe_mils)
            if gap > probe_mils:
                continue
            key = (a.net, b.net, a.layer)
            if key not in best or gap < best[key]["gap_mils"]:
                bx = a.shape.bbox
                best[key] = {"net_a": a.net, "net_b": b.net, "layer": a.layer,
                             "gap_mils": round(max(gap, 0.0), 2),
                             "near": f"{a.label} / {b.label}",
                             "at": (round((bx[0] + bx[2]) / 2, 1),
                                    round((bx[1] + bx[3]) / 2, 1))}
    return sorted(best.values(), key=lambda r: r["gap_mils"])


def hot_loop_widths(board: LayoutBoard, hot_nets: set[str],
                    currents: Optional[dict[str, float]] = None) -> list[dict]:
    """Tracks on hot-loop nets narrower than the IPC-2221 width for their
    current. Only nets with a current in ``currents`` are judged."""
    from eda_agent.design.trace_sizing import trace_width_for_current

    out = []
    for net, amps in (currents or {}).items():
        if net not in hot_nets or amps <= 0:
            continue
        need = trace_width_for_current(amps).min_width_mils
        narrow = [t for t in board.tracks if t.net == net and t.width < need]
        if narrow:
            out.append({"net": net, "current_a": amps,
                        "min_width_mils": round(need, 1),
                        "narrowest_mils": round(min(t.width for t in narrow), 1),
                        "tracks_below": len(narrow),
                        "source": "IPC-2221, 10 C rise, external, 1 oz"})
    return out


def smart_drc(board: LayoutBoard, *, spacing_mils: float = 40.0,
              probe_mils: float = 100.0,
              currents: Optional[dict[str, float]] = None,
              pin_names: Optional[dict[str, dict[str, str]]] = None,
              limit: int = 50) -> dict:
    """Run the temporary-rule checks above. Changes nothing."""
    sensitive, noisy, hot, loops = loop_net_sets(board, pin_names)
    pairs = net_pair_gaps(board, sensitive, noisy, probe_mils) \
        if sensitive and noisy else []
    spacing = [dict(p, required_mils=spacing_mils) for p in pairs
               if p["gap_mils"] < spacing_mils]
    widths = hot_loop_widths(board, hot, currents)
    return {
        "pass": not spacing and not widths,
        "counts": {"sensitive_nets": len(sensitive), "noisy_nets": len(noisy),
                   "spacing_violations": len(spacing),
                   "width_violations": len(widths),
                   "pairs_within_probe": len(pairs)},
        "spacing": spacing[:limit],
        "tightest_pairs": pairs[:limit],
        "hot_loop_width": widths,
        "rules": {"spacing_mils": spacing_mils, "probe_mils": probe_mils,
                  "sensitive": sorted(sensitive), "noisy": sorted(noisy)},
        "note": ("Temporary rules, evaluated offline; nothing was written to "
                 "the board. Sensitive and noisy nets come from the circuit "
                 "loops; on a board without pin names that means structure "
                 "only (crystals, switch nodes)."),
    }


__all__ = ["smart_drc", "net_pair_gaps", "loop_net_sets", "hot_loop_widths"]
