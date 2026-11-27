# SPDX-License-Identifier: Apache-2.0
"""Where each manufacturer publishes a part's documents.

A manufacturer is described by its domains, the product-page URLs to try
for an MPN, and (where the site has one) a direct datasheet URL that
needs no page at all. ``needs_browser`` marks a site that answers a plain
HTTP client with HTTP 429 or a JavaScript check, so the first request
goes to a real browser rather than spending a rate-limited try.

Every entry was checked against the live site on 2026-11-27: Texas
Instruments served ``/lit/ds/symlink/<gpn>.pdf`` and its product page to
plain HTTP; Qorvo answered plain HTTP with 429 and a check page, while a
browser loaded ``/products/p/<MPN>`` and its ``/products/r/ra...``
resources; Analog Devices never answered plain HTTP (timeout).

A manufacturer not listed here still works through the generic route:
the datasheet URL a catalogue (Nexar, Digi-Key, Mouser) gives, and the
documents linked from the page that URL came from.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = ["Site", "SITES", "classify_link", "generic_part_numbers",
           "mentions_portal",
           "site_for_host", "site_for_manufacturer", "site_for_mpn"]


def generic_part_numbers(mpn: str, limit: int = 4) -> list[str]:
    """The MPN, then shorter forms of it down to the family part number.

    An orderable number carries package, temperature and reel suffixes
    (TPS54331DR, ADM7150ACPZ-3.3-R7) that a product page does not use, so
    the suffix is taken off a piece at a time: first after a ``-``, ``/``
    or ``#``, then one trailing letter at a time while what is left still
    ends in a digit run plus at most two letters.
    """
    mpn = str(mpn or "").strip()
    out: list[str] = []

    def add(value: str) -> None:
        value = value.strip(" -/#.")
        if value and value.upper() not in (v.upper() for v in out):
            out.append(value)

    add(mpn)
    base = re.split(r"[-/#,]", mpn, maxsplit=1)[0]
    add(base)
    core = re.match(r"^([A-Za-z]*\d+[A-Za-z]{0,1})", base)
    trimmed = base
    while core and len(trimmed) > len(core.group(1)) and len(out) < limit + 3:
        trimmed = trimmed[:-1]
        if re.search(r"\d[A-Za-z]{0,2}$", trimmed):
            add(trimmed)
    if core:
        add(core.group(1))
        digits = re.match(r"^([A-Za-z]*\d+)", core.group(1))
        if digits:
            add(digits.group(1))
    return out[:limit + 2]


@dataclass(frozen=True)
class Site:
    name: str
    domains: tuple[str, ...]
    product_pages: Callable[[str], list[str]]
    direct_datasheets: Callable[[str], list[str]] = lambda mpn: []
    needs_browser: bool = False
    #: MPN prefixes that name this manufacturer with little doubt, used
    #: only when no manufacturer was given and no catalogue answered.
    prefixes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _family_first(mpn: str) -> list[str]:
    """Product pages are named after the family part number, so the
    shortest form is tried first and the full MPN last."""
    return list(reversed(generic_part_numbers(mpn)))


def _ti_pages(mpn: str) -> list[str]:
    return [f"https://www.ti.com/product/{g.upper()}" for g in _family_first(mpn)]


def _ti_direct(mpn: str) -> list[str]:
    return [f"https://www.ti.com/lit/ds/symlink/{g.lower()}.pdf"
            for g in _family_first(mpn)]


def _adi_pages(mpn: str) -> list[str]:
    return [f"https://www.analog.com/en/products/{g.lower()}.html"
            for g in _family_first(mpn)]


def _qorvo_pages(mpn: str) -> list[str]:
    return [f"https://www.qorvo.com/products/p/{g.upper()}"
            for g in generic_part_numbers(mpn, limit=2)]


def _microchip_pages(mpn: str) -> list[str]:
    return [f"https://www.microchip.com/en-us/product/{g.upper()}"
            for g in _family_first(mpn)]


def _st_pages(mpn: str) -> list[str]:
    return [f"https://www.st.com/en/search.html#q={urllib.parse.quote(mpn)}"]


SITES: tuple[Site, ...] = (
    Site("Texas Instruments", ("ti.com",), _ti_pages, _ti_direct,
         prefixes=("TPS", "TLV", "LMR", "LMZ", "OPA", "INA", "TXB", "TCAN",
                   "SN74", "SN65", "DRV", "BQ", "TMS320", "MSP430", "CC13",
                   "CC26", "ADS", "DAC", "LMK", "DS90"),
         aliases=("ti", "texas instruments", "national semiconductor")),
    Site("Analog Devices", ("analog.com",), _adi_pages, needs_browser=True,
         prefixes=("ADM", "ADP", "ADA", "ADL", "ADRF", "ADF", "ADuM", "HMC",
                   "LTC", "LTM", "LT", "AD"),
         aliases=("adi", "analog devices", "linear technology", "maxim",
                  "maxim integrated", "hittite")),
    Site("Qorvo", ("qorvo.com",), _qorvo_pages, needs_browser=True,
         prefixes=("QPA", "QPL", "QPQ", "QPC", "QPF", "QPM", "QPB", "TQP",
                   "TGA", "RFFM"),
         aliases=("qorvo", "triquint", "rfmd")),
    Site("Microchip", ("microchip.com",), _microchip_pages,
         prefixes=("PIC", "ATSAM", "ATMEGA", "ATTINY", "MCP", "KSZ", "LAN"),
         aliases=("microchip", "microchip technology", "atmel", "microsemi")),
    Site("STMicroelectronics", ("st.com",), _st_pages, needs_browser=True,
         prefixes=("STM32", "STM8", "LSM", "LIS", "VL53", "L6", "STLINK"),
         aliases=("st", "stmicroelectronics", "st microelectronics")),
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text or "").lower()).strip()


def site_for_manufacturer(name: str) -> Optional[Site]:
    key = _norm(name)
    if not key:
        return None
    for site in SITES:
        names = (_norm(site.name),) + tuple(_norm(a) for a in site.aliases)
        if key in names or any(key.startswith(n + " ") for n in names if n):
            return site
    return None


def site_for_host(host: str) -> Optional[Site]:
    host = str(host or "").lower()
    for site in SITES:
        if any(host == d or host.endswith("." + d) for d in site.domains):
            return site
    return None


def site_for_mpn(mpn: str, min_prefix: int = 1) -> Optional[Site]:
    """A guess from the MPN's prefix alone; the longest prefix wins.

    ``min_prefix`` ignores short prefixes (``AD``, ``LT``), which other
    makers' part numbers also start with.
    """
    upper = str(mpn or "").upper()
    best: tuple[int, Optional[Site]] = (0, None)
    for site in SITES:
        for prefix in site.prefixes:
            if len(prefix) < min_prefix:
                continue
            if upper.startswith(prefix.upper()) and len(prefix) > best[0]:
                best = (len(prefix), site)
    return best[1]


# --- what a link on a product page is ------------------------------------

_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("model3d", (".step", ".stp", "3d model", "step model", "3d cad",
                 "/3d-models/", "step file")),
    ("package", ("package drawing", "package outline", "mechanical drawing",
                 "land pattern", "footprint", "outline dimensions",
                 "package information", "/lit/ml/", "/media/en/package-pcb",
                 "pcb layout guide", "mds.analog.com/api/public/content/")),
    ("appnote", ("application note", "app note", "appnote", "/lit/an/",
                 "/application-notes/", "/an-", "application brief",
                 "design note", "technical note", "user guide", "/lit/ug/",
                 "/user-guides/")),
    ("datasheet", ("data sheet", "datasheet", "/data-sheets/", "/lit/ds/",
                   "/lit/gpn/", "product brief")),
)

_SKIP = ("gerber", "s-parameter", "s parameter", "s2p", "spice", "ibis",
         "evaluation board", "eval board", "bom", "schematic", "license",
         "privacy", "terms", "cookie", "login", "register", "cart",
         "mailto:", "javascript:", "linkedin", "twitter", "facebook",
         "youtube", "samacsys", "ultralibrarian", "snapeda", "componentsearchengine")


#: "8-Lead LFCSP", "16-pin QFN", "64-ball BGA": a package's own drawing.
_PACKAGE_TEXT = re.compile(r"(?<![\w.])\d+\s*-?\s*(?:lead|ball|pin|pad)s?(?![a-z])")
#: TI literature numbers ending in A are application reports (SLVA317).
_TI_APPNOTE = re.compile(r"ti\.com/lit/(?:pdf|an)/s[a-z]{2}a\d+")


def mentions_portal(href: str, text: str) -> str:
    """The sign-in model portal a link points at, or ''."""
    blob = f"{href} {text}".lower()
    for key, name in (("samacsys", "SamacSys"), ("componentsearchengine", "SamacSys"),
                      ("ultralibrarian", "Ultra Librarian"),
                      ("ultra librarian", "Ultra Librarian"),
                      ("snapeda", "SnapEDA")):
        if key in blob:
            return name
    return ""


def classify_link(href: str, text: str, site: Optional[Site] = None) -> str:
    """``datasheet``, ``package``, ``appnote``, ``model3d`` or ``""``.

    Read from the link's text and URL together. Links to sign-in-only
    model portals (SamacSys, Ultra Librarian, SnapEDA) and to resources
    nobody asked for (Gerbers, S-parameters, SPICE) are not documents
    here and come back as ``""``.
    """
    url = str(href or "")
    low_url = url.lower()
    low_text = str(text or "").lower()
    blob = f"{low_text} {low_url}"
    if not low_url.startswith(("http://", "https://")):
        return ""
    if any(s in blob for s in _SKIP):
        return ""
    is_resource = bool(re.search(r"\.(pdf|step|stp|zip)(\?|#|$)", low_url)) or (
        site is not None and site.name == "Qorvo" and "/products/r/" in low_url) or (
        "/lit/" in low_url)
    if is_resource and _PACKAGE_TEXT.search(low_text):
        return "package"
    if is_resource and _TI_APPNOTE.search(low_url):
        return "appnote"
    for kind, words in _KIND_RULES:
        if any(w in blob for w in words):
            if kind == "model3d" and not (is_resource or ".step" in low_url
                                          or ".stp" in low_url):
                return ""
            if kind != "model3d" and not is_resource:
                return ""
            return kind
    return ""
