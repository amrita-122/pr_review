import json
import logging

import pytest
import structlog

from app.logging import configure_logging
from app.review.llm import load_prompt


def test_logs_are_json_lines_with_bound_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id="r1")
    try:
        structlog.get_logger().info("hello", file="a.py")
        logging.getLogger("third.party").warning("plain stdlib line")
    finally:
        structlog.contextvars.clear_contextvars()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert lines[0]["event"] == "hello" and lines[0]["run_id"] == "r1"
    assert lines[0]["level"] == "info" and "timestamp" in lines[0]
    assert lines[1]["event"] == "plain stdlib line"


def test_prompt_version_cannot_escape_the_prompt_directory() -> None:
    with pytest.raises(ValueError):
        load_prompt("../../config")
    assert "untrusted" in load_prompt("security_v2").lower()


def test_http_client_request_logs_are_silenced() -> None:
    configure_logging()
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
