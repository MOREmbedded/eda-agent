# SPDX-License-Identifier: Apache-2.0
"""Compare one part's parameters against another's, and say why.

The question this answers is "can this candidate stand in for that
part", and the whole design of it follows from one refusal: it never
returns "equivalent". It returns EVIDENCE, per attribute, and leaves
the call to a human.

That is not timidity. ``providers/__init__.py``'s MPN normalisation
already refuses to fold wildcards because doing so "would assert an
equivalence this code cannot support", and the same limit applies with
more force here. A distributor's parametric map is a sales catalogue,
not a datasheet: it is partial, its attribute names differ per source,
and the parameter that actually kills a substitution (an inrush
specification, a startup sequence, a qualification the board needs) is
routinely not in it at all. Software that scored those two parts as 97%
equivalent would be stating a confidence nothing in the input supports.

So the output is three lists -- what matches, what BLOCKS, and what
nobody checked -- and the third one never empties, because an attribute
the candidate does not publish cannot be verified by not mentioning it.

Three safety properties worth stating because each one is a way this
could quietly be wrong:

**Units are never crossed.** ``16 V`` and ``16 A`` parse to the same
number and are not comparable. Every quantity carries its unit and a
mismatch is refused rather than compared.

**Direction is per attribute, from a table.** More voltage rating is
safe; more ESR is not; a wider temperature range is safe; a different
dielectric is not safe at all. An attribute with no rule is compared as
text and reported as unreviewed, never silently passed.

**A hyphen is not a range separator.** ``-55 ~ 125`` is a range and
``-55`` is a negative number, and the two cannot be told apart if ``-``
splits. Only ``~``, ``to``, ``...`` and the dashes that are not the
minus sign do.

No PCN, no errata, no product-change notices. No distributor API serves
them, so nothing here checks them, and the result says so rather than
leaving a reader to assume the silence means "clean".
"""
from __future__ import annotations

import math
import re
from typing import Any, NamedTuple, Optional

__all__ = [
    "AT_LEAST",
    "AT_MOST",
    "CLOSE",
    "CONTAINS",
    "EQUAL",
    "Quantity",
    "Range",
    "compare_parameters",
    "canonical_for",
    "normalise_key",
    "parse_quantity",
    "parse_range",
    "rule_for",
    "summarise",
]

# --- comparison rules -------------------------------------------------
#: Candidate may exceed the original; falling short is a downgrade.
AT_LEAST = "at_least"
#: Candidate may undercut the original; exceeding it is a downgrade.
AT_MOST = "at_most"
#: Must be the same value. The attribute IS the part's function.
CLOSE = "close"
#: The candidate's range must span the original's.
CONTAINS = "contains"
#: Categorical. Same string or it is a different thing.
EQUAL = "equal"

#: SI prefixes, as powers of ten. Case matters: ``M`` is mega and ``m``
#: is milli, and confusing them is a factor of 1e9. ``K`` is accepted as
#: a kilo alias because catalogues write it, but ``k`` is correct.
#:
#: Exponents rather than multipliers so the value is rebuilt as
#: ``"100e-9"`` and parsed once by float(), with no second rounding.
_PREFIX: dict[str, int] = {
    "f": -15, "p": -12, "n": -9, "u": -6, "m": -3,
    "c": -2, "k": 3, "K": 3, "M": 6, "G": 9, "T": 12,
}

#: Units whose leading letter collides with a prefix, so the string must
#: be read as a whole unit before any prefix is split off it. Without
#: this, ``"5 F"`` (farad) reads as femto-nothing and ``"10 K"`` (kelvin)
#: as kilo-nothing.
_WHOLE_UNITS = (
    "ppm", "dBm", "dB", "VA", "mAh", "Hz", "ohm", "Ohm", "OHM",
    "F", "H", "V", "A", "W", "C", "K", "S", "J", "N", "T", "s", "g", "m",
)

