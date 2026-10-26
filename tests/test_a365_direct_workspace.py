# SPDX-License-Identifier: Apache-2.0
"""The direct Altium 365 route: the workspace's own API, not Nexar.

Altium documents a GraphQL API on the workspace itself
(altium.com/documentation/altium-developer-center/altium-365/api): POST
to ``https://{workspace-domain}/api/graphql/`` with a bearer token
created under Admin > Developer. This fork only ever spoke to Nexar,
which proxies the design domain but carries none of the collaboration,
workflow or script-execution contexts.

The schema is the same ``des*`` one either way, so one client serves
both and these tests pin what actually differs: the URL, where the token
comes from, and that an expired direct token is reported rather than
refreshed against an identity server that knows nothing about it.

NOT verified against a live workspace. Every test here drives a fake
opener, so what is proven is the request this code builds, not that
Altium accepts it. The live check needs a workspace domain and a token;
see docs/RELEASE_VERIFICATION.md.
"""
from __future__ import annotations

import json

import pytest

from eda_agent.engineering import a365
from eda_agent.engineering.config import A365Policy
from eda_agent.engineering.errors import AuthenticationFailed


class _Reply:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _client(**policy):
    """A client whose transport records the request instead of sending it."""
    sent: list = []

    def opener(req, timeout=None):
        sent.append(req)
        return _Reply({"data": {"desProjects": {"nodes": []}}})

    cfg = A365Policy(client_id="", cache_ttl_s=0, **policy)
    return a365.NexarClient(cfg, opener=opener), sent


def _direct(**extra):
    return _client(workspace_domain="acme.365.altium.com",
                   direct_token="tok-from-admin-developer", **extra)


# --- which endpoint ---------------------------------------------------------

def test_a_workspace_domain_posts_to_that_workspace():
    client, sent = _direct()
    client.execute("query { desProjects { nodes { id } } }")
    assert sent[0].full_url == "https://acme.365.altium.com/api/graphql/"


def test_without_a_domain_it_still_goes_to_nexar():
    client, sent = _client()
    client.store.load = lambda: {"access_token": "n", "expires_in": 9999,
                                 "obtained_at": 10 ** 12}
    client.execute("query { desProjects { nodes { id } } }")
    assert sent[0].full_url == "https://api.nexar.com/graphql"


@pytest.mark.parametrize("given", [
    "acme.365.altium.com",
    "https://acme.365.altium.com",
    "acme.365.altium.com/",
    "  https://acme.365.altium.com/  ",
])
def test_the_domain_is_accepted_however_it_is_written(given):
    """Pasted from a browser it carries a scheme, a slash, or both."""
    cfg = A365Policy(client_id="", workspace_domain=given)
    assert cfg.endpoint == "https://acme.365.altium.com/api/graphql/"


def test_the_trailing_slash_is_kept():
    """The documented spelling. A redirect to add it would drop the
    Authorization header on the hop, which reads as a bad token."""
    cfg = A365Policy(client_id="", workspace_domain="acme.365.altium.com")
    assert cfg.endpoint.endswith("/api/graphql/")


# --- the request Altium documents -------------------------------------------

def test_the_request_is_the_one_the_documentation_shows():
    client, sent = _direct()
    client.execute("query { desProjects(first: 5) { nodes { id name } } }")
    req = sent[0]

    assert req.method == "POST"
    assert req.get_header("Authorization") == "Bearer tok-from-admin-developer"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data)
    assert body["query"] == "query { desProjects(first: 5) { nodes { id name } } }"


# --- tokens -----------------------------------------------------------------

def test_the_direct_token_is_used_and_the_store_is_left_alone():
    """No OAuth client, no stored token, no refresh: just the header."""
    client, sent = _direct()

    def fail(*_a, **_k):
        raise AssertionError("the direct route must not touch the token store")

    client.store.load = fail
    client.execute("query { desProjects { nodes { id } } }")
    assert sent[0].get_header("Authorization").endswith("tok-from-admin-developer")


def test_a_missing_direct_token_says_where_to_make_one():
    client, _ = _client(workspace_domain="acme.365.altium.com")
    with pytest.raises(AuthenticationFailed) as caught:
        client.execute("query { desProjects { nodes { id } } }")
    message = str(caught.value)
    assert "acme.365.altium.com" in message
    assert "Admin > Developer" in message


def test_the_direct_route_never_calls_the_nexar_identity_server():
    """A workspace token has no refresh grant. Trying one would fail with
    an error about Nexar, which is not where the token came from."""
    client, _ = _direct()
    client._clock = lambda: 10 ** 12          # far past any expiry
    client.store.load = lambda: {"access_token": "stale", "expires_in": 1,
                                 "obtained_at": 0, "refresh_token": "r"}
    assert client._token() == "tok-from-admin-developer"


# --- file uploads -----------------------------------------------------------

def test_uploads_follow_the_route(tmp_path):
    cfg = A365Policy(client_id="", workspace_domain="acme.365.altium.com")
    assert cfg.files_endpoint == "https://acme.365.altium.com/api/files"
    assert A365Policy(client_id="").files_endpoint.startswith("https://files.nexar.com")
