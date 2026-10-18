# SPDX-License-Identifier: Apache-2.0
"""Parametric comparison: the evidence, and the things it refuses to say.

Most of these are about a WRONG answer being worse than no answer.
A substitution tool that says "equivalent" when it compared 16 V
against 16 A, or that reads 4.7k as 4.7 kelvin, or that calls a
narrower temperature range a pass, is more dangerous than one that
says nothing -- the engineer stops checking either way, and only one of
those is safe to stop checking.
"""
from __future__ import annotations

import pytest

from eda_agent.engineering.part_match import (
    AT_LEAST,
    AT_MOST,
    CLOSE,
    CONTAINS,
    EQUAL,
    canonical_for,
    compare_parameters,
    normalise_key,
    parse_quantity,
    parse_range,
    rule_for,
    summarise,
)


# ---- parsing one value -------------------------------------------------

@pytest.mark.parametrize("text,value,unit", [
    ("100 nF", 1e-7, "F"),
    ("0.1uF", 1e-7, "F"),
    ("100nF", 1e-7, "F"),
    ("16 V", 16.0, "V"),
    ("50V", 50.0, "V"),
    ("100 mA", 0.1, "A"),
    ("10 kHz", 1e4, "Hz"),
    ("1 MHz", 1e6, "Hz"),
    ("2.2 uH", 2.2e-6, "H"),
    ("10%", 10.0, "%"),
    ("470", 470.0, ""),
    ("1e3 V", 1000.0, "V"),
])
def test_a_value_parses_to_base_si_with_its_unit(text, value, unit):
    q = parse_quantity(text)
    assert q is not None, text
    assert q.value == pytest.approx(value)
    assert q.unit == unit


def test_the_micro_sign_and_the_greek_mu_both_mean_micro():
    """Catalogues emit either, and they are different code points."""
    assert parse_quantity("4.7µF").value == pytest.approx(4.7e-6)
    assert parse_quantity("4.7μF").value == pytest.approx(4.7e-6)


def test_both_omega_characters_mean_ohm():
    """GREEK CAPITAL LETTER OMEGA is what keyboards emit; OHM SIGN is
    kept by Unicode for compatibility. Both appear in catalogue text."""
    for omega in ("Ω", "Ω"):
        q = parse_quantity(f"4.7k{omega}")
        assert (q.value, q.unit) == (4700.0, "ohm"), omega


def test_a_plus_minus_sign_is_not_part_of_the_number():
    assert parse_quantity("±10%").value == 10.0
    assert parse_quantity("±5%").value == 5.0


def test_kilo_is_not_kelvin():
    """The collision that case-insensitive unit matching gets wrong, and
    it is a factor of 1000 on every resistor in the design."""
    assert parse_quantity("4.7k") == (4700.0, "", "4.7k")
    assert parse_quantity("10 K") == (10.0, "K", "10 K")


def test_mega_is_not_milli():
    assert parse_quantity("1 MHz").value == 1e6
    assert parse_quantity("1 mHz").value == pytest.approx(1e-3)


def test_a_package_code_is_not_a_measurement():
    """"0603 (1608 Metric)" would otherwise read as 603 of something
    called "(1608 Metric)", which compares numerically against 805."""
    assert parse_quantity("0603 (1608 Metric)") is None
    assert parse_quantity("TO-220-3") is None


def test_a_qualified_unit_stays_qualified():
    """50 V DC and 50 V AC are different ratings. Folding the qualifier
    away to compare the numbers is the worse error, so the qualifier
    stays in the unit and an AC/DC pair reports as not comparable."""
    dc, ac = parse_quantity("50 V DC"), parse_quantity("50 V AC")
    assert dc.value == ac.value == 50.0
    assert dc.unit != ac.unit


@pytest.mark.parametrize("text", ["X7R", "", "   ", "Automotive", None, []])
def test_an_unreadable_value_is_none_rather_than_a_guess(text):
    assert parse_quantity(text) is None


# ---- parsing a range ---------------------------------------------------

