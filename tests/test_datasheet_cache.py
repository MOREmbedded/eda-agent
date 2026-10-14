# SPDX-License-Identifier: Apache-2.0
"""Datasheet caching: deny-by-default hosts, real-PDF checking, manifests.

No test here touches the network: get_bytes is replaced, which is the
seam every provider already uses, so what is exercised is this module's
own policy rather than urllib's behaviour.
"""
from __future__ import annotations

import json

import pytest

from eda_agent.libimport import datasheet_cache as dc
from eda_agent.libimport.providers._http import FetchError

PDF = b"%PDF-1.7\n' real enough for a magic-byte check\n%%EOF\n"


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv(dc.HOSTS_ENV, raising=False)


def _serve(monkeypatch, payload, record=None):
    def fake_get_bytes(url, hosts):
        if record is not None:
            record.append((url, hosts))
        if isinstance(payload, Exception):
            raise payload
        return payload
    monkeypatch.setattr(dc, "get_bytes", fake_get_bytes)


def test_no_configured_hosts_refuses_and_says_how_to_fix_it(monkeypatch):
    calls = []
    _serve(monkeypatch, PDF, calls)
    result = dc.cache_datasheet("https://vendor.example/ds.pdf")
    assert result["ok"] is False
    assert dc.HOSTS_ENV in result["reason"]
    assert "vendor.example" in result["reason"]
    assert calls == [], "nothing is fetched before the policy is satisfied"


def test_a_pdf_from_an_allowed_host_is_cached_with_a_manifest(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, PDF)

    result = dc.cache_datasheet("https://vendor.example/ds.pdf", mpn="TPS54331D")
    assert result["ok"] is True and result["cached"] is False
    assert result["mpn"] == "TPS54331D"
    assert result["bytes"] == len(PDF)
    assert len(result["sha256"]) == 64

    from pathlib import Path
    assert Path(result["path"]).read_bytes() == PDF
    manifest = Path(result["path"]).with_suffix(".json")
    assert json.loads(manifest.read_text(encoding="utf-8"))["url"].endswith("ds.pdf")


def test_the_configured_allowlist_is_what_reaches_the_fetcher(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, " vendor.example , Other.Example ")
    calls = []
    _serve(monkeypatch, PDF, calls)
    dc.cache_datasheet("https://vendor.example/ds.pdf")
    assert calls[0][1] == {"vendor.example", "other.example"}


def test_a_second_request_reuses_the_cached_copy(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    calls = []
    _serve(monkeypatch, PDF, calls)

    first = dc.cache_datasheet("https://vendor.example/ds.pdf")
    second = dc.cache_datasheet("https://vendor.example/ds.pdf")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls) == 1, "the second call did not go to the network"
    assert second["path"] == first["path"]


def test_force_refetches_even_when_cached(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    calls = []
    _serve(monkeypatch, PDF, calls)
    dc.cache_datasheet("https://vendor.example/ds.pdf")
    dc.cache_datasheet("https://vendor.example/ds.pdf", force=True)
    assert len(calls) == 2


def test_html_pretending_to_be_a_datasheet_is_refused(monkeypatch):
    """A vendor page that redirects to HTML is the usual failure, and
    Content-Type is not reliable enough to catch it."""
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, b"<!DOCTYPE html><html>Sign in to continue</html>")
    result = dc.cache_datasheet("https://vendor.example/ds.pdf")
    assert result["ok"] is False and "%PDF-" in result["reason"]
    assert dc.cached_entry("https://vendor.example/ds.pdf") is None


def test_a_fetch_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, FetchError("HTTP 404 from https://vendor.example/ds.pdf"))
    result = dc.cache_datasheet("https://vendor.example/ds.pdf")
    assert result["ok"] is False and "404" in result["reason"]


def test_an_empty_url_is_refused(monkeypatch):
    assert dc.cache_datasheet("")["ok"] is False
    assert dc.cache_datasheet("   ")["ok"] is False


def test_a_manifest_without_its_pdf_counts_as_a_miss(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, PDF)
    result = dc.cache_datasheet("https://vendor.example/ds.pdf")

    from pathlib import Path
    Path(result["path"]).unlink()
    assert dc.cached_entry("https://vendor.example/ds.pdf") is None


def test_list_cached_reports_what_is_on_disk(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, PDF)
    assert dc.list_cached() == []
    dc.cache_datasheet("https://vendor.example/a.pdf", mpn="A")
    dc.cache_datasheet("https://vendor.example/b.pdf", mpn="B")
    assert {e["mpn"] for e in dc.list_cached()} == {"A", "B"}


def test_distinct_urls_do_not_collide(monkeypatch):
    monkeypatch.setenv(dc.HOSTS_ENV, "vendor.example")
    _serve(monkeypatch, PDF)
    a = dc.cache_datasheet("https://vendor.example/a.pdf")
    b = dc.cache_datasheet("https://vendor.example/b.pdf")
    assert a["path"] != b["path"]
