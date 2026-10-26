# SPDX-License-Identifier: Apache-2.0
"""Altium 365 design data through the Nexar GraphQL API (read-only).

Vendor facts used (support.nexar.com, Appendix A of the manual):
  * endpoint https://api.nexar.com/graphql (Hot Chocolate GraphQL server, introspection enabled);
  * design data ("des" operations) requires the OAuth2 Authorization Code flow with PKCE (S256);
    authorize https://identity.nexar.com/connect/authorize, token .../connect/token;
    scopes "openid profile email user.access design.domain offline_access";
  * access tokens are valid for 24 hours; the redirect URL must be registered in the
    Nexar application's Authorization tab;
  * documented queries: desWorkspaces, desProjects(workspaceUrl, after, where) with
    totalCount, pageInfo, nodes { id name description }; desProjectById(id);
  * Nexar terms: do not store Nexar data for more than 24 hours. This module caches
    results in memory only, with a TTL below 24 h, and never writes them to disk.

Field names used by built-in queries that are not shown in the vendor examples are
checked against the live schema by `doctor()` (CLI: altium-ai a365-doctor) before use.

Write operations are deliberately not implemented: raw queries containing `mutation`
or `subscription` are rejected.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from .config import A365Policy
from ..atomicfile import replace_with_retry
from .errors import AuthenticationFailed, NotFound, PermissionDenied, ValidationFailed

SCOPES = "openid profile email user.access design.domain offline_access"

QUERIES = {
    "workspaces": "query { desWorkspaces { url name } }",
    "workspace_infos": "query { desWorkspaceInfos { url workspaceId name description } }",
    "projects": ("query($ws: String!, $after: String) { desProjects(workspaceUrl: $ws, after: $after) "
                 "{ totalCount pageInfo { hasNextPage endCursor } nodes { id name description } } }"),
    "project": "query($id: ID!) { desProjectById(id: $id) { id name description } }",
}
# Library and write operations (Nexar support articles: library access, creating symbols, releasing components)
QUERIES.update({
    "library_folders": "query($ws: String!) { desLibrary(workspaceUrl: $ws) { folders { id name path } } }",
    "library_components": "query($ws: String!) { desLibrary(workspaceUrl: $ws) { components { nodes { id name } } } }",
    "library_components_params": ("query($ws: String!) { desLibrary(workspaceUrl: $ws) { components { nodes { id name "
                                  "details { parameters { type name value } } } } } }"),
    "component": ("query($id: ID!) { desComponentById(id: $id) { id name details { parameters { type name value } } } }"),
    "symbol": "query($id: ID!) { desSymbolById(id: $id) { name revisionInternalId folder { id name path } } }",
})
MUTATIONS = {
    "create_symbol": ("mutation($fileId: String!, $relativePath: String!, $folderId: ID!, $ws: String!, $name: String!) "
                      "{ desCreateSymbol(input: { files: { fileId: $fileId, relativePath: $relativePath }, folderId: $folderId, "
                      "name: $name, workspaceUrl: $ws }) { errors { message } id } }"),
    "create_footprint": ("mutation($fileId: String!, $relativePath: String!, $folderId: ID!, $ws: String!, $name: String!) "
                         "{ desCreateFootprint(input: { files: { fileId: $fileId, relativePath: $relativePath }, folderId: $folderId, "
                         "name: $name, workspaceUrl: $ws }) { errors { message } id } }"),
}
_GQL_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def gql_literal(v) -> str:
    """Serialise a Python value as a GraphQL input literal (keys validated, strings JSON-escaped).
    Used for desReleaseComponent, whose input is documented inline; this avoids depending on an
    undocumented input type name."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(gql_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if not _GQL_KEY.fullmatch(k):
                raise ValidationFailed(f"invalid GraphQL key {k!r}")
            parts.append(f"{k}: {gql_literal(x)}")
        return "{ " + ", ".join(parts) + " }"
    raise ValidationFailed(f"unsupported value type {type(v).__name__}")


def release_mutation(input_obj: dict) -> str:
    return "mutation { desReleaseComponent(input: " + gql_literal(input_obj) + ") { errors { message } } }"
FILE_UPLOAD_URL = "https://files.nexar.com/File/Upload"

