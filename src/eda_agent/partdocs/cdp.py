# SPDX-License-Identifier: Apache-2.0
"""A real browser, driven over the Chrome DevTools Protocol.

Some manufacturer sites answer a plain HTTP client with HTTP 429 or a
JavaScript check that only a browser can run (Qorvo, Analog Devices).
For those, the document is fetched the way a person would fetch it: an
ordinary Edge or Chrome window opens the page, the page's own scripts
run, and the file the site then serves is kept. Nothing here forges a
browser identity, solves a captcha or hides automation. When a site asks
for a human (a captcha, a sign-in), the fetch stops and says so.

Standard library only. The DevTools protocol runs over a WebSocket, and
the few lines of RFC 6455 it needs (a client handshake, masked text
frames out, unmasked frames in) are here rather than in a dependency.

The browser uses a profile of its own (never the person's), started with
``--remote-debugging-port=0`` so it picks a free port and writes it to
``DevToolsActivePort`` in that profile.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["BrowserError", "BrowserSession", "find_browser",
           "looks_like_human_check"]

#: How the window is shown. ``offscreen``: a normal window placed off the
#: visible desktop, so nothing flashes up while it works. ``visible``: a
#: normal window where it can be watched. ``headless``: no window at all;
#: some sites refuse a headless browser, so it is not the default.
MODES = ("offscreen", "visible", "headless")

_HUMAN_CHECK_MARKERS = (
    "captcha", "verify you are human", "are you a robot",
    "i am not a robot", "press & hold", "press and hold",
    "complete the security check", "unusual traffic",
)


class BrowserError(RuntimeError):
    """The browser could not be started, reached or driven."""


def find_browser(explicit: Optional[str] = None) -> Optional[str]:
    """A Chromium-family browser on this machine (Edge first), or None."""
    override = explicit or os.environ.get("EDA_AGENT_BROWSER", "").strip()
    if override:
        return override if Path(override).exists() else None
    from ..render.rasterize import find_browser as _find
    return _find(None)


def looks_like_human_check(title: str, text: str) -> bool:
    """True when a page asks for a person: a captcha or a 'press and hold'."""
    blob = f"{title}\n{text[:5000]}".lower()
    return any(marker in blob for marker in _HUMAN_CHECK_MARKERS)


# --- WebSocket (RFC 6455), client side, just what CDP needs ---------------

class _WebSocket:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(request.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserError("the browser closed the DevTools connection")
            head += chunk
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise BrowserError(f"DevTools handshake refused: {status!r}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise BrowserError("the browser closed the DevTools connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, timeout: float) -> Optional[str]:
        """One text message, or None when nothing came within ``timeout``."""
        self.sock.settimeout(timeout)
        message = b""
        try:
            while True:
                b0, b1 = self._read(2)
                opcode, length = b0 & 0x0F, b1 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read(8))[0]
                mask = self._read(4) if b1 & 0x80 else b""
                data = self._read(length)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 0x8:
                    raise BrowserError("the browser closed the DevTools connection")
                if opcode == 0x9:                     # ping: answer, keep reading
                    continue
                if opcode in (0x0, 0x1, 0x2):
                    message += data
                    if b0 & 0x80:
                        return message.decode("utf-8", "replace")
        except socket.timeout:
            if message:
                raise BrowserError("a DevTools message was cut off")
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --- the browser ---------------------------------------------------------

class BrowserSession:
    """One browser process and one tab, closed by ``close()`` or ``with``."""

    def __init__(self, profile_dir: Path, *, mode: str = "offscreen",
                 browser: Optional[str] = None, start_timeout: float = 60.0) -> None:
        if mode not in MODES:
            raise BrowserError(f"mode must be one of {MODES}, not {mode!r}")
        exe = find_browser(browser)
        if not exe:
            raise BrowserError(
                "no Edge or Chrome found; set EDA_AGENT_BROWSER to the browser's .exe")
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port_file = self.profile_dir / "DevToolsActivePort"
        try:
            port_file.unlink()
        except OSError:
            pass
        args = [exe, "--remote-debugging-port=0",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-features=msEdgeFirstRunExperience",
                "--window-size=1280,1000"]
        if mode == "offscreen":
            args.append("--window-position=-32000,-32000")
        elif mode == "headless":
            args.append("--headless=new")
        args.append("about:blank")
        from ..noconsole import no_window
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **no_window())
        deadline = time.monotonic() + start_timeout
        ws_path = ""
        port = 0
        while time.monotonic() < deadline:
            try:
                lines = port_file.read_text(encoding="utf-8").split("\n")
                port, ws_path = int(lines[0]), lines[1].strip()
                if port and ws_path:
                    break
            except (OSError, ValueError, IndexError):
                pass
            time.sleep(0.2)
        if not (port and ws_path):
            self.close()
            raise BrowserError(
                f"the browser at {exe} did not open its DevTools port within "
                f"{start_timeout:.0f} s (is another instance using the profile "
                f"{self.profile_dir}?)")
        self.ws = _WebSocket(f"ws://127.0.0.1:{port}{ws_path}")
        self._next = 0
        self._events: deque = deque()
        self.session = ""
        target = self.call("Target.createTarget", {"url": "about:blank"})
        attached = self.call("Target.attachToTarget",
                             {"targetId": target["targetId"], "flatten": True})
        self.session = attached["sessionId"]
        self.call("Page.enable")
        self.call("Runtime.enable")

    # -- protocol -----------------------------------------------------

    def send(self, method: str, params: Optional[dict] = None,
             browser_level: bool = False) -> int:
        """Send a command and return its id without waiting for the answer.

        Needed where the answer depends on events this side must handle
        first: ``Page.navigate`` does not answer while the response it
        waits for is held by ``Fetch``.
        """
        self._next += 1
        mid = self._next
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if self.session and not browser_level:
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        return mid

    def call(self, method: str, params: Optional[dict] = None,
             timeout: float = 60.0, browser_level: bool = False) -> dict:
        mid = self.send(method, params, browser_level)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BrowserError(f"{method} got no answer in {timeout:.0f} s")
            raw = self.ws.recv(min(left, 1.0))
            if raw is None:
                continue
            reply = json.loads(raw)
            if reply.get("id") == mid:
                if "error" in reply:
                    raise BrowserError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result") or {}
            if "method" in reply:
                self._events.append(reply)

    def events(self, timeout: float) -> list[dict]:
        """Events already queued, plus any arriving within ``timeout``."""
        out = list(self._events)
        self._events.clear()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.ws.recv(max(0.05, min(0.5, deadline - time.monotonic())))
            if raw is None:
                if out:
                    break
                continue
            msg = json.loads(raw)
            if "method" in msg:
                out.append(msg)
        return out

    def evaluate(self, expression: str, timeout: float = 60.0) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True}, timeout=timeout)
        if result.get("exceptionDetails"):
            text = result["exceptionDetails"].get("text", "script error")
            raise BrowserError(f"page script failed: {text}")
        return (result.get("result") or {}).get("value")

    # -- what callers use ---------------------------------------------

    def open_page(self, url: str, *, settle_s: float = 30.0) -> dict:
        """Open ``url`` and wait for the page to settle.

        A site's JavaScript check reloads the page once it has run, so the
        wait goes on while the title looks like a check page. Returns
        ``{url, title, text, links: [[href, text]], human_check}``.
        """
        self.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + settle_s
        info: dict = {}
        stable = 0
        last = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                info = self.evaluate(
                    "({url: location.href, title: document.title, "
                    "ready: document.readyState, "
                    "text: (document.body ? document.body.innerText : '').slice(0, 20000), "
                    "links: Array.from(document.querySelectorAll('a[href]'))"
                    ".slice(0, 3000).map(a => [a.href, (a.innerText || a.title || "
                    "a.getAttribute('aria-label') || '').trim().slice(0, 200)])})",
                    timeout=20) or {}
            except BrowserError:
                continue                      # navigating: the context went away
            key = (info.get("url"), info.get("title"), len(info.get("links") or []))
            stable = stable + 1 if key == last and info.get("ready") == "complete" else 0
            last = key
            if stable >= 2 and len(info.get("links") or []) > 5:
                break
        info["human_check"] = looks_like_human_check(
            str(info.get("title", "")), str(info.get("text", "")))
        return info

    def download(self, url: str, accept: Callable[[str, bytes], bool], *,
                 timeout_s: float = 60.0, max_bytes: int = 60 * 1024 * 1024) -> dict:
        """Navigate to ``url`` and keep the first non-HTML response ``accept``s.

        Every document response is held at the response stage. HTML (a
        check page, a redirect page) is let through, so the site's own
        scripts run as they would for a person; the first response whose
        body ``accept(content_type, body)`` approves is returned as
        ``{ok, url, content_type, body}``. A response the browser would
        save as a download is caught here too, before it is written.
        """
        self.call("Fetch.enable", {"patterns": [
            {"urlPattern": "*", "requestStage": "Response",
             "resourceType": "Document"}]})
        try:
            self.send("Page.navigate", {"url": url})
            deadline = time.monotonic() + timeout_s
            seen: list[str] = []
            while time.monotonic() < deadline:
                for event in self.events(1.0):
                    if event.get("method") != "Fetch.requestPaused":
                        continue
                    p = event.get("params") or {}
                    rid = p.get("requestId")
                    headers = {h.get("name", "").lower(): h.get("value", "")
                               for h in p.get("responseHeaders") or []}
                    ctype = headers.get("content-type", "")
                    status = int(p.get("responseStatusCode") or 0)
                    req_url = (p.get("request") or {}).get("url", "")
                    seen.append(f"{status} {ctype or '-'} {req_url}")
                    body = b""
                    if status == 200 and "text/html" not in ctype.lower():
                        try:
                            length = int(headers.get("content-length") or 0)
                        except ValueError:
                            length = 0
                        if length <= max_bytes:
                            try:
                                got = self.call("Fetch.getResponseBody",
                                                {"requestId": rid}, timeout=120)
                                body = (base64.b64decode(got.get("body", ""))
                                        if got.get("base64Encoded")
                                        else str(got.get("body", "")).encode("utf-8"))
                            except BrowserError:
                                body = b""
                    try:
                        self.call("Fetch.continueRequest", {"requestId": rid})
                    except BrowserError:
                        pass
                    if body and len(body) <= max_bytes and accept(ctype, body):
                        return {"ok": True, "url": req_url, "content_type": ctype,
                                "body": body, "responses": seen}
            title, text = "", ""
            try:
                title = str(self.evaluate("document.title", timeout=10) or "")
                text = str(self.evaluate(
                    "document.body ? document.body.innerText.slice(0, 5000) : ''",
                    timeout=10) or "")
            except BrowserError:
                pass
            return {"ok": False, "responses": seen, "title": title,
                    "human_check": looks_like_human_check(title, text)}
        finally:
            try:
                self.call("Fetch.disable")
            except BrowserError:
                pass

    def close(self) -> None:
        ws = getattr(self, "ws", None)
        if ws is not None:
            try:
                self.call("Browser.close", browser_level=True, timeout=5)
            except Exception:                           # noqa: BLE001
                pass
            ws.close()
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except Exception:                           # noqa: BLE001
                from ..render.rasterize import _kill_tree
                _kill_tree(proc)
        _stop_profile_processes(self.profile_dir)

    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _stop_profile_processes(profile_dir: Path) -> None:
    """Stop any browser process still running on this profile.

    Edge hands its window to a second process and lets the first exit,
    so the process started here is not the one left running. Only
    processes whose command line names this profile are touched; the
    person's own browser never is.
    """
    try:
        import psutil
    except ImportError:
        return
    marker = f"--user-data-dir={profile_dir}".lower()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            line = " ".join(proc.info.get("cmdline") or []).lower()
            if marker in line:
                proc.kill()
        except (psutil.Error, OSError):
            continue


def remove_profile(profile_dir: Path) -> None:
    """Drop a browser profile (the caller's choice; the default keeps it)."""
    shutil.rmtree(profile_dir, ignore_errors=True)
