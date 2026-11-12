# SPDX-License-Identifier: Apache-2.0
"""The fab design-rule database and its connectors.

Holds the data to the library's rules (every connector names its pages,
every stored fab has a connector) and the code to its promises: a rule
is the fab's for the build asked about, a missing one says why, an IPC
class is refused rather than invented, and a refresh never rewrites a
figure. Each check below also has a seeded break that must be caught.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import urllib.error
from pathlib import Path

import pytest

from eda_agent.design import fab_connectors as fc
from eda_agent.design import fab_library as fl
from eda_agent.design import fab_rules as fr

_DATA = Path(fl._DATA)


# --- the registry ---------------------------------------------------------

def test_every_stored_fab_has_a_connector_and_every_connector_its_pages():
    src = fc.sources()
    assert len(src) >= 13
    stored = {v["vendor"] for v in fl.list_vendors()}
    with_files = {s["vendor"] for s in src.values() if s["data_file"]}
    assert stored == with_files
    for key, s in src.items():
        assert s["urls"] and all(u.startswith("https://") for u in s["urls"]), key
        assert s["status"] in ("data", "partial", "pending"), key
        if s["data_file"]:
            assert (_DATA / s["data_file"]).is_file(), key
        else:
            assert s["status"] == "pending" and s.get("note"), key


def test_a_fab_marked_data_publishes_the_core_minimums():
    """'data' must mean pcb_rules_plan can be fed: track, gap, drill."""
    for key, s in fc.sources().items():
        if s["status"] != "data":
            continue
        got = fl.vendor(s["vendor"])
        for tier, figs in got["tiers"].items():
            for k in ("min_track", "min_gap"):
                assert any(fl._mils(f) for f in figs.get(k, [])), (key, tier, k)


# --- one build's rules ------------------------------------------------------

def test_rules_follow_the_build():
    two = fr.fab_rules("JLCPCB", copper_layers=2)["rules"]
    four = fr.fab_rules("JLCPCB", copper_layers=4)["rules"]
    heavy = fr.fab_rules("JLCPCB", copper_layers=4, outer_oz=2)["rules"]
    assert two["min_track"]["mils"] == pytest.approx(3.937, abs=1e-3)
    assert four["min_track"]["mils"] == pytest.approx(3.543, abs=1e-3)
    assert heavy["min_track"]["mils"] == pytest.approx(5.906, abs=1e-3)


def test_every_rule_carries_its_quote_and_page():
    for v in fl.list_vendors():
        for tier in v["tiers"]:
            got = fr.fab_rules(v["vendor"], copper_layers=4, tier=tier)
            if not got["ok"]:
                assert "does not list 4" in got["reason"]
                continue
            for key, rule in got["rules"].items():
                assert rule["quote"].strip(), (v["vendor"], tier, key)
                assert rule["url"].startswith("https://")
            for m in got["missing"]:
                assert m["reason"].strip()


def test_a_missing_rule_says_why_and_is_not_filled():
    got = fr.fab_rules("Technoservice", copper_layers=4)
    missing = {m["key"]: m["reason"] for m in got["missing"]}
    assert "min_mask_sliver" in missing and "bridge" in missing["min_mask_sliver"]
    assert "min_mask_sliver" not in got["rules"]


def test_micrometre_figures_convert():
    rules = fr.fab_rules("Multi Circuit Boards", copper_layers=4)["rules"]
    assert rules["min_track"]["mils"] == pytest.approx(100 / 25.4, abs=1e-3)


def test_a_layer_count_the_tier_does_not_list_is_refused():
    got = fr.fab_rules("OSH Park", copper_layers=4, tier="two_layer")
    assert got["ok"] is False and got["copper_layer_counts"] == [2]


def test_an_unknown_tier_or_fab_is_refused_with_the_choices():
    assert "tiers" in fr.fab_rules("JLCPCB", copper_layers=2, tier="gold")
    assert "vendors" in fr.fab_rules("No Such Fab", copper_layers=2)


# --- IPC class --------------------------------------------------------------

def test_an_ipc_class_is_refused_until_the_catalogue_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "_ipc_catalogue", lambda: tmp_path / "absent.json")
    got = fr.fab_rules("JLCPCB", copper_layers=4, ipc_class=3)
    assert got["ok"] is False and "refused rather than guessed" in got["reason"]
    monkeypatch.setattr(fr, "_ipc_catalogue", lambda: None)   # no profile
    got = fr.fab_rules("JLCPCB", copper_layers=4, ipc_class=3)
    assert got["ok"] is False and "refused rather than guessed" in got["reason"]
    assert fr.ipc_floors(4)["ok"] is False


def test_the_stricter_of_fab_and_class_governs(monkeypatch, tmp_path):
    cat = tmp_path / "ipc.json"
    cat.write_text(json.dumps({"source": {"standard": "TEST"}, "classes": {"3": {
        "min_track": {"mils": 5.0, "clause": "test"},
        "min_gap": {"mils": 1.0, "clause": "test"}}}}))
    monkeypatch.setattr(fr, "_ipc_catalogue", lambda: cat)
    rules = fr.fab_rules("JLCPCB", copper_layers=4, ipc_class=3)["rules"]
    assert rules["min_track"]["governs"] == "ipc"
    assert rules["min_track"]["design_mils"] == 5.0
    assert rules["min_gap"]["governs"] == "fab"
    assert rules["min_gap"]["design_mils"] == pytest.approx(3.543, abs=1e-3)


# --- comparison -------------------------------------------------------------

def test_no_ipc_values_ship_in_the_repository():
    """IPC values are licensed: the floors live per profile, never here."""
    assert not list(_DATA.glob("ipc*.json"))


def test_compare_lists_every_fab_that_builds_it():
    got = fr.compare(copper_layers=4)
    vendors = {r["vendor"] for r in got["rows"]}
    assert {"JLCPCB", "Technoservice", "NextPCB", "OSH Park"} <= vendors
    osh = next(r for r in got["rows"] if r["vendor"] == "OSH Park")
    assert osh["tier"] == "four_layer"          # the 2-layer tiers drop out
    assert fr.compare(["nonsense"])["ok"] is False


# --- connectors -------------------------------------------------------------

def test_quote_matching_tolerates_layout_but_not_a_changed_number():
    page = fc.normalise("Min. track | width\n 0.10 mm (4 mil)  other text 0.2 mm")
    assert fc.quote_on_page("Min. track width 0.10 mm (4 mil)", page)
    assert fc.quote_on_page("Min. track ... other text", page)
    assert not fc.quote_on_page("Min. track width 0.09 mm (4 mil)", page)
    assert not fc.quote_on_page("other text ... Min. track", page)   # order


def test_quote_matching_survives_real_page_layouts():
    """Layouts met on real pages (2026-11-12): JLCPCB's en dash with
    spaces, TS PCB's PDF dot leaders, AdvancedPCB's standard column
    between label and advanced value."""
    jlc = fc.normalise("Drill Diameter\n |     1-layer: 0.3 \u2013 6.3 mm")
    assert fc.quote_on_page("Drill Diameter 1-layer: 0.3-6.3 mm", jlc)
    ts = fc.normalise("\u2212 minimalna średnica wiertła:  \u2026\u2026\u2026...\u2026\u2026.. 0,25 mm")
    assert fc.quote_on_page("minimalna średnica wiertła: 0,25 mm", ts)
    assert not fc.quote_on_page("minimalna średnica wiertła: 0,2 mm", ts.replace("0,25", "0,3"))
    ac = fc.normalise('Min. Hole Size \n |    0.010" \n |    0.004" \n |')
    assert fc.quote_on_page('Min. Hole Size ... 0.004"', ac)
    assert not fc.quote_on_page('Min. Hole Size ... 0.003"', ac)


def _pages_from_quotes(vendor: str) -> str:
    """A page carrying every stored quote, as the fab's page does."""
    got = fl.vendor(vendor)
    return "\n".join(f["quote"] for figs in got["tiers"].values()
                     for opts in figs.values() for f in opts if f.get("quote"))