# (type, field) pairs that doctor() must find in the live schema
SCHEMA_EXPECTATIONS = [("Query", "desWorkspaces"), ("Query", "desProjects"), ("Query", "desProjectById"),
                       ("DesWorkspace", "url"), ("DesWorkspace", "name"),
                       ("DesProject", "id"), ("DesProject", "name"), ("DesProject", "description"),
                       ("Query", "desLibrary"), ("Query", "desComponentById"), ("Query", "desSymbolById")]
# write operations: checked separately so a missing mutation disables only that tool
WRITE_EXPECTATIONS = {"update_parameters": ("Mutation", "desUpdateComponentRevisionParameters"),
                      "create_symbol": ("Mutation", "desCreateSymbol"),
                      "create_footprint": ("Mutation", "desCreateFootprint"),
                      "release_component": ("Mutation", "desReleaseComponent")}

_FORBIDDEN_OPS = re.compile(r"\b(mutation|subscription)\b", re.I)


def assert_read_only(query: str) -> None:
    stripped = re.sub(r"\"(?:\\.|[^\"\\])*\"", '""', query)  # ignore string literals
    stripped = re.sub(r"#[^\n]*", "", stripped)              # ignore comments
    if _FORBIDDEN_OPS.search(stripped):
        raise PermissionDenied("only read-only GraphQL queries are allowed")
    if len(query) > 20000:
        raise ValidationFailed("query too long")