#: Spellings that mean the same unit. Compared after this mapping, so
#: ``4.7kΩ`` and ``4700 ohm`` are the same quantity.
_UNIT_ALIASES = {
    # Two distinct code points: GREEK CAPITAL LETTER OMEGA, which is
    # what keyboards emit, and OHM SIGN, kept by Unicode for
    # compatibility. Both turn up in catalogue text and they are NOT
    # the same character, so both are listed.
    "Ω": "ohm", "Ω": "ohm",
    "ohms": "ohm", "Ohm": "ohm", "OHM": "ohm",
    "OHMS": "ohm", "R": "ohm",
    "°c": "degc", "°C": "degc", "℃": "degc", "degc": "degc",
    "c": "degc", "C": "degc",
    "°f": "degf", "°F": "degf",
    "sec": "s", "secs": "s", "seconds": "s", "second": "s",
    "hz": "Hz", "HZ": "Hz",
    "v": "V", "a": "A", "w": "W", "f": "F", "h": "H",
}

#: ``~`` is what every catalogue uses; the dashes are the typographic
#: ones, which are never a minus sign. Plain ``-`` is deliberately
#: absent -- see the module docstring.
_RANGE_SPLIT = re.compile(r"\s*(?:~|–|—|\.{2,}|…|\bto\b)\s*",
                          re.IGNORECASE)

_NUMBER = re.compile(r"^[+\-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+\-]?\d+)?")


class Quantity(NamedTuple):
    """A number in base SI units, with the unit it was measured in.

    ``unit`` is empty for a dimensionless value. Two quantities with
    different units are NOT comparable, which is the point of keeping
    it rather than reducing everything to a float.
    """

    value: float
    unit: str
    text: str = ""


class Range(NamedTuple):
    """A closed interval, both ends in the same unit."""

    low: float
    high: float
    unit: str
    text: str = ""

    def spans(self, other: "Range") -> bool:
        return self.low <= other.low and self.high >= other.high


def normalise_key(key: str) -> str:
    """Fold an attribute name to something comparable across sources.

    Nexar says ``voltagerating_dc``, Digi-Key says ``Voltage - Rated``
    and Mouser says ``Voltage Rating``. Pairing the two parts' maps on
    the raw key would compare almost nothing, so everything but letters
    and digits is dropped and the rest lowercased.
    """
    return re.sub(r"[^a-z0-9]+", "", str(key or "").lower())


# --- parsing ----------------------------------------------------------

def _split_unit(text: str) -> tuple[str, str]:
    """Split a trailing unit off a value string, prefix included.

    Returns ``(number_part, canonical_unit)``. The unit is matched as a
    whole first (see _WHOLE_UNITS) so that a unit beginning with a
    prefix letter is not eaten a character at a time.
    """
    s = text.strip()
    s = s.replace("µ", "u").replace("μ", "u")  # micro signs
    s = s.lstrip("±+")                               # a leading +/-
    s = s.replace("−", "-")                          # unicode minus
    match = _NUMBER.match(s)
    if not match:
        return "", ""
    number = match.group(0)
    rest = s[len(number):].strip()
    if not rest:
        return number, ""

    for unit in _WHOLE_UNITS:
        # A ONE-LETTER unit must match exactly. "K" is kelvin and "k" is
        # kilo; "T" is tesla and "t" is nothing; folding the case turns
        # "4.7k" into 4.7 kelvin. Longer names have no such collision,
        # so "OHM" and "ohm" are allowed to be the same unit.
        if rest == unit or (len(unit) > 1 and rest.lower() == unit.lower()):
            return number, _UNIT_ALIASES.get(rest, _UNIT_ALIASES.get(
                rest.lower(), rest))
    if rest in _UNIT_ALIASES:
        return number, _UNIT_ALIASES[rest]

    # Degrees are written attached to their letter and must not be read
    # as a unit called "degree" with a stray C after it.
    if rest and rest[0] in "°℃℉":
        return number, _UNIT_ALIASES.get(rest, _UNIT_ALIASES.get(
            rest.lower(), "degc" if "c" in rest.lower() else rest))

    prefix, unit = rest[0], rest[1:].strip()
    if prefix in _PREFIX and unit:
        canonical = _UNIT_ALIASES.get(unit, _UNIT_ALIASES.get(
            unit.lower(), unit))
        return f"{number}e{_PREFIX[prefix]}", canonical
    if prefix in _PREFIX and not unit:
        # A bare prefix with nothing after it ("4.7k") is a multiplier on
        # a dimensionless number, which is how resistance is usually
        # written. The unit stays empty rather than being guessed at.
        return f"{number}e{_PREFIX[prefix]}", ""
    return number, _UNIT_ALIASES.get(rest, _UNIT_ALIASES.get(
        rest.lower(), rest))


