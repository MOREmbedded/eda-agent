# SPDX-License-Identifier: Apache-2.0
"""Joining the two timing logs.

activity.log times the handler inside Altium; bridge_trace.log times
the round trip the caller saw. The difference is everything that is
not the handler, and nothing read both until this. These fixtures are
real line shapes, copied from a live session's logs.
"""
from __future__ import annotations

import pytest

from eda_agent.diag.perf import (
    build_report,
    correlate,
    format_report,
    parse_activity_log,
    parse_bridge_trace,
    percentile,
    report_from_workspace,
    request_key,
)

TRACE = """\
1791917156.118 SEND cmd=application.ping id=500aec90
1791917156.140 POLL_START id=500aec90 timeout=3.0s interval=0.01s
1791917159.185 POLL_TIMEOUT id=500aec90 elapsed=3045ms polls=290 parse_errs=0 extensions=0 first_seen_ms=-1
1791917418.070 SEND cmd=application.get_active_document id=5d765674
1791917418.102 POLL_SEEN id=5d765674 after=30ms polls=6
1791917418.108 POLL_MATCH id=5d765674 elapsed=36ms polls=6 parse_errs=0 extensions=0
1791917433.100 SEND cmd=application.save_all id=b57abe13
1791917433.119 POLL_EXTEND id=b57abe13 extensions=1/30 elapsed=15008ms
1791917508.153 POLL_MATCH id=b57abe13 elapsed=90043ms polls=8550 parse_errs=0 extensions=6
"""

ACTIVITY = """\
2026-10-15 00:00:10.000,0,_session_start,version=2026.10.13.5,0,
2026-10-15 00:00:16.751,16,application.get_active_document,OK,264,{"protocol_version":2,"id":"5d765674e207407ea8657a6d742a01b7"}
2026-10-15 00:00:17.532,89900,application.save_all,OK,517,{"protocol_version":2,"id":"b57abe13ea584678954d2c1c3642d47b"}
2026-10-15 00:10:17.718,0,_session_end,requests=21,quitting=no
"""


# ---------------------------------------------------------------- parsing

def test_a_send_line_gives_the_command_for_an_id():
    calls = parse_bridge_trace(TRACE)
    assert calls["5d765674"].command == "application.get_active_document"


def test_a_match_line_gives_the_round_trip():
    call = parse_bridge_trace(TRACE)["5d765674"]
    assert call.round_trip_ms == 36 and call.polls == 6
    assert call.timed_out is False


def test_a_timeout_is_recorded_as_one_rather_than_dropped():
    """A call that timed out still cost the caller its full wait."""
    call = parse_bridge_trace(TRACE)["500aec90"]
    assert call.timed_out is True and call.round_trip_ms == 3045


def test_deadline_extensions_are_counted():
    assert parse_bridge_trace(TRACE)["b57abe13"].extensions == 6


def test_a_dialog_block_is_attached_to_its_call():
    trace = TRACE + (
        "1791918404.392 POLL_DIALOG id=5d765674 after=3.0s summary=Altium is "
        "BLOCKED by a modal. information: 'Information' says \"no template\"\n")
    call = parse_bridge_trace(trace)["5d765674"]
    assert "BLOCKED by a modal" in call.blocked_by_dialog


def test_activity_rows_are_keyed_by_the_full_request_id():
    rows = parse_activity_log(ACTIVITY)
    assert rows["5d765674e207407ea8657a6d742a01b7"]["handler_ms"] == 16
    assert rows["5d765674e207407ea8657a6d742a01b7"]["response_bytes"] == 264


def test_session_markers_are_not_counted_as_calls():
    """_session_start / _session_end carry no id and are not requests."""
    rows = parse_activity_log(ACTIVITY)
    assert len(rows) == 2
    assert not any("session" in cmd["command"] for cmd in rows.values())


# ---------------------------------------------------------------- correlation

def test_a_log_from_before_session_tags_still_joins_on_the_prefix():
    """Ids were a bare uuid then: bridge_trace wrote the leading 8
    characters, activity.log carried all 32, and those 8 did name one
    call. Widening the id must not cost anyone their old logs."""
    calls = {c.request_id: c for c in
             correlate(parse_bridge_trace(TRACE), parse_activity_log(ACTIVITY))}
    assert calls["5d765674"].handler_ms == 16


def test_overhead_is_the_round_trip_minus_the_handler():
    calls = {c.request_id: c for c in
             correlate(parse_bridge_trace(TRACE), parse_activity_log(ACTIVITY))}
    assert calls["5d765674"].overhead_ms == 36 - 16