# ---------------------------------------------------------------- token storage
class TokenStore:
    """Refresh/access token persistence. On Windows the blob is encrypted with DPAPI
    (CryptProtectData, current user scope); elsewhere it is stored with 0600 permissions
    (tests and development only)."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _dpapi(data: bytes, protect: bool) -> bytes:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = (fn(ctypes.byref(blob_in), "altium-ai-a365" if protect else None, None, None, None, 0x01,
                 ctypes.byref(blob_out)) if protect else
              fn(ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)))
        if not ok:
            raise AuthenticationFailed("DPAPI operation failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)

    def save(self, token: dict) -> None:
        raw = json.dumps(token).encode("utf-8")
        data = self._dpapi(raw, True) if sys.platform == "win32" else raw
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(data)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        # A scanner or indexer can hold the target for a moment on
        # Windows, which makes a bare os.replace fail and lose the
        # freshly written token.
        replace_with_retry(tmp, self.path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        data = self.path.read_bytes()
        raw = self._dpapi(data, False) if sys.platform == "win32" else data
        return json.loads(raw)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# ---------------------------------------------------------------- PKCE login
def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg: A365Policy, challenge: str, state: str) -> str:
    q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
         "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    return f"{cfg.identity_url}/connect/authorize?{urllib.parse.urlencode(q)}"


def _post_form(url: str, data: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AuthenticationFailed(f"token endpoint returned {e.code}", details={"body": e.read()[:500].decode(errors="replace")})


def interactive_login(cfg: A365Policy, client_secret: str | None = None, open_browser=webbrowser.open,
                      wait_s: float = 300) -> dict:  # pragma: no cover - needs a browser and Nexar account
    """Owner-only (CLI). Opens the Nexar login page, receives the code on the loopback redirect URI,
    exchanges it for tokens. The redirect URI must be registered in the Nexar app."""
    u = urllib.parse.urlparse(cfg.redirect_uri)
    if u.hostname not in ("localhost", "127.0.0.1"):
        raise ValidationFailed("redirect_uri must be a loopback URL for the CLI login")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path == u.path and q.get("state", [""])[0] == state:
                result["code"] = q.get("code", [""])[0]
                msg = b"Altium AI: Nexar login complete. You can close this tab."
            else:
                msg = b"Unexpected request."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg)

    srv = http.server.HTTPServer(("127.0.0.1", u.port or 80), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        open_browser(authorize_url(cfg, challenge, state))
        t0 = time.monotonic()
        while "code" not in result and time.monotonic() - t0 < wait_s:
            time.sleep(0.2)
    finally:
        srv.shutdown()
    if not result.get("code"):
        raise AuthenticationFailed("no authorization code received")
    data = {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": cfg.redirect_uri,
            "client_id": cfg.client_id, "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    tok = _post_form(f"{cfg.identity_url}/connect/token", data, cfg.timeout_s)
    tok["obtained_at"] = time.time()
    return tok


# ---------------------------------------------------------------- client
class NexarClient:
    def __init__(self, cfg: A365Policy, store: TokenStore | None = None, client_secret: str | None = None,
                 opener=urllib.request.urlopen, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.store = store or TokenStore(cfg.token_store)
        self.client_secret = client_secret or os.environ.get("ALTIUM_AI_NEXAR_CLIENT_SECRET")
        self._open, self._clock, self._sleep = opener, clock, sleep
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # -- tokens
    def _token(self) -> str:
        tok = self.store.load()
        if not tok:
            raise AuthenticationFailed("no Nexar token; owner must run `altium-ai a365-login`")
        if self._clock() > tok.get("obtained_at", 0) + int(tok.get("expires_in", 0)) - 120:
            if not tok.get("refresh_token"):
                raise AuthenticationFailed("Nexar token expired and no refresh token; run `altium-ai a365-login`")
            data = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": self.cfg.client_id}
            if self.client_secret:
                data["client_secret"] = self.client_secret
            new = _post_form(f"{self.cfg.identity_url}/connect/token", data, self.cfg.timeout_s)
            new.setdefault("refresh_token", tok["refresh_token"])
            new["obtained_at"] = self._clock()
            self.store.save(new)
            tok = new
        return tok["access_token"]

    # -- transport
    def execute(self, query: str, variables: dict | None = None, *, use_cache: bool = True,
                _mutation_template: str | None = None) -> dict:
        if _mutation_template is None:
            assert_read_only(query)
        else:
            fixed = MUTATIONS.get(_mutation_template)
            ok = (fixed == query) if fixed else (
                (_mutation_template == "release_component" and query.startswith("mutation { desReleaseComponent(input: "))
                or (_mutation_template == "update_parameters_inline"
                    and query.startswith("mutation { desUpdateComponentRevisionParameters(input: ")))
            if not ok:
                raise PermissionDenied("mutations may only use the fixed templates in a365.py")
            use_cache = False
        key = hashlib.sha256(json.dumps([query, variables], sort_keys=True).encode()).hexdigest()
        now = self._clock()
        if use_cache and self.cfg.cache_ttl_s > 0:
            with self._lock:
                hit = self._cache.get(key)
                if hit and now - hit[0] < self.cfg.cache_ttl_s:
                    return {**hit[1], "cached": True}
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        delay = 1.0
        for attempt in range(4):
            req = urllib.request.Request(self.cfg.api_url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self._token()}"})
            try:
                with self._open(req, timeout=self.cfg.timeout_s) as r:
                    payload = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < 3:
                    self._sleep(delay)
                    delay *= 2
                    continue
                if e.code in (401, 403):
                    raise AuthenticationFailed(f"Nexar returned {e.code}")
                raise ValidationFailed(f"Nexar HTTP {e.code}", details={"body": e.read()[:500].decode(errors="replace")})
        if payload.get("errors"):
            raise ValidationFailed("GraphQL errors", details={"errors": payload["errors"][:10]})
        result = {"data": payload.get("data"), "cached": False}
        if _mutation_template is not None:
            return result
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cfg.cache_ttl_s}
            self._cache[key] = (now, result)
        return result

    # -- policy
    def check_workspace(self, url: str) -> str:
        if not url.endswith("/"):
            url += "/"
        if self.cfg.workspaces and url not in self.cfg.workspaces:
            raise PermissionDenied(f"workspace {url} is not on the owner allow-list")
        return url

    # -- high level
    def workspaces(self) -> list[dict]:
        ws = self.execute(QUERIES["workspaces"])["data"]["desWorkspaces"] or []
        if self.cfg.workspaces:
            ws = [w for w in ws if w.get("url") in self.cfg.workspaces]
        return ws

    def workspace_infos(self) -> list[dict]:
        """Like workspaces(), plus workspaceId and description (desWorkspaceInfos)."""
        ws = self.execute(QUERIES["workspace_infos"])["data"]["desWorkspaceInfos"] or []
        if self.cfg.workspaces:
            ws = [w for w in ws if (w.get("url") or "") in self.cfg.workspaces]
        return ws

    def projects(self, workspace_url: str, name_contains: str = "", limit: int = 200) -> dict:
        ws = self.check_workspace(workspace_url)
        out, after, pages = [], None, 0
        needle = name_contains.lower()
        while len(out) < limit and pages < 100:
            d = self.execute(QUERIES["projects"], {"ws": ws, "after": after})["data"]["desProjects"]
            pages += 1
            out.extend(n for n in d["nodes"] if needle in (n.get("name") or "").lower())
            if not d["pageInfo"]["hasNextPage"]:
                break
            after = d["pageInfo"]["endCursor"]
        return {"total_in_workspace": d["totalCount"], "matched": len(out[:limit]), "projects": out[:limit]}

    def project(self, project_id: str) -> dict:
        d = self.execute(QUERIES["project"], {"id": project_id})["data"]["desProjectById"]
        if d is None:
            raise NotFound(f"project {project_id} not found or not accessible")
        return d

    def type_fields(self, type_name: str) -> dict:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", type_name):
            raise ValidationFailed("invalid type name")
        q = ('query($n: String!) { __type(name: $n) { name kind description fields { name description '
             'args { name type { name kind ofType { name kind } } } type { name kind ofType { name kind ofType { name kind } } } } } }')
        t = self.execute(q, {"n": type_name})["data"]["__type"]
        if t is None:
            raise NotFound(f"type {type_name} not in schema")
        return t

    # ------------------------------------------------------------ library (read)
    def library_folders(self, workspace_url: str) -> list[dict]:
        ws = self.check_workspace(workspace_url)
        return self.execute(QUERIES["library_folders"], {"ws": ws})["data"]["desLibrary"]["folders"] or []

    def library_components(self, workspace_url: str, name_contains: str = "") -> list[dict]:
        ws = self.check_workspace(workspace_url)
        nodes = self.execute(QUERIES["library_components"], {"ws": ws})["data"]["desLibrary"]["components"]["nodes"] or []
        return [n for n in nodes if name_contains.lower() in (n.get("name") or "").lower()]

    def component(self, component_id: str) -> dict:
        d = self.execute(QUERIES["component"], {"id": component_id})["data"]["desComponentById"]
        if d is None:
            raise NotFound(f"component {component_id} not found")
        return d

    def library_components_params(self, workspace_url: str) -> list[dict]:
        """Like library_components(), plus each component's parameters (one call instead of N)."""
        ws = self.check_workspace(workspace_url)
        nodes = self.execute(QUERIES["library_components_params"], {"ws": ws})["data"]["desLibrary"]["components"]["nodes"]
        return nodes or []

    def entity(self, query_field: str, entity_id: str, max_fields: int = 60) -> dict:
        """Schema-driven read of any des*ById entity: selects the entity's scalar fields as listed by the live
        schema (no guessed field names)."""
        if not re.fullmatch(r"des[A-Za-z]+ById", query_field):
            raise ValidationFailed("query_field must look like desXxxById")
        qf = {f["name"]: f for f in (self.type_fields("Query").get("fields") or [])}
        if query_field not in qf:
            raise NotFound(f"{query_field} is not in the live schema")
        t = qf[query_field]["type"]
        while t.get("ofType"):
            t = t["ofType"]
        tf = self.type_fields(t["name"]).get("fields") or []

        def scalar(f):
            ty = f["type"]
            while ty.get("ofType") and ty["kind"] in ("NON_NULL", "LIST"):
                ty = ty["ofType"]
            return ty["kind"] in ("SCALAR", "ENUM")
        names = [f["name"] for f in tf if scalar(f) and not f.get("args")][:max_fields]
        objs = [f["name"] for f in tf if not scalar(f)]
        q = f'query($id: ID!) {{ {query_field}(id: $id) {{ {" ".join(names)} }} }}'
        d = self.execute(q, {"id": entity_id})["data"][query_field]
        if d is None:
            raise NotFound(f"{query_field}({entity_id}) returned nothing")
        return {"type": t["name"], "fields": d, "object_fields_available": objs[:80]}

    # ------------------------------------------------------------ library (write; owner-approved jobs only)
    def write_enabled(self, op: str) -> None:
        typ, field = WRITE_EXPECTATIONS[op]
        fields = {f["name"] for f in (self.type_fields(typ).get("fields") or [])}
        if field not in fields:
            raise NotFound(f"Nexar schema has no {typ}.{field}; tool disabled", details={"op": op})

    def upload_file(self, path: Path, boundary: str | None = None) -> str:
        """Nexar File Service: multipart/form-data POST with header 'token'; returns the file id (valid 24 h)."""
        data = Path(path).read_bytes()
        b = boundary or "----altiumai" + secrets.token_hex(12)
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{Path(path).name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request(FILE_UPLOAD_URL, data=body, method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={b}", "token": self._token()})
        with self._open(req, timeout=self.cfg.timeout_s) as r:
            fid = r.read().decode("utf-8", errors="replace").strip().strip('"')
        if not fid:
            raise ValidationFailed("file service returned no file id")
        return fid

    def _check_errors(self, payload: dict, field: str) -> dict:
        res = (payload.get("data") or {}).get(field) or {}
        errs = [e.get("message") for e in (res.get("errors") or [])]
        if errs:
            raise ValidationFailed(f"{field} returned errors", details={"errors": errs})
        return res

    def create_model(self, kind: str, workspace_url: str, folder_id: str, name: str, file_path: Path) -> dict:
        op = {"symbol": "create_symbol", "footprint": "create_footprint"}[kind]
        self.write_enabled(op)
        ws = self.check_workspace(workspace_url)
        fid = self.upload_file(file_path)
        q = MUTATIONS[op]
        payload = self.execute(q, {"fileId": fid, "relativePath": f"Released/{Path(file_path).name}",
                                   "folderId": folder_id, "ws": ws, "name": name}, _mutation_template=op)
        res = self._check_errors(payload, "desCreateSymbol" if kind == "symbol" else "desCreateFootprint")
        if not res.get("id"):
            raise ValidationFailed(f"desCreate{kind.capitalize()} returned no id")
        return {"id": res["id"], "file_id": fid}

    def release_component(self, input_obj: dict) -> dict:
        self.write_enabled("release_component")
        self.check_workspace(input_obj["workspaceUrl"])
        payload = self.execute(release_mutation(input_obj), {}, _mutation_template="release_component")
        self._check_errors(payload, "desReleaseComponent")
        return {"released": input_obj["componentItemName"]}

    def update_parameters(self, component_id: str, parameters: list[dict], release_note: str,
                          replace_existing: bool = False) -> dict:
        """desUpdateComponentRevisionParameters (Nexar parameters guide). replace_existing=True deletes ALL
        existing parameters first (documented); the component id changes with the new revision."""
        self.write_enabled("update_parameters")
        payload = self._update_params_payload(component_id, parameters, release_note, replace_existing)
        new_id = ((payload.get("data") or {}).get("desUpdateComponentRevisionParameters") or {}).get("componentId")
        if not new_id:
            raise ValidationFailed("parameter update returned no componentId", details={"payload": payload})
        return {"new_component_id": new_id}

    def _update_params_payload(self, component_id, parameters, release_note, replace_existing):
        # inline input exactly as in Nexar's documented example (no undocumented input type names)
        vars_ = {"componentId": component_id, "releaseNote": release_note, "replaceExisting": replace_existing,
                 "parameters": [{"name": p["name"], "value": str(p["value"])} for p in parameters]}
        inline = "mutation { desUpdateComponentRevisionParameters(input: " + gql_literal(vars_) + ") { componentId } }"
        return self.execute(inline, {}, _mutation_template="update_parameters_inline")

    def doctor(self) -> dict:
        """Check that every field used by built-in queries exists in the live schema."""
        missing, cache = [], {}
        for typ, field in SCHEMA_EXPECTATIONS:
            if typ not in cache:
                try:
                    cache[typ] = {f["name"] for f in (self.type_fields(typ).get("fields") or [])}
                except NotFound:
                    cache[typ] = set()
            if field not in cache[typ]:
                missing.append(f"{typ}.{field}")
        writes = {}
        for op, (typ, field) in WRITE_EXPECTATIONS.items():
            if typ not in cache:
                try:
                    cache[typ] = {f["name"] for f in (self.type_fields(typ).get("fields") or [])}
                except NotFound:
                    cache[typ] = set()
            writes[op] = field in cache[typ]
        return {"ok": not missing, "missing": missing, "checked": len(SCHEMA_EXPECTATIONS), "write_operations": writes}
