# SPDX-License-Identifier: Apache-2.0
"""Headless .PcbLib reader.

Two layers, deliberately. The byte-level tests carry stream contents
captured verbatim from a library Altium itself wrote, so the parsing
rules stay covered on a fresh clone where the binary fixture is absent
(it is local-only, like the other Altium binaries: see conftest's
_LOCAL_ONLY_FIXTURES). The end-to-end test uses that fixture and skips
automatically without it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eda_agent.fileio.altium_pcblib import (
    _as_int,
    _fields,
    _length_prefixed_text,
    read_pcblib_footprints,
    read_pcblib_header,
)

LIB = Path(__file__).resolve().parent / "integration" / "fixtures" / "EDAAgentTest.PcbLib"

# Captured from a real .PcbLib, byte for byte.
REAL_PARAMETERS = (b'I\x00\x00\x00|PATTERN=PCBComponent_1|HEIGHT=0mil'
                   b'|DESCRIPTION=|ITEMGUID=|REVISIONGUID=\x00')
REAL_TOC = (b'8\x00\x00\x00Name=PCBComponent_1|Pad Count=2|Height=0'
            b'|Description=\r\n\x00')


def test_length_prefixed_text_strips_the_prefix_and_trailing_nul():
    assert _length_prefixed_text(REAL_PARAMETERS).startswith("|PATTERN=")
    assert "\x00" not in _length_prefixed_text(REAL_PARAMETERS)


def test_length_prefixed_text_tolerates_a_truncated_stream():
    assert _length_prefixed_text(b"") == ""
    assert _length_prefixed_text(b"\x02\x00") == ""


def test_parameters_stream_parses_into_its_fields():
    fields = _fields(_length_prefixed_text(REAL_PARAMETERS))
    assert fields["PATTERN"] == "PCBComponent_1"
    assert fields["HEIGHT"] == "0mil"
    # An empty DESCRIPTION is a value, not a missing key: a hygiene check
    # needs to tell "described as nothing" from "no such field".
    assert fields["DESCRIPTION"] == ""
    assert "REVISIONGUID" in fields


def test_toc_keys_with_spaces_survive():
    """"Pad Count" is one key, not two."""
    fields = _fields(_length_prefixed_text(REAL_TOC))
    assert fields["Name"] == "PCBComponent_1"
    assert fields["Pad Count"] == "2"


def test_as_int_reports_unparseable_as_none():
    assert _as_int("2") == 2
    assert _as_int(" 7 ") == 7
    assert _as_int("") is None
    assert _as_int("many") is None


def test_a_file_that_is_not_ole_is_refused(tmp_path):
    junk = tmp_path / "x.PcbLib"
    junk.write_bytes(b"not ole")
    with pytest.raises(ValueError):
        read_pcblib_footprints(junk)
    with pytest.raises(ValueError):
        read_pcblib_header(junk)


def test_reads_footprint_headers_from_a_real_library():
    footprints = read_pcblib_footprints(LIB)
    assert footprints, "the fixture has at least one footprint"
    by_name = {f["name"]: f for f in footprints}
    fp = by_name["PCBComponent_1"]
    assert fp["pattern"] == "PCBComponent_1"
    assert fp["pad_count"] == 2
    assert fp["height"] == "0mil"
    # Library/ and FileVersionInfo/ also hold a Data stream and are not
    # footprints; a reader that enumerated them blindly would invent two.
    assert "Library" not in by_name and "FileVersionInfo" not in by_name


def test_reads_the_library_format_string():
    assert read_pcblib_header(LIB) == "PCB 6.0 Binary Library File"