@pytest.mark.parametrize("text,low,high,unit", [
    ("4.5 V ~ 16 V", 4.5, 16.0, "V"),
    ("-55°C ~ 125°C", -55.0, 125.0, "degc"),
    ("-40 to 85°C", -40.0, 85.0, "degc"),
    ("-40 ~ 125", -40.0, 125.0, ""),
    ("1.8 V \u2013 5.5 V", 1.8, 5.5, "V"),   # en dash, written as an escape
])
def test_a_range_parses_both_ends(text, low, high, unit):
    r = parse_range(text)
    assert r is not None, text
    assert (r.low, r.high, r.unit) == (pytest.approx(low),
                                       pytest.approx(high), unit)


def test_a_hyphen_does_not_split_a_range():
    """"-55" is a negative number and "-55 ~ 125" is a range, and a
    splitter that treats "-" as a separator cannot tell them apart. So
    it is not one, and a hyphenated range reads as unparseable rather
    than as the wrong two numbers."""
    assert parse_range("-55 - 125") is None
    assert parse_quantity("-55").value == -55.0


def test_a_range_with_two_different_units_is_refused():
    assert parse_range("4.5 V ~ 16 A") is None


def test_an_elided_prefix_inside_a_range_is_refused():
    """"100 ~ 200 nF" might mean 100 nF or 100 F, and the string does
    not say. A unit elided on the low end is safe to carry over, a
    PREFIX is not."""
    assert parse_range("100 ~ 200 nF") is None
    # The unit alone, with no prefix in play, does carry over.
    assert parse_range("-40 ~ 85°C").low == -40.0


def test_a_reversed_range_is_ordered_rather_than_rejected():
    assert parse_range("125 ~ -55").low == -55.0


@pytest.mark.parametrize("text", ["16 V", "Up to 16 V", "", "X7R"])
def test_a_non_range_is_none(text):
    assert parse_range(text) is None


# ---- the attribute table -----------------------------------------------

def test_keys_fold_across_the_catalogues_that_wrote_them():
    assert normalise_key("Voltage - Rated") == "voltagerated"
    assert normalise_key("voltagerating_dc") == "voltageratingdc"


@pytest.mark.parametrize("key,rule", [
    ("Capacitance", CLOSE),
    ("Voltage - Rated", AT_LEAST),
    ("voltagerating_dc", AT_LEAST),
    ("Tolerance", AT_MOST),
    ("ESR (Equivalent Series Resistance)", AT_MOST),
    ("Operating Temperature", CONTAINS),
    ("Dielectric", EQUAL),
    ("Package / Case", EQUAL),
    ("Mounting Type", EQUAL),
    ("Power (Watts)", AT_LEAST),
])
def test_each_attribute_gets_the_direction_a_designer_would_give_it(key, rule):
    assert rule_for(key) == rule


def test_an_output_voltage_must_match_rather_than_merely_exceed():
    """The ordering trap in the table: "Output Voltage" contains
    "voltage", and a 5 V regulator standing in for a 3.3 V one is not
    an improvement."""
    assert rule_for("Output Voltage") == CLOSE
    assert rule_for("Voltage - Output") == CLOSE


def test_three_catalogues_spellings_of_one_attribute_pair_up():
    """The reason the table carries spellings at all: without them the
    same attribute from two sources never meets, and every useful
    comparison lands in "unchecked"."""
    names = {"voltagerating_dc", "Voltage - Rated", "Voltage Rating",
             "Rated Voltage"}
    assert {canonical_for(n) for n in names} == {"voltage_rating"}


def test_an_unknown_attribute_gets_no_rule_rather_than_a_default():
    """A default of EQUAL would block harmless differences; a default of
    AT_LEAST would approve dangerous ones. Neither is a safe guess."""
    assert rule_for("Mystery Attribute") is None
    assert canonical_for("Mystery Attribute") is None


def test_an_override_pins_an_attribute_the_table_does_not_know():
    assert rule_for("Inrush Limit", {"inrush limit": AT_LEAST}) == AT_LEAST
    # And wins over the table for one it does.
    assert rule_for("Tolerance", {"Tolerance": EQUAL}) == EQUAL


# ---- comparison --------------------------------------------------------

def _compare(original, candidate, **kw):
    return compare_parameters(original, candidate, **kw)


