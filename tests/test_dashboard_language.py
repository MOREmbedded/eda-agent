# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The dashboard speaks the language Altium's interface is in.

Altium translates its UI when "Use Localized Resources" is on AND it
ships a translation for the Windows language (both read on this
machine, AD 26.11). The dashboard follows the same two facts, offers a
picker, and takes its design terms from Altium's own words.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eda_agent.web import i18n

_STATIC = Path(i18n.__file__).resolve().parent / "dashboard_static" / "i18n"


@pytest.fixture
def altium(monkeypatch):
    state = {"prefs": {}, "lcid": 1033, "installed": {}}
    monkeypatch.setattr(i18n, "_altium_prefs", lambda: state["prefs"])
    monkeypatch.setattr(i18n, "_windows_lcid", lambda system: state["lcid"])
    monkeypatch.setattr(i18n.altium_locale, "installed_languages",
                        lambda install=None: state["installed"])
    return state


def test_localized_resources_off_means_english(altium):
    altium["prefs"] = {"Use Localized Resources": 0}
    altium["lcid"], altium["installed"] = 2052, {"2052": "zh-CN"}
    assert i18n.altium_language()["language"] == "en"


def test_on_with_a_translated_windows_language_follows_it(altium):
    altium["prefs"] = {"Use Localized Resources": 1}
    altium["lcid"], altium["installed"] = 2052, {"2052": "zh-CN"}
    assert i18n.altium_language()["language"] == "zh-CN"


def test_a_regional_variant_finds_its_primary_language(altium):
    """German (Austria) is 3079; Altium files German under 7."""
    altium["prefs"] = {"Use Localized Resources": 1}
    altium["lcid"], altium["installed"] = 3079, {"7": "de"}
    assert i18n.altium_language()["language"] == "de"


def test_on_but_untranslated_windows_language_is_english(altium):
    """What this machine has: localized resources on, Windows English."""
    altium["prefs"] = {"Use Localized Resources": 1}
    altium["lcid"], altium["installed"] = 1033, {"2052": "zh-CN"}
    assert i18n.altium_language()["language"] == "en"


def _tables():
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in _STATIC.glob("*.json") if not p.name.startswith("_")}


def test_every_language_table_covers_every_label():
    labels = json.loads((_STATIC / "_labels.json").read_text(encoding="utf-8"))
    for lang, table in _tables().items():
        missing = [lab for lab in labels if lab not in table
                   and lab not in i18n.UNTRANSLATED]
        assert not missing, (lang, missing)
        assert not [k for k, v in table.items() if not v.strip()], lang


def test_a_translation_keeps_every_placeholder():
    """{0} is a value the page inserts (a tool name, an element); a
    translation that drops or invents one loses it or prints "{1}"."""
    for lang, table in _tables().items():
        bad = [k for k, v in table.items()
               if sorted(re.findall(r"\{\d\}", k)) != sorted(re.findall(r"\{\d\}", v))]
        assert not bad, (lang, bad)


def _page():
    return (_STATIC.parent / "index.html").read_text(encoding="utf-8")


def test_each_whole_sentence_has_its_key():
    """A sentence broken by inline elements is translated whole, keyed
    with {n} where each element sits, the way the page builds the key."""
    found = re.findall(r'data-i18n="whole"[^>]*>(.*?)</(?:dd|div)>', _page(), re.S)
    assert len(found) == 3, found
    labels = json.loads((_STATIC / "_labels.json").read_text(encoding="utf-8"))
    for inner in found:
        n = iter(range(10))
        key = re.sub(r"<(\w+)[^>]*>.*?</\1>", lambda m: "{%d}" % next(n), inner)
        key = re.sub(r"\s+", " ", key).strip()
        assert key in labels, key


#: Strings in the page script that are code, not words: header and key
#: names, parameter names built around a number, the product's name.
_SCRIPT_CODE = {"ComponentLink{0}Description", "ComponentLink{0}URL",
                "Content-Type", "EDA Agent - Design Dashboard", "Enter", "Escape"}


def _js_string(literal):
    """A JS double-quoted literal's value. JS allows escapes JSON does
    not (\\' and a backslash before any punctuation); those stand for the
    character itself."""
    return json.loads(re.sub(r'\\([^"\\/bfnrtu])', r"\1", literal))


def test_every_message_the_script_writes_is_translated():
    """The page script builds banners and tooltips at run time; a new
    one written in English only would show English in every language.
    Literal chains are joined ("a " + "b"), a value in the chain is {n}."""
    page = _page()
    body = page[page.index("<script", page.index("</header>")):
                page.rindex("// Language. The dashboard")]
    lit = r'"(?:[^"\\\n]|\\.)*"'
    var = r'[A-Za-z_$][\w$.]*(?:\([^()]*\))?'
    chain = re.compile(rf'(?:{lit}|{var})(?:\s*\+\s*(?:{lit}|{var}))+|{lit}')
    de = _tables()["de"]
    missing = []
    for m in chain.finditer(body):
        parts = re.findall(rf'{lit}|{var}', m.group(0))
        if not any(p.startswith('"') for p in parts):
            continue
        n = iter(range(10))
        text = "".join(_js_string(p) if p.startswith('"') else "{%d}" % next(n)
                       for p in parts).strip()
        if (not re.match(r"[A-Z]", text) or not re.search(r"[a-z]{3}", text)
                or "<" in text or "=" in text or text.startswith("{")):
            continue
        if re.fullmatch(r"[A-Z][A-Za-z]+", text) and text[1:].lower() != text[1:]:
            continue
        if text not in de and text not in _SCRIPT_CODE:
            missing.append(text)
    assert not missing, missing


def test_design_terms_come_from_altium(monkeypatch):
    i18n.catalog.cache_clear()
    monkeypatch.setattr(
        i18n.altium_locale, "translations",
        lambda english, install=None:
            {"zh-CN": {"封装 (&F)"}} if english == "Footprints" else {})
    try:
        table = i18n.catalog("zh-CN")
        assert table["Footprints"] == "封装"
        assert table["Refresh"] == "刷新", "the shipped table fills the rest"
    finally:
        i18n.catalog.cache_clear()


def test_the_endpoints_serve_language_and_table(altium, tmp_path):
    from eda_agent.web.dashboard import create_app
    altium["prefs"] = {"Use Localized Resources": 1}
    altium["lcid"], altium["installed"] = 1041, {"17": "ja"}
    client = create_app(workspace_dir=tmp_path).test_client()
    info = client.get("/api/language").get_json()
    assert info["language"] == "ja" and info["name"] == "日本語"
    codes = {lang["code"] for lang in info["available"]}
    assert {"en", "ja", "zh-CN"} <= codes
    assert not [c for c in codes if c.startswith("_")], "a data file offered as a language"
    assert client.get("/api/i18n/ja").get_json()["Refresh"] == "更新"
    assert client.get("/api/i18n/xx").get_json() == {}


def test_the_page_carries_the_picker_and_translator():
    page = (_STATIC.parent / "index.html").read_text(encoding="utf-8")
    assert 'id="lang-select"' in page
    assert 'fetch("/api/language")' in page and "/api/i18n/" in page
