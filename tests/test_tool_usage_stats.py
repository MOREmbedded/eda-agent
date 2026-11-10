# SPDX-License-Identifier: Apache-2.0
"""Token usage of the EDA tools (eda_agent.diag.usage): what a call is
counted as, where it is kept, the setting that switches it off, and the
three places that show it (the status window's Stats tab, the dashboard's
Usage view, and the server hook that feeds them)."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path

import pytest
from mcp.types import ImageContent, TextContent

from eda_agent.diag import usage

ROOT = Path(__file__).resolve().parents[1]


def _png(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk
            + struct.pack(">I", zlib.crc32(chunk)))


def _recorder(tmp_path, on=True, **kw) -> usage.Recorder:
    flag = {"on": on}
    rec = usage.Recorder(tmp_path / "ws", tmp_path / "ipc", session="abcd1234",
                         remember=False, switch=lambda: flag["on"], **kw)
    (tmp_path / "ipc").mkdir()
    rec.flag = flag
    return rec


# -- estimating ---------------------------------------------------------------

def test_tokens_are_characters_over_four_rounded_up():
    assert usage.estimate_tokens(0) == 0
    assert usage.estimate_tokens(1) == 1
    assert usage.estimate_tokens(400) == 100
    assert usage.estimate_tokens(401) == 101


def test_image_sizes_are_read_and_shrunk_like_the_model_does():
    assert usage.image_size(_png(800, 600)) == (800, 600)
    assert usage.image_tokens(750, 1) == 1
    assert usage.image_tokens(800, 600) == 640
    # Larger than the long edge: shrunk first, so a huge render costs no
    # more than one at the limit.
    assert usage.image_tokens(3136, 3136) == usage.image_tokens(1568, 1568)
    assert usage.image_size(b"not an image") is None


def test_a_result_is_measured_as_the_larger_copy_plus_images():
    text = TextContent(type="text", text='{"ok":true,"items":[1,2,3]}')
    image = ImageContent(type="image", mimeType="image/png",
                         data=base64.b64encode(_png(800, 600)).decode())
    m = usage.measure_result(([text, image], {"ok": True, "items": [1, 2, 3], "extra": "x" * 40}))
    assert m.text_chars == len(text.text)
    assert m.structured_chars > m.text_chars
    assert m.chars == m.structured_chars
    assert m.images == 1 and m.image_tokens == 640
    assert m.tokens == usage.estimate_tokens(m.structured_chars) + 640
    assert m.ok


def test_a_refusal_is_counted_as_failed():
    assert not usage.measure_result(([TextContent(type="text", text='{"ok":false}')], None)).ok
    assert not usage.measure_result(([], {"ok": False, "reason": "no"})).ok


# -- recording ----------------------------------------------------------------

def test_each_call_is_kept_per_workspace_and_ignored_by_git(tmp_path):
    rec = _recorder(tmp_path)
    got = rec.record("pcb_get_components", {"items": ["a"]},
                     [TextContent(type="text", text="x" * 4000)], ms=120)
    assert got["result_tokens"] == 1000 and got["args_tokens"] == 4
    assert got["session"] == "abcd1234" and got["tool"] == "pcb_get_components"
    folder = tmp_path / "ws" / ".eda-agent" / "usage"
    assert (folder / ".gitignore").read_text().splitlines()[-1] == "*"
    (line,) = [json.loads(x) for f in folder.glob("calls-*.jsonl")
               for x in f.read_text().splitlines()]
    assert line["result_tokens"] == 1000 and line["ms"] == 120
    # A new server on the same workspace starts from what was kept.
    again = usage.Recorder(tmp_path / "ws", None, session="next", remember=False,
                           switch=lambda: True)
    again.record("pcb_get_nets", {}, [TextContent(type="text", text="y" * 40)])
    snap = again.snapshot()
    assert snap["this_session"]["total"]["calls"] == 1
    assert snap["workspace_totals"]["total"]["calls"] == 2
    assert {r["session"] for r in snap["workspace_totals"]["by_session"]} == {"abcd1234", "next"}


def test_the_stats_tab_file_lists_the_costliest_tools_first(tmp_path):
    rec = _recorder(tmp_path)
    rec.record("small_tool", {}, [TextContent(type="text", text="x" * 40)], ms=5)
    rec.record("big_tool", {}, [TextContent(type="text", text="x" * 40000)], ms=2500)
    rec.record("big_tool", {}, [TextContent(type="text", text="x" * 40000)], ms=2500)
    text = (tmp_path / "ipc" / usage.STATUS_FILE).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("Token use, estimated")
    body = [ln for ln in lines if ln.startswith(("big_tool", "small_tool"))]
    assert body[0].startswith("big_tool") and body[1].startswith("small_tool")
    assert "20k" in body[0] and "5.0s" in body[0]
    assert any(ln.startswith("This session") and " 3 " in ln for ln in lines)
    assert any(ln.startswith("This workspace") for ln in lines)
    # Fits the panel: Consolas in a 380 px memo.
    assert max(len(ln) for ln in lines if not ln.startswith("Kept in")) <= 56
    text.encode("ascii")


def test_switched_off_nothing_is_counted_or_written(tmp_path):
    rec = _recorder(tmp_path, on=False)
    assert rec.record("pcb_get_components", {}, []) is None
    assert not (tmp_path / "ws" / ".eda-agent").exists()
    assert "off" in (tmp_path / "ipc" / usage.STATUS_FILE).read_text()
    assert rec.snapshot()["this_session"]["total"]["calls"] == 0
    rec.flag["on"] = True
    rec.record("pcb_get_components", {}, [])
    assert rec.snapshot()["this_session"]["total"]["calls"] == 1


def test_the_setting_and_the_environment_switch(monkeypatch, tmp_path):
    from eda_agent import preferences
    monkeypatch.setattr(preferences, "_read", lambda ws: {"usage_stats": "off"})
    monkeypatch.setattr(preferences, "_profile_layer", lambda: {})
    assert not usage.setting_on()
    monkeypatch.setattr(preferences, "_read", lambda ws: {})
    assert usage.setting_on()
    # The profile wins over the panel, so each client's profile decides.
    monkeypatch.setattr(preferences, "_profile_layer", lambda: {"usage_stats": "off"})
    assert not usage.setting_on()
    monkeypatch.setattr(preferences, "_profile_layer", lambda: {})
    monkeypatch.setenv(usage.ENV_OFF, "1")
    assert not usage.setting_on()


def test_a_broken_record_never_breaks_the_tool(tmp_path):
    rec = usage.Recorder(tmp_path / "file-not-dir", None, session="s", remember=False,
                         switch=lambda: True)
    (tmp_path / "file-not-dir").write_text("x")
    # The workspace is a file, so the folder cannot be made: swallowed.
    assert rec.record("t", {}, []) is None


def test_torn_lines_are_skipped(tmp_path):
    folder = usage.usage_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "calls-2026-11.jsonl").write_text(
        '{"ts":1,"tool":"a","result_tokens":5}\n{"ts":2,"tool":\n[1]\n', encoding="utf-8")
    rep = usage.workspace_report(tmp_path)
    assert rep["total"]["calls"] == 1 and rep["by_tool"][0]["tool"] == "a"


def test_the_last_days_only(tmp_path, monkeypatch):
    folder = usage.usage_dir(tmp_path)
    folder.mkdir(parents=True)
    now = 2_000_000_000.0
    monkeypatch.setattr(usage.time, "time", lambda: now)
    (folder / "calls-2033-05.jsonl").write_text(
        json.dumps({"ts": now - 10 * 86400, "tool": "old"}) + "\n"
        + json.dumps({"ts": now - 3600, "tool": "new"}) + "\n", encoding="utf-8")
    assert [r["tool"] for r in usage.workspace_report(tmp_path, days=7)["by_tool"]] == ["new"]
    assert usage.workspace_report(tmp_path)["total"]["calls"] == 2


def test_the_registry_offers_every_workspace_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    usage._remember_workspace(tmp_path / "a")
    usage._remember_workspace(tmp_path / "b")
    usage._remember_workspace(tmp_path / "a")
    assert [r["path"] for r in usage.known_workspaces()] == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_a_test_run_never_records_into_the_folder_it_ran_from(monkeypatch):
    monkeypatch.delenv(usage.ENV_DIR, raising=False)
    assert usage.default_workspace() is None


# -- the server hook --------------------------------------------------------------

def test_every_tool_call_through_the_server_is_counted(tmp_path, monkeypatch):
    from eda_agent import server
    rec = usage.Recorder(tmp_path, None, session="s", remember=False, switch=lambda: True)
    usage.set_recorder(rec)
    try:
        name = asyncio.run(server.mcp.list_tools())[0].name
        try:
            asyncio.run(server.mcp.call_tool(name, {}))
        except Exception:  # noqa: BLE001 - a refused call is counted as well
            pass
    finally:
        usage.set_recorder(None)
    snap = rec.snapshot()
    assert snap["this_session"]["total"]["calls"] == 1
    assert snap["this_session"]["by_tool"][0]["tool"].startswith(name)


def test_a_family_member_is_named_after_the_member():
    from eda_agent.server import _usage_name
    assert _usage_name("pcb_calc", {"kind": "x"}, {"tool": "pcb_calc_impedance"}) == "pcb_calc/impedance"
    assert _usage_name("tool_invoke", {}, {"tool": "pcb_get_nets"}) == "tool_invoke/pcb_get_nets"
    assert _usage_name("pcb_get_nets", {}, {"tool": "pcb_get_nets"}) == "pcb_get_nets"
    assert _usage_name("t", {}, None) == "t"


# -- the dashboard ------------------------------------------------------------------

def test_the_dashboard_usage_view_reads_the_records(tmp_path, monkeypatch):
    from eda_agent.web import usage_view
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    rec = usage.Recorder(tmp_path / "ws", None, session="s", remember=True, switch=lambda: True)
    rec.record("pcb_get_components", {}, [TextContent(type="text", text="x" * 400)])
    usage.set_recorder(usage.Recorder(None, None, session="", remember=False, switch=lambda: True))
    try:
        got = usage_view.state()
    finally:
        usage.set_recorder(None)
    assert got["workspace"] == str(tmp_path / "ws")
    assert got["report"]["total"]["result_tokens"] == 100


def test_the_page_has_the_usage_mode():
    page = (ROOT / "src" / "eda_agent" / "web" / "dashboard_static" / "workbench.html").read_text(encoding="utf-8")
    assert 'data-mode="usage"' in page and "/api/wb/usage" in page
    assert 'plain("usage_stats"' in page


# -- the status window ---------------------------------------------------------------

def test_the_status_window_shows_the_servers_file():
    pas = (ROOT / "scripts" / "altium" / "StatusForm.pas").read_text(encoding="utf-8")
    dfm = (ROOT / "scripts" / "altium" / "StatusForm.dfm").read_text(encoding="utf-8")
    assert f"USAGE_STATUS_FILE    = '{usage.STATUS_FILE}';" in pas
    for name in ("tab_Stats", "mmo_Stats", "opt_StatsOn", "opt_StatsOff", "lbl_CfgStatsHint"):
        assert f"object {name}:" in dfm, name
    assert "If mmo_Stats.Visible Then RefreshStatsPanel(False);" in pas


def test_the_view_opens_on_a_workspace_with_records(tmp_path, monkeypatch):
    """A dashboard started on its own records nothing; its start folder
    must not push the workspaces that have records out of first place."""
    from eda_agent.web import usage_view
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    usage.Recorder(tmp_path / "ws", None, session="s", remember=True,
                   switch=lambda: True).record("t", {}, [])
    usage.set_recorder(usage.Recorder(tmp_path / "dash", None, session="d",
                                      remember=False, switch=lambda: True))
    try:
        got = usage_view.state()
    finally:
        usage.set_recorder(None)
    assert got["workspace"] == str(tmp_path / "ws")
    assert str(tmp_path / "dash") not in got["workspaces"]