def test_a_call_logged_on_only_one_side_has_no_overhead_figure():
    """A missing half is not a zero half: the ping never reached
    activity.log, so its overhead is unknown rather than equal to its
    whole round trip."""
    calls = {c.request_id: c for c in
             correlate(parse_bridge_trace(TRACE), parse_activity_log(ACTIVITY))}
    assert calls["500aec90"].handler_ms is None
    assert calls["500aec90"].overhead_ms is None


def test_calls_come_back_in_send_order():
    calls = correlate(parse_bridge_trace(TRACE), parse_activity_log(ACTIVITY))
    assert [c.request_id for c in calls] == ["500aec90", "5d765674", "b57abe13"]


# ---------------------------------------------------------------- report

def test_the_report_counts_what_it_could_and_could_not_correlate():
    report = build_report(correlate(parse_bridge_trace(TRACE),
                                    parse_activity_log(ACTIVITY)))
    assert report["calls"] == 3
    assert report["correlated"] == 2, "the ping has no handler time"
    assert report["timeouts"] == 1
    assert report["extended"] == 1


def test_commands_are_ranked_by_total_time_not_by_call_count():
    """One 90 s save matters more than many fast reads."""
    report = build_report(correlate(parse_bridge_trace(TRACE),
                                    parse_activity_log(ACTIVITY)))
    assert report["commands"][0]["command"] == "application.save_all"


def test_a_timeout_is_attributed_to_its_command():
    report = build_report(correlate(parse_bridge_trace(TRACE),
                                    parse_activity_log(ACTIVITY)))
    ping = next(c for c in report["commands"] if c["command"] == "application.ping")
    assert ping["timeouts"] == 1 and ping["calls"] == 1


# ---------------------------------------------------------------- percentiles

@pytest.mark.parametrize("values,fraction,expected", [
    ([], 0.5, None),
    ([7], 0.95, 7),
    ([1, 2, 3, 4], 0.5, 2),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95, 10),
])
def test_percentiles_are_nearest_rank(values, fraction, expected):
    """Nearest rank, not interpolated: an interpolated p95 reports a
    duration no call actually took."""
    assert percentile(values, fraction) == expected


# ---------------------------------------------------------------- workspace

def test_a_workspace_with_both_logs_reports_sources(tmp_path):
    (tmp_path / "bridge_trace.log").write_text(TRACE, encoding="utf-8")
    (tmp_path / "activity.log").write_text(ACTIVITY, encoding="utf-8")
    report = report_from_workspace(tmp_path)
    assert report["correlated"] == 2
    assert report["sources"]["bridge_trace"] and report["sources"]["activity_log"]


def test_only_bridge_trace_still_gives_round_trips(tmp_path):
    (tmp_path / "bridge_trace.log").write_text(TRACE, encoding="utf-8")
    report = report_from_workspace(tmp_path)
    assert report["calls"] == 3 and report["correlated"] == 0
    assert report["sources"]["activity_log"] is None


def test_an_empty_workspace_reports_nothing_rather_than_failing(tmp_path):
    report = report_from_workspace(tmp_path)
    assert report["calls"] == 0 and report["commands"] == []


def test_a_malformed_line_does_not_derail_the_parse(tmp_path):
    (tmp_path / "bridge_trace.log").write_text(
        "this is not a log line\n" + TRACE, encoding="utf-8")
    assert report_from_workspace(tmp_path)["calls"] == 3


# ------------------------------------------------- the session collapse
#
# Ids are <session tag>-<sequence>-<random> and the tag is fixed for the
# life of a server process, so every id in a session opens with the same
# 8 characters. bridge_trace logged exactly those 8 and the join keyed on
# them, so a session's calls landed on one entry and only the last
# survived. Two sessions, three calls each, in the shapes the live logs
# on this machine hold.

