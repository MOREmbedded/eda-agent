# SPDX-License-Identifier: Apache-2.0
"""Compare an LTspice circuit with an Altium schematic.

The usual direction of collaboration is LTspice first: a reference
circuit from a vendor, or a circuit tuned in simulation, that the board
schematic is meant to reproduce. This module reads the LTspice netlist
(the ``.net`` LTspice writes for an ``.asc``, or any ``.cir``) and checks
it against the parts and connectivity read from Altium:

* parts present on only one side;
* value differences, compared as numbers (``10k`` = ``10K`` = ``10000``
  = ``10k 1%``) so formatting is not reported;
* wiring differences: the two sides almost never share net NAMES
  (``N003`` against ``VOUT``), so nets are paired by the terminals they
  join, and a terminal whose pairing disagrees with the rest is reported.

Terminal identity: R/L/C/V/I are two-terminal and orientation free (the
orientation that agrees with the other parts is chosen); D/Q/M/J use pin
names (A/K, C/B/E, D/G/S); a subcircuit uses a pin map when one is given.
Parts with no way to line their terminals up are listed in
``unchecked`` rather than guessed.

Pure: no Altium, no LTspice.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Optional

from . import pin_map

_ELEMENT_NODES = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,
                  "Q": 3, "J": 3, "M": 4, "B": 2, "E": 4, "G": 4, "F": 2, "H": 2,
                  "S": 4, "W": 2, "T": 4, "U": 3, "Z": 3}
_TWO_TERMINAL = frozenset("RCLVI")

_SUFFIX = {"T": 1e12, "G": 1e9, "MEG": 1e6, "K": 1e3, "M": 1e-3, "U": 1e-6,
           "µ": 1e-6, "N": 1e-9, "P": 1e-12, "F": 1e-15, "MIL": 25.4e-6}
_NUM = re.compile(r"^([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|[+-]?\.\d+(?:[eE][+-]?\d+)?)"
                  r"(MEG|MIL|[TGKMUNPFµ])?", re.I)
_RKM = re.compile(r"^(\d+)([RKMLUNP])(\d+)$", re.I)


def spice_value(text: str) -> Optional[float]:
    """The number a SPICE or BOM value states, or None.

    Handles SPICE suffixes (``4.7u``, ``1meg``), RKM notation (``4K7``,
    ``2R2``, ``100N``) and trailing text (``10k 1%``, ``100nF X7R``).
    """
    s = str(text or "").strip().replace(",", ".")
    if not s:
        return None
    first = s.split()[0]
    m = _RKM.match(first)
    if m:
        mult = {"R": 1.0, "L": 1e-3, "K": 1e3, "M": 1e6, "U": 1e-6,
                "N": 1e-9, "P": 1e-12}[m.group(2).upper()]
        # 'M' in RKM is mega for resistors; there is no milli in RKM.
        return float(f"{m.group(1)}.{m.group(3)}") * mult
    m = _NUM.match(first)
    if not m:
        return None
    base = float(m.group(1))
    suf = (m.group(2) or "").upper()
    if suf == "µ":
        suf = "U"
    return base * _SUFFIX.get(suf, 1.0)


def _is_number(tok: str) -> bool:
    m = _NUM.match(tok)
    return bool(m) and m.end() == len(tok)


def _same_value(a: str, b: str) -> Optional[bool]:
    va, vb = spice_value(a), spice_value(b)
    if va is None or vb is None:
        if not a or not b:
            return None
        return a.strip().upper() == b.strip().upper()
    if va == vb:
        return True
    return abs(va - vb) <= 1e-6 * max(abs(va), abs(vb))


def _logical(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("+") and out:
            out[-1] += " " + s[1:]
        else:
            out.append(s)
    return out


def parse_netlist(text: str) -> dict[str, Any]:
    """Elements of an LTspice/SPICE netlist: name, prefix, nodes, value.

    The designator is the element name with the SPICE letter dropped
    when LTspice added it for a schematic part (``XU1`` -> ``U1``,
    ``QQ1`` -> ``Q1``; ``R1`` stays ``R1``).
    """
    parts: list[dict[str, Any]] = []
    for line in _logical(text):
        if not line or line[0] in "*;.":
            continue
        line = line.split(";", 1)[0].strip()
        toks = line.split()
        if len(toks) < 3:
            continue
        name = toks[0]
        prefix = name[0].upper()
        if not prefix.isalpha():
            continue
        if prefix == "X":
            body = [t for t in toks[1:] if "=" not in t and t.lower() != "params:"]
            if len(body) < 2:
                continue
            nodes, value = body[:-1], body[-1]
        else:
            n = _ELEMENT_NODES.get(prefix)
            if n is None or len(toks) < n + 1:
                continue
            if prefix in "QM":
                # A 4th (substrate/bulk) node or not: the 5th positional
                # word is then a model name, not an area number.
                body = [t for t in toks[1:] if "=" not in t]
                n = 4 if len(body) >= 5 and not _is_number(body[4]) else 3
            nodes = toks[1:1 + n]
            value = " ".join(toks[1 + n:])
        designator = name[1:] if prefix == "X" else name
        if len(designator) > 1 and designator[0].upper() == prefix and \
                designator[1].upper() == prefix:
            designator = designator[1:]          # QQ1 -> Q1
        if prefix == "X" and not designator:
            designator = name
        parts.append({"name": name, "designator": designator, "prefix": prefix,
                      "nodes": nodes, "value": value.strip()})
    return {"parts": parts}


def _terminals_altium(prefix: str, pins: list[dict[str, Any]],
                      pin_maps: dict[str, list[str]], designator: str,
                      lt_nodes: int) -> Optional[list[list[str]]]:
    """For each LTspice node position, the Altium pin numbers it is.

    None when there is no trustworthy way to line them up.
    """
    if prefix in _TWO_TERMINAL:
        nums = [str(p.get("pin_number", "")) for p in
                sorted(pins, key=lambda p: str(p.get("pin_number", "")).zfill(6))]
        return [[n] for n in nums] if len(nums) == 2 == lt_nodes else None
    if designator in pin_maps:
        groups = [[x for x in str(e).split("+") if x] for e in pin_maps[designator]]
        return groups if len(groups) == lt_nodes else None
    std = pin_map.standard_order(prefix, pins)
    if std and len(std["nodes"]) <= lt_nodes:
        groups = [n["pins"] for n in std["nodes"]]
        if len(groups) == lt_nodes or (prefix in "MQ" and lt_nodes == len(groups) + 1):
            # A 4th MOSFET/BJT node (bulk/substrate) has no symbol pin.
            return groups + [[]] * (lt_nodes - len(groups))
    return None


def compare(ltspice: dict[str, Any], altium: list[dict[str, Any]],
            ground_net: str = "GND",
            designator_map: Optional[dict[str, str]] = None,
            pin_maps: Optional[dict[str, list[str]]] = None) -> dict[str, Any]:
    """Compare a parsed LTspice netlist with Altium components.

    ``altium`` entries: ``designator``, ``value`` (or ``comment``) and
    ``pins`` [{pin_number, pin_name, net}]. ``designator_map`` renames
    LTspice designators to Altium ones (``{"R1": "R101"}``) when the two
    were numbered differently. ``pin_maps`` gives, per Altium designator,
    the Altium pin number(s) for each LTspice node of a subcircuit.
    """
    dmap = {k.upper(): v for k, v in (designator_map or {}).items()}
    pin_maps = {k: list(v) for k, v in (pin_maps or {}).items()}
    lt_parts = {}
    for p in ltspice.get("parts", []):
        des = dmap.get(p["designator"].upper(), p["designator"])
        lt_parts[des.upper()] = {**p, "designator": des}
    alt_parts = {str(c.get("designator", "")).upper(): c for c in altium
                 if c.get("designator")}

    only_lt = sorted(lt_parts[k]["designator"] for k in lt_parts.keys() - alt_parts.keys())
    only_alt = sorted(alt_parts[k]["designator"] for k in alt_parts.keys() - lt_parts.keys())
    common = sorted(lt_parts.keys() & alt_parts.keys())

    value_diffs = []
    for k in common:
        lt, al = lt_parts[k], alt_parts[k]
        if lt["prefix"] in "RCL":
            alt_value = str(al.get("value") or al.get("comment") or "")
            same = _same_value(lt["value"], alt_value)
            if same is False:
                value_diffs.append({"designator": al["designator"],
                                    "ltspice": lt["value"], "altium": alt_value})

    ground = ground_net.strip().upper()

    def lt_net(n: str) -> str:
        return "0" if n in ("0", "GND") else n

    def alt_net(n: str) -> str:
        return "0" if n.strip().upper() == ground else n

    # Pair each terminal: (designator, position) -> (lt net, alt net).
    pairs: list[tuple[str, int, str, str]] = []
    two_terminal: list[tuple[str, list[str], list[str]]] = []
    unchecked = []
    for k in common:
        lt, al = lt_parts[k], alt_parts[k]
        pins = list(al.get("pins") or [])
        by_num = {str(p.get("pin_number", "")): p for p in pins}
        groups = _terminals_altium(lt["prefix"], pins, pin_maps, al["designator"],
                                   len(lt["nodes"]))
        if groups is None:
            unchecked.append({"designator": al["designator"],
                              "why": "no pin names or pin map line its terminals up "
                                     "with the LTspice nodes; pass pin_maps"})
            continue
        alt_nets = []
        for g in groups:
            nets = [str(by_num.get(n, {}).get("net") or "") for n in g]
            nets = [x for x in nets if x]
            alt_nets.append(alt_net(nets[0]) if nets else "")
        lts = [lt_net(n) for n in lt["nodes"]]
        if lt["prefix"] in _TWO_TERMINAL:
            two_terminal.append((al["designator"], lts, alt_nets))
        else:
            for i, (a, b) in enumerate(zip(lts, alt_nets)):
                if b:
                    pairs.append((al["designator"], i + 1, a, b))

    # Votes from oriented parts first, then orient the two-terminal parts
    # whichever way agrees with the votes so far (ground counts for both).
    votes: dict[str, Counter] = defaultdict(Counter)
    votes["0"]["0"] += 1000
    for _, _, a, b in pairs:
        votes[a][b] += 1
    pending = list(two_terminal)
    for round_ in range(3):
        rest = []
        for des, lts, alts in pending:
            straight = votes[lts[0]][alts[0]] + votes[lts[1]][alts[1]]
            crossed = votes[lts[0]][alts[1]] + votes[lts[1]][alts[0]]
            if straight == crossed == 0 and round_ < 2:
                rest.append((des, lts, alts))
                continue
            if crossed > straight:
                alts = [alts[1], alts[0]]
            for i in range(2):
                pairs.append((des, i + 1, lts[i], alts[i]))
                votes[lts[i]][alts[i]] += 1
        pending = rest
        if not pending:
            break

    net_map = {lt: c.most_common(1)[0][0] for lt, c in votes.items() if c}
    wiring = []
    for des, pos, a, b in pairs:
        expected = net_map.get(a)
        if expected is not None and expected != b:
            wiring.append({"designator": des, "terminal": pos,
                           "ltspice_net": a, "altium_net": b,
                           "altium_net_expected": expected})

    reverse = defaultdict(set)
    for lt, al in net_map.items():
        reverse[al].add(lt)
    merged = {al: sorted(lts) for al, lts in reverse.items() if len(lts) > 1}

    same = not (only_lt or only_alt or value_diffs or wiring or merged)
    return {
        "match": same and not unchecked,
        "parts_compared": len(common),
        "only_in_ltspice": only_lt,
        "only_in_altium": only_alt,
        "value_differences": value_diffs,
        "wiring_differences": wiring,
        "nets_merged_in_altium": merged,
        "net_map": {k: v for k, v in sorted(net_map.items())},
        "unchecked": unchecked,
    }