def parse_quantity(text: Any) -> Optional[Quantity]:
    """One value with its unit, in base SI, or ``None`` if unreadable.

    ``None`` is the honest answer for anything this cannot read, and the
    caller reports it as unreviewed. Guessing would turn a parse failure
    into a comparison result, which is the one outcome worth avoiding.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return Quantity(float(text), "", str(text))
    if not isinstance(text, str) or not text.strip():
        return None
    number, unit = _split_unit(text)
    if not number:
        return None
    # A "unit" carrying digits or brackets is not a unit, it is the rest
    # of a sentence: "0603 (1608 Metric)" would otherwise parse as 603
    # of something called "(1608 Metric)". Refusing keeps a package code
    # from being treated as a measurement. Units with a SPACE are kept,
    # because "50 V DC" and "50 V AC" are genuinely different and
    # folding them together would be the worse error.
    if any(c.isdigit() or c in "()[]{}" for c in unit):
        return None
    try:
        return Quantity(float(number), unit, text.strip())
    except ValueError:
        return None


def parse_range(text: Any) -> Optional[Range]:
    """A two-ended range, or ``None``.

    ``-55°C ~ 125°C`` and ``4.5 V ~ 16 V`` are the two shapes
    that matter. A unit written only on the second end is applied to
    both, which is how ``-40 ~ 85°C`` is meant to be read; a range
    whose two ends carry DIFFERENT units is refused rather than
    reconciled.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    parts = _RANGE_SPLIT.split(text.strip())
    parts = [p for p in parts if p.strip()]
    if len(parts) != 2:
        return None
    low, high = parse_quantity(parts[0]), parse_quantity(parts[1])
    if low is None or high is None:
        return None
    if low.unit and high.unit and low.unit != high.unit:
        return None
    unit = low.unit or high.unit
    # A bare low end takes the high end's unit, which also means it may
    # have missed a prefix ("100 ~ 200 nF" would read as 100 and 2e-7).
    # Refusing is better than inventing the prefix.
    if not low.unit and high.unit and _has_prefix(parts[1]):
        return None
    if low.value > high.value:
        low, high = high, low
    return Range(low.value, high.value, unit, text.strip())


def _has_prefix(text: str) -> bool:
    number, _ = _split_unit(text)
    return "e" in number.lower() and not text.strip().lower().count("e")
# --- the attribute table ----------------------------------------------
#
# Each row is ONE attribute: a canonical name, the direction in which a
# difference is safe, and the spellings catalogues use for it.
#
# The direction is domain knowledge, written down rather than inferred,
# because it is the whole safety argument: swapping AT_LEAST for AT_MOST
# on a voltage rating turns a blocked substitution into an approved one.
#
# The spellings are what makes a cross-source comparison possible at
# all. Nexar says ``voltagerating_dc``, Digi-Key says ``Voltage -
# Rated`` and Mouser says ``Voltage Rating``; all three normalise to
# different keys, so pairing on the key alone compared almost nothing
# and reported the result as "unchecked" -- honest, and useless. Rows
# give the three a shared identity, so they pair.
#
# Spellings are matched as SUBSTRINGS of the normalised key, FIRST ROW
# WINNING, so a more specific attribute must come first ("outputvoltage"
# before "voltagerating"). An unmatched key gets no canonical name and
# no rule, which rule_for returns as None rather than defaulting.

_ATTRIBUTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # -- the value itself: must match ---------------------------------
    # Ahead of the rated-voltage row: an output voltage that is merely
    # HIGHER is a different regulator, not a safer one.
    ("output_voltage", CLOSE, ("outputvoltage", "voltageoutput")),
    ("capacitance", CLOSE, ("capacitance",)),
    # These three sit here, out of the AT_MOST group they belong to,
    # because each contains "resistance" and the generic row below
    # would otherwise claim them. An ESR compared as CLOSE blocks every
    # substitution with a BETTER capacitor, and a DCR compared as CLOSE
    # does the same to every better inductor. Grouping by direction is
    # a reading aid; specificity decides the order, and the guard test
    # in tests/test_part_match.py enforces it.
    ("esr", AT_MOST, ("esr", "equivalentseriesresistance")),
    ("dc_resistance", AT_MOST, ("dcresistance", "dcr")),
    ("on_resistance", AT_MOST, ("rdson", "onresistance", "resistanceon")),
    ("resistance", CLOSE, ("resistance", "resistanceohms")),
    ("inductance", CLOSE, ("inductance",)),
    ("frequency", CLOSE, ("resonantfrequency", "frequency", "clockspeed")),
    ("gain", CLOSE, ("gain",)),
    ("turns_ratio", CLOSE, ("turnsratio",)),

    # -- categorical: a different value is a different component ------
    ("dielectric", EQUAL, ("dielectric", "temperaturecoefficient",
                           "tempco")),
    ("mounting_type", EQUAL, ("mountingtype", "mounting")),
    ("package", EQUAL, ("packagecase", "casepackage",
                        "supplierdevicepackage", "package", "casecode",
                        "case")),
    ("polarity", EQUAL, ("polarity",)),
    # Before composition: its "termination" spelling would otherwise
    # claim "Number of Terminations", which is a pin count.
    ("pin_count", EQUAL, ("numberofpins", "pincount",
                          "numberofterminations", "numberofcontacts")),
    ("composition", EQUAL, ("composition", "corematerial", "technology",
                            "termination")),
    ("output_type", EQUAL, ("outputtype", "interface", "logicfamily")),
    ("rohs", EQUAL, ("rohs",)),

    # -- ranges that must span ----------------------------------------
    ("operating_temperature", CONTAINS,
     ("operatingtemperature", "temperatureoperating", "temperaturerange",
      "ambienttemperature", "junctiontemperature")),
    ("supply_voltage", CONTAINS, ("supplyvoltage", "voltagesupply",
                                  "inputvoltage", "voltageinput")),

    # -- smaller is the safe side -------------------------------------
    ("tolerance", AT_MOST, ("tolerance",)),
    # esr / dc_resistance / on_resistance are AT_MOST too, and are
    # listed further up where specificity requires -- see the note there.
    ("quiescent_current", AT_MOST, ("quiescent", "standbycurrent",
                                    "leakagecurrent", "currentleakage")),
    ("propagation_delay", AT_MOST, ("propagationdelay",)),
    ("height", AT_MOST, ("height", "thickness")),

    # -- bigger is the safe side --------------------------------------
    ("voltage_rating", AT_LEAST,
     ("voltagerating", "ratedvoltage", "voltagerated", "breakdownvoltage",
      "voltagebreakdown", "withstandvoltage", "voltagedc")),
    ("current_rating", AT_LEAST,
     ("currentrating", "ratedcurrent", "currentrated", "outputcurrent",
      "currentoutput", "saturationcurrent", "currentsaturation")),
    ("power_rating", AT_LEAST,
     ("powerrating", "powerdissipation", "ratedpower", "power")),
    ("memory_size", AT_LEAST, ("memorysize", "ramsize",
                               "programmemorysize", "flashsize")),
)

#: Built once: ``{spelling: (canonical, rule)}`` is the wrong shape for
#: substring matching, so the rows stay a sequence and this is only a
#: flattened view for the lookup loop.
_ATTRIBUTE_PATTERNS: tuple[tuple[str, str, str], ...] = tuple(
    (pattern, canonical, rule)
    for canonical, rule, patterns in _ATTRIBUTES
    for pattern in patterns
)


def _match_attribute(key: str) -> tuple[str, str] | tuple[None, None]:
    """``(canonical, rule)`` for an attribute name, or ``(None, None)``."""
    normalised = normalise_key(key)
    if not normalised:
        return None, None
    for pattern, canonical, rule in _ATTRIBUTE_PATTERNS:
        if pattern in normalised:
            return canonical, rule
    return None, None


def canonical_for(key: str) -> Optional[str]:
    """The shared name for an attribute, so two catalogues' spellings of
    it can be paired. ``None`` when the table does not know it, in which
    case the normalised key is used instead and only an identical
    spelling will pair."""
    return _match_attribute(key)[0]


def rule_for(key: str, overrides: Optional[dict[str, str]] = None
             ) -> Optional[str]:
    """The comparison rule for an attribute, or ``None`` if there is no
    rule for it.

    ``None`` rather than a default direction, on purpose. A default of
    EQUAL would quietly block a harmless difference; a default of
    AT_LEAST would quietly approve a dangerous one. Having no rule is a
    real state and the caller reports it as unreviewed.

    ``overrides`` is keyed the same way (normalised) and wins, so a
    house convention can pin an attribute this table does not know.
    """
    normalised = normalise_key(key)
    if not normalised:
        return None
    if overrides:
        for raw, rule in overrides.items():
            if normalise_key(raw) == normalised:
                return rule
    return _match_attribute(key)[1]


