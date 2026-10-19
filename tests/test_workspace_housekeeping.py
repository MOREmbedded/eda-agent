# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The workspace is trimmed when a server starts.

Before this nothing was: both logs were append-only from the first
install, and every checkpoint and screenshot was kept (850 MB after six
months).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from eda_agent import housekeeping
from eda_agent.checkpoint import CheckpointStore


def test_a_small_log_is_left_alone(tmp_path: Path):
    log = tmp_path / "bridge_trace.log"
    log.write_bytes(b"one\ntwo\n")
    assert housekeeping.rotate_log(log, max_bytes=100) is False
    assert log.read_bytes() == b"one\ntwo\n"
    assert not (tmp_path / "bridge_trace.log.1").exists()


def test_a_big_log_moves_to_dot_1_and_keeps_its_last_whole_lines(tmp_path: Path):
    log = tmp_path / "bridge_trace.log"
    lines = [f"{i:05d} SEND cmd=application.ping".encode() for i in range(400)]
    log.write_bytes(b"\n".join(lines) + b"\n")
    original = log.read_bytes()

    assert housekeeping.rotate_log(log, max_bytes=1000, keep_tail=300) is True

    assert (tmp_path / "bridge_trace.log.1").read_bytes() == original
    kept = log.read_bytes()
    assert 0 < len(kept) <= 300
    assert original.endswith(kept)
    assert kept.split(b"\n")[0] in lines      # starts on a whole line
    assert kept.endswith(b"00399 SEND cmd=application.ping\n")
    assert not list(tmp_path.glob("*.tmp"))


def test_a_second_rotation_replaces_the_old_dot_1(tmp_path: Path):
    log = tmp_path / "activity.log"
    (tmp_path / "activity.log.1").write_bytes(b"old\n")
    log.write_bytes(b"x" * 50 + b"\n" + b"y" * 50 + b"\n")
    assert housekeeping.rotate_log(log, max_bytes=10, keep_tail=60)
    assert (tmp_path / "activity.log.1").read_bytes().startswith(b"x")


def _checkpoint(store: CheckpointStore, cid: str, days_old: float,
                project: str = ""):
    store.manifests.mkdir(parents=True, exist_ok=True)
    store.blobs.mkdir(parents=True, exist_ok=True)
    blob = store.blobs / f"blob{cid}"
    blob.write_bytes(cid.encode())
    old = time.time() - 3600
    os.utime(blob, (old, old))
    created = (datetime.now() - timedelta(days=days_old)).isoformat()
    (store.manifests / f"{cid}.json").write_text(json.dumps({
        "id": cid, "created": created, "label": "", "project_file": project,
        "file_count": 1, "total_bytes": 1,
        "files": {"a.PcbDoc": {"hash": f"blob{cid}", "size": 1}},
    }), encoding="utf-8")


def test_a_checkpoint_goes_only_when_old_and_past_the_newest_kept(tmp_path: Path):
    store = CheckpointStore(tmp_path / "checkpoints")
    # ids sort newest first; 5 recent, 5 old
    for i in range(10):
        _checkpoint(store, f"2026{i:04d}", days_old=1 if i >= 5 else 90)

    removed = store.prune_aged(keep=3, max_age_days=30)

    # the 5 recent stay even though only 3 are "kept"; the 5 old go
    assert sorted(removed) == [f"2026{i:04d}" for i in range(5)]
    assert len(store.list()) == 5
    assert sorted(b.name for b in store.blobs.iterdir()) == \
        [f"blob2026{i:04d}" for i in range(5, 10)]


def test_old_checkpoints_within_the_newest_kept_stay(tmp_path: Path):
    store = CheckpointStore(tmp_path / "checkpoints")
    for i in range(4):
        _checkpoint(store, f"2026{i:04d}", days_old=200)
    assert store.prune_aged(keep=20, max_age_days=30) == []
    assert len(store.list()) == 4


def test_a_project_left_alone_keeps_its_newest_three(tmp_path: Path):
    store = CheckpointStore(tmp_path / "checkpoints")
    for i in range(5):                       # old, quiet project
        _checkpoint(store, f"2025{i:04d}", days_old=120, project="quiet.PrjPcb")
    for i in range(30):                      # busy project, all recent-ish
        _checkpoint(store, f"2026{i:04d}", days_old=1, project="busy.PrjPcb")

    removed = store.prune_aged(keep=20, max_age_days=30, keep_per_project=3)

    assert sorted(removed) == ["20250000", "20250001"]
    left = [c.id for c in store.list() if c.project_file == "quiet.PrjPcb"]
    assert sorted(left) == ["20250002", "20250003", "20250004"]


def test_only_old_default_screenshots_are_deleted(tmp_path: Path):
    old = time.time() - 40 * 86400
    stale = tmp_path / "altium_1788108472.png"
    fresh = tmp_path / "altium_1791736946.png"
    other = tmp_path / "board_render.png"
    for p in (stale, fresh, other):
        p.write_bytes(b"png")
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    assert housekeeping.prune_screenshots(tmp_path, max_age_days=30) == 1
    assert not stale.exists() and fresh.exists() and other.exists()


def test_run_never_raises_on_an_empty_workspace(tmp_path: Path):
    report = housekeeping.run(tmp_path)
    assert report == {"rotated": [], "checkpoints_removed": 0,
                      "screenshots_removed": 0}


def test_the_server_starts_housekeeping_off_the_main_thread():
    src = (Path(__file__).resolve().parents[1] / "src" / "eda_agent" /
           "server.py").read_text(encoding="utf-8")
    assert "target=housekeeping.run, args=(config.workspace_dir,)" in src
    assert 'name="workspace-housekeeping", daemon=True).start()' in src
