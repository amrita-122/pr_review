"""10.17: /dismiss marks a finding a false positive, or drops a held review."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import runs
from app.db.models import FindingRow, Job, ReviewRun
from app.jobs import handlers
from app.jobs.handlers import DISMISSED_HOLD_COMMENT, DISMISSED_REPLY, run_confirm_job
from app.jobs.payload import ConfirmJobPayload
from app.review.dedupe import KnownFinding
from tests.helpers import Pipeline
from tests.test_confirm_job import (  # noqa: F401
    EVENT,
    REPO,
    RUN_ID,
    Saver,
    hold_review,
    permission,
    saver,  # noqa: F401  (fixture)
)
from tests.test_confirm_webhook import RAW as COMMENT_RAW
from tests.test_confirm_webhook import comment, hold
from tests.test_fixed import COMMENT_ID, Setup, thread
from tests.test_webhook_signatures import post_signed

NUMBER = COMMENT_RAW["issue"]["number"]
WEBHOOK_REPO = f"{COMMENT_RAW['repository']['owner']['login']}/{COMMENT_RAW['repository']['name']}"


@pytest.fixture(autouse=True)
def confirmation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()


def review_comment(body: str = "/dismiss", **changes: object) -> bytes:
    raw = {
        "action": "created",
        "comment": {
            "id": 9000,
            "body": body,
            "user": {"login": "maint-bob", "type": "User"},
            "in_reply_to_id": COMMENT_ID,
        },
        "pull_request": {"number": NUMBER, "user": {"login": "pr-author", "type": "User"}},
        "repository": COMMENT_RAW["repository"],
        "installation": COMMENT_RAW["installation"],
    }
    for key, value in changes.items():
        if key == "user":
            raw["comment"]["user"].update(value)  # type: ignore[index, union-attr]
        elif key == "in_reply_to_id":
            raw["comment"]["in_reply_to_id"] = value  # type: ignore[index]
        else:
            raw[key] = value
    return json.dumps(raw).encode()


def post_finding(db: Session, run_id: str = "run1", comment_id: int = COMMENT_ID) -> int:
    """A finished review of the webhook fixture's PR with one posted finding on GitHub."""
    runs.create_queued(
        db, run_id=run_id, repo=WEBHOOK_REPO, pr_number=NUMBER, head_sha="a" * 40,
        prompt_version="v", model="m",
    )  # fmt: skip
    row = FindingRow(
        run_id=run_id, file="app/db.py", line=2, severity="high", category="secrets",
        title="t", posted=True, github_comment_id=comment_id,
    )  # fmt: skip
    db.add(row)
    run = db.get(ReviewRun, run_id)
    assert run is not None
    run.posted_at = datetime.now(UTC)
    db.commit()
    return row.id


def send(body: bytes, delivery: str | None = None):  # noqa: ANN201
    return post_signed(body, event="pull_request_review_comment", delivery=delivery)


# --- the webhook ---------------------------------------------------------------------------


def test_a_dismiss_reply_to_one_of_our_comments_queues_one_job(db_session: Session) -> None:
    post_finding(db_session)
    assert send(review_comment()).json() == {"status": "accepted"}
    job = db_session.scalars(select(Job)).one()
    assert job.payload["command"] == "dismiss" and job.payload["target_comment_id"] == COMMENT_ID
    assert job.payload["commenter_login"] == "maint-bob"
    assert job.payload["author_login"] == "pr-author"


@pytest.mark.parametrize(
    "changes",
    [
        {"body": "/dismiss please"},  # not an exact command
        {"body": "/confirm"},  # the other command does nothing here
        {"in_reply_to_id": None},  # not a reply: nothing says which finding
        {"in_reply_to_id": 4242},  # a reply to somebody else's comment
        {"user": {"type": "Bot"}},  # includes our own app
        {"user": {"login": "bad login!"}},  # goes into a URL path later
        {"action": "edited"},  # editing an old comment into a command must not work
    ],
)
def test_everything_else_is_ignored_before_any_row_is_written(
    db_session: Session, changes: dict
) -> None:
    from sqlalchemy import func

    from app.db.models import WebhookDelivery

    post_finding(db_session)
    body = changes.get("body", "/dismiss")
    extra = {k: v for k, v in changes.items() if k != "body"}
    if "action" in extra:
        raw = json.loads(review_comment(str(body)))
        raw["action"] = extra["action"]
        payload = json.dumps(raw).encode()
    else:
        payload = review_comment(str(body), **extra)
    assert send(payload).json() == {"status": "ignored"}
    assert db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert db_session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


