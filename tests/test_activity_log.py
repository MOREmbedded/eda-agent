# SPDX-License-Identifier: Apache-2.0
"""Rotating, capped, redacting file logging (activity_log.py)."""
from __future__ import annotations

import logging

from eda_agent import activity_log as L


def test_redact_hides_bearer_tokens_and_secret_like_fields():
    text = ('Authorization: Bearer abc.def.ghi client_secret="s3cr3t" '
            'MOUSER_API_KEY=plainkey password: hunter2 normal=value')
    out = L.redact(text)
    assert "abc.def.ghi" not in out
    assert "s3cr3t" not in out
    assert "plainkey" not in out
    assert "hunter2" not in out
    assert "normal=value" in out, "an unrelated key=value pair must survive untouched"


def test_json_formatter_emits_redacted_structured_lines():
    record = logging.makeLogRecord({
        "name": "eda_agent.engineering", "levelname": "INFO", "levelno": logging.INFO,
        "created": 1700000000.0, "msg": "token=%s", "args": ("Bearer sekrit",)})
    line = L.JsonFormatter().format(record)
    assert "sekrit" not in line
    import json
    parsed = json.loads(line)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "eda_agent.engineering"


def test_capped_rotating_handler_rotates_and_enforces_the_total_cap(tmp_path):
    handler = L.CappedRotatingHandler(
        tmp_path / "eda_agent.log", max_bytes=500, backups=10,
        total_max_bytes=1200, min_free_bytes=0)
    handler.setFormatter(L.JsonFormatter())
    logger = logging.getLogger("test_activity_log.rotation")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for i in range(400):
        logger.info("event number %d with some padding text to grow the file", i)
    handler.close()

    files = [p for p in tmp_path.glob("*.log*") if p.is_file()]
    assert len(files) > 1, "nothing rotated"
    total = sum(p.stat().st_size for p in files)
    assert total <= 1200 + 4096, "the total-directory cap was not enforced"


def test_low_disk_guard_suppresses_info_and_logs_one_critical(tmp_path):
    handler = L.CappedRotatingHandler(
        tmp_path / "eda_agent.log", max_bytes=10_000_000, backups=3,
        total_max_bytes=10_000_000, min_free_bytes=10**15)
    handler.setFormatter(L.JsonFormatter())
    handler._last_disk_check = 0
    logger = logging.getLogger("test_activity_log.low_disk")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("should_be_dropped")
    logger.warning("kept_warning")
    handler.close()

    text = (tmp_path / "eda_agent.log").read_text(encoding="utf-8")
    assert "low disk space" in text
    assert "should_be_dropped" not in text
    assert "kept_warning" in text


def test_install_attaches_one_handler_and_is_idempotent(tmp_path):
    logger = logging.getLogger("test_activity_log.install")
    logger.handlers.clear()
    try:
        L.install(logger, log_dir=tmp_path)
        L.install(logger, log_dir=tmp_path)  # must replace, not stack
        marked = [h for h in logger.handlers
                 if getattr(h, "_eda_agent_activity_log", False)]
        assert len(marked) == 1
        logger.setLevel(logging.INFO)
        logger.info("hello")
        for h in logger.handlers:
            h.flush()
        assert (tmp_path / "eda_agent.log").exists()
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()


def test_install_defaults_to_a_logs_dir_beside_the_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("EDA_AGENT_LOG_DIR", raising=False)
    from eda_agent.config import configure
    configure(workspace_dir=tmp_path / "workspace")

    logger = logging.getLogger("test_activity_log.default_dir")
    logger.handlers.clear()
    try:
        resolved = L.install(logger)
        assert resolved == tmp_path / "logs"
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()


def test_tail_reads_only_the_last_n_lines(tmp_path):
    (tmp_path / "eda_agent.log").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    out = L.tail(tmp_path, lines=5)
    assert out == [f"line {i}" for i in range(95, 100)]


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert L.tail(tmp_path) == []
