# SPDX-License-Identifier: Apache-2.0
"""The app icon: one drawing on the status window, the browser tab and the
desktop shortcut.

scripts/build_icons.py turns the SVG sources in assets/icon into the
favicon, the shortcut's .ico and the status window's ``Icon.Data``. These
tests keep the built files in step with the sources and check each place
that shows the icon still points at it.
"""
from __future__ import annotations

import importlib.util
import io
import re
import struct
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "eda_agent" / "web" / "dashboard_static"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_icons", ROOT / "scripts" / "build_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_builder()
DFM_TEXT = build.DFM.read_text(encoding="utf-8")


def _entries(ico: bytes) -> list[tuple[int, bytes]]:
    """(size, image bytes) for each image in an ICO file."""
    reserved, kind, count = struct.unpack_from("<HHH", ico, 0)
    assert (reserved, kind) == (0, 1), "not an ICO header"
    out = []
    for i in range(count):
        w, _h, _c, _r, _p, bpp, length, offset = struct.unpack_from("<BBBBHHII", ico, 6 + 16 * i)
        assert bpp == 32
        assert offset + length <= len(ico), "image runs past the end of the file"
        out.append((w or 256, ico[offset:offset + length]))
    return out


# -- built files match their sources ------------------------------------------

def test_built_icons_are_current():
    """Edit an SVG or PNG without rebuilding and this fails; run
    `python scripts/build_icons.py` (or --no-render)."""
    assert build.main(["--check"]) == 0


@pytest.mark.parametrize("size", build.SIZES)
def test_every_size_has_a_png_of_that_size(size):
    with Image.open(build.PNG_DIR / f"eda-agent-{size}.png") as img:
        assert img.size == (size, size)
        assert img.mode == "RGBA"


@pytest.mark.parametrize("size", build.HINTED)
def test_small_sizes_are_drawn_on_their_own_grid(size):
    svg = build.source_for(size).read_text(encoding="utf-8")
    assert f'viewBox="0 0 {size} {size}"' in svg


def test_web_icon_holds_every_size_and_matches_the_pngs():
    ico = build.WEB_ICO.read_bytes()
    assert sorted(s for s, _ in _entries(ico)) == sorted(build.WEB_SIZES)
    for size in build.WEB_SIZES:
        with Image.open(io.BytesIO(ico)) as img:
            img.size = (size, size)
            got = img.convert("RGBA")
        with Image.open(build.PNG_DIR / f"eda-agent-{size}.png") as want:
            assert got.tobytes() == want.convert("RGBA").tobytes(), size


def test_web_svg_is_the_master_drawing():
    assert build.WEB_SVG.read_bytes() == (build.SRC / "eda-agent.svg").read_bytes()


# -- the status window ----------------------------------------------------------

def test_status_window_carries_the_icon():
    data = build.dfm_icon(DFM_TEXT)
    assert data is not None, "StatusForm.dfm has no form-level Icon.Data"
    assert data == build.ico_bytes(build.DFM_SIZES)


def test_status_window_icon_uses_bitmap_entries_only():
    """PNG-compressed entries need a newer TIcon than every Altium has."""
    for size, image in _entries(build.dfm_icon(DFM_TEXT)):
        assert not image.startswith(b"\x89PNG"), size
        header_size, width, height = struct.unpack_from("<Iii", image, 0)
        assert (header_size, width, height) == (40, size, 2 * size)


def test_icon_data_is_a_property_of_the_form_itself():
    """Before the first child object, at the form's own indent."""
    first_child = DFM_TEXT.index("\n  object ")
    at = DFM_TEXT.index("\n  Icon.Data = {")
    assert at < first_child
    assert DFM_TEXT.count("Icon.Data") == 1


def test_icon_data_survives_the_control_name_parse():
    """The hex block must not look like an `object` line to other tests."""
    names = re.findall(r"^\s*object\s+(\w+)\s*:", DFM_TEXT, flags=re.MULTILINE)
    assert names[0] == "StatusForm"
    assert all(not re.fullmatch(r"[0-9A-F]+", n) for n in names)


def test_dfm_block_round_trips():
    data = bytes(range(256)) * 3
    text = "object F: TF\n  FormStyle = fsNormal\n  object p: TPanel\n  end\nend\n"
    built = build.dfm_with_icon(text, data)
    assert build.dfm_icon(built) == data
    assert build.dfm_with_icon(built, data) == built
    assert build.dfm_icon(build.dfm_with_icon(built, b"\x01\x02")) == b"\x01\x02"


# -- the web dashboard ------------------------------------------------------------

@pytest.mark.parametrize("page", ["workbench.html", "index.html"])
def test_pages_link_the_icon(page):
    head = (STATIC / page).read_text(encoding="utf-8").split("</head>", 1)[0]
    assert '<link rel="icon" href="/eda-agent.svg" type="image/svg+xml">' in head
    assert '<link rel="icon" href="/eda-agent.ico"' in head


@pytest.fixture
def client(tmp_path):
    from eda_agent.web.dashboard import create_app
    app = create_app(workspace_dir=tmp_path / "ws")
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("url,mimetype,path", [
    ("/favicon.ico", "image/x-icon", build.WEB_ICO),
    ("/eda-agent.ico", "image/x-icon", build.WEB_ICO),
    ("/eda-agent.svg", "image/svg+xml", build.WEB_SVG),
])
def test_dashboard_serves_the_icon(client, url, mimetype, path):
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.mimetype == mimetype
    assert resp.data == path.read_bytes()


# -- the desktop shortcut --------------------------------------------------------

def test_shortcut_points_at_the_packaged_icon():
    from eda_agent import workspace_start
    assert Path(workspace_start._shortcut_icon()) == build.WEB_ICO.resolve()
    source = Path(workspace_start.__file__).read_text(encoding="utf-8")
    assert "$l.IconLocation = $env:_EDA_WS_ICON + ',0'" in source
    assert '"_EDA_WS_ICON": _shortcut_icon()' in source
