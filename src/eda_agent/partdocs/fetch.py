# SPDX-License-Identifier: Apache-2.0
"""Fetch a part's official documents from its manufacturer, unattended.

``fetch_part_docs("QPA9903")`` finds the manufacturer's product page,
reads the documents it links (datasheet, package drawing and land
pattern, application notes, STEP model), downloads them, checks each by
its bytes, and keeps them in the design workspace:

* ``<workspace>/.eda/datasheets/<MPN>.pdf``: the datasheet;
* ``<workspace>/.eda/datasheets/<MPN>/``: package drawings, app notes;
* ``<workspace>/.eda/models/<MPN>/``: STEP models;
* ``<workspace>/.eda/datasheets/<MPN>.json``: where each file came from,
  its SHA-256, and what was looked for and not found.

Finding the page, in order: the manufacturer's own site (known URL
patterns in :mod:`.sites`), then a catalogue (Nexar/Octopart, Digi-Key,
Mouser, element14, whichever has credentials) for the manufacturer's
name and the datasheet URL it records.

Downloading, in order: plain HTTP with an honest User-Agent, then, for a
site that refuses plain HTTP (HTTP 429, 403, a check page, no answer), a
real Edge or Chrome window placed off screen (:mod:`.cdp`). The browser
runs the site's own scripts as it would for a person; the file is taken
from the response in memory, so no Save-as dialog opens and nothing is
written to the Downloads folder. A captcha or a sign-in is never
answered: the fetch stops and reports which site asked.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html.parser
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from eda_agent.atomicfile import replace_with_retry

from . import files as _files
from .http import HttpClient
from .sites import (Site, classify_link, generic_part_numbers,
                    mentions_portal, site_for_host, site_for_manufacturer,
                    site_for_mpn)

__all__ = ["DEFAULT_KINDS", "KINDS", "docs_root", "fetch_part_docs"]

KINDS = ("datasheet", "package", "appnote", "model3d")
DEFAULT_KINDS = KINDS
#: How many documents of each kind are kept at most.
LIMITS = {"datasheet": 1, "package": 3, "appnote": 4, "model3d": 3}
_CATALOGUES = ("nexar", "digikey", "mouser", "element14")


# --- where the files go --------------------------------------------------

def docs_root(workspace: str | os.PathLike | None = None,
              start: Optional[Path] = None) -> Path:
    """The ``.eda`` folder documents are kept in.

    ``workspace`` is the design workspace (its ``.eda`` is used) or a
    ``.eda`` folder itself. Without one: ``EDA_AGENT_DOCS_ROOT``, else the
    nearest ``.eda`` folder at or above the current directory. Raises
    ValueError when none of those names a place, rather than writing to
    a folder nobody chose.
    """
    if workspace:
        path = Path(workspace).expanduser()
        if path.suffix.lower() in (".prjpcb", ".pcbdoc", ".schdoc"):
            path = path.parent
        return path if path.name.lower() == ".eda" else path / ".eda"
    env = os.environ.get("EDA_AGENT_DOCS_ROOT", "").strip()
    if env:
        return docs_root(env)
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / ".eda").is_dir():
            return folder / ".eda"
        if folder.name.lower() == ".eda":
            return folder
    raise ValueError(
        "no workspace given and no .eda folder at or above "
        f"{here}; pass the design workspace (the folder holding the "
        "projects, e.g. the one with .eda in it) or set EDA_AGENT_DOCS_ROOT")


# --- links in an HTML page -----------------------------------------------

class _Links(html.parser.HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[list[str]] = []
        self.title = ""
        self._href: Optional[str] = None
        self._text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = urllib.parse.urljoin(self.base, href)
                self._text = [dict(attrs).get("title") or ""]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append([self._href, " ".join(
                t.strip() for t in self._text if t.strip())[:200]])
            self._href = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
        if self._in_title:
            self.title += data


def links_in_html(text: str, base: str) -> tuple[str, list[list[str]]]:
    parser = _Links(base)
    try:
        parser.feed(text)
    except Exception:                                   # noqa: BLE001
        pass
    return parser.title.strip(), parser.links


# --- one run -------------------------------------------------------------

@dataclass
class _Candidate:
    kind: str
    url: str
    title: str = ""
    referer: str = ""
    source: str = ""
    rank: int = 0


@dataclass
class _Run:
    mpn: str
    root: Path
    browser_mode: str
    use_browser: str
    timeout_s: float
    http: Any = None
    browser: Any = None
    browser_error: str = ""
    browser_hosts: set = field(default_factory=set)
    human_check_hosts: set = field(default_factory=set)
    #: Files an earlier fetch of this part wrote, which may be replaced.
    own_paths: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    log: list = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.http is None:
            self.http = HttpClient()

    def out_of_time(self) -> bool:
        return time.monotonic() - self.started > self.timeout_s

    # -- browser ------------------------------------------------------

    def get_browser(self):
        if self.browser is not None or self.browser_error:
            return self.browser
        if self.use_browser == "never":
            self.browser_error = "the browser fallback was turned off"
            return None
        from .cdp import BrowserError, BrowserSession
        profile = Path(os.environ.get("EDA_AGENT_BROWSER_PROFILE", "").strip()
                       or (self.root / "browser-profile"))
        try:
            self.browser = BrowserSession(profile, mode=self.browser_mode)
        except BrowserError as exc:
            # Another run may hold the shared profile: use one of this run's own.
            try:
                self.browser = BrowserSession(
                    profile.with_name(f"{profile.name}-{os.getpid()}"),
                    mode=self.browser_mode)
            except BrowserError:
                self.browser_error = str(exc)
        except OSError as exc:
            self.browser_error = str(exc)
        return self.browser

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def wants_browser_first(self, url: str) -> bool:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if self.use_browser == "always":
            return True
        if host in self.browser_hosts:
            return True
        site = site_for_host(host)
        return bool(site and site.needs_browser and self.use_browser != "never")

    # -- pages --------------------------------------------------------

    def read_page(self, url: str) -> Optional[dict]:
        """``{url, title, links, text}`` of a product page, or None."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not self.wants_browser_first(url):
            got = self.http.get(url, accept="text/html,*/*;q=0.5")
            self.log.append(f"http page {url}: "
                            f"{got.status or ''} {got.reason or 'ok'}".strip())
            if got.ok and not _files.is_pdf(got.body):
                text = got.body.decode("utf-8", "replace")
                if not _is_check_page(text):
                    title, links = links_in_html(text, got.final_url or url)
                    return {"url": got.final_url or url, "title": title,
                            "links": links, "text": _visible_text(text)}
                got.wants_browser = True
            if not got.wants_browser:
                return None
            self.browser_hosts.add(host)
        browser = self.get_browser()
        if browser is None:
            return None
        from .cdp import BrowserError
        try:
            info = browser.open_page(url)
        except BrowserError as exc:
            self.log.append(f"browser page {url}: {exc}")
            return None
        self.log.append(f"browser page {url}: {info.get('title', '')!r}")
        if info.get("human_check"):
            self.human_check_hosts.add(host)
            return None
        return info

    # -- files --------------------------------------------------------

    def download(self, cand: _Candidate) -> tuple[Optional[dict], str]:
        """``({body, final_url, via, content_type}, "")`` or ``(None, why)``."""
        accept = _acceptor(cand.kind)
        host = (urllib.parse.urlsplit(cand.url).hostname or "").lower()
        why = ""
        if not self.wants_browser_first(cand.url):
            got = self.http.get(cand.url, referer=cand.referer)
            self.log.append(f"http {cand.url}: {got.status or ''} "
                            f"{got.reason or 'ok'} {len(got.body)} bytes".strip())
            if got.ok and accept(got.content_type, got.body):
                return ({"body": got.body, "final_url": got.final_url or cand.url,
                         "via": "http", "content_type": got.content_type}, "")
            if got.ok and _files.looks_like_html(got.body) and _is_check_page(
                    got.body.decode("utf-8", "replace")):
                got.wants_browser = True
            why = got.reason or "the reply was not the expected file type"
            if not got.wants_browser:
                return None, why
            self.browser_hosts.add(host)
        browser = self.get_browser()
        if browser is None:
            return None, why or f"needs a browser: {self.browser_error}"
        from .cdp import BrowserError
        try:
            res = browser.download(cand.url, accept)
        except BrowserError as exc:
            self.log.append(f"browser {cand.url}: {exc}")
            return None, f"browser: {exc}"
        self.log.append(f"browser {cand.url}: ok={res.get('ok')} "
                        f"{'; '.join(res.get('responses') or [])[:300]}")
        if res.get("ok"):
            return ({"body": res["body"], "final_url": res.get("url") or cand.url,
                     "via": "browser", "content_type": res.get("content_type", "")}, "")
        if res.get("human_check"):
            self.human_check_hosts.add(host)
            return None, (f"{host} asked for a person (a captcha or a press-and-"
                          "hold check); that is not answered automatically")
        return None, "the browser got no file of the expected type"


