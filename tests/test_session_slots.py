# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Claude sessions sharing one Altium take turns in time slots.

One script loop serves every session. It took whichever request file the
directory listed first, so a session sending command after command could
keep another waiting for as long as it liked. Requests now carry a session
tag and a sequence; Altium serves a lone session straight through and gives
each waiting session one slot in turn. A client whose request is queued
waits while the queue is visibly alive instead of reporting a dead loop.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from eda_agent.bridge import altium_bridge as ab

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "scripts" / "altium" / "Main.pas"
HARNESS = ROOT / "tests" / "cross_validate_pascal.pas"


def _function(code: str, name: str) -> str:
    start = code.index(f"Function {name}(")
    end = code.index("\nEnd;", start) + 5
    return code[start:end]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\r", "")).strip()


def test_the_cross_checked_copy_is_the_shipped_function():
    main = _function(MAIN.read_text(encoding="ascii"), "PickSession")
    copy = _function(HARNESS.read_text(encoding="ascii"), "PickSession")
    assert _norm(main) == _norm(copy)


def test_the_scan_picks_through_pick_session_and_notes_only_contention():
    scan = _function(MAIN.read_text(encoding="ascii"), "ScanForRequestFile")
    assert "NextTag := PickSession(Pending, SlotSession, NowTick - SlotStartTick," in scan
    # queue.json is written only while more than one session is pending,
    # so a session working alone sees no change at all.
    note = scan.index("'queue.json'")
    guard = scan.rindex("If Tags.Count > 1 Then", 0, note)
    assert guard < note
    # Each session's next request is its lowest sequence; old ids are one
    # 'legacy' session.
    assert "If Seq < BestSeq[Pick] Then" in scan
    assert "Tag := 'legacy';" in scan
    # Only names this binding has run before: no CompareStr, no 'Now'.
    assert "CompareStr" not in scan and not re.search(r"\bNow\b", scan)


def test_ids_carry_the_session_and_its_order():
    ids = [ab._short_id() for _ in range(3)]
    shape = re.compile(r"^[0-9a-f]{8}-\d{8}-[0-9a-f]{12}$")
    assert all(shape.match(i) for i in ids)
    assert len({i[:8] for i in ids}) == 1 and ids[0][:8] == ab._SESSION_TAG
    seqs = [int(i[9:17]) for i in ids]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    # Pascal's IsValidRequestId: letters, digits, '-', '_', at most 64.
    assert all(len(i) <= 64 and re.fullmatch(r"[0-9A-Za-z_-]+", i) for i in ids)


def test_a_live_queue_is_told_from_a_leftover(tmp_path):
    assert not ab._queue_is_alive(tmp_path)
    q = tmp_path / "queue.json"
    q.write_text("{}")
    assert ab._queue_is_alive(tmp_path)
    old = time.time() - 60
    import os
    os.utime(q, (old, old))
    assert not ab._queue_is_alive(tmp_path)
    # This session's own marker is a leftover; another session's is work.
    own = tmp_path / f"progress_{ab._SESSION_TAG}-00000001-abc.json"
    own.write_text("{}")
    assert not ab._queue_is_alive(tmp_path)
    (tmp_path / "progress_ffff0000-00000007-abc.json").write_text("{}")
    assert ab._queue_is_alive(tmp_path)


@pytest.fixture
def bridge(tmp_path):
    from eda_agent.config import configure
    configure(workspace_dir=tmp_path)
    b = ab.AltiumBridge()
    yield b
    b.detach()


def test_a_queued_request_waits_past_its_window(bridge, tmp_path):
    rid = ab._short_id()
    (tmp_path / f"request_{rid}.json").write_text("{}")
    stop = threading.Event()

    def altium_busy_with_another_session():
        # queue.json refreshed while another session holds the slot, then
        # our request is taken and answered.
        for _ in range(8):
            (tmp_path / "queue.json").write_text('{"serving":"ffff0000"}')
            time.sleep(0.2)
        (tmp_path / f"request_{rid}.json").unlink()
        (tmp_path / f"response_{rid}.json").write_text(
            '{"protocol_version": 2, "id": "%s", "success": true, '
            '"data": {"ok": 1}, "error": null}' % rid)
        stop.set()

    t = threading.Thread(target=altium_busy_with_another_session)
    t.start()
    try:
        resp = bridge._poll_response(rid, timeout=0.5)
    finally:
        t.join()
    assert resp.success and resp.data == {"ok": 1}


def test_a_session_alone_still_times_out_on_a_dead_loop(bridge, tmp_path):
    from eda_agent.bridge.exceptions import AltiumTimeoutError

    rid = ab._short_id()
    (tmp_path / f"request_{rid}.json").write_text("{}")
    start = time.monotonic()
    with pytest.raises(AltiumTimeoutError):
        bridge._poll_response(rid, timeout=0.5)
    assert time.monotonic() - start < 5
