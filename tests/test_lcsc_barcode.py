# SPDX-License-Identifier: Apache-2.0
"""LCSC reel/bag label decoding.

The sample below is a real decoded payload, not an invented one; the
awkward parts of it (an empty value, keys nobody has documented) are
exactly what a hand-written parser gets wrong.
"""
from __future__ import annotations

from eda_agent.libimport.lcsc_barcode import parse_lcsc_barcode

REAL = ("{pbn:PICK2403120102,on:GB2403120671,pc:C916399,pm:JSM2301S,qty:50,"
        "mc:,cc:1,pdi:107160244,hp:0,wc:ZH}")


def test_a_real_label_yields_part_mpn_and_quantity():
    result = parse_lcsc_barcode(REAL)
    assert result["ok"] is True
    assert result["lcsc_part"] == "C916399"
    assert result["mpn"] == "JSM2301S"
    assert result["quantity"] == 50
    assert result["warnings"] == []


def test_an_empty_value_is_kept_rather_than_ending_the_parse():
    """A real label carries "mc:" with nothing after it."""
    result = parse_lcsc_barcode(REAL)
    assert result["fields"]["mc"] == ""
    assert result["fields"]["wc"] == "ZH"


def test_undocumented_keys_are_passed_through_without_being_named():
    result = parse_lcsc_barcode(REAL)
    assert result["undocumented_fields"] == [
        "cc", "hp", "mc", "on", "pbn", "pdi", "wc"]
    # They survive verbatim, so a caller who learns what one means can use
    # it without this module having guessed first.
    assert result["fields"]["pbn"] == "PICK2403120102"


def test_a_payload_without_braces_still_parses():
    result = parse_lcsc_barcode("pc:C123,pm:ABC,qty:7")
    assert result["ok"] is True and result["quantity"] == 7


def test_a_truncated_payload_is_refused_rather_than_half_read():
    result = parse_lcsc_barcode("{pc:C123,pm:ABC,qty:")
    assert result["ok"] is False
    assert "truncated" in result["reason"]


def test_empty_input_is_refused():
    assert parse_lcsc_barcode("")["ok"] is False
    assert parse_lcsc_barcode("   ")["ok"] is False


def test_a_segment_that_is_not_key_value_is_refused():
    result = parse_lcsc_barcode("{pc:C123,garbage,qty:5}")
    assert result["ok"] is False and "key:value" in result["reason"]


def test_a_non_numeric_quantity_is_reported_not_guessed():
    result = parse_lcsc_barcode("{pc:C123,qty:many}")
    assert result["ok"] is True
    assert result["quantity"] is None
    assert any("many" in w for w in result["warnings"])


def test_a_missing_quantity_is_none_not_zero():
    """"The label did not say" and "the bag is empty" are different."""
    result = parse_lcsc_barcode("{pc:C123,pm:ABC}")
    assert result["quantity"] is None


def test_a_part_number_not_starting_with_c_is_flagged():
    result = parse_lcsc_barcode("{pc:X999,qty:1}")
    assert result["ok"] is True
    assert any("does not start with 'C'" in w for w in result["warnings"])


def test_a_payload_with_no_pc_field_is_flagged_as_probably_not_lcsc():
    result = parse_lcsc_barcode("{foo:bar}")
    assert result["ok"] is True
    assert any("may not be an LCSC label" in w for w in result["warnings"])


def test_whitespace_around_values_is_trimmed():
    result = parse_lcsc_barcode("{ pc : C123 , qty : 10 }")
    assert result["lcsc_part"] == "C123" and result["quantity"] == 10
