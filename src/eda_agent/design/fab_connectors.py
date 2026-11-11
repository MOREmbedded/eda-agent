# SPDX-License-Identifier: Apache-2.0
"""Fab connectors: re-read a fab's published pages and check the stored data.

No fab offers an API for its capabilities, so a connector is the fab's
page list (``fab_data/fab_sources.json``) plus one generic reader:

1. fetch each page (HTML or PDF) with an honest user agent, obeying the
   site's robots.txt; or take a copy the user saved from a browser;
2. reduce it to plain text and keep a dated snapshot OUTSIDE the repo
   (``%USERPROFILE%/.eda-agent/fab_snapshots/<fab>/``);
3. look for every stored quote in that text. A quote still on the page
   confirms its figure; a quote that is gone means the fab changed the
   page (or moved the figure) and a person must read it again.

A connector never rewrites a figure. A value typed from a page and a
value parsed from it look the same afterwards, so a changed figure is
reported for review, not overwritten. Only when EVERY quote of a fab is
still on its pages may ``write_retrieved`` move the reading date on.

Quotes may elide with ``...``; each fragment must then appear, in order.
Whitespace, case, ``|`` table separators and typographic variants
(≥/≧, ×/x, µ/u) are normalised on both sides.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path
from typing import Any, Callable, Optional

from . import fab_library as _fl

USER_AGENT = "eda-agent-fab-refresh/1.0 (+https://github.com/MOREmbedded/eda-agent)"
SOURCES = _fl._DATA / "fab_sources.json"
TIMEOUT_S = 30


# --- the registry -------------------------------------------------------

def sources() -> dict[str, dict]:
    """{fab key: {vendor, data_file?, urls, status, note}} for every fab
    with a connector, including fabs whose pages have not been read yet."""
    return json.loads(SOURCES.read_text(encoding="utf-8"))["fabs"]


def _key(name: str) -> Optional[str]:
    want = name.strip().lower()
    for key, s in sources().items():
        if want in (key, s["vendor"].lower()):
            return key
    return None


def list_connectors(today: Optional[_dt.date] = None) -> list[dict]:
    """Every connector with its status and, when data exists, its age."""
    have = {v["vendor"]: v for v in _fl.list_vendors(today)}
    out = []
    for key, s in sources().items():
        row = {"key": key, "vendor": s["vendor"], "country": s.get("country", ""),
               "status": s["status"], "urls": s["urls"]}
        if s.get("note"):
            row["note"] = s["note"]
        if s["vendor"] in have:
            v = have[s["vendor"]]
            row.update({"tiers": v["tiers"], "retrieved": v["retrieved"],
                        "age_days": v["age_days"], "stale": v["stale"]})
        out.append(row)
    return out


# --- text ----------------------------------------------------------------

_SUBS = [("≧", "≥"), ("⩾", "≥"), ("≦", "≤"), ("⩽", "≤"), ("×", "x"),
         ("μ", "u"), ("µ", "u"), ("\u2013", "-"), ("\u2014", "-"), ("，", ","),
         ("：", ":"), ("（", "("), ("）", ")"), (" ", " "), ("|", " ")]


def normalise(text: str) -> str:
    """The form both a page and a quote are compared in."""
    for a, b in _SUBS:
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return _html.unescape(raw)


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader          # a dependency already
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def to_text(data: bytes, content_type: str = "", name: str = "") -> str:
    if data[:4] == b"%PDF" or "pdf" in content_type or name.lower().endswith(".pdf"):
        return pdf_to_text(data)
    text = data.decode("utf-8", errors="replace")
    return html_to_text(text) if "<" in text[:2000] else text


# --- fetching -----------------------------------------------------------

def _robots_allow(url: str, opener: Callable) -> tuple[bool, str]:
    parts = urllib.parse.urlsplit(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        body = opener(robots)[0].decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 4xx: no robots.txt, everything allowed (RFC 9309); 5xx: assume not.
        return (exc.code < 500,
                "" if exc.code < 500 else f"robots.txt answered {exc.code}")
    except Exception as exc:              # network down: say so, don't guess
        return False, f"robots.txt unreachable: {exc}"
    rp.parse(body.splitlines())
    ok = rp.can_fetch(USER_AGENT, url)
    return ok, "" if ok else "the site's robots.txt disallows automated reading"


def _urlopen(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (https only, from the registry)
        return resp.read(), resp.headers.get("Content-Type", "")


def fetch(url: str, *, opener: Callable = _urlopen) -> dict:
    """One page as text, or why it could not be read."""
    if not url.startswith("https://"):
        return {"ok": False, "url": url, "reason": "only https pages are read"}
    allowed, why = _robots_allow(url, opener)
    if not allowed:
        return {"ok": False, "url": url, "reason": why,
                "hint": "save the page from a browser and pass it with --file"}
    try:
        data, ctype = opener(url)
    except Exception as exc:
        return {"ok": False, "url": url, "reason": f"fetch failed: {exc}"}
    return {"ok": True, "url": url, "text": to_text(data, ctype, url)}


# --- checking -----------------------------------------------------------

def quote_on_page(quote: str, page: str) -> bool:
    """Each ``...``-separated fragment of the quote, in order, on the page
    (both normalised)."""
    at = 0
    for frag in (normalise(f) for f in re.split(r"\.\.\.|…", quote)):
        if not frag:
            continue
        i = page.find(frag, at)
        if i < 0:
            return False
        at = i + len(frag)
    return True


def check(name: str, texts: list[str]) -> dict:
    """Every stored quote of one fab against the pages' text."""
    got = _fl.vendor(name)
    if not got["ok"]:
        return got
    page = normalise("\n".join(texts))
    confirmed, gone = [], []
    for tier, figs in got["tiers"].items():
        for key, options in figs.items():
            for f in options:
                q = f.get("quote")
                if not q:
                    continue
                row = {"tier": tier, "key": key, "quote": q,
                       **{u: f[u] for u in ("mm", "mil", "um", "value", "counts") if u in f}}
                (confirmed if quote_on_page(q, page) else gone).append(row)
    return {"ok": True, "vendor": got["vendor"], "confirmed": len(confirmed),
            "not_found": gone, "all_confirmed": not gone and bool(confirmed)}


