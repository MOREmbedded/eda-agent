# SPDX-License-Identifier: Apache-2.0
"""A part's symbol and footprint, drawn before it is placed.

The symbol read (library.get_symbol_geometry) loads the part the way
placement does and leaves nothing on a sheet; the footprint comes from the
library its current footprint model names. These tests hold the drawing,
the choice of footprint, the background reader behind the dashboard, the
offline search beside it, the routes, the MCP tool and the Pascal side's
promises (routed, read-only, a loaded part destroyed again).
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

import pytest

from eda_agent import library_preview as lp
from eda_agent.render.symbol_svg import render_symbol_svg, symbol_bbox, symbol_geometry
from eda_agent.tools import library as library_tools

from tests.pascal_source import load

ROOT = Path(__file__).resolve().parents[1]
_LIB = ROOT / "scripts" / "altium" / "Library.pas"
_BUILT = ROOT / "scripts" / "altium" / "Altium_MCP.pas"

# An op-amp-ish symbol: body, a label, an arc, three pins (one hidden).
SYMBOL = {
    "lib_ref": "OPA_TEST", "designator": "U?", "comment": "OPA_TEST",
    "description": "test amplifier", "source": "workspace Acme", "part": 1,
    "part_count": 2,
    "primitives": [
        {"kind": "rect", "x1": 0, "y1": -200, "x2": 400, "y2": 200, "color": 128,
         "line_width": 1, "area_color": 11599871, "is_solid": True},
        {"kind": "arc", "cx": 200, "cy": 0, "r": 50, "start": 0, "end": 180,
         "color": 128, "line_width": 1},
        {"kind": "text", "name": "", "text": "AMP", "x": 150, "y": 50, "rot": 0, "color": 0},
    ],
    "pins": [
        {"des": "1", "name": "IN+", "x": 0, "y": 100, "rot": 180, "len": 300,
         "electrical": "input", "hidden": False, "part": 1},
        {"des": "2", "name": "OUT", "x": 400, "y": 0, "rot": 0, "len": 200,
         "electrical": "output", "hidden": False, "part": 1},
        {"des": "8", "name": "VCC_HIDDEN", "x": 200, "y": 200, "rot": 90, "len": 900,
         "electrical": "power", "hidden": True, "part": 0},
    ],
    "models": [
        {"model_name": "OLD_FP", "model_type": "PCBLIB", "is_current": False,
         "datafile_links": [{"entity_name": "OLD_FP", "location": r"C:\libs\old.PcbLib"}]},
        {"model_name": "SOIC8", "model_type": "PCBLIB", "is_current": True,
         "datafile_links": [{"entity_name": "SOIC-8_N", "location": r"C:\libs\ic.PcbLib"}]},
        {"model_name": "OPA_SIM", "model_type": "SIM", "is_current": True,
         "datafile_links": []},
    ],
}

PADS = {"pads": [
    {"name": "1", "x_mm": -2.7, "y_mm": 1.9, "w_mm": 1.5, "h_mm": 0.6, "shape": "rectangle"},
    {"name": "2", "x_mm": -2.7, "y_mm": 0.6, "w_mm": 1.5, "h_mm": 0.6, "shape": "rectangle"},
]}
PRIMS = {"library_path": r"C:\libs\ic.PcbLib", "tracks": [
    {"layer": "TopOverlay", "x1": -1.9, "y1": 2.5, "x2": 1.9, "y2": 2.5, "width": 0.15}],
    "arcs": [], "regions": [], "fills": []}


class FakeAltium:
    """Answers the reads a preview makes, and records them."""

    def __init__(self, symbol=None, fail=None, delay=0.0):
        self.symbol = SYMBOL if symbol is None else symbol
        self.fail = fail or {}
        self.delay = delay
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, command, params, timeout):
        self.sent.append((command, dict(params)))
        if self.delay:
            time.sleep(self.delay)
        if command in self.fail:
            raise RuntimeError(self.fail[command])
        return {"library.get_symbol_geometry": self.symbol,
                "library.get_pad_geometry": PADS,
                "library.get_footprint_primitives": PRIMS,
                "library.get_mech_layers": {"layers": []}}[command]


# -- the symbol drawing ------------------------------------------------------------


def test_the_box_reaches_both_ends_of_every_drawn_pin():
    x1, y1, x2, y2 = symbol_bbox(SYMBOL)
    # IN+ runs left from x=0 by 300; OUT right from 400 by 200.
    assert (x1, x2) == (-300, 600)
    # The hidden pin (900 up) does not stretch the frame.
    assert y2 == 200 and y1 == -200
    assert symbol_bbox(SYMBOL, hidden_pins=True)[3] == 1100


def test_an_empty_symbol_has_no_box_and_still_draws():
    assert symbol_bbox({"primitives": [], "pins": []}) is None
    assert render_symbol_svg({"lib_ref": "EMPTY"}).startswith("<svg")


def test_the_symbol_draws_its_graphics_pins_and_text_but_not_hidden_pins():
    svg = render_symbol_svg(SYMBOL)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<title>OPA_TEST</title>" in svg
    assert svg.count('class="pin"') == 2
    assert 'data-pin="1"' in svg and 'data-pin="2"' in svg
    assert "VCC_HIDDEN" not in svg
    assert "prim-rect" in svg and "prim-arc" in svg and ">AMP<" in svg
    assert "pin-glyph-input" in svg and "pin-glyph-output" in svg
    assert ">U?<" in svg


def test_text_rides_as_symbol_text_not_as_a_graphic():
    geom = symbol_geometry(SYMBOL)
    comp = geom["components"][0]
    assert [p["kind"] for p in comp["primitives"]] == ["rect", "arc"]
    assert [t["text"] for t in comp["params"]] == ["AMP"]
    assert {p["comp"] for p in geom["pins"]} == {"U?"}


def test_markup_in_names_is_escaped():
    sym = dict(SYMBOL, lib_ref="A<B>", pins=[dict(SYMBOL["pins"][0], name="<script>")])
    svg = render_symbol_svg(sym)
    assert "<script>" not in svg and "&lt;script&gt;" in svg


# -- which footprint ---------------------------------------------------------------


def test_the_current_footprint_model_and_its_library_are_chosen():
    assert lp.footprint_link(SYMBOL["models"]) == {
        "name": "SOIC-8_N", "library": r"C:\libs\ic.PcbLib", "model": "SOIC8"}


def test_with_no_current_model_the_first_footprint_is_taken():
    models = [dict(m, is_current=False) for m in SYMBOL["models"]]
    assert lp.footprint_link(models)["name"] == "OLD_FP"
    assert lp.footprint_link([]) == {"name": "", "library": "", "model": ""}


# -- the preview -------------------------------------------------------------------


def test_a_workspace_part_is_read_once_and_its_footprint_from_its_library():
    alt = FakeAltium()
    got = lp.preview_part(alt, design_item_id="CMP-0001", vault="Acme", part=2)
    assert got["ok"] is True and got["lib_ref"] == "OPA_TEST"
    assert alt.sent[0] == ("library.get_symbol_geometry",
                           {"part": "2", "design_item_id": "CMP-0001", "vault": "Acme"})
    asked = {c: p for c, p in alt.sent}
    assert asked["library.get_pad_geometry"] == {"footprint_name": "SOIC-8_N",
                                                 "library_path": r"C:\libs\ic.PcbLib"}
    assert got["symbol"]["pins"] == 3 and got["symbol"]["hidden_pins"] == 1
    assert got["symbol"]["by_type"] == {"input": 1, "output": 1, "power": 1}
    fp = got["footprint"]
    assert fp["ok"] is True and fp["pads"] == 2 and fp["svg"].startswith("<svg")


def test_a_library_file_part_names_its_library_and_symbol():
    alt = FakeAltium()
    lp.preview_part(alt, library_path=r"C:\libs\ic.SchLib", component_name="OPA_TEST",
                    footprint=False)
    assert alt.sent == [("library.get_symbol_geometry", {
        "part": "1", "component_name": "OPA_TEST", "library_path": r"C:\libs\ic.SchLib"})]


def test_a_footprint_link_with_no_library_file_is_said_and_not_read():
    sym = dict(SYMBOL, models=[{"model_name": "FP", "model_type": "PCBLIB", "is_current": True,
                                "datafile_links": [{"entity_name": "FP", "location": ""}]}])
    alt = FakeAltium(symbol=sym)
    got = lp.preview_part(alt, design_item_id="CMP-1")
    assert got["ok"] is True and got["footprint"]["ok"] is False
    assert "names no library file" in got["footprint"]["reason"]
    assert [c for c, _ in alt.sent] == ["library.get_symbol_geometry"]


def test_a_footprint_in_an_intlib_is_not_handed_to_altium():
    sym = dict(SYMBOL, models=[{"model_name": "FP", "model_type": "PCBLIB", "is_current": True,
                                "datafile_links": [{"entity_name": "FP",
                                                    "location": r"C:\libs\all.IntLib"}]}])
    alt = FakeAltium(symbol=sym)
    got = lp.preview_part(alt, design_item_id="CMP-1")
    assert got["footprint"]["ok"] is False
    assert [c for c, _ in alt.sent] == ["library.get_symbol_geometry"]


def test_a_part_with_no_footprint_says_so():
    got = lp.preview_part(FakeAltium(symbol=dict(SYMBOL, models=[])), design_item_id="X")
    assert got["footprint"] == {"ok": False, "name": "", "library": "",
                                "reason": "the part has no footprint model"}


def test_a_symbol_that_does_not_read_fails_the_preview():
    got = lp.preview_part(FakeAltium(fail={"library.get_symbol_geometry": "NOT_FOUND_IN_VAULT"}),
                          design_item_id="CMP-404")
    assert got == {"ok": False, "reason": "the symbol did not read: NOT_FOUND_IN_VAULT"}


def test_a_footprint_that_does_not_read_keeps_the_symbol():
    got = lp.preview_part(FakeAltium(fail={"library.get_pad_geometry": "no such footprint"}),
                          design_item_id="CMP-1")
    assert got["ok"] is True and got["symbol"]["svg"].startswith("<svg")
    assert got["footprint"]["reason"] == "the footprint did not read: no such footprint"


def test_an_empty_symbol_read_is_noted():
    got = lp.preview_part(FakeAltium(symbol={"lib_ref": "E", "models": []}), design_item_id="E")
    assert "the symbol read back with no graphics and no pins" in got["notes"]


# -- the background reader -----------------------------------------------------------


def _wait(view, timeout=5.0):
    end = time.time() + timeout
    while view.snapshot().get("state") == "reading" and time.time() < end:
        time.sleep(0.01)
    return view.snapshot()


def test_a_preview_is_read_in_the_background_and_kept():
    alt = FakeAltium()
    view = lp.PreviewView(send=alt)
    assert view.ask({"design_item_id": "CMP-1"})["ok"] is True
    snap = _wait(view)
    assert snap["state"] == "ready" and snap["lib_ref"] == "OPA_TEST"
    reads = len(alt.sent)
    again = view.ask({"design_item_id": "CMP-1"})
    assert again["cached"] is True and len(alt.sent) == reads
    view.ask({"design_item_id": "CMP-1", "again": True})
    _wait(view)
    assert len(alt.sent) > reads


def test_another_part_part_is_another_read():
    alt = FakeAltium()
    view = lp.PreviewView(send=alt)
    view.ask({"design_item_id": "CMP-1"})
    _wait(view)
    view.ask({"design_item_id": "CMP-1", "part": 2})
    assert _wait(view)["key"].endswith(":2")
    assert sum(1 for c, _ in alt.sent if c == "library.get_symbol_geometry") == 2


def test_one_read_at_a_time_and_a_part_must_be_named():
    view = lp.PreviewView(send=FakeAltium(delay=0.3))
    assert view.ask({})["ok"] is False
    assert view.ask({"library_path": r"C:\x.SchLib"})["ok"] is False
    assert view.ask({"design_item_id": "A"})["ok"] is True
    busy = view.ask({"design_item_id": "B"})
    assert busy == {"ok": False, "reason": "already reading a part"}
    _wait(view)


def test_a_failed_read_is_shown_and_not_kept():
    alt = FakeAltium(fail={"library.get_symbol_geometry": "boom"})
    view = lp.PreviewView(send=alt)
    view.ask({"design_item_id": "CMP-9"})
    snap = _wait(view)
    assert snap["state"] == "failed" and "boom" in snap["reason"]
    view.ask({"design_item_id": "CMP-9"})
    _wait(view)
    assert sum(1 for c, _ in alt.sent if c == "library.get_symbol_geometry") == 2


def test_only_the_last_few_previews_are_kept():
    view = lp.PreviewView(send=FakeAltium())
    view.KEEP = 2
    for name in ("A", "B", "C"):
        view.ask({"design_item_id": name})
        _wait(view)
    assert [k.split(":")[2] for k in view._done] == ["B", "C"]


# -- finding a part offline ------------------------------------------------------------


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "library" / "acme.365.altium.com"
    db.mkdir(parents=True)
    (db / "components.json").write_text(json.dumps([
        {"name": "CMP-00012-3", "comment": "10k", "description": "Resistor 0402",
         "folder": "Components/Resistors", "lifecycle": "Released",
         "symbols": ["SYM-1"], "footprints": ["PCC-0402"],
         "manufacturer_parts": [{"manufacturer": "Yageo", "mpn": "RC0402FR-0710KL"}]},
        {"name": "CMP-00013-1", "comment": "100n", "description": "Capacitor 0402",
         "folder": "Components/Capacitors", "lifecycle": "Draft",
         "symbols": ["SYM-2"], "footprints": ["PCC-C0402"], "manufacturer_parts": []},
    ]), "utf-8")
    (db / "meta.json").write_text(json.dumps(
        {"workspace_url": "https://acme.365.altium.com"}), "utf-8")
    return db


def test_parts_are_found_offline_by_value_description_and_mpn(tmp_path):
    db = _db(tmp_path)
    for q in ("10k", "resistor", "RC0402FR"):
        got = lp.find_parts(q, databases=[db])
        assert [r["name"] for r in got["results"]] == ["CMP-00012-3"], q
    row = lp.find_parts("10k", databases=[db])["results"][0]
    assert row["item"] == "CMP-00012"
    assert row["footprints"] == ["PCC-0402"] and row["mpns"] == ["RC0402FR-0710KL"]
    assert row["workspace"] == "https://acme.365.altium.com"


def test_a_search_needs_two_letters_and_a_scanned_library(tmp_path):
    assert lp.find_parts("1", databases=[_db(tmp_path)])["results"] == []
    none = lp.find_parts("10k", databases=[])
    assert none["results"] == [] and "no Altium 365 library" in none["note"]


# -- the dashboard routes ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)
    from eda_agent.web.dashboard import create_app
    app = create_app(workspace_dir=tmp_path / "ws")
    app.config["TESTING"] = True
    return app.test_client()


def test_the_routes_find_ask_and_show(client, tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr("eda_agent.altium365_library.known_databases", lambda *a: [db])
    found = client.get("/api/wb/parts/find?q=100n").get_json()
    assert [r["item"] for r in found["results"]] == ["CMP-00013"]

    view = lp.PreviewView(send=FakeAltium())
    monkeypatch.setattr(lp, "_VIEW", view)
    assert client.get("/api/wb/parts/preview").get_json()["state"] == "empty"
    asked = client.post("/api/wb/parts/preview", json={"design_item_id": "CMP-00013"})
    assert asked.get_json()["ok"] is True
    _wait(view)
    shown = client.get("/api/wb/parts/preview").get_json()
    assert shown["state"] == "ready" and shown["symbol"]["svg"].startswith("<svg")


def test_the_page_has_the_lookup_view_and_asks_the_routes():
    page = (ROOT / "src" / "eda_agent" / "web" / "dashboard_static" / "workbench.html").read_text("utf-8")
    assert 'data-libview="find"' in page
    assert '"/api/wb/parts/find?q="' in page and '"/api/wb/parts/preview"' in page
    # The library read is not started for a person who only looks parts up.
    assert 'S.libView !== "find") pollLibrary(true)' in page


# -- the MCP tool --------------------------------------------------------------------------


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Bridge:
    def __init__(self, alt):
        self.alt = alt

    def send_command(self, command, params=None, timeout=None):
        return self.alt(command, params or {}, timeout)


def _tool(monkeypatch, alt):
    monkeypatch.setattr(library_tools, "get_bridge", lambda: _Bridge(alt))
    mcp = _Mcp()
    library_tools.register_library_tools(mcp)
    return mcp.tools["lib_preview_part"]


def test_the_tool_writes_both_drawings(monkeypatch, tmp_path):
    tool = _tool(monkeypatch, FakeAltium())
    got = asyncio.run(tool(design_item_id="CMP-1", out_dir=str(tmp_path)))
    assert got["ok"] is True
    sym = Path(got["symbol"]["svg_path"])
    fp = Path(got["footprint"]["svg_path"])
    assert sym.name == "OPA_TEST_symbol.svg" and fp.name == "OPA_TEST_footprint.svg"
    assert sym.read_text("utf-8").startswith("<svg") and fp.read_text("utf-8").startswith("<svg")
    assert "svg" not in got["symbol"] and "svg" not in got["footprint"]


def test_the_tool_refuses_without_a_part(monkeypatch):
    alt = FakeAltium()
    got = asyncio.run(_tool(monkeypatch, alt)(component_name="X"))
    assert got["ok"] is False and not alt.sent


# -- the Pascal side ------------------------------------------------------------------------


def _fn(name: str) -> str:
    return load(_LIB)[name]


def test_the_read_is_routed_and_keeps_the_focus():
    src = _LIB.read_text(encoding="latin-1")
    assert "'get_symbol_geometry':  Result := Lib_GetSymbolGeometry(" in src
    assert "'get_symbol_geometry'" in _fn("LibActionIsReadOnly")


def test_a_loaded_part_is_destroyed_and_nothing_is_placed():
    body = _fn("Lib_GetSymbolGeometry")
    assert "SchServer.LoadComponent(" in body
    # An .IntLib never reaches Altium: it raises a modal nothing catches.
    assert "LoadComponentFromLibrary(" not in body
    assert "LibPath := Resolved" in body and "ResolveSchLibForLoad(LibPath" in body
    finally_part = body[body.rindex("Finally"):]
    assert "If Owned Then" in finally_part and "DestroySchObject(Comp)" in finally_part
    for write in ("AddSchObject", "SchBeginEdit", "MarkDocDirty", "MarkLibDirty"):
        assert write not in body, write


def test_a_part_from_the_open_library_is_not_destroyed():
    body = _fn("Lib_GetSymbolGeometry")
    lookup = body[body.index("LookupLibComponent(SchLib, CompName)") - 400:
                  body.index("LookupLibComponent(SchLib, CompName)")]
    # The .SchLib branch: a component of a document is never Owned.
    assert "Owned := True" not in lookup


def test_only_the_part_asked_for_and_the_normal_display_mode_are_drawn():
    body = _fn("OnSymbolPart")
    assert "(Owner = 0) Or (Owner = Part)" in body and "(Mode = 0)" in body


def test_the_graphics_reader_dispatches_flat_on_the_object_kind():
    body = _fn("SymbolPrimJson")
    assert "Else If" not in body
    for kind in ("eRectangle", "eRoundRectangle", "eLine", "eArc", "eEllipticalArc",
                 "ePolyline", "ePolygon", "eEllipse", "eBezier", "eLabel"):
        assert re.search(rf"Kind = {kind}\b", body), kind


def test_the_built_script_carries_the_new_read():
    built = _BUILT.read_text(encoding="latin-1")
    for name in ("Function SymbolPrimJson(", "Function OnSymbolPart(",
                 "Function Lib_GetSymbolGeometry("):
        assert built.count(name) == 1, name