# --- comparison -------------------------------------------------------

def _reconcile_units(a: str, b: str) -> bool:
    """Whether two readings of THE SAME attribute are comparable.

    Identical units obviously are. So is one side stating a unit where
    the other states none, and that case needs its argument written
    down because silently accepting it is how a comparison goes wrong:

    the two values are the same attribute of two parts -- they are
    paired on the attribute name before they ever reach here -- so they
    cannot be of different dimensions. One catalogue writing "125" where
    another writes "125 degC" is an elision, not a different
    measurement. Scale is not at risk either, because a value carrying
    an SI prefix always parses with that prefix applied ("4.7k" is 4700
    with no unit, not 4.7), so there is no unitless reading that is
    silently a thousand times out.

    Two DIFFERENT units are refused outright. 16 V and 16 A are the same
    number and nothing else.
    """
    return a == b or not a or not b


def _text_equal(a: str, b: str) -> bool:
    def fold(x: str) -> str:
        return re.sub(r"\s+", " ", str(x or "")).strip().lower()
    return fold(a) == fold(b)


def _close(a: float, b: float, rel_tol: float) -> bool:
    return math.isclose(a, b, rel_tol=max(rel_tol, 1e-9), abs_tol=0.0)


def _compare_one(rule: str, original: str, candidate: str,
                 rel_tol: float) -> tuple[str, str]:
    """``(verdict, note)`` for one attribute under one rule."""
    if rule == EQUAL:
        if _text_equal(original, candidate):
            return "same", ""
        return "mismatch", "categorical attribute, and the two differ"

    if rule == CONTAINS:
        a, b = parse_range(original), parse_range(candidate)
        if a is None or b is None:
            return "unreadable", ("not a range this can read, so the span "
                                  "was not checked")
        if not _reconcile_units(a.unit, b.unit):
            return "unreadable", (f"units differ ({a.unit} vs {b.unit}), "
                                  f"not comparable")
        if b.spans(a):
            return ("same" if (b.low == a.low and b.high == a.high)
                    else "better"), "candidate's range covers the original's"
        return "worse", "candidate's range does not cover the original's"

    a_q, b_q = parse_quantity(original), parse_quantity(candidate)
    if a_q is None or b_q is None:
        # Numeric rule, unreadable value: fall back to text so an exact
        # string match is still worth something, but say that is all it
        # was. Reporting it as "same" with no qualifier would overstate
        # a string comparison as a numeric one.
        if _text_equal(original, candidate):
            return "same", "compared as text; the value did not parse"
        return "unreadable", "the value did not parse, so it was not compared"
    if not _reconcile_units(a_q.unit, b_q.unit):
        return "unreadable", (f"units differ ({a_q.unit} vs {b_q.unit}), "
                              f"not comparable")

    if _close(a_q.value, b_q.value, rel_tol):
        return "same", ""
    if rule == CLOSE:
        return "mismatch", "the value itself differs"
    if rule == AT_LEAST:
        return ("better", "candidate exceeds the original") \
            if b_q.value > a_q.value else \
            ("worse", "candidate falls short of the original")
    if rule == AT_MOST:
        return ("better", "candidate is below the original") \
            if b_q.value < a_q.value else \
            ("worse", "candidate exceeds the original")
    return "unreadable", f"no comparison defined for rule {rule!r}"


#: Verdicts that stop a substitution until a human overrules them.
_BLOCKING = ("mismatch", "worse")
#: Verdicts that mean nobody checked, which is not the same as a pass.
_UNREVIEWED = ("unreadable", "unruled", "missing")


