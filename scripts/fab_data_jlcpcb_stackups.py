# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Regenerate src/eda_agent/design/fab_data/jlcpcb_stackups.json from
JLCPCB's impedance page.

WHY A SCRIPT. The page lists every stack-up JLCPCB builds for impedance
work (34 at 1.6 mm when this was written), ply by ply. Copied by hand,
one wrong digit in a prepreg height would look exactly like JLCPCB's own
figure. Parsed, every number is the page's, and the file can be redone
the day the page changes: the stack-up data says when it was read and
the tools report it stale after a year.

The page shows the selection it opens with (1.6 mm; its other board
thicknesses are filled in by the browser and are not in the HTML), so
only those stack-ups are written. Dielectric constants come from the
page's own tables, by glass style for prepreg and one value for core;
the page states no frequency for them, and the file says so.

Usage:
    python scripts/fab_data_jlcpcb_stackups.py              # fetch the page
    python scripts/fab_data_jlcpcb_stackups.py saved.html   # a saved copy
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://jlcpcb.com/impedance"
OUT = (Path(__file__).resolve().parents[1] / "src" / "eda_agent" / "design"
       / "fab_data" / "jlcpcb_stackups.json")

_HEAD = re.compile(r"\d+\) (No requirement|JLC\d{5}H-\w+) Stackup Layer Material Type Thickness ")
_OUTER = re.compile(r"(Top|Bottom) Layer Copper ([\d.]+)mm")
_PREPREG = re.compile(r"Prepreg (\d+)\*(\d+) ([\d.]+)mm")
_CORE = re.compile(r"Inner Layer (L\d+) Core Inner Layer (L\d+) Copper Core Copper "
                   r"([\d.]+)mm ([\d.]+)mm ([\d.]+)mm ([\d.]+mm .*?(?:with|without) copper)")
# A core with no copper of its own, between two prepregs.
_BARE_CORE = re.compile(r"Core Core ([\d.]+)mm ([\d.]+ .*?(?:with|without) copper)")


def page_text(raw: str) -> str:
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)))


def dielectric_constants(text: str) -> dict:
    m = re.search(r"Prepreg dielectric constant: Prepreg type Dielectric constant "
                  r"((?:\d+ [\d.]+ )+)", text)
    if not m:
        raise SystemExit("prepreg dielectric constant table not found")
    nums = m.group(1).split()
    prepreg = {nums[i]: float(nums[i + 1]) for i in range(0, len(nums), 2)}
    core = re.search(r"Core dielectric constant Core dielectric constant ([\d.]+)", text)
    mask = re.search(r"Coating Dielectric CEr [\d.]+mil [\d.]+mil [\d.]+mil ([\d.]+)", text)
    if not core or not mask:
        raise SystemExit("core or solder mask dielectric constant not found")
    return {"prepreg_by_glass": prepreg, "core": float(core.group(1)),
            "solder_mask": float(mask.group(1)), "frequency": None,
            "note": "The page states no frequency or test method for these."}


def parse_plies(body: str, dk: dict) -> list[dict]:
    """The plies of one stack-up, top to bottom, from its row text."""
    found = []
    for m in _OUTER.finditer(body):
        found.append((m.start(), {"kind": "copper", "name": m.group(1),
                                  "thickness_mm": float(m.group(2))}))
    for m in _PREPREG.finditer(body):
        glass = m.group(1)
        ply = {"kind": "prepreg", "material": f"{glass}*{m.group(2)}",
               "thickness_mm": float(m.group(3)),
               "er": dk["prepreg_by_glass"].get(glass)}
        if ply["er"] is None:
            # Never borrowed from another glass style: a guess here would
            # read exactly like the page's figure.
            ply["er_missing"] = f"the page's prepreg table gives no Dk for {glass}"
        found.append((m.start(), ply))
    for m in _CORE.finditer(body):
        at = m.start()
        found.append((at, {"kind": "copper", "name": m.group(1),
                           "thickness_mm": float(m.group(3))}))
        found.append((at + 1, {"kind": "core", "material": "Core",
                               "thickness_mm": float(m.group(4)),
                               "label": m.group(6), "er": dk["core"]}))
        found.append((at + 2, {"kind": "copper", "name": m.group(2),
                               "thickness_mm": float(m.group(5))}))
    for m in _BARE_CORE.finditer(body):
        found.append((m.start(), {"kind": "core", "material": "Core",
                                  "thickness_mm": float(m.group(1)),
                                  "label": m.group(2), "er": dk["core"]}))
    # Every word of the rows must belong to a ply: a row shape this does
    # not know would otherwise drop a ply and leave a thinner board that
    # still parses.
    rest = body
    for rx in (_OUTER, _PREPREG, _CORE, _BARE_CORE):
        rest = rx.sub(" ", rest)
    if rest.strip():
        raise SystemExit(f"unparsed stack-up text: {rest.strip()[:200]!r}")
    return [p for _, p in sorted(found, key=lambda f: f[0])]


def parse(raw: str) -> dict:
    text = page_text(raw)
    dk = dielectric_constants(text)
    stackups = []
    # Each layer-count section restarts its "1) No requirement" entry.
    sections = re.split(r"(\d+)-Layer Impedance Control Stackup", text)
    for i in range(1, len(sections), 2):
        layers, body = int(sections[i]), sections[i + 1]
        heads = list(_HEAD.finditer(body))
        for j, h in enumerate(heads):
            end = heads[j + 1].start() if j + 1 < len(heads) else len(body)
            rows = body[h.end():end]
            # A stack-up ends at its bottom copper; what follows the last
            # one is the next section's heading or the page footer.
            bottom = list(re.finditer(r"Bottom Layer Copper [\d.]+mm", rows))
            if bottom:
                rows = rows[:bottom[-1].end()]
            plies = parse_plies(rows, dk)
            copper = [p for p in plies if p["kind"] == "copper"]
            if len(copper) != layers:
                raise SystemExit(f"{h.group(1)}: {len(copper)} copper plies, "
                                 f"section says {layers}")
            name = h.group(1)
            stackups.append({
                "name": name if name != "No requirement"
                else f"JLCPCB {layers}-layer default (no impedance requirement)",
                "copper_layers": layers,
                "total_mm": round(sum(p["thickness_mm"] for p in plies), 4),
                "plies": plies,
            })
    if not stackups:
        raise SystemExit("no stack-ups found; the page layout has changed")
    return {
        "vendor": "JLCPCB",
        "source": {"url": URL, "retrieved": _dt.date.today().isoformat(),
                   "page_state": "the selection the page opens with (1.6 mm)"},
        "dielectric_constants": dk,
        "stackups": stackups,
    }


def main(argv: list[str]) -> int:
    if argv:
        raw = Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    else:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", errors="replace")
    data = parse(raw)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(data['stackups'])} stack-ups -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
