"""POST /webhooks/github with issue_comment events: what may become a /confirm job."""

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import Job, WebhookDelivery
from app.github.webhooks import is_confirm_command, parse_issue_comment_event
from app.jobs.payload import CONFIRM_REVIEW
from tests.test_webhook_signatures import post_signed

FIXTURE = Path(__file__).parent / "fixtures" / "issue_comment_created.json"
RAW = json.loads(FIXTURE.read_bytes())
REPO = f"{RAW['repository']['owner']['login']}/{RAW['repository']['name']}"
NUMBER = RAW["issue"]["number"]


@pytest.fixture(autouse=True)
def confirmation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()


def hold(db: Session, run_id: str = "run1", repo: str = REPO, number: int = NUMBER) -> None:
    runs.create_queued(
        db, run_id=run_id, repo=repo, pr_number=number, head_sha="a" * 40, prompt_version="v",
        model="m",
    )  # fmt: skip
    assert runs.mark_running(db, run_id, prompt_version="v", model="m")
    assert runs.mark_awaiting(db, run_id, installation_id=1)


def comment(**changes: Any) -> bytes:
    raw = json.loads(json.dumps(RAW))
    for key, value in changes.items():
        if key == "body":
            raw["comment"]["body"] = value
        elif key == "user":
            raw["comment"]["user"].update(value)
        elif key == "plain_issue":
            raw["issue"].pop("pull_request", None)
        else:
            raw[key] = value
    return json.dumps(raw).encode()


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# --- the parser --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/confirm", True),
        ("  /confirm\n", True),
        ("/confirm please", False),
        ("> /confirm", False),
        ("/Confirm", False),
        ("I will /confirm later", False),
        ("", False),
    ],
)
def test_only_an_exact_confirm_command_counts(body: str, expected: bool) -> None:
    assert is_confirm_command(body) is expected


def test_real_fixture_parses_into_a_confirm_request() -> None:
    event = parse_issue_comment_event(FIXTURE.read_bytes())
    assert (event.owner, event.repo, event.number) == (
        RAW["repository"]["owner"]["login"],
        RAW["repository"]["name"],
        NUMBER,
    )
    assert event.is_confirm_request


def test_bot_payload_still_parses_but_is_never_a_confirm_request() -> None:
    event = parse_issue_comment_event(comment(user={"login": "review-bot[bot]", "type": "Bot"}))
    assert not event.is_confirm_request


# --- the route ---------------------------------------------------------------------------------


def test_signature_is_still_enforced_for_comments() -> None:
    from tests.test_webhook_signatures import client

    resp = client.post(
        "/webhooks/github",
        content=comment(),
        headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert resp.status_code == 401


def test_maintainer_confirm_on_a_held_pr_queues_one_job(db_session: Session) -> None:
    hold(db_session)
    resp = post_signed(comment(), event="issue_comment")
    assert (resp.status_code, resp.json()) == (202, {"status": "accepted"})
    job = db_session.scalars(select(Job)).one()
    assert job.kind == CONFIRM_REVIEW
    assert job.payload["run_id"] == "run1"
    assert job.payload["commenter_login"] == RAW["comment"]["user"]["login"]
    assert job.payload["comment_id"] == RAW["comment"]["id"]


def test_same_delivery_twice_queues_one_job(db_session: Session) -> None:
    hold(db_session)
    first = post_signed(comment(), event="issue_comment", delivery="d1")
    second = post_signed(comment(), event="issue_comment", delivery="d1")
    assert (first.json(), second.json()) == ({"status": "accepted"}, {"status": "duplicate"})
    assert count(db_session, Job) == 1


def test_no_held_run_means_no_job_and_no_delivery_row(db_session: Session) -> None:
    resp = post_signed(comment(), event="issue_comment")
    assert resp.json() == {"status": "ignored"}
    assert (count(db_session, Job), count(db_session, WebhookDelivery)) == (0, 0)


def test_held_run_on_another_pr_does_not_count(db_session: Session) -> None:
    hold(db_session, number=NUMBER + 1)
    assert post_signed(comment(), event="issue_comment").json() == {"status": "ignored"}
    assert count(db_session, Job) == 0


def test_superseded_hold_is_ignored(db_session: Session) -> None:
    hold(db_session)
    runs.supersede_older(db_session, REPO, NUMBER, "b" * 40)
    assert post_signed(comment(), event="issue_comment").json() == {"status": "ignored"}
    assert count(db_session, Job) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "edited"},
        {"body": "/confirm please"},
        {"body": "Reply /confirm to post"},
        {"plain_issue": True},
        {"user": {"login": "review-bot[bot]", "type": "Bot"}},
    ],
    ids=["edited", "extra-words", "quoted-in-sentence", "plain-issue", "bot-sender"],
)
def test_untrusted_comments_that_are_not_a_confirm_are_ignored(
    db_session: Session, changes: dict[str, Any]
) -> None:
    hold(db_session)
    assert post_signed(comment(**changes), event="issue_comment").json() == {"status": "ignored"}
    assert (count(db_session, Job), count(db_session, WebhookDelivery)) == (0, 0)


def test_feature_flag_off_ignores_everything(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONFIRMATION_ENABLED", "false")
    get_settings.cache_clear()
    hold(db_session)
    assert post_signed(comment(), event="issue_comment").json() == {"status": "ignored"}
    assert count(db_session, Job) == 0


def test_malformed_comment_payload_is_a_422() -> None:
    assert post_signed(b'{"action": "created"}', event="issue_comment").status_code == 422
