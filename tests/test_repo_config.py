"""Per-repo .review-bot.yml (10.29): read from the BASE commit only, can only tighten."""

import base64

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.github.repo_config import RepoConfig, fetch_repo_config, parse_repo_config
from app.jobs.handlers import FAILURE_COMMENT  # noqa: F401  (import check: handlers still load)
from app.review.graph import ReviewLimits
from tests.helpers import Pipeline
from tests.test_jobs import Drain, Enqueue, make_event

LIMITS = ReviewLimits(max_files=20, max_chars_per_file=10_000, max_comments=25)
CONTENTS = "https://api.github.com/repos/o/r/contents/.review-bot.yml"


def test_valid_config_parses() -> None:
    cfg = parse_repo_config("agents: [security]\nignore:\n  - 'docs/**'\nmax_files: 5\n")
    assert cfg.agents == ["security"] and cfg.ignores("docs/a/b.md") and not cfg.ignores("a.py")


@pytest.mark.parametrize(
    "text",
    [
        "agents: [security\n",  # broken YAML
        "unknown_key: 1\n",  # extra=forbid
        "max_files: 0\n",
        "max_files: lots\n",
        "- a\n- b\n",  # not a mapping
        "x: &a [1]\ny: *a\n",  # anchors/aliases
        "ignore: " + "[" + ",".join(f"'p{i}'" for i in range(60)) + "]\n",  # too many globs
        "# " + "x" * 20_000,  # too large
    ],
)
def test_bad_config_means_defaults(text: str) -> None:
    assert parse_repo_config(text) == RepoConfig()


def test_empty_file_means_defaults() -> None:
    assert parse_repo_config("") == RepoConfig()


def test_config_can_lower_limits_but_never_raise_them() -> None:
    lowered = parse_repo_config("max_files: 5\nmax_comments: 3\n").tighten(LIMITS)
    assert (lowered.max_files, lowered.max_comments) == (5, 3)
    raised = parse_repo_config("max_files: 999\nmax_chars_per_file: 10000000\n").tighten(LIMITS)
    assert (raised.max_files, raised.max_chars_per_file) == (20, 10_000)


def test_agents_are_narrowed_and_cannot_be_emptied_or_added() -> None:
    enabled = ["security", "quality"]
    assert parse_repo_config("agents: [security]").choose_agents(enabled) == ["security"]
    assert parse_repo_config("agents: [bogus]").choose_agents(enabled) == enabled
    assert parse_repo_config("agents: [security]").choose_agents(["quality"]) == ["quality"]
    assert RepoConfig().choose_agents(enabled) == enabled


def _contents(text: str) -> httpx.Response:
    body = base64.b64encode(text.encode()).decode()
    return httpx.Response(
        200, json={"type": "file", "encoding": "base64", "content": body, "size": len(text)}
    )


@respx.mock
async def test_config_is_read_at_the_base_sha_and_never_the_head() -> None:
    route = respx.get(CONTENTS).mock(return_value=_contents("max_files: 2\n"))
    respx.post(url__regex=r".*/access_tokens").mock(return_value=httpx.Response(201, json={}))
    cfg = await fetch_repo_config("tok", "o", "r", "base123")
    assert cfg.max_files == 2
    assert route.calls[0].request.url.params["ref"] == "base123"


@respx.mock
async def test_no_base_sha_or_missing_file_or_error_means_defaults() -> None:
    route = respx.get(CONTENTS).mock(return_value=httpx.Response(404))
    assert await fetch_repo_config("t", "o", "r", None) == RepoConfig()
    assert not route.called
    assert await fetch_repo_config("t", "o", "r", "b") == RepoConfig()
    respx.get(CONTENTS).mock(side_effect=httpx.ConnectError("down"))
    assert await fetch_repo_config("t", "o", "r", "b") == RepoConfig()


@respx.mock
async def test_job_skips_ignored_files_and_reports_why(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    event = make_event()
    assert event.base_sha  # the fixture carries pull_request.base.sha
    gh = Pipeline(event)
    route = respx.get(url__regex=r".*/contents/.review-bot.yml").mock(
        return_value=_contents("ignore: ['app/**']\n")
    )
    enqueue(event)
    await drain()
    assert route.calls[0].request.url.params["ref"] == event.base_sha
    sent = gh.sent_review()
    assert sent["comments"] == []  # app/db.py was the only file with a finding
    assert "app/db.py" in sent["body"] and "ignored by .review-bot.yml" in sent["body"]