def test_the_same_value_written_two_ways_is_the_same_value():
    r = _compare({"Capacitance": "100 nF"}, {"Capacitance": "0.1 uF"})
    assert r["attributes"][0]["verdict"] == "same"
    assert r["blocking"] == []


def test_more_voltage_rating_is_better_and_less_is_blocking():
    better = _compare({"Voltage - Rated": "16 V"},
                      {"Voltage - Rated": "50 V"})
    assert better["attributes"][0]["verdict"] == "better"
    worse = _compare({"Voltage - Rated": "50 V"},
                     {"Voltage - Rated": "16 V"})
    assert worse["attributes"][0]["verdict"] == "worse"
    assert worse["blocking"] == ["Voltage - Rated"]


def test_less_esr_is_better_and_more_is_blocking():
    """The direction that is the opposite of the voltage one, which is
    exactly why the table exists rather than a single "bigger is safer"
    rule."""
    better = _compare({"ESR": "100 mohm"}, {"ESR": "50 mohm"})
    assert better["attributes"][0]["verdict"] == "better"
    worse = _compare({"ESR": "50 mohm"}, {"ESR": "100 mohm"})
    assert worse["attributes"][0]["verdict"] == "worse"


def test_a_narrower_temperature_range_blocks():
    r = _compare({"Operating Temperature": "-55°C ~ 125°C"},
                 {"Operating Temperature": "-40°C ~ 85°C"})
    assert r["attributes"][0]["verdict"] == "worse"
    assert r["blocking"] == ["Operating Temperature"]


def test_a_wider_temperature_range_is_better():
    r = _compare({"Operating Temperature": "-40°C ~ 85°C"},
                 {"Operating Temperature": "-55°C ~ 125°C"})
    assert r["attributes"][0]["verdict"] == "better"


def test_a_different_dielectric_blocks():
    """X5R for X7R passes every numeric check and is still the wrong
    part: the capacitance holds over a narrower temperature span."""
    r = _compare({"Dielectric": "X7R"}, {"Dielectric": "X5R"})
    assert r["attributes"][0]["verdict"] == "mismatch"
    assert r["blocking"] == ["Dielectric"]


def test_different_units_are_never_compared():
    """16 V and 16 A are the same number and nothing else."""
    r = _compare({"Current - Rated": "16 V"}, {"Current - Rated": "16 A"})
    assert r["attributes"][0]["verdict"] == "unreadable"
    assert "units differ" in r["attributes"][0]["note"]
    assert r["blocking"] == []
    assert r["unreviewed"] == ["Current - Rated"]


def test_one_side_omitting_the_unit_still_compares():
    """The two values are the same attribute of two parts, so they
    cannot be of different dimensions; one catalogue writing "125"
    where another writes "125 degC" is an elision."""
    r = _compare({"Operating Temperature": "-55°C ~ 125°C"},
                 {"Operating Temperature": "-55 ~ 125"})
    assert r["attributes"][0]["verdict"] == "same"


def test_an_attribute_the_candidate_does_not_publish_is_unreviewed():
    """And NOT a pass. Saying nothing about a rating is not the same as
    meeting it."""
    r = _compare({"Voltage - Rated": "16 V"}, {"Capacitance": "100 nF"})
    record = r["attributes"][0]
    assert record["verdict"] == "missing"
    assert r["unreviewed"] == ["Voltage - Rated"]
    assert r["blocking"] == []


def test_an_attribute_with_no_rule_is_unreviewed_even_when_it_matches():
    r = _compare({"Mystery Spec": "Grade 2"}, {"Mystery Spec": "Grade 2"})
    record = r["attributes"][0]
    assert record["verdict"] == "unruled"
    assert record["attribute"] in r["unreviewed"]
    assert record["attribute"] not in r["same"]


def test_attributes_only_the_candidate_states_are_kept():
    r = _compare({"Capacitance": "100 nF"},
                 {"Capacitance": "100 nF", "Thickness (Max)": "0.87mm"})
    assert [c["attribute"] for c in r["candidate_only"]] == ["Thickness (Max)"]