SESSIONS_TRACE = """\
1793176203.785 SEND cmd=application.ping id=a3c05b5f-00000001
1793176203.828 POLL_START id=a3c05b5f-00000001 timeout=3.0s interval=0.01s
1793176203.853 POLL_SEEN id=a3c05b5f-00000001 after=25ms polls=5
1793176203.859 POLL_MATCH id=a3c05b5f-00000001 elapsed=31ms polls=5 parse_errs=0 extensions=0
1793176203.880 SEND cmd=application.get_open_documents id=a3c05b5f-00000002
1793176203.881 POLL_START id=a3c05b5f-00000002 timeout=15.0s interval=0.01s
1793176205.225 POLL_MATCH id=a3c05b5f-00000002 elapsed=1344ms polls=129 parse_errs=0 extensions=0
1793176209.100 SEND cmd=application.save_all id=a3c05b5f-00000003
1793176212.140 POLL_MATCH id=a3c05b5f-00000003 elapsed=3040ms polls=300 parse_errs=0 extensions=0
1793176300.010 SEND cmd=application.ping id=00597274-00000001
1793176300.052 POLL_MATCH id=00597274-00000001 elapsed=42ms polls=4 parse_errs=0 extensions=0
1793176301.000 SEND cmd=pcb.get_board_info id=00597274-00000002
1793176301.610 POLL_MATCH id=00597274-00000002 elapsed=610ms polls=60 parse_errs=0 extensions=0
1793176302.000 SEND cmd=sch.get_components id=00597274-00000003
1793176304.500 POLL_TIMEOUT id=00597274-00000003 elapsed=2500ms polls=250 parse_errs=0 extensions=0 first_seen_ms=-1
"""

SESSIONS_ACTIVITY = """\
2026-10-28 09:29:32.761,0,_session_start,version=2026.10.27.7,protocol=2
2026-10-28 09:29:32.776,0,_tick_first,interval=10
2026-10-28 09:30:03.853,16,application.ping,OK,361,{"protocol_version":2,"id":"a3c05b5f-00000001-435402a91e3b","success":true,"data":{"pong":true}}
2026-10-28 09:30:05.212,1297,application.get_open_documents,OK,156905,{"protocol_version":2,"id":"a3c05b5f-00000002-bac877d73bc7","success":true,"data":[{"file_name":"CMP-00000268-1.CmpLib"}]}
2026-10-28 09:30:12.140,2990,application.save_all,OK,517,{"protocol_version":2,"id":"a3c05b5f-00000003-7d2b4e1c9a05","success":true}
2026-10-28 09:31:00.052,9,application.ping,OK,361,{"protocol_version":2,"id":"00597274-00000001-f26a650ce2ed","success":true}
2026-10-28 09:31:01.610,580,pcb.get_board_info,OK,2104,{"protocol_version":2,"id":"00597274-00000002-96d1642e82e4","success":true}
2026-10-28 09:31:40.100,0,_sdk_probe,IPCB_Board,0,
2026-10-28 09:31:41.200,0,_purge_requests,count=3,0,
2026-10-28 09:40:17.718,0,_session_end,requests=6,quitting=no
"""

#: One of those sessions as a log written before the id was widened
#: holds it: the tag alone, repeated on every line.
LEGACY_TRACE = """\
1793176203.785 SEND cmd=application.ping id=a3c05b5f
1793176203.859 POLL_MATCH id=a3c05b5f elapsed=31ms polls=5 parse_errs=0 extensions=0
1793176203.880 SEND cmd=application.get_open_documents id=a3c05b5f
1793176205.225 POLL_MATCH id=a3c05b5f elapsed=1344ms polls=129 parse_errs=0 extensions=0
1793176209.100 SEND cmd=application.save_all id=a3c05b5f
1793176212.140 POLL_MATCH id=a3c05b5f elapsed=3040ms polls=300 parse_errs=0 extensions=0
"""


def test_ids_that_share_a_session_tag_are_one_call_each():
    """The defect in one line: six requests across two sessions, whose
    ids share 8 leading characters in threes. Keying on those 8 gave
    two calls and lost four."""
    calls = parse_bridge_trace(SESSIONS_TRACE)
    assert len(calls) == 6
    assert calls["a3c05b5f-00000002"].command == "application.get_open_documents"
    assert calls["a3c05b5f-00000002"].round_trip_ms == 1344


def test_a_handler_time_lands_on_its_own_call_not_the_sessions_last():
    """The join has to match a call to the row Altium wrote for IT. On
    the session tag alone every call of a session took whichever row
    came last, so a 16 ms ping reported the save_all handler."""
    calls = {c.request_id: c for c in
             correlate(parse_bridge_trace(SESSIONS_TRACE),
                       parse_activity_log(SESSIONS_ACTIVITY))}
    assert calls["a3c05b5f-00000001"].handler_ms == 16
    assert calls["a3c05b5f-00000002"].handler_ms == 1297
    assert calls["a3c05b5f-00000003"].handler_ms == 2990
    assert calls["a3c05b5f-00000001"].overhead_ms == 31 - 16


