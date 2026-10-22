# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The server keeps its own capped log, with secrets taken out.

stderr alone left nothing to read after a problem when the MCP client
dropped it. A file per server process (a shared one cannot be rotated on
Windows while another process has it open), capped and pruned, and a
redacting filter on every handler.
"""
from __future__ import annotations

import logging
import os
import time

from eda_agent import server_log
from eda_agent.housekeeping import run as housekeeping_run


def test_secrets_are_taken_out_and_the_rest_is_kept():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcDEF123_-x"
    cases = {
        'posting {"api_key": "sk-123abc", "part": "LM317"}':
            'posting {"api_key": "***", "part": "LM317"}',
        # The connection-string rule takes the value up to the next ";",
        # so a URL loses what follows the token too: the safe side.
        "GET /x?access_token=AbC123&page=2": "GET /x?access_token=***",
        "Authorization: Bearer abc.def-123": "Authorization: Bearer ***",
        f"token was {jwt} then": "token was ***jwt*** then",
        "Provider=SQLOLEDB;User ID=sa;Password=hunter2;Data Source=x":
            "Provider=SQLOLEDB;User ID=sa;Password=***;Data Source=x",
        "R1 changed to 10k": "R1 changed to 10k",
    }
    for raw, want in cases.items():
        assert server_log.redact(raw) == want, raw


def test_the_filter_redacts_formatted_records():
    record = logging.LogRecord("eda_agent.t", logging.INFO, __file__, 1,
                               "connecting with password=%s", ("hunter2",),
                               None)
    assert server_log.RedactingFilter().filter(record)
    assert record.getMessage() == "connecting with password=***"


def test_install_writes_a_per_process_file(tmp_path):
    root = logging.getLogger("eda_agent.test_server_log")
    root.handlers.clear()
    root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)
    try:
        path = server_log.install(root, tmp_path)
        assert path == tmp_path / "logs" / f"server-{os.getpid()}.log"
        root.info("api_key=secret123 for %s", "R7")
        for h in root.handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert "api_key=***" in text and "secret123" not in text
        assert all(any(isinstance(f, server_log.RedactingFilter)
                       for f in h.filters) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)


def test_old_server_logs_are_pruned_by_housekeeping(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old, new = logs / "server-1.log", logs / "server-2.log"
    old.write_text("x")
    new.write_text("y")
    past = time.time() - (server_log.MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (past, past))
    report = housekeeping_run(tmp_path)
    assert report["server_logs_removed"] == 1
    assert not old.exists() and new.exists()
