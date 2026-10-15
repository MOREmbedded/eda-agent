# SPDX-License-Identifier: Apache-2.0
"""Schematic components plus connectivity -> an LTspice deck.

The piece that was missing between the two halves this project already
had: Altium knows what is on the sheet and what connects to what, and
ltspice.py can run a deck and read the results back, but nothing turned
the first into the second. So LTspice was a calculator bolted on the
side rather than something connected to the design.

Pure by design: no Altium, no LTspice, no filesystem. It takes the
readiness payload (which the bridge's Gen_GetSpiceReadiness already
produces, including the judgement about which parts need a vendor
model) and the pin-to-net map that proj_get_connectivity_many returns,
and emits text. That keeps every interesting case testable without
either program installed.

Two things here are deliberately conservative, because a SPICE deck
that is subtly wrong still simulates:

* **Node order.** SPICE reads meaning from position: ``D1 anode
  cathode``, ``Q1 collector base emitter``. R, L and C do not care, and
  V/I only flip a sign. For anything else a wrong guess yields a clean
  run and wrong answers, so every such part is reported in
  ``pin_order_assumptions`` rather than quietly trusted.
* **Ground.** SPICE needs node 0. If the named ground net matches
  nothing on the sheet, this refuses instead of emitting: a deck with no
  ground either fails to converge or solves a different circuit, and the
  second is worse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Element letters whose terminals are positionally interchangeable (R, L,
#: C) or where order only sets a sign convention (V, I). Everything else
#: encodes device physics in the order and gets flagged.
_ORDER_SAFE = frozenset({"R", "L", "C", "V", "I"})

#: How many nodes each element letter takes. Anything absent is emitted
#: with whatever pins it has and flagged.
_EXPECTED_NODES = {"R": 2, "L": 2, "C": 2, "V": 2, "I": 2, "D": 2,
                   "Q": 3, "J": 3, "M": 4, "X": None}

#: Analyses LTspice understands. The caller supplies the whole directive
#: ("tran 1m"), and only the leading word is checked, so a typo becomes a
#: refusal here rather than a silent no-op run.
_ANALYSES = frozenset({"tran", "ac", "dc", "op", "noise", "tf", "four"})

#: SPICE node names: letters, digits, underscore. Everything else (a bus
#: slash, a space, a bracket) is replaced, and the rename is reported so
#: nobody has to guess which net a node refers to.
_UNSAFE_NODE = re.compile(r"[^A-Za-z0-9_]+")


@dataclass
class DeckResult:
    """A deck, and everything needed to judge whether to trust it."""

    ok: bool
    deck: str = ""
    reason: str = ""
    emitted: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    pin_order_assumptions: list[dict[str, Any]] = field(default_factory=list)
    net_renames: dict[str, str] = field(default_factory=dict)
    missing_models: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            **({"reason": self.reason} if self.reason else {}),
            "deck": self.deck,
            "emitted": self.emitted,
            "emitted_count": len(self.emitted),
            "skipped": self.skipped,
            "pin_order_assumptions": self.pin_order_assumptions,
            "net_renames": self.net_renames,
            "missing_models": self.missing_models,
        }


def _sanitise_node(net: str, renames: dict[str, str]) -> str:
    cleaned = _UNSAFE_NODE.sub("_", net.strip()).strip("_")
    if not cleaned:
        cleaned = "N_unnamed"
    if cleaned != net:
        renames[net] = cleaned
    return cleaned


def _prefix_of(component: dict[str, Any]) -> str:
    """The element letter, preferring what the sheet already says."""
    for key in ("spice_prefix", "suggested_prefix"):
        value = str(component.get(key) or "").strip()
        if value:
            return value[0].upper()
    return ""


def _value_of(component: dict[str, Any]) -> str:
    for key in ("value", "suggested_value", "comment"):
        value = str(component.get(key) or "").strip()
        if value:
            return value
    return ""


def _pins_in_order(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pins sorted by pin number, numerically where possible.

    A symbol numbering its pins 1, 2, 10 must not order them 1, 10, 2,
    which is what plain string sorting does and is exactly the kind of
    error that produces a deck that runs and lies.
    """
    def key(pin):
        raw = str(pin.get("pin_number", "")).strip()
        try:
            return (0, int(raw), "")
        except ValueError:
            return (1, 0, raw)
    return sorted(pins, key=key)


def validate_analysis(analysis: str) -> str | None:
    """None if usable, else why not."""
    text = str(analysis or "").strip().lstrip(".")
    if not text:
        return "no analysis given (e.g. 'tran 1m', 'ac dec 100 1 1meg', 'op')"
    head = text.split()[0].lower()
    if head not in _ANALYSES:
        return (f"unknown analysis {head!r}; expected one of "
                f"{sorted(_ANALYSES)}")
    return None


