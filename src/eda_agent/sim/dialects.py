# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The deck from deck_from_plan, written for ngspice or for LTspice.

The circuit lines are plain SPICE and read the same in both. What differs
is the file around them: LTspice takes a ``.net`` it can open and run as
it is, with its own ``.backanno``; ngspice reads a ``.cir`` and is driven
here by the simulator wrapper. ngspice stays the engine eda-agent runs;
the LTspice file is for someone who wants to open the circuit there.

Analyses are given as data, not text, so a bad one is refused here
instead of being found by the simulator:

* ``{"op": true}``
* ``{"tran": {"step": 1e-6, "stop": 1e-3}}`` (optional ``start``)
* ``{"ac": {"points": 50, "start": 1, "stop": 1e6}}`` (decade sweep)
* ``{"dc": {"source": "Vsup_VIN", "start": 0, "stop": 12, "step": 0.1}}``
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

DIALECTS = ("ngspice", "ltspice")
_SOURCE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _number(spec: dict, key: str, *, positive: bool = True) -> float:
    try:
        value = float(spec[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{key} must be {'positive' if positive else 'finite'}")
    return value


def analysis_lines(analyses: Optional[dict]) -> list[str]:
    """SPICE directives for ``analyses``; ValueError names a bad one."""
    out: list[str] = []
    for kind, spec in (analyses or {"op": True}).items():
        kind = str(kind).lower()
        if kind == "op":
            if spec:
                out.append(".op")
        elif kind == "tran":
            step, stop = _number(spec, "step"), _number(spec, "stop")
            if step >= stop:
                raise ValueError("tran step must be smaller than stop")
            start = float(spec.get("start") or 0.0)
            out.append(f".tran {step:g} {stop:g}" + (f" {start:g}" if start else ""))
        elif kind == "ac":
            points = int(_number(spec, "points"))
            f0, f1 = _number(spec, "start"), _number(spec, "stop")
            if f0 >= f1:
                raise ValueError("ac start must be below stop")
            out.append(f".ac dec {points} {f0:g} {f1:g}")
        elif kind == "dc":
            source = str(spec.get("source") or "")
            if not _SOURCE.match(source):
                raise ValueError("dc needs the name of a source to sweep")
            out.append(f".dc {source} {_number(spec, 'start', positive=False):g} "
                       f"{_number(spec, 'stop', positive=False):g} "
                       f"{_number(spec, 'step'):g}")
        else:
            raise ValueError(f"unknown analysis {kind!r}: use op, tran, ac or dc")
    if not out:
        raise ValueError("no analysis to run")
    return out


def write_deck(netlist: str, dialect: str, analyses: Optional[dict] = None,
               gaps: Optional[list[str]] = None) -> str:
    """``netlist`` (ending in .end) as a complete file for ``dialect``.

    ``gaps`` are written as comments at the top: what a partial deck is
    missing, so it cannot be mistaken for the whole circuit.
    """
    dialect = dialect.lower()
    if dialect not in DIALECTS:
        raise ValueError(f"dialect must be one of {', '.join(DIALECTS)}")
    body = [ln for ln in netlist.splitlines() if ln.strip().lower() != ".end"]
    head, rest = body[:1], body[1:]
    notes = [f"* NOT IN THIS DECK: {g}" for g in gaps or []]
    directives = analysis_lines(analyses)
    if dialect == "ltspice":
        directives.append(".backanno")
    return "\n".join([*head, *notes, *rest, *directives, ".end", ""])


def deck_gaps(deck: dict[str, Any]) -> list[str]:
    """Everything deck_from_plan could not put in, one line each."""
    gaps = [f"{m['refdes']}: no manufacturer model ({m.get('mpn') or m.get('lib_ref')})"
            for m in deck.get("missing_models") or []]
    gaps += [f"{m['refdes']}: model pins not matched to the plan's pins"
             for m in deck.get("needs_pin_map") or []]
    gaps += [f"{b['refdes']}: value {b.get('value')!r} not understood"
             for b in deck.get("bad_values") or []]
    gaps += [f"rail {n}: voltage not known" for n in deck.get("unknown_supplies") or []]
    gaps += list(deck.get("problems") or [])
    return gaps