def test_check_confirms_an_unchanged_page_and_catches_a_changed_figure():
    page = _pages_from_quotes("OSH Park")
    ok = fc.check("OSH Park", [page])
    assert ok["all_confirmed"] and not ok["not_found"]
    seeded = page.replace("6mil (0.1524mm) trace width", "5mil (0.127mm) trace width")
    bad = fc.check("OSH Park", [seeded])
    assert not bad["all_confirmed"]
    assert any(r["key"] == "min_track" for r in bad["not_found"])


def _opener(pages: dict, robots: str = ""):
    def open_(url):
        if url.endswith("/robots.txt"):
            if robots is None:
                raise urllib.error.HTTPError(url, 404, "nf", None, None)
            return robots.encode(), "text/plain"
        return pages[url].encode(), "text/html"
    return open_


def test_refresh_reads_snapshots_and_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "snapshot_dir", lambda: tmp_path)
    src = fc.sources()["oshpark"]
    body = "<html><body><p>" + _pages_from_quotes("OSH Park") + "</p></body></html>"
    pages = {u: body for u in src["urls"]}
    got = fc.refresh("oshpark", opener=_opener(pages, robots=None),
                     today=dt.date(2026, 11, 12))
    assert got["ok"] and got["check"]["all_confirmed"]
    assert Path(got["snapshot"]).is_file()
    assert Path(got["snapshot"]).parent == tmp_path / "oshpark"


