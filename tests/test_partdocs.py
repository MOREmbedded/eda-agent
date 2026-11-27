# SPDX-License-Identifier: Apache-2.0
"""part_fetch_docs: finding, downloading, checking and caching a part's
documents, with the network and the browser stubbed."""

from __future__ import annotations

import io
import json
import socket
import threading
import zipfile
from pathlib import Path

import pytest

from eda_agent.partdocs import cdp, fetch as fetch_mod, files, sites
from eda_agent.partdocs.fetch import docs_root, fetch_part_docs, links_in_html
from eda_agent.partdocs.http import HttpResult

PDF = b"%PDF-1.7\n1 0 obj << /Type /Page >> endobj\n/Title (Test)\n%%EOF\n"
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


# --- part numbers and sites ------------------------------------------------

@pytest.mark.parametrize("mpn, expected", [
    ("TPS54331DR", ["TPS54331DR", "TPS54331D", "TPS54331"]),
    ("ADM7150ACPZ-3.3-R7", ["ADM7150ACPZ-3.3-R7", "ADM7150ACPZ", "ADM7150AC",
                            "ADM7150A", "ADM7150"]),
    ("QPA9903", ["QPA9903"]),
    ("LTC3780EGN#PBF", ["LTC3780EGN#PBF", "LTC3780EGN", "LTC3780EG",
                        "LTC3780E", "LTC3780"]),
])
def test_generic_part_numbers_strip_the_order_suffix(mpn, expected):
    assert sites.generic_part_numbers(mpn) == expected


def test_product_pages_try_the_family_number_first():
    ti = sites.site_for_manufacturer("Texas Instruments")
    assert ti.product_pages("TPS54331DR")[0] == "https://www.ti.com/product/TPS54331"
    assert ti.direct_datasheets("TPS54331DR")[0] == \
        "https://www.ti.com/lit/ds/symlink/tps54331.pdf"
    adi = sites.site_for_manufacturer("Analog Devices Inc.")
    assert adi.product_pages("ADM7150ACPZ-3.3-R7")[0] == \
        "https://www.analog.com/en/products/adm7150.html"
    qorvo = sites.site_for_manufacturer("qorvo")
    assert qorvo.product_pages("QPA9903") == ["https://www.qorvo.com/products/p/QPA9903"]


def test_manufacturer_names_hosts_and_prefixes_resolve():
    assert sites.site_for_manufacturer("Linear Technology").name == "Analog Devices"
    assert sites.site_for_manufacturer("TI").name == "Texas Instruments"
    assert sites.site_for_manufacturer("Acme Widgets") is None
    assert sites.site_for_host("mds.analog.com").name == "Analog Devices"
    assert sites.site_for_host("notanalog.com") is None
    assert sites.site_for_host("www.qorvo.com").name == "Qorvo"
    assert sites.site_for_mpn("QPA9903").name == "Qorvo"
    assert sites.site_for_mpn("LTC3780").name == "Analog Devices"
    # A two-letter prefix is too common to name a maker on its own.
    assert sites.site_for_mpn("AD8065", min_prefix=3) is None


@pytest.mark.parametrize("href, text, site, kind", [
    ("https://www.qorvo.com/products/r/ra005685", "Product Data Sheet", "Qorvo", "datasheet"),
    ("https://www.qorvo.com/products/r/ra008306",
     "QPA9903 1.525-1.889GHz Rev A Application Note", "Qorvo", "appnote"),
    ("https://www.qorvo.com/products/r/ra005687", "Gerber PCB Layout Files", "Qorvo", ""),
    ("https://www.qorvo.com/products/r/ra005686", "S-Parameters", "Qorvo", ""),
    ("https://www.qorvo.com/products/r/ra008362", "5G Brochure", "Qorvo", ""),
    ("https://www.analog.com/media/en/technical-documentation/data-sheets/ADM7150.pdf",
     "Data Sheet (Rev. 0)", "Analog Devices", "datasheet"),
    ("https://mds.analog.com/api/public/content/cp_8_11.pdf",
     "8-Lead LFCSP (3mm x 3mm w/ EP)", "Analog Devices", "package"),
    ("https://www.analog.com/media/en/technical-documentation/user-guides/UG-553.pdf",
     "UG-553: Evaluating the ADM7150", "Analog Devices", "appnote"),
    ("https://vendor.ultralibrarian.com/adi/embedded?vdrPN=ADM7150", "Ultra Librarian",
     "Analog Devices", ""),
    ("https://www.ti.com/lit/gpn/TPS54331", "Data sheet", "Texas Instruments", "datasheet"),
    ("https://www.ti.com/lit/pdf/SLVA317", "Create an Inverting Power Supply",
     "Texas Instruments", "appnote"),
    ("https://www.ti.com/lit/zip/SLVM659", "PSpice Transient Model", "Texas Instruments", ""),
    ("https://example.com/parts/x.step", "3D model", None, "model3d"),
    ("https://example.com/about.html", "Datasheet library", None, ""),
])
def test_links_are_sorted_by_kind(href, text, site, kind):
    s = sites.site_for_manufacturer(site) if site else None
    assert sites.classify_link(href, text, s) == kind