def test_a_cross_catalogue_comparison_pairs_what_it_should():
    """End to end on the shape the providers actually return: Nexar
    shortnames on one side, Digi-Key labels on the other."""
    r = _compare(
        {"capacitance": "100 nF", "voltagerating_dc": "16 V",
         "tolerance": "10%", "dielectric": "X7R",
         "operatingtemperature": "-55°C ~ 125°C",
         "case_package": "0603 (1608 Metric)"},
        {"Capacitance": "0.1 uF", "Voltage - Rated": "50V",
         "Tolerance": "±5%", "Dielectric": "X5R",
         "Operating Temperature": "-40 ~ 85",
         "Package / Case": "0603 (1608 Metric)"})
    verdicts = {a["attribute"]: a["verdict"] for a in r["attributes"]}
    assert verdicts == {
        "capacitance": "same",
        "voltagerating_dc": "better",
        "tolerance": "better",
        "dielectric": "mismatch",
        "operatingtemperature": "worse",
        "case_package": "same",
    }
    assert r["candidate_only"] == []


def test_the_first_spelling_of_a_duplicated_attribute_wins():
    """Two candidate keys folding to one canonical attribute must not be
    chosen between silently; the loser stays visible in candidate_only."""
    r = _compare({"Voltage - Rated": "16 V"},
                 {"Voltage Rating": "25 V", "Rated Voltage": "50 V"})
    assert len(r["attributes"]) == 1
    assert len(r["candidate_only"]) == 1


def test_the_summary_never_claims_an_equivalence():
    """The one sentence most likely to be pasted into a review, so it
    carries the limit rather than leaving it to a footnote."""
    text = summarise(_compare({"Capacitance": "100 nF"},
                              {"Capacitance": "100 nF"}))
    assert "evidence, not an equivalence" in text
    for word in ("equivalent", "compatible", "drop-in", "safe to"):
        assert word not in text.lower()


@pytest.mark.parametrize("bad", [None, "", 7, [], {"": "x"}])
def test_junk_input_produces_an_empty_comparison_rather_than_raising(bad):
    """Parameter maps come from remote catalogues, so the shape is not
    this code's to assume."""
    result = compare_parameters(bad, bad)
    assert result["attributes"] == []
    assert result["blocking"] == [] and result["unreviewed"] == []


def test_no_table_row_is_shadowed_by_a_more_generic_one_above_it():
    """The table matches substrings, first row winning, so a generic
    spelling placed above a specific one silently claims it.

    This was not hypothetical. "resistance" sat above "esr",
    "dcresistance" and "onresistance", so every one of those was
    compared as CLOSE instead of AT_MOST -- which blocks a substitution
    for having LOWER ESR, exactly backwards. Four rows were wrong at
    once, and nothing but this check distinguishes a correct table from
    a plausible one.
    """
    from eda_agent.engineering.part_match import _ATTRIBUTES

    rows = list(enumerate(_ATTRIBUTES))
    shadowed = [
        f"{generic!r} (row {i}, {name_a}) sits above {specific!r} "
        f"(row {j}, {name_b}) and claims it"
        for i, (name_a, _, patterns_a) in rows
        for j, (name_b, _, patterns_b) in rows
        if name_a != name_b
        for generic in patterns_a
        for specific in patterns_b
        if generic != specific and generic in specific and i < j
    ]
    assert not shadowed, "\n  ".join([""] + shadowed)


def test_every_table_row_is_self_consistent():
    """A row's spellings must all be distinct, and its rule must be one
    of the five. Cheap, and the kind of thing a copy-paste breaks."""
    from eda_agent.engineering.part_match import _ATTRIBUTES

    seen: dict[str, str] = {}
    for canonical, rule, patterns in _ATTRIBUTES:
        assert rule in (AT_LEAST, AT_MOST, CLOSE, CONTAINS, EQUAL), canonical
        assert patterns, canonical
        for pattern in patterns:
            assert pattern == normalise_key(pattern), (
                f"{pattern!r} in {canonical} is not in normalised form, so "
                f"it can never match")
            assert pattern not in seen, (
                f"{pattern!r} is claimed by both {seen[pattern]} and "
                f"{canonical}")
            seen[pattern] = canonical
