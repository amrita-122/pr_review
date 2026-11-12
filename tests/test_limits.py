"""Spend and abuse limits and failure alerts (10.34), plus the stored rationale (for /explain)."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import FindingRow, Job, ReviewRun
from app.jobs.alerts import send_alert
from app.jobs.handlers import BUDGET_COMMENT
from tests.helpers import Pipeline
from tests.test_jobs import Drain, Enqueue, make_event, settings_env

HOOK = "https://hooks.example.test/T000/secret-path"


def _other_run(db: Session, cost: str) -> None:
    db.add(
        ReviewRun(
            id="earlier",
            repo="o/other",
            pr_number=9,
            head_sha="e" * 40,
            status="ok",
            prompt_version="v",
            model="m",
            cost_usd=Decimal(cost),
            started_at=datetime.now(UTC),
        )
    )
    db.commit()


@respx.mock
async def test_over_the_daily_budget_skips_the_review_and_says_so(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, MAX_COST_PER_DAY_USD="1")
    _other_run(db_session, "5")
    event = make_event()
    gh = Pipeline(event)
    enqueue(event)
    await drain()
    assert (
        gh.reviews.call_count == 0 and gh.files.call_count == 0
    )  # nothing was fetched or paid for
    assert json.loads(gh.comments.calls[0].request.content) == {"body": BUDGET_COMMENT}
    run = db_session.scalars(select(ReviewRun).where(ReviewRun.repo != "o/other")).one()
    assert run.status == "skipped"


@respx.mock
async def test_under_the_budget_reviews_normally(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, MAX_COST_PER_DAY_USD="10")
    _other_run(db_session, "5")
    event = make_event()
    gh = Pipeline(event)
    enqueue(event)
    await drain()
    assert gh.reviews.call_count == 1


def test_no_budget_configured_means_no_limit(db_session: Session, enqueue: Enqueue) -> None:
    _other_run(db_session, "9999")
    enqueue(make_event())
    assert db_session.scalars(select(Job)).all()


def test_author_rate_limit_stops_the_second_push_in_the_hour(
    db_session: Session, enqueue: Enqueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, MAX_RUNS_PER_AUTHOR_PER_HOUR="1")
    first = make_event(sha="a" * 40)
    assert first.author_login  # the fixture has pull_request.user.login
    enqueue(first)
    enqueue(make_event(sha="b" * 40))
    assert len(db_session.scalars(select(Job)).all()) == 1
    rows = db_session.scalars(select(ReviewRun)).all()  # the refused push leaves a skipped run
    assert sorted(r.status for r in rows) == ["queued", "skipped"]
    assert {r.author_login for r in rows} == {first.author_login}


def test_rate_limit_off_by_default(db_session: Session, enqueue: Enqueue) -> None:
    assert get_settings().max_runs_per_author_per_hour == 0
    enqueue(make_event(sha="a" * 40))
    enqueue(make_event(sha="b" * 40))
    assert len(db_session.scalars(select(Job)).all()) == 2


def test_a_login_that_is_not_well_formed_is_not_stored() -> None:
    event = make_event()
    event.pull_request.user.login = "bad login\n"  # type: ignore[union-attr]
    assert event.author_login is None


@respx.mock
async def test_posted_findings_keep_their_rationale_for_explain(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    event = make_event()
    Pipeline(event)
    enqueue(event)
    await drain()
    row = db_session.scalars(select(FindingRow)).one()
    assert row.posted and row.rationale


@respx.mock
async def test_alert_is_sent_with_static_text_and_never_without_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await send_alert(get_settings(), "x") is False  # not configured
    settings_env(monkeypatch, ALERT_WEBHOOK_URL=HOOK)
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    assert await send_alert(get_settings(), "review job failed for good: reason=model") is True
    assert json.loads(route.calls[0].request.content) == {
        "text": "review job failed for good: reason=model"
    }


@respx.mock
async def test_a_broken_alert_endpoint_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_env(monkeypatch, ALERT_WEBHOOK_URL=HOOK)
    respx.post(HOOK).mock(side_effect=httpx.ConnectError("down"))
    assert await send_alert(get_settings(), "x") is False
    respx.post(HOOK).mock(return_value=httpx.Response(500))
    assert await send_alert(get_settings(), "x") is False
