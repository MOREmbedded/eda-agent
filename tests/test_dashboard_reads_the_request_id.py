# SPDX-License-Identifier: Apache-2.0
"""The dashboard reads the id that is actually in the log.

A request id is ``<session tag>-<sequence>-<random>``. The dashboard
looked for ``"id":"<hex>"``, so it stopped at the first dash, never met
the closing quote and matched nothing: every row written since ids
gained a session tag reached the drawer with no id, and the only thing
it could say was "no request id captured". Cutting whatever it found to
8 characters was the second half of the same fault, because those 8
name the session rather than the call.

These fixtures are real line shapes, copied from the live logs in the
IPC workspace on this machine.
"""
from __future__ import annotations

import pytest

from eda_agent.diag.perf import request_key
from eda_agent.web.dashboard import _parse_line
from tests.conftest import wait_until

#: Two calls of one session, so their ids share 8 leading characters.
PING = ('2026-10-28 09:30:03.853,16,application.ping,OK,361,'
        '{"protocol_version":2,"id":"a3c05b5f-00000001-435402a91e3b",'
        '"success":true,"data":{"pong":true}}')
DOCS = ('2026-10-28 09:30:05.212,1297,application.get_open_documents,OK,156905,'
        '{"protocol_version":2,"id":"a3c05b5f-00000002-bac877d73bc7",'
        '"success":true,"data":[{"file_name":"CMP-00000268-1.CmpLib"}]}')
#: An id from before session tags: a bare uuid.
OLD = ('2026-10-15 00:00:16.751,16,application.get_active_document,OK,264,'
       '{"protocol_version":2,"id":"5d765674e207407ea8657a6d742a01b7"}')
#: Pascal bookkeeping, which carries no payload at all.
PROBE = '2026-10-28 09:31:40.100,0,_sdk_probe,IPCB_Board,0,'

TRACE = """\
1793176203.785 SEND cmd=application.ping id=a3c05b5f-00000001
1793176203.859 POLL_MATCH id=a3c05b5f-00000001 elapsed=31ms polls=5 parse_errs=0 extensions=0
1793176203.880 SEND cmd=application.get_open_documents id=a3c05b5f-00000002
1793176205.225 POLL_MATCH id=a3c05b5f-00000002 elapsed=1344ms polls=129 parse_errs=0 extensions=0
"""


# ------------------------------------------------------------- parsing

def test_an_id_carrying_a_session_tag_is_read_at_all():
    """The whole fault in one line: this came back empty."""
    assert _parse_line(PING).request_id


def test_the_id_shown_is_the_one_bridge_trace_writes():
    """So that copying it out of the drawer and grepping the trace with
    it finds the call, and the drawer's own peek does too."""
    assert _parse_line(PING).request_id == "a3c05b5f-00000001"
    assert _parse_line(PING).request_id == request_key(
        "a3c05b5f-00000001-435402a91e3b")


def test_two_calls_of_one_session_do_not_share_an_id():
    """Their ids open with the same 8 characters, which is what the
    dashboard used to keep."""
    assert _parse_line(PING).request_id != _parse_line(DOCS).request_id


def test_an_id_from_before_session_tags_reads_as_it_always_did():
    """Those were a bare uuid, whose leading 8 characters did name one
    call. Old logs must look no different than before."""
    assert _parse_line(OLD).request_id == "5d765674"


def test_a_row_with_no_payload_has_no_id_rather_than_a_wrong_one():
    assert _parse_line(PROBE).request_id == ""


def test_the_full_id_is_still_there_in_the_payload_prefix():
    """The drawer shortens the id it shows, so the untouched one has to
    stay visible somewhere for anyone chasing a request file on disk."""
    assert "a3c05b5f-00000001-435402a91e3b" in _parse_line(PING).payload_prefix


# -------------------------------------------------------------- drawer

@pytest.fixture
def client(tmp_path):
    from eda_agent.web.dashboard import create_app

    (tmp_path / "activity.log").write_text(
        "\n".join([PING, DOCS, OLD, PROBE]) + "\n", encoding="utf-8")
    (tmp_path / "bridge_trace.log").write_text(TRACE, encoding="utf-8")
    app = create_app(workspace_dir=tmp_path)
    app.testing = True
    c = app.test_client()
    # The tailer backfills the log on its own thread.
    wait_until(lambda: len(c.get("/api/snapshot").get_json()["entries"]) == 4,
               message="tailer did not read the fixture log")
    return c


def test_the_drawer_finds_a_call_by_the_id_it_was_given(client):
    """The lookup used to cut the id to 8 characters before comparing,
    so it asked for a session tag against an id that was never one and
    answered 404 for every call in the log."""
    detail = client.get("/api/entry/a3c05b5f-00000002")
    assert detail.status_code == 200
    data = detail.get_json()
    assert data["entry"]["cmd"] == "application.get_open_documents"
    assert "a3c05b5f-00000002-bac877d73bc7" in data["payload_prefix"]


def test_the_drawer_shows_the_trace_of_that_call_and_no_other(client):
    """Both calls belong to one session. Searching the trace for the
    session's 8 characters would have returned all four lines under one
    call's heading."""
    trace = client.get("/api/entry/a3c05b5f-00000001").get_json()["trace"]
    assert len(trace) == 2
    assert all("a3c05b5f-00000001" in line for line in trace)
    assert all("00000002" not in line for line in trace)


def test_an_id_no_row_carries_is_not_found(client):
    assert client.get("/api/entry/a3c05b5f-00009999").status_code == 404
