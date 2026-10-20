# SPDX-License-Identifier: Apache-2.0
"""Library file tools that need no Altium: embedded STEP models, LibPkg
packages, per-library backups, and the footprint preview and compare
built on the live reads."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from eda_agent.design.footprint_compare import compare_footprints
from eda_agent.fileio import altium_pcblib
from eda_agent.fileio.altium_libpkg import render_libpkg, write_libpkg
from eda_agent.library_backup import backup_library, list_backups, restore_backup
from eda_agent.render.footprint_svg import layer_kind, render_footprint_svg


# --- embedded STEP models ---------------------------------------------------

def _record(fields: str) -> bytes:
    payload = fields.encode("latin-1") + b"\x00"
    return struct.pack("<I", len(payload)) + payload


class _FakeOle:
    """The three Library/Models streams of a two-model PcbLib."""

    def __init__(self, streams: dict[tuple, bytes]):
        self.streams = streams

    def openstream(self, path):
        from io import BytesIO
        key = tuple(path)
        if key not in self.streams:
            raise OSError(path)
        return BytesIO(self.streams[key])

    def exists(self, path):
        return tuple(path) in self.streams

    def close(self):
        pass


def _two_models() -> _FakeOle:
    step = b"ISO-10303-21;\r\nHEADER;\r\nEND-ISO-10303-21;\r\n"
    data = (_record("EMBED=TRUE|MODELSOURCE=Undefined|ID={A}|ROTX=0.000|ROTY=0.000|"
                    "ROTZ=90.000|DZ=161417|CHECKSUM=1|NAME=0603.stp")
            + _record("EMBED=TRUE|ID={B}|ROTX=90.000|DZ=0|NAME=body/odd:name"))
    return _FakeOle({("Library", "Models", "Data"): data,
                     ("Library", "Models", "Header"): struct.pack("<I", 2),
                     ("Library", "Models", "0"): zlib.compress(step),
                     ("Library", "Models", "1"): b"not zlib"})


def test_model_records_are_read_in_stream_order():
    models = altium_pcblib._models(_two_models())
    assert [m["name"] for m in models] == ["0603.stp", "body/odd:name"]
    assert models[0]["rotation_deg"] == [0.0, 0.0, 90.0]
    # DZ is 1/10000 mil: 161417 -> 16.14 mil -> 0.41 mm.
    assert models[0]["dz_mm"] == pytest.approx(0.41, abs=1e-3)
    assert models[0]["embedded"] and models[0]["stream"]


def test_extraction_writes_step_and_reports_a_bad_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(altium_pcblib, "_open_pcblib", lambda path: _two_models())
    r = altium_pcblib.extract_pcblib_models("x.PcbLib", tmp_path)
    (w,) = r["written"]
    assert Path(w["path"]).name == "0603.stp" and w["is_step"]
    assert Path(w["path"]).read_bytes().startswith(b"ISO-10303-21")
    assert r["failed"][0]["name"] == "body/odd:name"
    # A second run leaves the file alone unless told to overwrite.
    again = altium_pcblib.extract_pcblib_models("x.PcbLib", tmp_path)
    assert again["skipped"] and not again["written"]


def test_extraction_by_name_reports_names_it_did_not_find(tmp_path, monkeypatch):
    monkeypatch.setattr(altium_pcblib, "_open_pcblib", lambda path: _two_models())
    r = altium_pcblib.extract_pcblib_models("x.PcbLib", tmp_path, names=["0603.STP", "nope"])
    assert len(r["written"]) == 1 and r["not_found"] == ["nope"]


def test_unsafe_model_names_become_safe_filenames():
    assert altium_pcblib._safe_filename("body/odd:name", 1) == "body_odd_name.step"
    assert altium_pcblib._safe_filename("", 3) == "model_3.step"


# --- LibPkg -----------------------------------------------------------------

def test_libpkg_lists_libraries_relative_to_the_package(tmp_path):
    (tmp_path / "lib").mkdir()
    sch = tmp_path / "lib" / "Parts.SchLib"
    pcb = tmp_path / "lib" / "Parts.PcbLib"
    sch.write_bytes(b"x")
    pcb.write_bytes(b"x")
    pkg = tmp_path / "Parts.LibPkg"
    out = write_libpkg(pkg, [sch, pcb])
    raw = pkg.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    text = raw[3:].decode("utf-8")
    assert text.startswith("[Design]\r\nVersion=1.0\r\n")
    assert "[Document1]\r\nDocumentPath=lib\\Parts.SchLib\r\n" in text
    assert "[Document2]\r\nDocumentPath=lib\\Parts.PcbLib\r\n" in text
    assert out["documents"] == ["lib\\Parts.SchLib", "lib\\Parts.PcbLib"]


def test_libpkg_refuses_what_it_cannot_package(tmp_path):
    lib = tmp_path / "A.SchLib"
    lib.write_bytes(b"x")
    with pytest.raises(ValueError, match="not a .LibPkg"):
        write_libpkg(tmp_path / "A.PrjPcb", [lib])
    with pytest.raises(ValueError, match="no such file"):
        write_libpkg(tmp_path / "A.LibPkg", [tmp_path / "B.PcbLib"])
    with pytest.raises(ValueError, match="listed twice"):
        write_libpkg(tmp_path / "A.LibPkg", [lib, lib])
    with pytest.raises(ValueError, match="not a library"):
        write_libpkg(tmp_path / "A.LibPkg", [tmp_path / "A.SchDoc"])
    write_libpkg(tmp_path / "A.LibPkg", [lib])
    with pytest.raises(ValueError, match="overwrite"):
        write_libpkg(tmp_path / "A.LibPkg", [lib])


def test_libpkg_numbers_documents_from_one(tmp_path):
    text = render_libpkg(tmp_path / "P.LibPkg", [tmp_path / "a.SchLib"])
    assert "[Document1]" in text and "[Document0]" not in text


# --- backups ----------------------------------------------------------------

def test_backup_restore_round_trip_and_restore_is_undoable(tmp_path):
    ws = tmp_path / "ws"
    lib = tmp_path / "Parts.PcbLib"
    lib.write_bytes(b"version 1")
    first = backup_library(lib, ws, label="before edit")
    assert not first["deduplicated"]
    assert backup_library(lib, ws)["deduplicated"]  # same bytes: stored once
    lib.write_bytes(b"version 2")
    backup_library(lib, ws)
    assert [b["bytes"] for b in list_backups(ws, lib)] == [9, 9]

    r = restore_backup(first["backup_path"], ws)
    assert lib.read_bytes() == b"version 1"
    assert r["previous"]["deduplicated"]  # version 2 was already backed up
    assert len(list_backups(ws)) == 2


def test_restore_refuses_a_file_that_is_not_a_backup(tmp_path):
    stray = tmp_path / "x.PcbLib"
    stray.write_bytes(b"x")
    with pytest.raises(ValueError, match="not a library backup"):
        restore_backup(stray, tmp_path / "ws")


def test_restore_refuses_a_backup_whose_bytes_changed(tmp_path):
    ws = tmp_path / "ws"
    lib = tmp_path / "Parts.SchLib"
    lib.write_bytes(b"good")
    b = backup_library(lib, ws)
    Path(b["backup_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        restore_backup(b["backup_path"], ws)
    assert lib.read_bytes() == b"good"


def test_backup_refuses_a_file_that_is_not_a_library(tmp_path):
    doc = tmp_path / "board.PcbDoc"
    doc.write_bytes(b"x")
    with pytest.raises(ValueError, match="not a library"):
        backup_library(doc, tmp_path / "ws")


# --- footprint preview and compare -------------------------------------------

def _pads(**over) -> dict:
    p1 = {"name": "1", "x_mm": -0.75, "y_mm": 0, "w_mm": 0.9, "h_mm": 0.95,
          "shape": "Rectangular", "corner_pct": 0, "rotation": 0, "hole_mm": 0,
          "layer": "Top Layer"}
    p2 = dict(p1, name="2", x_mm=0.75, shape="RoundedRectangle", corner_pct=25)
    p2.update(over)
    return {"name": "R0603", "pads": [p1, p2]}


def _prims(**over) -> dict:
    out = {"name": "R0603", "height_mm": 0.5,
           "tracks": [{"layer": "Top Overlay", "x1": -1.5, "y1": 0.7, "x2": 1.5,
                       "y2": 0.7, "width": 0.12}],
           "arcs": [{"layer": "Top Overlay", "xc": -1.6, "yc": 0, "r": 0.1,
                     "start": 0, "end": 360, "width": 0.1}],
           "regions": [{"layer": "Mechanical 15", "x1": -1.7, "y1": -0.8,
                        "x2": 1.7, "y2": 0.8}],
           "fills": [], "texts": [], "vias": []}
    out.update(over)
    return out


def test_layers_are_classified_by_name():
    assert layer_kind("Top Layer") == "copper"
    assert layer_kind("MultiLayer") == "copper"
    assert layer_kind("Bottom Layer") == "bottom_copper"
    assert layer_kind("Top Overlay") == "overlay"
    assert layer_kind("Top Courtyard") == "courtyard"
    assert layer_kind("Mechanical 13") == "mechanical"
    assert layer_kind("Top Paste") == "paste"


def test_render_draws_every_pad_and_reports_the_extent():
    svg, summary = render_footprint_svg(_pads(), _prims())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count('class="pad"') == 2
    assert 'data-name="1"' in svg and 'rx="0.1125"' in svg  # 25% of 0.45
    assert summary["pads"] == 2 and summary["regions_as_boxes"] == 1
    assert summary["by_layer"]["Top Overlay"] == 2
    assert summary["bbox_mm"]["x1"] == pytest.approx(-1.7)
    assert summary["bbox_mm"]["x2"] == pytest.approx(1.7)


def test_render_punches_holes_for_through_hole_pads():
    pads = _pads(hole_mm=0.6, layer="Multi-Layer", shape="Round")
    svg, _ = render_footprint_svg(pads, _prims())
    assert 'class="hole"' in svg and 'r="0.3"' in svg


def test_identical_footprints_compare_identical():
    r = compare_footprints(_pads(), _prims(), _pads(), _prims())
    assert r["identical"] and r["pads"]["changed"] == {}


def test_compare_names_each_changed_field_and_primitive_count():
    r = compare_footprints(_pads(), _prims(), _pads(w_mm=1.0, corner_pct=50),
                           _prims(tracks=[], height_mm=0.6))
    assert not r["identical"]
    assert r["pads"]["changed"] == {"2": {"w_mm": [0.9, 1.0], "corner_pct": [25, 50]}}
    assert r["primitives"] == {"Top Overlay/tracks": [1, 0]}
    assert r["height_mm"] == [0.5, 0.6]


def test_compare_ignores_differences_inside_the_tolerance():
    r = compare_footprints(_pads(), _prims(), _pads(x_mm=0.7505), _prims(), tol_mm=0.001)
    assert r["identical"]


def test_compare_lists_pads_on_one_side_only():
    b = _pads()
    b["pads"].append(dict(b["pads"][0], name="EP"))
    r = compare_footprints(_pads(), _prims(), b, _prims())
    assert r["pads"]["only_b"] == ["EP"] and r["pad_counts"] == [2, 3]