def _acceptor(kind: str):
    if kind == "model3d":
        return lambda ctype, body: _files.is_step(body) or (
            _files.is_zip(body) and bool(_files.step_members(body)))
    return lambda ctype, body: _files.is_pdf(body)


_ERROR_TITLE = re.compile(r"\b(404|not found|page not found|error)\b", re.I)

_CHECK_MARKERS = ("_abck", "bm-verify", "challenge-platform", "cf-chl",
                  "just a moment", "access denied", "request unsuccessful",
                  "incapsula", "px-captcha", "captcha")


def _is_check_page(text: str) -> bool:
    low = text[:20000].lower()
    return any(m in low for m in _CHECK_MARKERS) and len(low) < 60000


def _visible_text(page: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)[:20000]


# --- the catalogue -------------------------------------------------------

def _catalogue(mpn: str, log: list) -> dict:
    """Manufacturer name and datasheet URLs from whichever catalogue answers."""
    from eda_agent.libimport.providers import get_provider, normalise_mpn
    want = normalise_mpn(mpn)
    out: dict[str, Any] = {"manufacturer": "", "datasheets": [], "sources": []}
    for name in _CATALOGUES:
        try:
            provider = get_provider(name)
            hits = provider.search(mpn, 10)
        except Exception as exc:                        # noqa: BLE001
            log.append(f"catalogue {name}: {str(exc)[:160]}")
            continue
        exact = [h for h in hits if normalise_mpn(h.mpn) == want]
        chosen = exact or [h for h in hits
                           if normalise_mpn(h.mpn).startswith(want)]
        log.append(f"catalogue {name}: {len(hits)} hits, {len(exact)} exact")
        for hit in chosen[:3]:
            if hit.manufacturer and not out["manufacturer"]:
                out["manufacturer"] = hit.manufacturer
            if hit.datasheet and hit.datasheet not in out["datasheets"]:
                out["datasheets"].append(hit.datasheet)
        if chosen:
            out["sources"].append(name)
        if out["manufacturer"] and out["datasheets"]:
            break
    return out


