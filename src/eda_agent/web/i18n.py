# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The dashboard's language: Altium's, unless the user picks another.

Altium shows its interface translated when "Use localized resources" is
on in its preferences (HKCU\\Software\\Altium\\<install>\\DesignExplorer\\
Preferences\\Client\\Client Preferences) AND it ships translations for
the language Windows runs (System\\Resources\\<LCID>). Both are read here,
so the dashboard follows Altium without asking it anything over the
bridge, and opens in English whenever Altium does.

A label is translated by the table shipped in
dashboard_static/i18n/<language>.json, except the design terms in
ALTIUM_TERMS, which take Altium's own translation from the install so the
tabs say "Footprints" exactly as Altium's panels do. A label in neither
stays English.
"""
from __future__ import annotations

import ctypes
import json
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..ui import altium_locale

_STATIC = Path(__file__).resolve().parent / "dashboard_static" / "i18n"
_PREFS = r"DesignExplorer\Preferences\Client\Client Preferences"

#: The language names the picker shows, each in its own language.
NAMES = {"en": "English", "de": "Deutsch", "fr": "Français", "ja": "日本語",
         "ko": "한국어", "ru": "Русский", "zh-CN": "简体中文",
         "zh-TW": "繁體中文"}


def _altium_prefs() -> dict:
    """{value name: value} from the first Altium install's client
    preferences that has them; {} off Windows or when none do."""
    if sys.platform != "win32":
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Altium")
    except OSError:
        return {}
    with root:
        index = 0
        while True:
            try:
                install = winreg.EnumKey(root, index)
            except OSError:
                return {}
            index += 1
            try:
                key = winreg.OpenKey(root, install + "\\" + _PREFS)
            except OSError:
                continue
            out = {}
            with key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    out[name] = value
            if out:
                return out


def _windows_lcid(system_locale: bool) -> Optional[int]:
    try:
        k32 = ctypes.windll.kernel32
        return int(k32.GetSystemDefaultLCID() if system_locale
                   else k32.GetUserDefaultUILanguage())
    except Exception:                            # noqa: BLE001
        return None


def altium_language() -> dict:
    """{"language", "why"}: the language Altium's interface is in."""
    prefs = _altium_prefs()
    if str(prefs.get("Use Localized Resources", "0")) != "1":
        return {"language": "en",
                "why": "Altium's localized resources are off"}
    lcid = _windows_lcid(str(prefs.get("Use System Locale Language", "0")) == "1")
    installed = altium_locale.installed_languages()
    for key in (str(lcid), str((lcid or 0) & 0x3FF)):
        if key in installed:
            return {"language": installed[key],
                    "why": f"Windows language {lcid}, translated by Altium"}
    return {"language": "en",
            "why": f"Altium has no translation for Windows language {lcid}"}


def available() -> list[dict]:
    """Languages the picker offers: English plus each one with a table."""
    langs = ["en"] + sorted(p.stem for p in _STATIC.glob("*.json")
                            if not p.stem.startswith("_"))
    return [{"code": c, "name": NAMES.get(c, c)} for c in langs]


@lru_cache(maxsize=16)
def catalog(language: str) -> dict[str, str]:
    """{English label: translation} for ``language``."""
    if language == "en":
        return {}
    table: dict[str, str] = {}
    path = _STATIC / f"{language}.json"
    if path.is_file():
        try:
            table.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    for label in ALTIUM_TERMS:
        found = altium_locale.translations(label).get(language) or set()
        # Half-width katakana (a cramped toolbar's ﾗｲﾌﾞﾗﾘ) read as normal.
        words = {unicodedata.normalize(
            "NFKC", altium_locale._ACCEL_SUFFIX.sub("", t).replace("&", "")
        ).strip() for t in found}
        # Only when Altium itself is consistent: a term it translates two
        # ways depends on where it appears, and the shipped table knows
        # the dashboard's sense. Never an abbreviation (German "Bibl.").
        if len(words) == 1:
            word = next(iter(words))
            if len(word) > 1 and not word.endswith("."):
                table[label] = word
    return table


#: Design terms the dashboard takes from Altium's own translation, so its
#: tabs say what Altium's panels say. Only these: a short generic word
#: matched across all of Altium's captions picked the wrong sense ("Alt",
#: the key, came back as the word for "all"; "first" as "first layer").
ALTIUM_TERMS = ("Components", "Nets", "Libraries", "Footprints", "Project",
                "Schematic", "Top", "Bottom", "Layers", "Status")

#: Labels that stay as they are in every language: key names, product
#: names and code.
UNTRANSLATED = ("Alt", "Enter", "Esc", "PageDown", "Shift", "Space",
                "J / →", "K / ←", "EDA Agent", "PCB", "IC", "audit.*")


__all__ = ["altium_language", "available", "catalog", "NAMES"]
