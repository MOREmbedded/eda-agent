# SPDX-License-Identifier: Apache-2.0
"""The app icon: one drawing on the status window, the browser tab and the
desktop shortcut.

scripts/build_icons.py turns the SVG sources in assets/icon into the
favicon and the .ico the shortcut and the status window use. These
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
DFM_TEXT = (ROOT / "scripts" / "altium" / "StatusForm.dfm").read_text(encoding="utf-8")


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
#
# Altium's script form loader drops Icon.Data from a .dfm, and an icon
# another process loads dies with that process, so the form loads the
# icon itself from the copy the server puts in the workspace.

PAS = (ROOT / "scripts" / "altium" / "StatusForm.pas").read_text(encoding="utf-8")


def _procedure(name: str) -> str:
    start = PAS.index(f"Procedure {name}(")
    return PAS[start:PAS.index("\nEnd;", start)]


def test_form_and_server_agree_on_the_icon_name():
    from eda_agent import app_icon
    assert f"APP_ICON_FILE        = '{app_icon.ICON_NAME}';" in PAS
    assert app_icon.PACKAGED_ICON == build.WEB_ICO.resolve()


def test_form_loads_the_icon_from_the_workspace():
    body = _procedure("ApplyAppIcon")
    assert "IconPath := WorkspaceDir + APP_ICON_FILE;" in body
    assert "StatusForm.Icon.LoadFromFile(IconPath);" in body
    assert "If AppIconDone Then Exit;" in body


def test_form_applies_the_icon_on_refresh_and_on_show():
    refresh = PAS[PAS.index("UpdateUpdateBadge(0);"):][:200]
    assert "ApplyAppIcon(0);" in refresh
    show = _procedure("ShowStatusForm")
    shown = show.index("StatusForm.Show;")
    assert show.index("AppIconDone := False;") > shown
    assert show.index("ApplyAppIcon(0);") > shown


def test_dfm_holds_no_icon_data():
    """Altium ignores it; 26 kB of hex the form would carry for nothing."""
    assert "Icon.Data" not in DFM_TEXT


def test_publish_copies_the_icon_once(tmp_path):
    from eda_agent import app_icon
    assert app_icon.publish_icon(tmp_path)
    target = tmp_path / app_icon.ICON_NAME
    assert target.read_bytes() == build.WEB_ICO.read_bytes()
    stamp = target.stat().st_mtime_ns
    assert app_icon.publish_icon(tmp_path)
    assert target.stat().st_mtime_ns == stamp
    target.write_bytes(b"old")
    assert app_icon.publish_icon(tmp_path)
    assert target.read_bytes() == build.WEB_ICO.read_bytes()
    assert not list(tmp_path.glob("*.tmp"))


def test_publish_never_raises(tmp_path):
    from eda_agent import app_icon
    assert app_icon.publish_icon(tmp_path / "missing" / "dir") is False


def test_workspace_setup_publishes_the_icon():
    from eda_agent import config
    source = Path(config.__file__).read_text(encoding="utf-8")
    body = source[source.index("def ensure_workspace(self)"):]
    body = body[:body.index("\n    def ")]
    assert "publish_icon(self.workspace_dir)" in body
    from eda_agent import cli
    cli_source = Path(cli.__file__).read_text(encoding="utf-8")
    install = cli_source[cli_source.index("def _install_scripts_into("):]
    install = install[:install.index("\ndef ")]
    assert "publish_icon(workspace_dir)" in install


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