# --- the cache -----------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    replace_with_retry(tmp, path)


def _cached(manifest: Path, root: Path, kinds: tuple) -> Optional[dict]:
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in meta.get("files") or []:
        path = root / entry.get("path", "")
        try:
            if not path.is_file() or _file_sha(path) != entry.get("sha256"):
                return None
        except OSError:
            return None
    tried = set(meta.get("kinds_tried") or [])
    if not set(kinds) <= tried:
        return None
    return meta


# --- the entry point -----------------------------------------------------

def fetch_part_docs(mpn: str, manufacturer: str = "", *,
                    workspace: str | os.PathLike | None = None,
                    kinds: Optional[list[str] | tuple[str, ...]] = None,
                    product_url: str = "",
                    refresh: bool = False,
                    browser: str = "auto",
                    browser_mode: str = "offscreen",
                    timeout_s: float = 300.0) -> dict:
    """Download a part's official documents into the workspace's ``.eda``.

    ``browser``: ``auto`` (plain HTTP first, a real browser where a site
    refuses it), ``always`` or ``never``. ``browser_mode``: ``offscreen``
    (a normal window placed off the desktop), ``visible`` or ``headless``.
    Never raises for a network fault; returns ``{ok, ...}``.
    """
    mpn = str(mpn or "").strip()
    if not mpn:
        return {"ok": False, "reason": "no MPN given"}
    wanted = tuple(k for k in (kinds or DEFAULT_KINDS) if k in KINDS)
    if not wanted:
        return {"ok": False, "reason": f"kinds must be among {list(KINDS)}"}
    if browser not in ("auto", "always", "never"):
        return {"ok": False, "reason": "browser must be auto, always or never"}
    try:
        root = docs_root(workspace)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    safe = _files.safe_name(mpn.upper(), "part")
    manifest = root / "datasheets" / f"{safe}.json"

    if not refresh and manifest.is_file():
        meta = _cached(manifest, root, wanted)
        if meta is not None:
            return _answer(meta, root, manifest, "hit")

    run = _Run(mpn, root, browser_mode, browser, timeout_s)
    run.own_paths = _own_paths(manifest)
    try:
        meta = _fetch(run, mpn, manufacturer, wanted, product_url, safe)
    finally:
        run.close()
    meta["kinds_tried"] = sorted(set(wanted) | set(meta.get("kinds_tried") or []))
    try:
        _write(manifest, json.dumps(meta, indent=2).encode("utf-8"))
    except OSError as exc:
        meta.setdefault("notes", []).append(f"manifest not written: {exc}")
    return _answer(meta, root, manifest, "miss")


