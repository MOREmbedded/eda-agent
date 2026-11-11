# SPDX-License-Identifier: Apache-2.0
"""The dashboard's theme: follow the system, light or dark.

The choice is a profile setting (``theme`` in the profile's
preferences.json), so each company's workspace keeps its own; the page
caches it in the browser so it paints in the right theme from the start,
and ``system`` follows the operating system's light or dark mode live.

Run against a temp HOME and cwd, isolated from any real ~/.eda-agent.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from eda_agent import preferences, profile as profile_mod, safety
from eda_agent.web.dashboard import create_app

pytestmark = pytest.mark.profile_preferences

PAGE = (pathlib.Path(preferences.__file__).parent / "web" / "dashboard_static"
        / "workbench.html")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("EDA_AGENT_PROFILE_DIR", "EDA_AGENT_IPC_DIR", "ALTIUM_AI_IPC_DIR",
                safety.READONLY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EDA_AGENT_MODE", "")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    from eda_agent.config import get_config
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(get_config(), "workspace_dir", tmp_path / "ws", raising=False)


def _central(name: str, prefs: dict | None = None) -> pathlib.Path:
    directory = profile_mod.central_profiles_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if prefs is not None:
        (directory / "preferences.json").write_text(json.dumps(prefs), encoding="utf-8")
    profile_mod.use_central_profile(name)
    return directory


@pytest.fixture
def client(tmp_path):
    app = create_app(workspace_dir=tmp_path / "ws")
    app.config["TESTING"] = True
    return app.test_client()


# -- the setting --------------------------------------------------------------


def test_the_theme_follows_the_system_unless_a_profile_says_otherwise(tmp_path):
    assert preferences.theme(tmp_path / "ws") == "system"
    assert preferences.resolved(tmp_path / "ws")["theme"]["source"] == "default"
    _central("acme", {"theme": "dark"})
    assert preferences.theme(tmp_path / "ws") == "dark"
    assert preferences.resolved(tmp_path / "ws")["theme"]["source"] == "profile"


def test_switching_profile_switches_the_theme(tmp_path):
    _central("acme", {"theme": "dark"})
    _central("globex", {"theme": "light"})
    assert preferences.theme(tmp_path / "ws") == "light"
    profile_mod.use_central_profile("acme")
    assert preferences.theme(tmp_path / "ws") == "dark"


def test_only_the_three_themes_are_accepted(tmp_path):
    directory = _central("acme")
    got = preferences.save_profile_preferences(directory, {"theme": "black"})
    assert not got["ok"] and "black" in got["reason"]
    assert not (directory / "preferences.json").exists()
    problems = preferences.check_profile_preferences({"theme": "black"})
    assert problems and "theme" in problems[0]
    assert preferences.check_profile_preferences({"theme": "dark"}) == []
    # A broken value in the file means the default, never a crash.
    (directory / "preferences.json").write_text('{"theme": "black"}', encoding="utf-8")
    assert preferences.theme(tmp_path / "ws") == "system"


def test_the_dashboard_saves_the_theme_in_the_profile(tmp_path, client):
    directory = _central("acme", {"value_format": "si"})
    got = client.post("/api/wb/workspace/settings", json={"changes": {"theme": "dark"}}).get_json()
    assert got["ok"], got
    assert json.loads((directory / "preferences.json").read_text(encoding="utf-8")) == {
        "value_format": "si", "theme": "dark"}
    st = client.get("/api/wb/workspace").get_json()["settings"]
    assert st["items"]["theme"] == {"value": "dark", "source": "profile", "panel": None, "profile": "dark"}
    assert st["choices"]["theme"] == ["system", "light", "dark"]
    # Clearing it hands the choice back to the system.
    assert client.post("/api/wb/workspace/settings", json={"changes": {"theme": None}}).get_json()["ok"]
    assert preferences.theme(tmp_path / "ws") == "system"
    # The panel's own file is never written from here.
    assert not (tmp_path / "ws" / "preferences.json").exists()


def test_without_a_profile_the_theme_cannot_be_kept_server_side(client):
    got = client.post("/api/wb/workspace/settings", json={"changes": {"theme": "dark"}}).get_json()
    assert not got["ok"] and "no profile is in effect" in got["reason"]


# -- the page -----------------------------------------------------------------


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _block(css: str, selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert m, selector
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)))


def test_the_theme_is_set_before_the_first_paint():
    page = _page()
    head = page[:page.index("<style>")]
    assert 'localStorage.getItem("wb.theme")' in head
    assert "prefers-color-scheme: dark" in head
    assert 'setAttribute("data-theme"' in head


def test_the_page_offers_the_three_themes_and_follows_the_system_live():
    page = _page()
    menu = re.search(r'<select id="theme">(.*?)</select>', page).group(1)
    assert re.findall(r'value="(\w+)"', menu) == ["system", "light", "dark"]
    assert '$("#theme").addEventListener("change"' in page
    assert 'darkOS.addEventListener("change"' in page
    assert 'plain("theme", T("Dashboard theme"))' in page
    assert "themeFrom(r);" in page and "changes: {theme: choice}" in page


def test_the_dark_theme_recolours_every_colour_the_light_one_names():
    css = _page().split("<style>", 1)[1].split("</style>", 1)[0]
    light, dark = _block(css, ":root"), _block(css, ':root[data-theme="dark"]')
    colours = {k for k, v in light.items() if re.match(r"#|rgba?\(", v.strip())}
    assert colours - {"--mask"} <= set(dark), sorted(colours - set(dark))


def test_no_light_only_colour_is_written_into_a_rule():
    """A light background written straight into a rule stays light in the
    dark theme. Every surface colour goes through a variable."""
    css = _page().split("<style>", 1)[1].split("</style>", 1)[0]
    rules = re.sub(r":root(\[data-theme=\"dark\"\])?\s*\{.*?\n\}", "", css, flags=re.S)
    bad = re.findall(r"background:\s*(#(?:fff|FFF|[EFef][0-9A-Fa-f]{5})\b|rgba\(2[45]\d,)", rules)
    assert not bad, bad


def _lum(hexcol: str) -> float:
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    def ch(v):
        c = int(v, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(h[i:i + 2]) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def test_dark_text_meets_wcag_aa():
    css = _page().split("<style>", 1)[1].split("</style>", 1)[0]
    dark = {**_block(css, ":root"), **_block(css, ':root[data-theme="dark"]')}
    for bg in ("--paper", "--film", "--paper2"):
        for fg in ("--ink", "--ink2", "--ink3", "--red", "--blue", "--amber", "--green"):
            ratio = _contrast(dark[fg], dark[bg])
            assert ratio >= 4.5, (fg, bg, round(ratio, 2))
