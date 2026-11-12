# SPDX-License-Identifier: Apache-2.0
"""The SPICE model index: parsing model files and LTspice symbols, scanning,
searching by part number, and where the index is kept."""
from __future__ import annotations

import json
import os

import pytest

from eda_agent.sim import model_library as lib

VENDOR_LIB = """\
* LT3080 - 1.1A adjustable low dropout regulator
* pins: IN OUT SET VCONTROL
.SUBCKT LT3080 IN OUT SET
+ VCONTROL PARAMS: Iset=10u
R1 IN OUT 1k
.MODEL DX D(Is=1e-14)
.ENDS LT3080
* Fast switching diode
.model 1N4148 D(Is=2.52n Rs=.568 N=1.752)
.model BC847B NPN(Is=1e-14 BF=300)
.subckt opamp_ideal inp inn out ; ideal amplifier
E1 out 0 inp inn 1e6
.ends
"""

ADI_SYMBOL = """\
Version 4
SymbolType CELL
LINE Normal -64 -32 64 -32
SYMATTR Value LT3045
SYMATTR Prefix X
SYMATTR SpiceModel LT3045.sub
SYMATTR Description 500mA ultralow noise LDO
PIN -64 -16 LEFT 8
PINATTR PinName OUT
PINATTR SpiceOrder 2
PIN -64 16 LEFT 8
PINATTR PinName IN
PINATTR SpiceOrder 1
PIN 0 64 BOTTOM 8
PINATTR PinName GND
PINATTR SpiceOrder 3
"""


def test_subckt_header_with_continuation_and_params():
    found = {e.name: e for e in lib.parse_model_text(VENDOR_LIB, "v.lib")}
    lt = found["LT3080"]
    assert lt.kind == "subckt"
    assert lt.nodes == ["IN", "OUT", "SET", "VCONTROL"]
    assert lt.params == ["Iset"]
    assert "regulator" in lt.description


def test_model_cards_inside_a_subckt_are_not_listed_as_library_models():
    names = [e.name for e in lib.parse_model_text(VENDOR_LIB) if e.kind == "model"]
    assert names == ["1N4148", "BC847B"]
    devices = {e.name: e.device for e in lib.parse_model_text(VENDOR_LIB)
               if e.kind == "model"}
    assert devices == {"1N4148": "D", "BC847B": "NPN"}


def test_inline_comment_is_not_a_node():
    found = {e.name: e for e in lib.parse_model_text(VENDOR_LIB)}
    assert found["opamp_ideal"].nodes == ["inp", "inn", "out"]


def test_adi_symbol_puts_the_file_in_spicemodel_and_orders_pins():
    sym = lib.parse_asy(ADI_SYMBOL, "LT3045.asy")
    assert sym.name == "LT3045"
    assert sym.model_file == "LT3045.sub"
    assert sym.prefix == "X"
    assert sym.nodes == ["IN", "OUT", "GND"]          # SpiceOrder, not file order


def test_utf16_files_are_read(tmp_path):
    p = tmp_path / "x.asy"
    p.write_bytes(ADI_SYMBOL.encode("utf-16"))
    assert "SpiceOrder" in lib.read_text(p)


def _tree(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sym").mkdir()
    (tmp_path / "sub" / "vendor.lib").write_text(VENDOR_LIB)
    (tmp_path / "sub" / "LT3045.sub").write_text("encrypted gibberish 0x1234\n")
    (tmp_path / "sym" / "LT3045.asy").write_text(ADI_SYMBOL)
    (tmp_path / "sub" / "notes.pdf").write_text("not a model")
    return tmp_path


def test_scan_indexes_models_and_symbols_and_reuses_unchanged_files(tmp_path):
    root = _tree(tmp_path)
    first = lib.scan([root])
    assert first["stats"]["models"] == 4 and first["stats"]["symbols"] == 1
    assert first["stats"]["read"] == 3                # the .pdf is ignored
    second = lib.scan([root], first)
    assert second["stats"]["read"] == 0 and second["stats"]["reused"] == 3
    (root / "sub" / "vendor.lib").write_text(VENDOR_LIB + ".model X2 D\n")
    os.utime(root / "sub" / "vendor.lib", (1, 1))
    third = lib.scan([root], second)
    assert third["stats"]["read"] == 1


@pytest.mark.parametrize("query,expected", [
    ("LT3080", "LT3080"),
    ("lt3080edd#trpbf", "LT3080"),                    # package and reel tail
    ("LT3080EDD", "LT3080"),
    ("1N4148W-7-F", "1N4148"),
    ("bc847b", "BC847B"),
])
def test_find_by_part_number(tmp_path, query, expected):
    index = lib.scan([_tree(tmp_path)])
    hits = lib.find(index, query)
    assert hits and hits[0]["name"] == expected


def test_find_does_not_match_short_fragments(tmp_path):
    index = lib.scan([_tree(tmp_path)])
    assert lib.find(index, "LT") == []
    assert lib.find(index, "TPS7A47") == []


def test_resolve_an_encrypted_model_through_its_symbol(tmp_path):
    index = lib.scan([_tree(tmp_path)])
    info = lib.resolve(index, "LT3045")
    assert info["definitions"] == []
    assert info["model_file"].endswith("LT3045.sub")
    assert info["symbols"][0]["nodes"] == ["IN", "OUT", "GND"]


def test_index_lives_in_the_design_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".eda-agent").mkdir(parents=True)
    (ws / ".eda-agent" / "workspace.json").write_text("{}")
    (ws / "sub").mkdir()
    monkeypatch.chdir(ws / "sub")
    assert lib.data_dir() == ws / ".eda-agent" / "spice-models"


def test_default_roots_include_workspace_models_and_env(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setenv("EDA_AGENT_SPICE_MODEL_DIRS", str(extra))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nolocal"))
    roots = lib.default_roots(tmp_path)
    assert tmp_path / "models" in roots and extra in roots


def test_save_and_load_round_trip(tmp_path):
    target = tmp_path / "a" / "index.json"
    lib.save_json(target, {"x": 1})
    assert lib.load_json(target, None) == {"x": 1}
    assert not list(target.parent.glob("*.tmp"))
    assert json.loads(target.read_text()) == {"x": 1}


def test_reel_and_lead_free_tails_still_give_an_exact_match(tmp_path):
    index = lib.scan([_tree(tmp_path)])
    assert lib.find(index, "LT3080#PBF")[0]["score"] == 100
    assert lib.find(index, "BC847B-TR")[0]["score"] == 100


def test_a_very_short_model_name_does_not_claim_every_part():
    index = {"version": lib.INDEX_VERSION, "files": {"x.lib": {"entries": [
        {"name": "LT", "kind": "model", "file": "x.lib"},
        {"name": "D", "kind": "model", "file": "x.lib"}]}}}
    assert lib.find(index, "LT3080") == []
    assert lib.find(index, "DMG2305") == []