def _fetch(run: _Run, mpn: str, manufacturer: str, wanted: tuple,
           product_url: str, safe: str) -> dict:
    site: Optional[Site] = site_for_manufacturer(manufacturer)
    catalogue: dict = {"manufacturer": "", "datasheets": [], "sources": []}
    if site is None and not product_url and not manufacturer:
        # A distinctive prefix (QPA, ADM, TPS) names the maker well enough,
        # and saves a catalogue query: Nexar's free plan counts parts.
        site = site_for_mpn(mpn, min_prefix=3)
        if site is not None:
            run.log.append(f"manufacturer taken from the MPN prefix: {site.name}")
    if site is None and not product_url:
        catalogue = _catalogue(mpn, run.log)
        manufacturer = manufacturer or catalogue["manufacturer"]
        site = site_for_manufacturer(manufacturer)
        for url in catalogue["datasheets"]:
            site = site or site_for_host(urllib.parse.urlsplit(url).hostname or "")
        site = site or site_for_mpn(mpn)
    if product_url and site is None:
        site = site_for_host(urllib.parse.urlsplit(product_url).hostname or "")

    candidates: list[_Candidate] = []
    generics = [g.upper() for g in generic_part_numbers(mpn)]

    def add(kind: str, url: str, title: str = "", referer: str = "",
            source: str = "", rank: int = 0) -> None:
        if kind in wanted and not any(c.url == url and c.kind == kind
                                      for c in candidates):
            candidates.append(_Candidate(kind, url, title, referer, source, rank))

    if site is not None and "datasheet" in wanted:
        for url in site.direct_datasheets(mpn):
            add("datasheet", url, source="manufacturer URL pattern", rank=0)

    page_info: Optional[dict] = None
    portals: set = set()
    pages = [product_url] if product_url else (
        site.product_pages(mpn) if site is not None else [])
    for url in pages[:4]:
        if run.out_of_time():
            break
        info = run.read_page(url)
        if not info:
            continue
        title = str(info.get("title", ""))
        title_blob = f"{title} {info.get('url', '')}".upper()
        if _ERROR_TITLE.search(title):
            run.log.append(f"page {url} is an error page ({title!r}); skipped")
            continue
        if not product_url and not any(g in title_blob for g in generics):
            run.log.append(f"page {url} does not name {mpn}; skipped")
            continue
        page_info = info
        for href, text in info.get("links") or []:
            portal = mentions_portal(href, text)
            if portal:
                portals.add(portal)
            kind = classify_link(href, text, site)
            if not kind:
                continue
            blob = f"{text} {href}".upper()
            named = any(g in blob for g in generics)
            rank = 1 if named else 2
            if kind == "datasheet" and "BRIEF" in blob:
                rank += 2
            add(kind, href, text.replace("\n", " ").strip(),
                referer=info.get("url") or url, source="product page", rank=rank)
        break

    for url in catalogue["datasheets"]:
        add("datasheet", url, source=f"catalogue ({', '.join(catalogue['sources'])})",
            rank=5)

    files: list[dict] = []
    missing: dict[str, str] = {}
    for kind in wanted:
        group = sorted((c for c in candidates if c.kind == kind),
                       key=lambda c: c.rank)
        got = 0
        last_reason = ""
        seen_hashes: set = set()
        for cand in group:
            if got >= LIMITS[kind] or run.out_of_time():
                break
            result, why = run.download(cand)
            if result is None:
                last_reason = why
                continue
            body = result["body"]
            sha = _sha256(body)
            if sha in seen_hashes:
                continue
            seen_hashes.add(sha)
            if kind == "datasheet":
                confirmed = _pdf_names_part(body, generics)
                if confirmed is False and cand.rank >= 5:
                    last_reason = f"{cand.url} does not name {mpn}"
                    continue
            stored = _store(run.root, safe, kind, cand, result, body,
                            run.own_paths)
            files.extend(stored)
            got += 1 if stored else 0
        if not got:
            if not group:
                missing[kind] = _none_found(kind, site, page_info, portals)
            else:
                missing[kind] = last_reason or "no candidate could be downloaded"

    notes = list(run.notes)
    if portals and "model3d" in wanted and not any(
            f["kind"] == "model3d" for f in files):
        notes.append(
            "a 3D model is offered through " + ", ".join(sorted(portals)) +
            ", which needs an account there; it was not fetched. Build the "
            "model from the package drawing instead (lib_create_3d_model)")
    if run.human_check_hosts:
        notes.append("asked for a person (not answered): " +
                     ", ".join(sorted(run.human_check_hosts)))
    if run.browser_error:
        notes.append(f"browser fallback unavailable: {run.browser_error}")
    if run.out_of_time():
        notes.append(f"stopped after the {run.timeout_s:.0f} s budget")

    return {
        "mpn": mpn, "manufacturer": manufacturer or (site.name if site else ""),
        "site": site.name if site else "",
        "product_page": (page_info or {}).get("url", ""),
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "files": files, "missing": missing, "notes": notes,
        "log": run.log[-60:], "kinds_tried": list(wanted),
    }