def test_every_call_of_a_session_is_counted_not_only_the_last():
    report = build_report(correlate(parse_bridge_trace(SESSIONS_TRACE),
                                    parse_activity_log(SESSIONS_ACTIVITY)))
    assert report["calls"] == 6
    assert report["requests"] == 6
    assert report["unresolved"] == 0
    assert report["correlated"] == 5, "sch.get_components timed out, never ran"


def test_an_activity_id_carrying_dashes_is_recovered():
    """The id gained dashes while the payload search still demanded
    hex, so it matched 8 characters, wanted a quote and met a dash.
    Two rows in 461 yielded an id on the real log, and every other
    handler time was dropped without a word."""
    rows = parse_activity_log(SESSIONS_ACTIVITY)
    assert "a3c05b5f-00000002-bac877d73bc7" in rows
    assert rows["a3c05b5f-00000002-bac877d73bc7"]["handler_ms"] == 1297


def test_pascals_own_markers_are_not_mistaken_for_calls():
    """_sdk_probe, _purge_requests and _tick_first are bookkeeping rows
    Pascal writes with no id, 161 of 410 rows on the real log. Only
    _session_start and _session_end were known here."""
    rows = parse_activity_log(SESSIONS_ACTIVITY)
    assert len(rows) == 5
    assert not any(data["command"].startswith("_") for data in rows.values())


# ------------------------------------------------------------ old logs

def test_a_log_written_before_the_id_was_widened_still_reads():
    """People have these on disk. Three requests share one logged id,
    so one entry is all that can survive, but the command must not fail
    and the parse must not invent anything."""
    calls = parse_bridge_trace(LEGACY_TRACE)
    assert len(calls) == 1
    assert calls["a3c05b5f"].requests == 3


def test_the_report_says_how_many_requests_it_could_not_tell_apart():
    """The silent half of the defect: 1244 trace lines came back as 129
    calls with nothing saying two thirds had gone."""
    report = build_report(correlate(parse_bridge_trace(LEGACY_TRACE), {}),
                          trace_lines=6)
    assert report["trace_lines"] == 6
    assert report["requests"] == 3
    assert report["calls"] == 1
    assert report["unresolved"] == 2
    assert "2 request(s) could not be told apart" in format_report(report)


def test_a_session_tag_is_never_joined_to_a_full_id():
    """A legacy trace line names a session whose handler times are all
    still in activity.log. Attaching one of them would be a guess
    dressed as a measurement."""
    calls = correlate(parse_bridge_trace(LEGACY_TRACE),
                      parse_activity_log(SESSIONS_ACTIVITY))
    assert [c.handler_ms for c in calls] == [None]


def test_old_and_new_lines_in_one_file_both_read():
    """A log does not start again when the server is upgraded: the file
    on disk holds the old shape and then the new one."""
    calls = parse_bridge_trace(LEGACY_TRACE + SESSIONS_TRACE)
    assert len(calls) == 7, "one collapsed legacy entry, six real calls"
    assert calls["a3c05b5f"].requests == 3
    assert calls["a3c05b5f-00000001"].requests == 1


# ----------------------------------------------------------- the key

def test_the_bridge_and_the_report_shorten_an_id_the_same_way():
    """The join holds only while the writer and the reader agree on
    what names a call. They are two functions in two modules, so this
    is where they are held together."""
    from eda_agent.bridge.altium_bridge import trace_id

    for request_id in ("a3c05b5f-00000001-435402a91e3b",
                       "00597274-00000123-f26a650ce2ed",
                       "5d765674e207407ea8657a6d742a01b7",
                       "500aec90",
                       ""):
        assert trace_id(request_id) == request_key(request_id)


def test_a_key_keeps_the_sequence_that_tells_two_calls_apart():
    assert request_key("a3c05b5f-00000001-435402a91e3b") == "a3c05b5f-00000001"
    assert request_key("a3c05b5f-00000002-bac877d73bc7") == "a3c05b5f-00000002"


def test_a_uuid_without_a_tag_keeps_its_leading_eight():
    """The shape of an id from before session tags."""
    assert request_key("5d765674e207407ea8657a6d742a01b7") == "5d765674"


# ------------------------------------------------------- the workspace

def test_the_workspace_report_counts_the_lines_it_read(tmp_path):
    (tmp_path / "bridge_trace.log").write_text(SESSIONS_TRACE, encoding="utf-8")
    (tmp_path / "activity.log").write_text(SESSIONS_ACTIVITY, encoding="utf-8")
    report = report_from_workspace(tmp_path)
    assert report["trace_lines"] == 15
    assert report["calls"] == 6 and report["unresolved"] == 0
    assert report["activity_rows"] == 5
    assert report["correlated"] == 5