def test_sign_in_portals_are_named_not_followed():
    assert sites.mentions_portal("https://www.analog.com/x.html#", "SamacSys") == "SamacSys"
    assert sites.mentions_portal("https://vendor.ultralibrarian.com/x", "") == "Ultra Librarian"
    assert sites.mentions_portal("https://www.ti.com/lit/gpn/x", "Data sheet") == ""


# --- file checks -----------------------------------------------------------

def test_pdf_and_step_are_judged_by_their_bytes():
    assert files.is_pdf(PDF)
    assert not files.is_pdf(b"<!doctype html><title>Just a moment</title>")
    assert files.looks_like_html(b"  <!DOCTYPE html>")
    assert files.is_step(STEP)
    assert not files.is_step(PDF)


def test_zip_members_keep_only_step_files_by_base_name():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil/part.step", STEP)
        zf.writestr("readme.txt", "x")
        zf.writestr("fake.stp", b"not a step")
    members = files.step_members(buf.getvalue())
    assert members == [("part.step", STEP)]


def test_safe_names_stay_inside_a_folder():
    assert files.safe_name("../QPA9903 Rev A: App/Note") == "QPA9903_Rev_A_App_Note"
    assert files.safe_name("CON") == "document"
    assert files.safe_name("LTC3780EGN#PBF") == "LTC3780EGN_PBF"


def test_links_in_html_resolves_relative_links():
    title, links = links_in_html(
        '<title>TPS54331 | TI.com</title><a href="/lit/gpn/tps54331">Data sheet</a>'
        '<a href="https://x/y.pdf" title="Guide"></a>', "https://www.ti.com/product/TPS54331")
    assert title == "TPS54331 | TI.com"
    assert links == [["https://www.ti.com/lit/gpn/tps54331", "Data sheet"],
                     ["https://x/y.pdf", "Guide"]]


# --- where files go --------------------------------------------------------

def test_docs_root_from_workspace_project_env_and_parents(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    assert docs_root(ws) == ws / ".eda"
    assert docs_root(ws / ".eda") == ws / ".eda"
    assert docs_root(ws / "board" / "Board.PrjPcb") == ws / "board" / ".eda"
    monkeypatch.setenv("EDA_AGENT_DOCS_ROOT", str(ws))
    assert docs_root() == ws / ".eda"
    monkeypatch.delenv("EDA_AGENT_DOCS_ROOT")
    (ws / ".eda").mkdir(parents=True)
    deep = ws / "a" / "b"
    deep.mkdir(parents=True)
    assert docs_root(start=deep) == (ws / ".eda").resolve()
    with pytest.raises(ValueError, match="no workspace given"):
        docs_root(start=tmp_path)


# --- the whole fetch, network stubbed --------------------------------------

class _FakeHttp:
    """Answers from a table of URL -> (status, content type, body)."""

    def __init__(self, table):
        self.table = table
        self.calls: list[str] = []

    def get(self, url, *, referer="", accept="*/*", retry_429=True):
        self.calls.append(url)
        status, ctype, body = self.table.get(url, (404, "text/html", b"nope"))
        if status != 200:
            return HttpResult(False, url, status=status, reason=f"HTTP {status}",
                              wants_browser=status in (403, 429, 503))
        return HttpResult(True, url, url, 200, ctype, body)


def _ti_table():
    page = (b"<title>TPS54331 data sheet | TI.com</title>"
            b'<a href="/lit/gpn/TPS54331">Data sheet</a>'
            b'<a href="/lit/pdf/SLVA551">Simple CC/CV Charger using TPS54331</a>'
            b'<a href="https://vendor.ultralibrarian.com/TI/x">Ultra Librarian</a>')
    return {
        "https://www.ti.com/lit/ds/symlink/tps54331.pdf": (200, "application/pdf", PDF),
        "https://www.ti.com/product/TPS54331": (200, "text/html", page),
        "https://www.ti.com/lit/pdf/SLVA551": (200, "application/pdf", PDF + b"an"),
    }


@pytest.fixture
def no_catalogue(monkeypatch):
    monkeypatch.setattr(fetch_mod, "_catalogue", lambda mpn, log: {
        "manufacturer": "", "datasheets": [], "sources": []})


def test_ti_part_is_fetched_over_http_and_then_cached(tmp_path, monkeypatch, no_catalogue):
    fake = _FakeHttp(_ti_table())
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: fake)
    out = fetch_part_docs("TPS54331DR", workspace=tmp_path, browser="never")
    assert out["ok"] and out["cache"] == "miss"
    assert out["manufacturer"] == "Texas Instruments"
    ds = Path(out["datasheet"])
    assert ds == tmp_path / ".eda" / "datasheets" / "TPS54331DR.pdf"
    assert ds.read_bytes() == PDF
    kinds = sorted(f["kind"] for f in out["files"])
    assert kinds == ["appnote", "datasheet"]
    assert "Ultra Librarian" in out["missing"]["model3d"]
    meta = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
    assert meta["files"][0]["sha256"]
    assert meta["files"][0]["path"] == "datasheets/TPS54331DR.pdf"

    def no_network():
        raise AssertionError("a cache hit must not fetch")
    monkeypatch.setattr(fetch_mod, "HttpClient", no_network)
    again = fetch_part_docs("TPS54331DR", workspace=tmp_path)
    assert again["cache"] == "hit" and again["datasheet"] == str(ds)


