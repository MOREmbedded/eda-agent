# SPDX-License-Identifier: Apache-2.0
"""Route the same synthetic boards with both routing engines and print
the comparison table of docs/ROUTER_ENGINES.md.

Four kinds of board, each built from a seed so that a run can be repeated:

- ``mixed2`` and ``mixed4``: a QFN-32, two SOIC-8s, an 8-pin header and
  18 chip parts on 2 or 4 layers, wired as GND, +3V3 and 31 two-pad
  signal nets between nearby pads.
- ``mixed4p``: ``mixed4`` with GND and +3V3 pours on the two inner layers.
- ``bga8``: an 8 x 8 ball array at 0.8 mm pitch on 4 layers, its balls
  wired out to 40 chip parts round the edge, with a GND pour on MidLayer1.

Every board has the same rules: a 6 mil gap, 6 to 10 mil tracks, and 18 to
24 mil vias with 8 to 12 mil holes. Each board goes through bench_run's
routing pass, which scores the router's own copper (no gloss) the same way
for both engines. The board is checked before routing too, so the table
counts only the violations the router added.

Usage:
    python scripts/bench_router_engines.py                      # all kinds, seeds 1-3
    python scripts/bench_router_engines.py --kinds mixed2 --seeds 1
    python scripts/bench_router_engines.py --json records.json  # keep the records
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eda_agent.layout.model import (  # noqa: E402
    Component, Layer, LayoutBoard, Pad, PadCopper, Region, Rule)

ENGINES = ("default", "euan")

RULES = [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
              "Clearance Constraint (Gap=6mil) (All),(All)"),
         Rule("Width", "1", "All", "All", 1, True, {},
              "Width Constraint (Min=6mil) (Max=10mil) (Preferred=6mil) (All)"),
         Rule("Vias", "2", "All", "All", 1, True, {},
              "Routing Via (MinHoleWidth=8mil) (MaxHoleWidth=12mil) (PreferredHoleWidth=10mil) "
              "(MinWidth=18mil) (MaxWidth=24mil) (PreferedWidth=20mil) (All)")]


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

def _layers(n: int) -> list[Layer]:
    names = ["TopLayer"] + [f"MidLayer{i}" for i in range(1, n - 1)] + ["BottomLayer"]
    return [Layer(name, "signal", i) for i, name in enumerate(names)]


def _comp(ref: str, x: float, y: float, w: float, h: float) -> Component:
    return Component(ref, x=x, y=y, courtyard=[(x - w / 2, y - h / 2), (x + w / 2, y - h / 2),
                                               (x + w / 2, y + h / 2), (x - w / 2, y + h / 2)])


def qfn(ref: str, x: float, y: float, n_side: int = 8, pitch: float = 19.685):
    """A QFN with ``n_side`` pads a side at 0.5 mm pitch, numbered round."""
    pads, k = [], 1
    span = pitch * (n_side - 1)
    edge = span / 2 + 35
    for side in range(4):
        for i in range(n_side):
            t = -span / 2 + i * pitch
            if side == 0:
                px, py, w, h = x + t, y - edge, 11, 30
            elif side == 1:
                px, py, w, h = x + edge, y + t, 30, 11
            elif side == 2:
                px, py, w, h = x - t, y + edge, 11, 30
            else:
                px, py, w, h = x - edge, y - t, 30, 11
            pads.append(Pad(ref, str(k), px, py, copper=[PadCopper("TopLayer", "rect", w, h)]))
            k += 1
    return _comp(ref, x, y, 2 * edge + 40, 2 * edge + 40), pads


def soic(ref: str, x: float, y: float, n: int = 8):
    pads = []
    half = n // 2
    for i in range(half):
        dy = (i - (half - 1) / 2) * 50
        pads.append(Pad(ref, str(i + 1), x - 105, y + dy,
                        copper=[PadCopper("TopLayer", "rect", 60, 24)]))
        pads.append(Pad(ref, str(n - i), x + 105, y + dy,
                        copper=[PadCopper("TopLayer", "rect", 60, 24)]))
    return _comp(ref, x, y, 290, 50 * half + 40), pads


def chip(ref: str, x: float, y: float, vertical: bool = False):
    """A 0603-sized two-pad part."""
    d = 31.5
    if vertical:
        pads = [Pad(ref, "1", x, y - d, copper=[PadCopper("TopLayer", "rect", 37, 35)]),
                Pad(ref, "2", x, y + d, copper=[PadCopper("TopLayer", "rect", 37, 35)])]
        return _comp(ref, x, y, 60, 110), pads
    pads = [Pad(ref, "1", x - d, y, copper=[PadCopper("TopLayer", "rect", 35, 37)]),
            Pad(ref, "2", x + d, y, copper=[PadCopper("TopLayer", "rect", 35, 37)])]
    return _comp(ref, x, y, 110, 60), pads


def header(ref: str, x: float, y: float, n: int = 6, layers=("TopLayer", "BottomLayer")):
    """A through-hole header at 100 mil pitch."""
    pads = [Pad(ref, str(i + 1), x, y + i * 100, hole=40.0,
                copper=[PadCopper(layer, "round", 65, 65) for layer in layers])
            for i in range(n)]
    return _comp(ref, x, y + (n - 1) * 50, 90, n * 100 + 10), pads


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

def mixed(seed: int, nlayers: int = 2, planes: bool = False) -> LayoutBoard:
    rnd = random.Random(seed)
    W, H = 2200, 1700
    b = LayoutBoard(name=f"mixed{nlayers}{'p' if planes else ''}-{seed}",
                    outline=[(0, 0), (W, 0), (W, H), (0, H)], layers=_layers(nlayers),
                    rules=list(RULES))
    layer_names = [layer.name for layer in b.layers]
    parts = [qfn("U1", 1000, 800), soic("U2", 450, 350), soic("U3", 1650, 1300),
             header("J1", 120, 450, 8, layers=tuple(layer_names))]
    spots = [(x, y) for x in range(350, 2000, 170) for y in range(250, 1550, 150)
             if not (700 < x < 1300 and 500 < y < 1100) and not (300 < x < 650 and 250 < y < 480)
             and not (1450 < x < 1850 and 1150 < y < 1450) and x > 250]
    rnd.shuffle(spots)
    for i in range(18):
        x, y = spots[i]
        parts.append(chip(f"C{i + 1}", x, y, vertical=rnd.random() < 0.5))
    for comp, pads in parts:
        b.components.append(comp)
        b.pads += pads
    # GND on many pads, a 3V3 rail, then signals between nearby pads.
    free = list(b.pads)
    rnd.shuffle(free)
    gnd = [p for p in free if p.comp.startswith("C") and p.name == "2"][:12] + \
          [p for p in b.pads if p.comp == "U1" and p.name in ("8", "16", "24", "32")] + \
          [p for p in b.pads if p.comp in ("U2", "U3") and p.name == "4"] + \
          [p for p in b.pads if p.comp == "J1" and p.name == "1"]
    for p in gnd:
        p.net = "GND"
    vcc = [p for p in b.pads if p.comp.startswith("C") and p.name == "1" and not p.net][:6] + \
          [p for p in b.pads if p.comp == "U1" and p.name in ("1", "17")] + \
          [p for p in b.pads if p.comp in ("U2", "U3") and p.name == "8"] + \
          [p for p in b.pads if p.comp == "J1" and p.name == "2"]
    for p in vcc:
        p.net = "+3V3"
    left = [p for p in b.pads if not p.net]
    n = 0
    while len(left) >= 2 and n < 34:
        a = left.pop(rnd.randrange(len(left)))
        # One of the nearest few pads of another part: local nets, as on a real board.
        left.sort(key=lambda q: math.hypot(q.x - a.x, q.y - a.y) + (1e9 if q.comp == a.comp else 0))
        c = left.pop(min(len(left) - 1, rnd.randrange(4)))
        a.net = c.net = f"S{n}"
        n += 1
    if planes:
        whole = [(10, 10), (W - 10, 10), (W - 10, H - 10), (10, H - 10)]
        b.regions.append(Region("MidLayer1", whole, [], "GND", "pour_boundary"))
        b.regions.append(Region("MidLayer2", list(whole), [], "+3V3", "pour_boundary"))
    return b


def bga(seed: int, n: int = 8, pitch: float = 31.5, ball: float = 16.0) -> LayoutBoard:
    rnd = random.Random(seed)
    W, H = 1400, 1400
    b = LayoutBoard(name=f"bga{n}-{seed}", outline=[(0, 0), (W, 0), (W, H), (0, H)],
                    layers=_layers(4), rules=list(RULES))
    x0 = y0 = 700 - pitch * (n - 1) / 2
    balls = [Pad("U1", f"{chr(65 + j)}{i + 1}", x0 + pitch * i, y0 + pitch * j,
                 copper=[PadCopper("TopLayer", "round", ball, ball)])
             for i in range(n) for j in range(n)]
    b.components.append(_comp("U1", 700, 700, pitch * n + 60, pitch * n + 60))
    b.pads += balls
    # Their partners: chip parts round the edge.
    ring = []
    k = 0
    for side in range(4):
        for t in range(10):
            s = 200 + t * 110
            x, y = [(s, 120), (1280, s), (1400 - s, 1280), (120, 1400 - s)][side]
            comp, pads = chip(f"R{k + 1}", x, y, vertical=side in (1, 3))
            b.components.append(comp)
            b.pads += pads
            ring += pads
            k += 1
    rnd.shuffle(balls)
    for p in balls[: n * n // 6]:
        p.net = "GND"
    for i, p in enumerate(balls[n * n // 6:]):
        if i < len(ring):
            p.net = ring[i].net = f"B{i}"
    for p in ring:
        if not p.net:
            p.net = "GND"
    b.regions.append(Region("MidLayer1", [(10, 10), (W - 10, 10), (W - 10, H - 10), (10, H - 10)],
                            [], "GND", "pour_boundary"))
    return b


KINDS = {
    "mixed2": ("Mixed, 2 layers", lambda seed: mixed(seed, 2)),
    "mixed4": ("Mixed, 4 layers", lambda seed: mixed(seed, 4)),
    "mixed4p": ("Mixed, 4 layers with planes", lambda seed: mixed(seed, 4, planes=True)),
    "bga8": ("8 x 8 BGA, 4 layers", bga),
}


# ---------------------------------------------------------------------------
# Routing and the table
# ---------------------------------------------------------------------------

def route(task: tuple[str, int, str]) -> dict:
    """One board, routed by one engine: the record the table is made from."""
    from eda_agent.layout.bench_run import BenchOptions, run_board

    kind, seed, engine = task
    rec = run_board(KINDS[kind][1](seed), BenchOptions(mode="route", router=engine))
    before, after = rec["human"], rec["engine"]
    return {"kind": kind, "seed": seed, "engine": engine, "pads": rec["pads"],
            "connections": after["connections_needed"],
            "missing": after["missing_connections"],
            "violations_before": before["violations"], "violations": after["violations"],
            "vias": after["vias"], "length": round(after["length"], 1),
            "seconds": after["seconds"]}


def table(records: list[dict]) -> list[str]:
    """The markdown table: one row per kind and engine, totals over the seeds."""
    seeds = sorted({r["seed"] for r in records})
    lines = [f"| Boards ({len(seeds)} of each) | Engine | Completion | Connections left "
             "| Violations added | Vias | Track length (in) | Time (s) |",
             "|---|---|---|---|---|---|---|---|"]
    for kind, (title, _) in KINDS.items():
        first = True
        for engine in ENGINES:
            rows = [r for r in records if r["kind"] == kind and r["engine"] == engine]
            if not rows:
                continue
            needed = sum(r["connections"] for r in rows)
            missing = sum(r["missing"] for r in rows)
            completion = 1.0 - missing / needed if needed else 1.0
            added = sum(r["violations"] - r["violations_before"] for r in rows)
            length_in = sum(r["length"] for r in rows) / 1000.0
            seconds = sum(r["seconds"] for r in rows)
            lines.append(f"| {title if first else ''} | {engine} | {completion:.1%} | {missing} "
                         f"| {added} | {sum(r['vias'] for r in rows)} | {length_in:.1f} "
                         f"| {seconds:.0f} |")
            first = False
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kinds", default=",".join(KINDS),
                    help=f"comma-separated, from {', '.join(KINDS)} (default: all)")
    ap.add_argument("--seeds", default="1,2,3", help="comma-separated seeds (default: 1,2,3)")
    ap.add_argument("--jobs", type=int, default=2,
                    help="boards routed at once (default: 2); 1 gives the steadiest times")
    ap.add_argument("--json", type=Path, help="also write the records to this file")
    args = ap.parse_args(argv)
    kinds = [k for k in args.kinds.split(",") if k]
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        ap.error(f"unknown kind {unknown[0]!r}; the kinds are {', '.join(KINDS)}")
    seeds = [int(s) for s in args.seeds.split(",") if s]
    tasks = [(kind, seed, engine) for kind in kinds for seed in seeds for engine in ENGINES]
    records = []
    with mp.get_context("spawn").Pool(max(1, args.jobs)) as pool:
        for rec in pool.imap_unordered(route, tasks):
            records.append(rec)
            print(json.dumps(rec), file=sys.stderr, flush=True)
    records.sort(key=lambda r: (list(KINDS).index(r["kind"]), r["seed"], ENGINES.index(r["engine"])))
    if args.json:
        args.json.write_text(json.dumps(records, indent=1) + "\n", encoding="utf-8")
    print("\n".join(table(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
