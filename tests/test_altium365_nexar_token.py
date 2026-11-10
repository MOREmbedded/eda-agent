# SPDX-License-Identifier: Apache-2.0
"""The Nexar token request: a confidential app's secret, and a refusal's reason.

All offline. A portal app at Nexar is a confidential client, so the
authorization-code and refresh grants carry its secret when one is set,
and a refused request reports what Nexar said instead of only the status.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from eda_agent import altium365

SECRET = "s3cr3t-value-for-tests"


class _Reply:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _token_endpoint(answer):
    """A fake urlopen that records each form sent and answers with ``answer``."""
    sent = []

    def urlopen(request, timeout=0):
        form = dict(urllib.parse.parse_qsl(request.data.decode()))
        sent.append((request.full_url, form))
        reply = answer(form)
        if isinstance(reply, Exception):
            raise reply
        return _Reply(json.dumps(reply).encode())
    return urlopen, sent


def _refused(code: int, body: bytes):
    return urllib.error.HTTPError(f"{altium365.NEXAR_AUTH}/token", code, "refused", {},
                                  io.BytesIO(body))


def test_the_environment_reads_the_secret_and_a_printed_config_hides_it():
    config = altium365.read_environment({"EDA_AGENT_NEXAR_APP_ID": "app",
                                         "EDA_AGENT_NEXAR_CLIENT_SECRET": f" {SECRET} "})
    assert config.nexar_client_secret == SECRET and config.mode == "nexar"
    assert SECRET not in repr(config)
    assert altium365.read_environment({"EDA_AGENT_NEXAR_APP_ID": "app"}).nexar_client_secret == ""


def test_the_client_form_carries_the_secret_only_when_set():
    assert altium365._client_form(altium365.Altium365Config(nexar_app_id="app")) == {
        "client_id": "app"}
    assert altium365._client_form(altium365.Altium365Config(
        nexar_app_id="app", nexar_client_secret=SECRET)) == {
        "client_id": "app", "client_secret": SECRET}


def test_a_token_request_posts_the_form_to_the_token_endpoint():
    urlopen, sent = _token_endpoint(lambda form: {"access_token": "A", "expires_in": 3600})
    token = altium365._ask_for_token({"grant_type": "authorization_code", "code": "C",
                                      "client_id": "app", "client_secret": SECRET},
                                     5, urlopen)
    assert token["access_token"] == "A"
    url, form = sent[0]
    assert url == f"{altium365.NEXAR_AUTH}/token"
    assert form["client_secret"] == SECRET and form["client_id"] == "app"


def test_invalid_client_is_named_with_a_hint_and_without_the_secret():
    body = json.dumps({"error": "invalid_client"}).encode()
    urlopen, _ = _token_endpoint(lambda form: _refused(400, body))
    with pytest.raises(altium365.Altium365Error) as caught:
        altium365._ask_for_token({"client_id": "app", "client_secret": SECRET}, 5, urlopen)
    text = str(caught.value)
    assert "HTTP 400, invalid_client" in text
    assert "EDA_AGENT_NEXAR_CLIENT_SECRET" in text
    assert SECRET not in text


def test_invalid_grant_keeps_nexars_description():
    body = json.dumps({"error": "invalid_grant",
                       "error_description": "Invalid redirect_uri"}).encode()
    urlopen, _ = _token_endpoint(lambda form: _refused(400, body))
    with pytest.raises(altium365.Altium365Error,
                       match=r"HTTP 400, invalid_grant\): Invalid redirect_uri\. Likely:"):
        altium365._ask_for_token({"client_id": "app"}, 5, urlopen)


def test_a_body_that_echoes_a_secret_or_code_is_masked():
    body = json.dumps({"error": "invalid_request",
                       "error_description": f"bad secret {SECRET} for code C0DE"}).encode()
    urlopen, _ = _token_endpoint(lambda form: _refused(400, body))
    with pytest.raises(altium365.Altium365Error) as caught:
        altium365._ask_for_token({"client_id": "app", "client_secret": SECRET,
                                  "code": "C0DE"}, 5, urlopen)
    text = str(caught.value)
    assert SECRET not in text and "C0DE" not in text and "***" in text


def test_a_body_that_is_not_json_is_shown_cut_short():
    urlopen, _ = _token_endpoint(lambda form: _refused(500, b"gateway " + b"x" * 1000))
    with pytest.raises(altium365.Altium365Error) as caught:
        altium365._ask_for_token({"client_id": "app"}, 5, urlopen)
    text = str(caught.value)
    assert text.startswith("Nexar turned the sign-in down (HTTP 500): gateway")
    assert len(text) < 400


def test_an_empty_body_still_gives_the_status():
    urlopen, _ = _token_endpoint(lambda form: _refused(400, b""))
    with pytest.raises(altium365.Altium365Error,
                       match=r"^Nexar turned the sign-in down \(HTTP 400\)$"):
        altium365._ask_for_token({"client_id": "app"}, 5, urlopen)


def test_a_refresh_sends_the_secret_and_keeps_the_new_login(tmp_path, monkeypatch):
    saved = {"access_token": "old", "refresh_token": "R", "expires_in": 3600, "saved_at": 0}
    stored = []
    monkeypatch.setattr(altium365, "fetch_login", lambda path: dict(saved))
    monkeypatch.setattr(altium365, "store_login", lambda path, token: stored.append(token))
    urlopen, sent = _token_endpoint(lambda form: {"access_token": "new", "expires_in": 3600})
    config = altium365.Altium365Config(nexar_app_id="app", nexar_client_secret=SECRET,
                                       saved_login=tmp_path / "login.bin")
    session = altium365.Altium365Session(config, urlopen=urlopen, now=lambda: 10_000.0)
    assert session._access_token() == "new"
    _, form = sent[0]
    assert form == {"grant_type": "refresh_token", "refresh_token": "R",
                    "client_id": "app", "client_secret": SECRET}
    assert stored[0]["refresh_token"] == "R" and stored[0]["saved_at"] == 10_000.0


def test_a_refresh_without_a_secret_sends_none(tmp_path, monkeypatch):
    monkeypatch.setattr(altium365, "fetch_login", lambda path: {
        "access_token": "old", "refresh_token": "R", "expires_in": 1, "saved_at": 0})
    monkeypatch.setattr(altium365, "store_login", lambda path, token: None)
    urlopen, sent = _token_endpoint(lambda form: {"access_token": "new", "expires_in": 3600})
    session = altium365.Altium365Session(
        altium365.Altium365Config(nexar_app_id="app", saved_login=tmp_path / "l.bin"),
        urlopen=urlopen, now=lambda: 10_000.0)
    assert session._access_token() == "new"
    assert "client_secret" not in sent[0][1]


# --- which workspaces a login reaches ------------------------------------------

class _Reads:
    """A session stand-in whose read() answers with fixed workspaces."""

    def __init__(self, found, allowed=(), fail=None):
        self.config = altium365.Altium365Config(nexar_app_id="app",
                                                allowed_workspaces=allowed)
        self._found, self._fail = found, fail

    def read(self, query, variables=None):
        if self._fail:
            raise self._fail
        return {"desWorkspaceInfos": self._found}


TWO = [{"name": "Own company", "url": "https://own.365.altium.com"},
       {"name": "Partner", "url": "https://partner.365.altium.com/"}]


def test_a_login_lists_every_workspace_and_says_how_to_pick():
    lines = altium365.login_workspaces(_Reads(TWO))
    assert lines[0] == "Workspaces this login reaches (2):"
    assert "  Own company  https://own.365.altium.com/" in lines
    assert "  Partner  https://partner.365.altium.com/" in lines
    assert "EDA_AGENT_ALTIUM365_WORKSPACES" in lines[-1]


def test_a_login_marks_the_workspaces_the_allow_list_leaves_out():
    lines = altium365.login_workspaces(_Reads(TWO, allowed=("https://partner.365.altium.com/",)))
    assert lines[1].endswith("(left out by EDA_AGENT_ALTIUM365_WORKSPACES)")
    assert lines[2] == "  Partner  https://partner.365.altium.com/"
    assert len(lines) == 3


def test_a_login_warns_when_the_allow_list_matches_none():
    lines = altium365.login_workspaces(_Reads(TWO, allowed=("https://other/",)))
    assert "refuse every workspace" in lines[-1]


def test_a_login_with_one_or_no_workspace_or_a_failed_read():
    assert len(altium365.login_workspaces(_Reads(TWO[:1]))) == 2
    assert "reaches no Altium 365 workspace" in altium365.login_workspaces(_Reads([]))[0]
    failed = altium365.login_workspaces(_Reads(TWO, fail=altium365.Altium365Error("HTTP 403")))
    assert failed == ["Signed in, but the workspaces could not be listed: HTTP 403"]


def test_the_login_command_prints_the_workspaces(monkeypatch, capsys):
    import sys
    from eda_agent import server
    monkeypatch.setattr(altium365, "read_environment",
                        lambda env=None: altium365.Altium365Config(nexar_app_id="app"))
    monkeypatch.setattr(altium365, "browser_login",
                        lambda config, open_url: {"signed_in": True, "expires_in_s": 3600})
    monkeypatch.setattr(altium365.Altium365Session, "read",
                        lambda self, query, variables=None: {"desWorkspaceInfos": TWO})
    monkeypatch.setattr(sys, "argv", ["eda-agent", "altium365-login"])
    assert server.main() == 0
    out = capsys.readouterr().out
    assert "'signed_in': True" in out and "Partner  https://partner.365.altium.com/" in out