def test_refresh_obeys_robots_txt(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "snapshot_dir", lambda: tmp_path)
    src = fc.sources()["oshpark"]
    got = fc.refresh("oshpark", opener=_opener({u: "x" for u in src["urls"]},
                                               robots="User-agent: *\nDisallow: /\n"))
    assert got["ok"] is False
    assert all("robots.txt" in f["reason"] for f in got["failed"])
    assert all("--file" in f["hint"] for f in got["failed"])


def test_refresh_takes_a_saved_page(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "snapshot_dir", lambda: tmp_path / "snaps")
    saved = tmp_path / "page.html"
    saved.write_text("<p>" + _pages_from_quotes("Technoservice") + "</p>", encoding="utf-8")
    got = fc.refresh("technoservice", files=[str(saved)])
    assert got["check"]["all_confirmed"]


def test_refresh_of_a_pending_fab_keeps_the_snapshot_for_transcription(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "snapshot_dir", lambda: tmp_path)
    saved = tmp_path / "wurth.txt"
    saved.write_text("Minimum conductor width 100 µm", encoding="utf-8")
    got = fc.refresh("wurth", files=[str(saved)])
    assert got["ok"] and "check" not in got and "transcribe" in got["note"]


def test_write_moves_the_date_only_when_every_quote_matched(tmp_path):
    data = tmp_path / "oshpark.json"
    data.write_text((_DATA / "oshpark.json").read_text(encoding="utf-8"), encoding="utf-8")
    refused = fc.write_retrieved("oshpark", {"key": "oshpark", "date": "2027-01-01",
                                             "check": {"all_confirmed": False,
                                                       "not_found": [{}]}},
                                 data_dir=tmp_path)
    assert refused["ok"] is False
    assert json.loads(data.read_text(encoding="utf-8"))["source"]["retrieved"] == "2026-11-11"
    done = fc.write_retrieved("oshpark", {"key": "oshpark", "date": "2027-01-01",
                                          "check": {"all_confirmed": True}},
                              data_dir=tmp_path)
    assert done["ok"] and done["from"] == "2026-11-11"
    after = json.loads(data.read_text(encoding="utf-8"))
    assert after["source"]["retrieved"] == "2027-01-01"
    before = json.loads((_DATA / "oshpark.json").read_text(encoding="utf-8"))
    assert after["tiers"] == before["tiers"]          # no figure touched
    fl._load.cache_clear()


def test_pdf_text_is_read():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    import io
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.write(buf)
    assert fc.to_text(buf.getvalue()) == ""


# --- the tools --------------------------------------------------------------

def test_mcp_tools_answer_offline():
    from mcp.server.fastmcp import FastMCP
    from eda_agent.tools.design import register_design_tools
    mcp = FastMCP("t")
    register_design_tools(mcp)

    async def call(name, **kw):
        tool = mcp._tool_manager.get_tool(name)
        return await tool.fn(**kw)

    fabs = asyncio.run(call("design_list_fabs"))
    assert any(f["key"] == "technoservice" for f in fabs["fabs"])
    rules = asyncio.run(call("design_fab_rules", vendor="Technoservice",
                             copper_layers=4))
    assert rules["ok"] and rules["rules"]["min_track"]["mils"] == 4.0
    cmp_ = asyncio.run(call("design_fab_compare", copper_layers=2))
    assert cmp_["ok"] and cmp_["rows"]


def test_tls_uses_certifi_when_installed():
    """A Windows certificate store that rejects valid Let's Encrypt chains
    made three fabs unreadable on a real PC; certifi's bundle reads them."""
    certifi = pytest.importorskip("certifi")
    ctx = fc._tls_context()
    want = {c["serialNumber"] for c in
            __import__("ssl").create_default_context(cafile=certifi.where()).get_ca_certs()}
    assert want and {c["serialNumber"] for c in ctx.get_ca_certs()} == want


def test_pdf_symbol_glyphs_and_spaced_colons_match():
    """TS PCB's PDF prints ± as the Symbol glyph U+F0B1; Eurocircuits
    writes 'Exposed copper (...) : 0.100mm' (2026-11-12, live pages)."""
    ts = fc.normalise("frezowania: ……..… 0,1 mm")
    assert fc.quote_on_page("frezowania: ... ±0,1 mm", ts)
    ec = fc.normalise("     Exposed copper  (or surface finished copper) :  0.100mm")
    assert fc.quote_on_page("Exposed copper (or surface finished copper): 0.100mm", ec)
