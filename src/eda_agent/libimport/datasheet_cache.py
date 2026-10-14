# SPDX-License-Identifier: Apache-2.0
"""Keep a local copy of a datasheet PDF, with its provenance.

This project's standing rule is that component values come from the
datasheet and are never invented (see tools/datasheet_hints.py, which
puts a vendor search query on every component-surfacing response). That
rule is easier to follow when the document itself is on disk: a cached
PDF can be re-read, diffed against a later revision, and cited by path
in a review, where a URL in a chat log cannot.

Fetching reuses providers/_http.py, so a datasheet download gets the
same protections every provider call does: HTTPS only, a host allowlist,
a byte cap, a timeout. What is added here is the part that matters for a
document you intend to trust later:

* the allowlist is DENY BY DEFAULT and comes from configuration, not
  from a built-in list of vendor domains. Such a list would be a guess
  that rots, and the refusal names the host so adding it is one step.
* the payload must actually be a PDF, checked by magic bytes rather than
  by the Content-Type header, which vendor CDNs get wrong routinely.
* a manifest records the URL, SHA-256 and fetch time next to the file,
  so "where did this come from" has an answer that outlives the session.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ..atomicfile import discard, replace_with_retry
from .providers._http import FetchError, cache_dir, get_bytes

#: Comma-separated hosts the datasheet fetcher may talk to. Deny by
#: default: a datasheet URL comes from a model or a web page, and neither
#: is a reason to fetch from an arbitrary host.
HOSTS_ENV = "EDA_AGENT_DATASHEET_HOSTS"

_PDF_MAGIC = b"%PDF-"


def datasheet_dir() -> Path:
    """Where cached datasheets live, under the existing parts cache."""
    return cache_dir() / "datasheets"


def allowed_hosts() -> set[str]:
    raw = os.environ.get(HOSTS_ENV, "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _manifest_path(key: str) -> Path:
    return datasheet_dir() / f"{key}.json"


def _pdf_path(key: str) -> Path:
    return datasheet_dir() / f"{key}.pdf"


def cached_entry(url: str) -> dict[str, Any] | None:
    """The manifest for an already-cached URL, or None.

    A manifest whose PDF has since been deleted counts as a miss, so the
    two can never disagree about what is on disk.
    """
    key = _key(url)
    manifest, pdf = _manifest_path(key), _pdf_path(key)
    if not (manifest.exists() and pdf.exists()):
        return None
    try:
        entry = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry["path"] = str(pdf)
    return entry


def cache_datasheet(url: str, mpn: str = "", force: bool = False) -> dict[str, Any]:
    """Download ``url`` to the datasheet cache and return its manifest.

    Returns ``{ok, path, url, mpn, sha256, bytes, fetched_at, cached}``,
    where ``cached`` is True when an existing copy was reused. Failures
    return ``{ok: False, reason: ...}`` rather than raising: a datasheet
    URL is frequently wrong or dead, and that is ordinary rather than
    exceptional.
    """
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "reason": "no url given"}
    url = url.strip()

    if not force:
        existing = cached_entry(url)
        if existing:
            return {"ok": True, "cached": True, **existing}

    hosts = allowed_hosts()
    if not hosts:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return {"ok": False, "reason":
                f"no datasheet hosts are allowed, so {host or url!r} was not "
                f"fetched. Set {HOSTS_ENV} to the hosts you trust, "
                f"comma-separated (this is deny-by-default on purpose: a "
                f"datasheet URL usually comes from a model or a web page)."}

    try:
        data = get_bytes(url, hosts)
    except FetchError as exc:
        return {"ok": False, "reason": str(exc)}

    if not data.startswith(_PDF_MAGIC):
        return {"ok": False, "reason":
                f"{url} did not return a PDF (no %PDF- header). A vendor "
                f"page that redirects to HTML looks exactly like this."}

    key = _key(url)
    directory = datasheet_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pdf = _pdf_path(key)
    tmp = pdf.with_suffix(".pdf.tmp")
    try:
        tmp.write_bytes(data)
        replace_with_retry(tmp, pdf)
    except OSError as exc:
        discard(tmp)
        return {"ok": False, "reason": f"cannot write {pdf}: {exc}"}

    entry = {
        "url": url,
        "mpn": mpn,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "fetched_at": time.time(),
    }
    manifest_tmp = _manifest_path(key).with_suffix(".json.tmp")
    try:
        manifest_tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        replace_with_retry(manifest_tmp, _manifest_path(key))
    except OSError as exc:
        discard(manifest_tmp)
        return {"ok": False, "reason": f"cannot write manifest: {exc}"}

    return {"ok": True, "cached": False, "path": str(pdf), **entry}


def list_cached() -> list[dict[str, Any]]:
    """Every cached datasheet's manifest, newest first."""
    directory = datasheet_dir()
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for manifest in directory.glob("*.json"):
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # an unreadable manifest is a miss, not a crash
        pdf = manifest.with_suffix(".pdf")
        if not pdf.exists():
            continue
        entry["path"] = str(pdf)
        out.append(entry)
    return sorted(out, key=lambda e: e.get("fetched_at", 0), reverse=True)