def test_a_changed_cached_file_is_fetched_again(tmp_path, monkeypatch, no_catalogue):
    fake = _FakeHttp(_ti_table())
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: fake)
    out = fetch_part_docs("TPS54331DR", workspace=tmp_path, kinds=["datasheet"],
                          browser="never")
    Path(out["datasheet"]).write_bytes(b"%PDF-1.4 tampered %%EOF")
    again = fetch_part_docs("TPS54331DR", workspace=tmp_path, kinds=["datasheet"],
                            browser="never")
    assert again["cache"] == "miss"
    assert again["datasheet"] == out["datasheet"]
    assert Path(again["datasheet"]).read_bytes() == PDF


def test_an_existing_different_datasheet_is_not_overwritten(tmp_path, monkeypatch, no_catalogue):
    own = tmp_path / ".eda" / "datasheets" / "TPS54331DR.pdf"
    own.parent.mkdir(parents=True)
    own.write_bytes(b"%PDF-1.4 someone else's copy %%EOF")
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp(_ti_table()))
    out = fetch_part_docs("TPS54331DR", workspace=tmp_path, kinds=["datasheet"],
                          browser="never")
    assert own.read_bytes() == b"%PDF-1.4 someone else's copy %%EOF"
    assert Path(out["datasheet"]).name.startswith("TPS54331DR-")


class _FakeBrowser:
    """Stands in for cdp.BrowserSession: one page, one resource table."""

    instances: list = []

    def __init__(self, profile_dir, mode="offscreen", **_kw):
        self.profile_dir = Path(profile_dir)
        self.opened: list[str] = []
        _FakeBrowser.instances.append(self)

    page = {"url": "https://www.qorvo.com/products/p/QPA9903", "title": "QPA9903 - Qorvo",
            "links": [["https://www.qorvo.com/products/r/ra005685", "Product Data Sheet"],
                      ["https://www.qorvo.com/products/r/ra008306",
                       "QPA9903 Rev A Application Note"],
                      ["https://www.qorvo.com/products/r/ra005687", "Gerber PCB Layout Files"],
                      ["https://www.qorvo.com/products/p/QPA9903#", "SamacSys"]],
            "text": "", "human_check": False}
    resources = {"https://www.qorvo.com/products/r/ra005685": PDF,
                 "https://www.qorvo.com/products/r/ra008306": PDF + b"note"}

    def open_page(self, url, settle_s=30.0):
        self.opened.append(url)
        return dict(self.page)

    def download(self, url, accept, **_kw):
        body = self.resources.get(url)
        if body and accept("application/octet-stream", body):
            return {"ok": True, "url": url, "content_type": "application/octet-stream",
                    "body": body, "responses": []}
        return {"ok": False, "responses": [], "human_check": False}

    def close(self):
        pass


def test_a_site_that_refuses_http_is_read_in_the_browser(tmp_path, monkeypatch, no_catalogue):
    fake = _FakeHttp({})
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: fake)
    monkeypatch.setattr(cdp, "BrowserSession", _FakeBrowser)
    _FakeBrowser.instances.clear()
    out = fetch_part_docs("QPA9903", workspace=tmp_path)
    assert out["ok"], out
    assert fake.calls == [], "Qorvo refuses plain HTTP; it should not be tried"
    assert {f["via"] for f in out["files"]} == {"browser"}
    assert sorted(f["kind"] for f in out["files"]) == ["appnote", "datasheet"]
    assert "SamacSys" in " ".join(out["notes"])
    browser = _FakeBrowser.instances[0]
    assert browser.profile_dir == tmp_path / ".eda" / "browser-profile"