def build_deck(components: list[dict[str, Any]],
               connectivity: dict[str, list[dict[str, Any]]],
               *,
               analysis: str,
               ground_net: str = "GND",
               title: str = "eda-agent generated deck",
               extra_directives: tuple[str, ...] = (),
               missing_models: tuple[str, ...] = (),
               on_missing_model: str = "refuse") -> DeckResult:
    """Emit an LTspice deck.

    ``components`` are readiness entries (designator + spice/suggested
    prefix and value); ``connectivity`` maps designator to that
    component's pins as proj_get_connectivity_many returns them
    (``pin_number``, ``net``). ``missing_models`` names parts the caller
    already knows need a vendor model, and ``on_missing_model`` decides
    whether that is fatal.
    """
    problem = validate_analysis(analysis)
    if problem:
        return DeckResult(ok=False, reason=problem)

    if on_missing_model not in ("refuse", "placeholder"):
        return DeckResult(ok=False, reason=(
            f"on_missing_model must be 'refuse' or 'placeholder', "
            f"not {on_missing_model!r}"))

    missing = sorted(set(missing_models))
    if missing and on_missing_model == "refuse":
        return DeckResult(ok=False, missing_models=missing, reason=(
            f"{len(missing)} part(s) need a manufacturer SPICE model and "
            f"none was supplied: {', '.join(missing)}. Fetch the vendor "
            f".lib/.mdl and attach it (sim_attach_model), or pass "
            f"on_missing_model='placeholder' to emit a deck that leaves "
            f"them as comments and will not run until they are filled in."))

    renames: dict[str, str] = {}
    lines: list[str] = []
    emitted: list[str] = []
    skipped: list[dict[str, str]] = []
    assumptions: list[dict[str, Any]] = []
    ground_seen = False
    ground_key = ground_net.strip().upper()

    for component in sorted(components, key=lambda c: str(c.get("designator", ""))):
        designator = str(component.get("designator", "")).strip()
        if not designator:
            continue
        if designator in missing:
            lines.append(f"* {designator} needs a manufacturer SPICE model; not emitted")
            skipped.append({"designator": designator, "why": "needs a vendor model"})
            continue

        prefix = _prefix_of(component)
        if not prefix:
            skipped.append({"designator": designator,
                            "why": "no SPICE prefix on the sheet and none suggested"})
            continue

        pins = _pins_in_order(connectivity.get(designator) or [])
        if not pins:
            skipped.append({"designator": designator,
                            "why": "no connectivity returned for it"})
            continue

        nodes = []
        for pin in pins:
            net = str(pin.get("net") or "").strip()
            if not net:
                nodes = []
                break
            if net.upper() == ground_key:
                ground_seen = True
                nodes.append("0")
            else:
                nodes.append(_sanitise_node(net, renames))
        if not nodes:
            skipped.append({"designator": designator,
                            "why": "at least one pin is on no net"})
            continue

        expected = _EXPECTED_NODES.get(prefix, None)
        if expected is not None and len(nodes) != expected:
            skipped.append({"designator": designator,
                            "why": f"{prefix} needs {expected} nodes, "
                                   f"connectivity gave {len(nodes)}"})
            continue

        value = _value_of(component)
        if not value:
            skipped.append({"designator": designator, "why": "no value"})
            continue

        name = designator if designator[0].upper() == prefix else prefix + designator
        lines.append(" ".join([name, *nodes, value]))
        emitted.append(designator)

        if prefix not in _ORDER_SAFE:
            assumptions.append({
                "designator": designator,
                "prefix": prefix,
                "nodes": nodes,
                "pin_numbers": [str(p.get("pin_number", "")) for p in pins],
                "why": (f"node order for {prefix} encodes the device's "
                        f"terminals and was taken from pin number order; "
                        f"check it against the symbol"),
            })

    if not emitted:
        return DeckResult(ok=False, skipped=skipped, missing_models=missing,
                          net_renames=renames, reason=(
                              "nothing could be emitted; see skipped for why "
                              "each component was left out"))

    if not ground_seen:
        return DeckResult(ok=False, skipped=skipped, missing_models=missing,
                          net_renames=renames, reason=(
                              f"no pin sits on the ground net {ground_net!r}, so the "
                              f"deck would have no node 0. SPICE needs one: without "
                              f"it the deck either fails to converge or solves a "
                              f"different circuit. Pass ground_net to name the net "
                              f"this schematic actually uses."))

    body = [f"* {title}", *lines, *extra_directives,
            "." + str(analysis).strip().lstrip("."), ".end", ""]
    return DeckResult(ok=True, deck="\n".join(body), emitted=emitted,
                      skipped=skipped, pin_order_assumptions=assumptions,
                      net_renames=renames, missing_models=missing)