def _none_found(kind: str, site: Optional[Site], page: Optional[dict],
                portals: set) -> str:
    if site is None and page is None:
        return ("the manufacturer was not recognised and no catalogue gave a "
                "page; pass manufacturer= or product_url=")
    if page is None:
        return "the manufacturer's product page could not be read"
    if kind == "model3d" and portals:
        return "only offered through " + ", ".join(sorted(portals)) + " (sign-in)"
    if kind == "package":
        return ("no separate package drawing is linked; the datasheet's "
                "mechanical section is the source")
    return f"the product page links no {kind}"


def _pdf_names_part(body: bytes, generics: list[str]) -> Optional[bool]:
    """Whether the PDF's first pages name the part; None when unreadable."""
    try:
        import io

        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body))
        text = " ".join((reader.pages[i].extract_text() or "")
                        for i in range(min(3, len(reader.pages)))).upper()
    except Exception:                                   # noqa: BLE001
        return None
    if not text.strip():
        return None
    return any(g in text.replace(" ", "") or g in text for g in generics)


def _own_paths(manifest: Path) -> set:
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(f.get("path", "")) for f in meta.get("files") or []}


def _store(root: Path, safe: str, kind: str, cand: _Candidate, result: dict,
           body: bytes, own: set = frozenset()) -> list[dict]:
    """Write one fetched file. A different file already at the path is
    kept, and this one is written beside it, unless an earlier fetch of
    this part wrote it (``own``)."""

    def taken(path: Path) -> bool:
        return (path.exists() and _rel(path, root) not in own
                and _file_sha(path) != _sha256(body))

    base = {"kind": kind, "url": cand.url, "final_url": result.get("final_url", ""),
            "via": result.get("via", ""), "source": cand.source,
            "title": cand.title or _files.pdf_title(body)}
    out: list[dict] = []
    if kind == "model3d":
        members = [(Path(urllib.parse.urlsplit(cand.url).path).name or f"{safe}.step",
                    body)] if not _files.is_zip(body) else _files.step_members(body)
        for name, data in members:
            name = _files.safe_name(name, f"{safe}.step")
            if not name.lower().endswith((".step", ".stp")):
                name += ".step"
            path = root / "models" / safe / name
            _write(path, data)
            out.append(dict(base, path=_rel(path, root), sha256=_sha256(data),
                            size=len(data)))
        return out
    if kind == "datasheet":
        path = root / "datasheets" / f"{safe}.pdf"
        if taken(path):
            path = root / "datasheets" / f"{safe}-{_sha256(body)[:8]}.pdf"
    else:
        tail = Path(urllib.parse.urlsplit(cand.url).path).stem
        label = _files.safe_name(cand.title.split("\n")[0], tail or kind, limit=60)
        path = root / "datasheets" / safe / f"{kind}-{label}.pdf"
        if taken(path):
            path = path.with_name(f"{path.stem}-{_sha256(body)[:8]}.pdf")
    _write(path, body)
    from eda_agent.design.datasheet_fetch import count_pages
    pages, _how = count_pages(body)
    out.append(dict(base, path=_rel(path, root), sha256=_sha256(body),
                    size=len(body), pages=pages))
    return out


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _answer(meta: dict, root: Path, manifest: Path, cache: str) -> dict:
    files = [dict(f, path=str(root / f["path"])) for f in meta.get("files") or []]
    datasheet = next((f["path"] for f in files if f["kind"] == "datasheet"), None)
    kinds = set(meta.get("kinds_tried") or [])
    ok = bool(datasheet) if "datasheet" in kinds else bool(files)
    out = {
        "ok": ok, "cache": cache, "mpn": meta.get("mpn"),
        "manufacturer": meta.get("manufacturer", ""),
        "product_page": meta.get("product_page", ""),
        "datasheet": datasheet, "files": files,
        "missing": meta.get("missing") or {}, "notes": meta.get("notes") or [],
        "root": str(root), "manifest": str(manifest),
    }
    if not ok:
        out["reason"] = ("no datasheet was found: " +
                         (meta.get("missing") or {}).get("datasheet", "unknown"))
        out["log"] = meta.get("log") or []
    else:
        out["next_step"] = (f"Read {datasheet} (datasheet_find_pages finds the "
                            "package drawing and land pattern pages) and cite "
                            "its URL and revision")
    return out
