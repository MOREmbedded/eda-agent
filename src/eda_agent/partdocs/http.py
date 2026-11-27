# SPDX-License-Identifier: Apache-2.0
"""Plain HTTP, the first and cheapest way to a document.

The client says who it is (an eda-agent User-Agent, never a browser's)
and behaves politely: cookies are kept for the run, requests to one host
are spaced out, and an HTTP 429 is answered once by waiting for its
``Retry-After`` (when short) rather than by hammering. A site that still
refuses, or that serves a check page, is handed to the real browser.
"""

from __future__ import annotations

import http.cookiejar
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

__all__ = ["HttpClient", "HttpResult", "USER_AGENT"]

USER_AGENT = ("eda-agent part documents "
              "(+https://github.com/MOREmbedded/eda-agent)")
MAX_BYTES = 60 * 1024 * 1024
_CHUNK = 1024 * 1024


@dataclass
class HttpResult:
    ok: bool
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    reason: str = ""
    #: True when the site refused in a way a browser may get past
    #: (429, 403, a timeout, a check page).
    wants_browser: bool = False


class HttpClient:
    def __init__(self, *, timeout: float = 25.0, min_gap_s: float = 1.0,
                 max_bytes: int = MAX_BYTES) -> None:
        self.timeout = timeout
        self.min_gap_s = min_gap_s
        self.max_bytes = max_bytes
        self._last: dict[str, float] = {}
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _pace(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_gap_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    def get(self, url: str, *, referer: str = "", accept: str = "*/*",
            retry_429: bool = True) -> HttpResult:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return HttpResult(False, url, reason="not an http(s) URL")
        host = parts.hostname.lower()
        headers = {"User-Agent": USER_AGENT, "Accept": accept,
                   "Accept-Language": "en"}
        if referer:
            headers["Referer"] = referer
        self._pace(host)
        try:
            with self._opener.open(urllib.request.Request(url, headers=headers),
                                   timeout=self.timeout) as resp:
                ctype = str(resp.headers.get("Content-Type") or "")
                chunks: list[bytes] = []
                total = 0
                while True:
                    block = resp.read(_CHUNK)
                    if not block:
                        break
                    chunks.append(block)
                    total += len(block)
                    if total > self.max_bytes:
                        return HttpResult(False, url, reason=(
                            f"larger than {self.max_bytes} bytes"))
                return HttpResult(True, url, resp.geturl() or url,
                                  int(getattr(resp, "status", 200) or 200),
                                  ctype, b"".join(chunks))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and retry_429:
                delay = _retry_after(exc.headers.get("Retry-After"))
                if delay is not None and delay <= 10:
                    time.sleep(delay)
                    return self.get(url, referer=referer, accept=accept,
                                    retry_429=False)
            return HttpResult(False, url, status=exc.code,
                              reason=f"HTTP {exc.code}",
                              wants_browser=exc.code in (403, 429, 503))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            return HttpResult(False, url, reason=f"no answer: {reason}",
                              wants_browser=True)


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
