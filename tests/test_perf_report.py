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
    parse_activity_log,
    parse_bridge_trace,
    percentile,
    report_from_workspace,
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

def test_the_short_and_long_ids_are_joined_on_the_prefix():
    """bridge_trace writes 8 characters, activity.log carries 32."""
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