def snapshot_dir() -> Path:
    from ..profile import _home
    return _home() / ".eda-agent" / "fab_snapshots"


def refresh(name: str, *, files: Optional[list[str]] = None,
            opener: Callable = _urlopen, save: bool = True,
            today: Optional[_dt.date] = None) -> dict[str, Any]:
    """Re-read one fab's pages (or saved copies) and check its data.

    ``files`` replaces fetching: pages the user saved from a browser
    (HTML, PDF or text), for sites that refuse automated readers."""
    key = _key(name)
    if key is None:
        return {"ok": False, "reason": f"no connector for {name!r}",
                "connectors": sorted(sources())}
    src = sources()[key]
    today = today or _dt.date.today()
    reads = []
    if files:
        for f in files:
            p = Path(f)
            if not p.is_file():
                return {"ok": False, "reason": f"{p} is not a file"}
            reads.append({"ok": True, "url": str(p),
                          "text": to_text(p.read_bytes(), name=p.name)})
    else:
        reads = [fetch(u, opener=opener) for u in src["urls"]]
    texts = [r["text"] for r in reads if r["ok"]]
    out: dict[str, Any] = {
        "ok": True, "key": key, "vendor": src["vendor"], "date": today.isoformat(),
        "read": [r["url"] for r in reads if r["ok"]],
        "failed": [{k: r[k] for k in ("url", "reason", "hint") if k in r}
                   for r in reads if not r["ok"]]}
    if save and texts:
        d = snapshot_dir() / key
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{today.isoformat()}.txt"
        path.write_text("\n\n".join(f"=== {r['url']}\n{r['text']}"
                                    for r in reads if r["ok"]), encoding="utf-8")
        out["snapshot"] = str(path)
    if not texts:
        out.update(ok=False, reason="no page could be read")
        return out
    if src.get("data_file") is None:
        out["note"] = ("no stored data for this fab yet: the snapshot is the "
                       "material to transcribe, quote by quote")
        return out
    out["check"] = check(src["vendor"], texts)
    return out


def write_retrieved(name: str, result: dict, *, data_dir: Path = _fl._DATA) -> dict:
    """Move a fab's reading date to the refresh date, only when every
    stored quote was found again. Returns what it did."""
    chk = result.get("check") or {}
    if not chk.get("all_confirmed"):
        return {"ok": False, "reason": "not every quote was found on the pages; "
                "read the changed ones and update the data by hand first",
                "not_found": len(chk.get("not_found", []))}
    src = sources()[result["key"]]
    path = data_dir / src["data_file"]
    text = path.read_text(encoding="utf-8")
    old = json.loads(text)["source"]["retrieved"]
    new_text = text.replace(f'"retrieved": "{old}"', f'"retrieved": "{result["date"]}"', 1)
    json.loads(new_text)
    path.write_text(new_text, encoding="utf-8")
    _fl._load.cache_clear()
    return {"ok": True, "file": str(path), "from": old, "to": result["date"]}


__all__ = ["sources", "list_connectors", "fetch", "check", "quote_on_page",
           "refresh", "write_retrieved", "normalise", "to_text"]