def test_the_feature_flag_turns_it_off(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_finding(db_session)
    monkeypatch.setenv("FEEDBACK_ENABLED", "false")
    get_settings.cache_clear()
    assert send(review_comment()).json() == {"status": "ignored"}


def test_a_refused_login_costs_nothing_the_second_time(db_session: Session) -> None:
    post_finding(db_session)
    runs.record_denial(db_session, "run1", "maint-bob")
    assert send(review_comment()).json() == {"status": "ignored"}


def test_same_delivery_twice_is_one_job(db_session: Session) -> None:
    post_finding(db_session)
    first, second = send(review_comment(), "d1"), send(review_comment(), "d1")
    assert (first.json(), second.json()) == ({"status": "accepted"}, {"status": "duplicate"})
    assert len(list(db_session.scalars(select(Job)))) == 1


def test_a_dismiss_comment_on_the_pr_dismisses_the_held_review(db_session: Session) -> None:
    hold(db_session)
    assert post_signed(comment(body="/dismiss"), event="issue_comment").json() == {
        "status": "accepted"
    }
    job = db_session.scalars(select(Job)).one()
    assert job.payload["command"] == "dismiss" and job.payload["target_comment_id"] is None


def test_a_dismiss_comment_with_nothing_held_is_ignored(db_session: Session) -> None:
    assert post_signed(comment(body="/dismiss"), event="issue_comment").json() == {
        "status": "ignored"
    }


# --- the worker: a finding in its thread ------------------------------------------------------


def dismiss_payload(login: str = "maintainer", **changes: object) -> ConfirmJobPayload:
    return ConfirmJobPayload(
        run_id="run1",
        installation_id=EVENT.installation_id,
        owner=EVENT.owner,
        repo=EVENT.repo,
        number=EVENT.number,
        comment_id=9000,
        commenter_login=login,
        command="dismiss",
        target_comment_id=COMMENT_ID,
        **changes,  # type: ignore[arg-type]
    )


def post_finding_for_event(db: Session) -> int:
    runs.create_queued(
        db, run_id="run1", repo=REPO, pr_number=EVENT.number, head_sha=EVENT.head_sha,
        prompt_version="v", model="m",
    )  # fmt: skip
    row = FindingRow(
        run_id="run1", file="app/db.py", line=2, severity="high", category="secrets",
        title="t", posted=True, github_comment_id=COMMENT_ID,
    )  # fmt: skip
    db.add(row)
    run = db.get(ReviewRun, "run1")
    assert run is not None
    run.posted_at = datetime.now(UTC)
    db.commit()
    return row.id


def row_of(db: Session, finding_id: int) -> FindingRow:
    db.expire_all()
    return db.get(FindingRow, finding_id)  # type: ignore[return-value]


@respx.mock
async def test_a_maintainer_dismisses_a_finding_once(db_session: Session) -> None:
    gh = Pipeline(EVENT)
    setup = Setup(gh, [thread()])
    permission("write")
    finding_id = post_finding_for_event(db_session)

    assert await run_confirm_job(dismiss_payload()) == "dismissed"
    assert await run_confirm_job(dismiss_payload()) == "ignored"  # a redelivery: nothing again

    row = row_of(db_session, finding_id)
    assert (row.false_positive, row.dismissed_by) == (True, "maintainer")
    assert setup.reply.call_count == 1
    assert json.loads(setup.reply.calls[0].request.content)["body"] == DISMISSED_REPLY
    assert setup.queries == ["threads", "resolve"]
    assert setup.deletes.call_count == 0
    run = db_session.get(ReviewRun, "run1")
    assert run is not None and run.dismissed_count == 1
    assert runs.dismissal_rate(db_session, "run1") == 1.0


@respx.mock
@pytest.mark.parametrize("level", ["read", "none", None])
async def test_someone_without_write_access_cannot_dismiss(
    db_session: Session, level: str | None
) -> None:
    gh = Pipeline(EVENT)
    setup = Setup(gh, [thread()])
    permission(level)
    finding_id = post_finding_for_event(db_session)

    assert await run_confirm_job(dismiss_payload()) == "ignored"

    assert row_of(db_session, finding_id).false_positive is False
    assert setup.reply.call_count == 0
    assert gh.comments.call_count == 1  # the one short refusal, like /confirm


@respx.mock
async def test_the_pr_author_cannot_dismiss_findings_on_their_own_pr(db_session: Session) -> None:
    gh = Pipeline(EVENT)
    setup = Setup(gh, [thread()])
    permission("admin")
    finding_id = post_finding_for_event(db_session)

    assert await run_confirm_job(dismiss_payload(author_login="Maintainer")) == "ignored"
    assert row_of(db_session, finding_id).false_positive is False
    assert setup.reply.call_count == 0


@respx.mock
async def test_a_failed_reply_keeps_the_decision(db_session: Session) -> None:
    gh = Pipeline(EVENT)
    setup = Setup(gh, [thread()])
    setup.reply.mock(return_value=httpx.Response(500))
    permission("write")
    finding_id = post_finding_for_event(db_session)

    assert await run_confirm_job(dismiss_payload()) == "dismissed"
    assert row_of(db_session, finding_id).false_positive is True


@respx.mock
async def test_a_comment_that_is_not_one_of_our_findings_does_nothing(
    db_session: Session,
) -> None:
    Pipeline(EVENT)
    permission("write")
    post_finding_for_event(db_session)
    payload = dismiss_payload().model_copy(update={"target_comment_id": 123456})
    assert await run_confirm_job(payload) == "ignored"


# --- dismissed findings stay dismissed --------------------------------------------------------


def test_a_dismissed_finding_is_still_known_so_it_is_not_posted_again_but_never_called_fixed(
    db_session: Session,
) -> None:
    finding_id = post_finding_for_event(db_session)
    runs.dismiss_finding(db_session, finding_id, "maintainer")

    known = runs.list_known_findings(db_session, REPO, EVENT.number, "other-run")
    assert [KnownFinding(*k) for k in known] == [KnownFinding("app/db.py", "secrets", 2)]
    assert runs.list_open_findings(db_session, REPO, EVENT.number, "other-run") == []


def test_undoing_a_dismissal_restores_the_counts(db_session: Session) -> None:
    finding_id = post_finding_for_event(db_session)
    runs.dismiss_finding(db_session, finding_id, "maintainer")
    runs.undo_dismiss(db_session, finding_id)
    run = db_session.get(ReviewRun, "run1")
    assert run is not None and run.dismissed_count == 0
    assert runs.dismissal_rate(db_session, "run1") == 0.0
    assert runs.dismissal_rate(db_session, "nope") is None


# --- a held review can be dismissed instead of waiting to expire -----------------------------


@respx.mock
async def test_dismissing_a_held_review_drops_it_cleans_up_and_posts_nothing(
    db_session: Session,
    saver: Saver,  # noqa: F811
) -> None:
    gh = Pipeline(EVENT)
    permission("write")
    await hold_review(db_session, saver)
    payload = ConfirmJobPayload(
        run_id=RUN_ID,
        installation_id=EVENT.installation_id,
        owner=EVENT.owner,
        repo=EVENT.repo,
        number=EVENT.number,
        comment_id=5,
        commenter_login="maintainer",
        command="dismiss",
    )

    assert await run_confirm_job(payload) == "dismissed"

    db_session.expire_all()
    run = db_session.get(ReviewRun, RUN_ID)
    assert run is not None and run.status == runs.DISMISSED
    assert not saver.has_thread()  # the checkpoint (source code) is gone
    assert gh.reviews.call_count == 0
    assert [json.loads(c.request.content)["body"] for c in gh.comments.calls] == [
        DISMISSED_HOLD_COMMENT
    ]
    # and a later /confirm finds nothing held
    assert runs.get_awaiting_for_pr(db_session, REPO, EVENT.number) is None
    assert await run_confirm_job(payload) == "ignored"
    assert handlers.DISMISSED_HOLD_COMMENT  # (text exists for the docs)