def compare_parameters(
        original: dict[str, Any],
        candidate: dict[str, Any],
        *,
        labels: Optional[dict[str, str]] = None,
        rules: Optional[dict[str, str]] = None,
        rel_tol: float = 0.0,
) -> dict[str, Any]:
    """Compare two parametric maps and report the evidence.

    ``original`` and ``candidate`` are ``{attribute: displayed value}``,
    as every provider in ``libimport.providers.distributors`` now
    publishes under ``PartHit.extra["parameters"]``.

    The two are paired by CANONICAL attribute where the table knows one
    (so Nexar's ``voltagerating_dc`` meets Digi-Key's ``Voltage -
    Rated``) and by normalised key otherwise. Where a candidate spells
    one canonical attribute twice, the first spelling wins and the
    other falls through to ``candidate_only`` rather than being picked
    between.

    Returns ``attributes`` (one record each, in a stable order),
    ``blocking`` (the ones that stop the substitution), ``unreviewed``
    (the ones nobody checked, which never empties quietly) and
    ``candidate_only`` (attributes the original does not state, kept
    because a candidate specifying something extra is information, not
    noise).

    There is no score and no verdict for the part as a whole. See the
    module docstring for why that is a design decision rather than an
    omission.
    """
    labels = labels or {}
    original = original if isinstance(original, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}

    # Indexed twice: by canonical name for a cross-source pairing, and
    # by normalised key so an attribute the table does not know still
    # pairs with an identical spelling. setdefault, not assignment: the
    # first spelling of a canonical attribute wins, rather than the
    # last one iteration happens to reach.
    by_canonical: dict[str, tuple[str, Any]] = {}
    by_key: dict[str, tuple[str, Any]] = {}
    for raw, value in candidate.items():
        normalised = normalise_key(raw)
        if not normalised:
            continue
        by_key.setdefault(normalised, (raw, value))
        canonical = canonical_for(raw)
        if canonical:
            by_canonical.setdefault(canonical, (raw, value))

    attributes: list[dict[str, Any]] = []
    used: set[str] = set()
    for raw, value in original.items():
        normalised = normalise_key(raw)
        if not normalised:
            continue
        rule = rule_for(raw, rules)
        canonical = canonical_for(raw)
        record: dict[str, Any] = {
            "attribute": raw,
            "label": labels.get(raw, ""),
            "canonical": canonical or "",
            "rule": rule or "",
            "original": "" if value is None else str(value),
        }
        match = (by_canonical.get(canonical) if canonical else None)             or by_key.get(normalised)
        if match is None:
            record.update(candidate="", verdict="missing",
                          note="the candidate does not publish this "
                               "attribute, so it was not checked")
            attributes.append(record)
            continue
        candidate_raw, candidate_value = match
        used.add(normalise_key(candidate_raw))
        record["candidate"] = ("" if candidate_value is None
                               else str(candidate_value))
        record["candidate_attribute"] = candidate_raw
        if rule is None:
            # Still worth comparing as text: an exact match is weak
            # evidence and a difference is worth a human's eye. What it
            # is NOT is a pass, which "unruled" keeps visible.
            same = _text_equal(record["original"], record["candidate"])
            record.update(
                verdict="unruled",
                note=("values match, but no rule says which direction is "
                      "safe for this attribute"
                      if same else
                      "values differ, and no rule says whether that "
                      "matters for this attribute"))
            attributes.append(record)
            continue
        verdict, note = _compare_one(rule, record["original"],
                                     record["candidate"], rel_tol)
        record.update(verdict=verdict, note=note)
        attributes.append(record)

    candidate_only = [
        {"attribute": raw, "candidate": "" if value is None else str(value)}
        for normalised, (raw, value) in sorted(by_key.items())
        if normalised not in used
    ]

    return {
        "attributes": attributes,
        "blocking": [a["attribute"] for a in attributes
                     if a["verdict"] in _BLOCKING],
        "unreviewed": [a["attribute"] for a in attributes
                       if a["verdict"] in _UNREVIEWED],
        "better": [a["attribute"] for a in attributes
                   if a["verdict"] == "better"],
        "same": [a["attribute"] for a in attributes
                 if a["verdict"] == "same"],
        "candidate_only": candidate_only,
        "compared": len(attributes),
    }


def summarise(comparison: dict[str, Any]) -> str:
    """One line a human can read, which still refuses to conclude.

    Worth having as its own function so every caller words the limit
    the same way, rather than each one inventing a phrasing that sounds
    more certain than the last.
    """
    blocking = len(comparison.get("blocking") or [])
    unreviewed = len(comparison.get("unreviewed") or [])
    same = len(comparison.get("same") or []) + len(
        comparison.get("better") or [])
    parts = [f"{same} of {comparison.get('compared', 0)} attributes match "
             f"or improve"]
    if blocking:
        parts.append(f"{blocking} block")
    if unreviewed:
        parts.append(f"{unreviewed} unchecked")
    return ("; ".join(parts)
            + ". This is evidence, not an equivalence: no catalogue states "
              "the parameter that usually decides a substitution.")