def test_http_429_hands_the_url_to_the_browser(tmp_path, monkeypatch, no_catalogue):
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp({}))
    monkeypatch.setattr(cdp, "BrowserSession", _FakeBrowser)
    run = fetch_mod._Run("X", tmp_path, "offscreen", "auto", 60)
    run.http = _FakeHttp({"https://acme.example/x.pdf": (429, "text/html", b"")})
    got, why = run.download(fetch_mod._Candidate(
        "datasheet", "https://acme.example/x.pdf"))
    assert got is None                      # the fake browser has no such file
    assert "acme.example" in run.browser_hosts
    assert why == "the browser got no file of the expected type"


def test_a_captcha_is_reported_not_answered(tmp_path, monkeypatch, no_catalogue):
    class Checked(_FakeBrowser):
        page = dict(_FakeBrowser.page, title="Verify you are human", human_check=True)

    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp({}))
    monkeypatch.setattr(cdp, "BrowserSession", Checked)
    out = fetch_part_docs("QPA9903", workspace=tmp_path)
    assert not out["ok"]
    assert "www.qorvo.com" in " ".join(out["notes"])


def test_browser_never_reports_why(tmp_path, monkeypatch, no_catalogue):
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp({
        "https://www.qorvo.com/products/p/QPA9903": (429, "text/html", b"")}))
    out = fetch_part_docs("QPA9903", workspace=tmp_path, browser="never")
    assert not out["ok"]
    assert any("turned off" in n for n in out["notes"])


def test_unknown_maker_uses_the_catalogue_datasheet(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_catalogue", lambda mpn, log: {
        "manufacturer": "Acme", "datasheets": ["https://acme.example/ds/XYZ1.pdf"],
        "sources": ["nexar"]})
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp({
        "https://acme.example/ds/XYZ1.pdf": (200, "application/pdf", PDF)}))
    out = fetch_part_docs("XYZ1", workspace=tmp_path, kinds=["datasheet"],
                          browser="never")
    assert out["ok"] and out["manufacturer"] == "Acme"
    assert out["files"][0]["source"] == "catalogue (nexar)"


def test_a_step_model_in_a_zip_lands_under_models(tmp_path, monkeypatch, no_catalogue):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/QFN16.step", STEP)
    page = (b"<title>XYZ1 product</title>"
            b'<a href="https://acme.example/x/XYZ1-3d.zip">STEP model</a>')
    monkeypatch.setattr(fetch_mod, "HttpClient", lambda: _FakeHttp({
        "https://acme.example/p/XYZ1": (200, "text/html", page),
        "https://acme.example/x/XYZ1-3d.zip": (200, "application/zip", buf.getvalue())}))
    out = fetch_part_docs("XYZ1", workspace=tmp_path, kinds=["model3d"],
                          product_url="https://acme.example/p/XYZ1", browser="never")
    assert out["ok"]
    model = Path(out["files"][0]["path"])
    assert model == tmp_path / ".eda" / "models" / "XYZ1" / "QFN16.step"
    assert model.read_bytes() == STEP


def test_bad_arguments_are_refused():
    assert not fetch_part_docs("")["ok"]
    assert "kinds" in fetch_part_docs("X", kinds=["gerber"])["reason"]
    assert "browser" in fetch_part_docs("X", browser="sometimes")["reason"]


# --- the DevTools WebSocket ------------------------------------------------

def test_websocket_frames_round_trip():
    server, client = socket.socketpair()
    ws = cdp._WebSocket.__new__(cdp._WebSocket)
    ws.sock, ws._buf = client, b""

    big = "x" * 70000
    received = {}

    def serve():
        # Read one masked client frame and echo two unmasked frames back:
        # a small one and one with a 64-bit length.
        head = server.recv(2)
        n = head[1] & 0x7F
        if n == 126:
            n = int.from_bytes(server.recv(2), "big")
        mask = server.recv(4)
        data = b""
        while len(data) < n:
            data += server.recv(n - len(data))
        received["text"] = bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode()
        server.sendall(b"\x81\x05hello")
        server.sendall(b"\x81\x7f" + len(big).to_bytes(8, "big") + big.encode())

    t = threading.Thread(target=serve)
    t.start()
    ws.send('{"id": 1, "method": "Page.enable"}' + " " * 200)
    assert ws.recv(5.0) == "hello"
    assert ws.recv(5.0) == big
    t.join(5)
    assert received["text"].startswith('{"id": 1')
    server.close()
    client.close()


def test_human_checks_are_recognised():
    assert cdp.looks_like_human_check("Just a sec", "Please press & hold the button")
    assert cdp.looks_like_human_check("hCaptcha", "")
    assert not cdp.looks_like_human_check("QPA9903 - Qorvo", "Data sheet")
