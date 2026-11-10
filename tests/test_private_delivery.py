"""10.20 / 10.21: on a PUBLIC repo, high/critical security findings never become PR text."""

import copy
import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import FindingRow
from app.github.webhooks import PullRequestEvent
from app.jobs import handlers
from app.jobs.handlers import pending_comment
from app.review.render import PUBLIC_CLOSING_LINE, render_summary
from app.review.schemas import Category, Finding, Severity
from app.review.visibility import PRIVATE_DELIVERED, PRIVATE_FAILED, divert, is_private_only
from tests.helpers import Pipeline
from tests.test_jobs import RAW, Drain, Enqueue, run_of

TITLE = "UNIQUE-TITLE-7731 SQL injection"
RATIONALE = "UNIQUE-RATIONALE-7731 the query is built with an f-string"


def public_event(sha: str | None = None, stamp: str | None = None) -> PullRequestEvent:
    raw = copy.deepcopy(RAW)
    raw["repository"]["private"] = False
    if sha is not None:
        raw["pull_request"]["head"]["sha"] = sha
    if stamp is not None:
        raw["pull_request"]["updated_at"] = stamp
    return PullRequestEvent.model_validate(raw)


EVENT = public_event()
ADVISORIES = f"https://api.github.com/repos/{EVENT.owner}/{EVENT.repo}/security-advisories"


def finding(
    severity: Severity = Severity.HIGH,
    category: Category = Category.INJECTION,
    line: int = 2,
    title: str = TITLE,
) -> Finding:
    return Finding(
        file_path="app/db.py",
        line=line,
        severity=severity,
        category=category,
        title=title,
        rationale=RATIONALE,
        suggestion="use a parameterised query",
    )


def use_reviewers(
    monkeypatch: pytest.MonkeyPatch, security: list[Finding], quality: list[Finding] | None = None
) -> None:
    async def sec(path: str, numbered: str) -> list[Finding]:
        return list(security)

    async def qual(path: str, numbered: str) -> list[Finding]:
        return list(quality or [])

    monkeypatch.setattr(handlers, "build_reviewers", lambda: {"security": sec, "quality": qual})


def channel(monkeypatch: pytest.MonkeyPatch, value: str, **extra: str) -> None:
    monkeypatch.setenv("PRIVATE_CHANNEL", value)
    for key, val in extra.items():
        monkeypatch.setenv(key.upper(), val)
    get_settings.cache_clear()


def public_text(gh: Pipeline) -> str:
    """Everything the bot sent to the PR itself: reviews and comments (never the advisory)."""
    calls = [*gh.reviews.calls, *gh.comments.calls]
    return "\n".join(call.request.content.decode() for call in calls)


def rows(db: Session) -> list[FindingRow]:
    db.expire_all()
    return list(db.scalars(select(FindingRow).order_by(FindingRow.id)))


# --- the pure rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "category", "expected"),
    [
        (Severity.CRITICAL, Category.INJECTION, True),
        (Severity.HIGH, Category.SECRETS, True),
        (Severity.MEDIUM, Category.INJECTION, False),
        (Severity.LOW, Category.CRYPTO, False),
        (Severity.CRITICAL, Category.BUG, False),  # quality findings are not vulnerability details
        (Severity.HIGH, Category.RESOURCE_LEAK, False),
    ],
)
def test_which_findings_are_private_only(
    severity: Severity, category: Category, expected: bool
) -> None:
    assert is_private_only(finding(severity, category)) is expected


def test_private_repos_divert_nothing() -> None:
    items = [finding(), finding(Severity.LOW)]
    assert divert(items, public=False) == (items, [])
    rest, private = divert(items, public=True)
    assert [f.severity for f in rest] == [Severity.LOW] and [f.severity for f in private] == [
        Severity.HIGH
    ]


def test_public_summary_never_says_no_findings_and_always_ends_the_same_way() -> None:
    clean = render_summary(2, [], [], public=True)
    assert "No findings" not in clean and clean.endswith(PUBLIC_CLOSING_LINE)
    # the same text whether or not something was diverted: it cannot be told apart
    assert clean == render_summary(2, [], [], public=True)
    assert "No findings" in render_summary(2, [], [])  # private repos are unchanged


def test_pending_comment_on_a_public_repo_names_no_severity_and_no_count() -> None:
    text = pending_comment(3, frozenset({Severity.CRITICAL}), public=True)
    assert "critical" not in text.lower() and "3" not in text and "`/confirm`" in text
    assert "critical" in pending_comment(3, frozenset({Severity.CRITICAL})).lower()


# --- end to end ----------------------------------------------------------------------------


@respx.mock
async def test_channel_off_nothing_reaches_the_pr_and_the_finding_stays_in_the_database(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_reviewers(monkeypatch, [finding()])
    gh = Pipeline(EVENT)
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    enqueue(EVENT)
    await drain()

    sent = gh.sent_review()
    assert sent["comments"] == [] and PUBLIC_CLOSING_LINE in sent["body"]
    assert advisory.call_count == 0  # no channel configured: fail closed
    for secret in (TITLE, RATIONALE, "high", "injection", "app/db.py", "UNIQUE"):
        assert secret not in public_text(gh), secret
    (row,) = rows(db_session)
    assert (row.posted, row.drop_reason, row.title) == (False, PRIVATE_FAILED, TITLE)
    run = run_of(db_session)
    assert run.status == "ok" and run.private_findings == 1 and run.repo_private is False


@respx.mock
async def test_with_the_advisory_channel_the_finding_goes_there_and_only_there(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory")
    use_reviewers(monkeypatch, [finding()])
    gh = Pipeline(EVENT)
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    enqueue(EVENT)
    await drain()

    assert advisory.call_count == 1
    body = json.loads(advisory.calls[0].request.content)
    assert body["summary"] == TITLE and RATIONALE in body["description"]
    assert body["severity"] == "high"
    assert body["vulnerabilities"][0]["package"]["ecosystem"] == "other"
    assert "app/db.py" in body["description"] and "line 2" in body["description"]
    for secret in (TITLE, RATIONALE, "UNIQUE", "injection", "app/db.py"):
        assert secret not in public_text(gh), secret
    (row,) = rows(db_session)
    assert (row.posted, row.drop_reason) == (False, PRIVATE_DELIVERED)


@respx.mock
async def test_a_failing_advisory_call_fails_closed_and_is_retried_on_the_next_push(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory")
    use_reviewers(monkeypatch, [finding()])
    first, second = (
        public_event("a" * 40, "2026-11-10T10:00:00Z"),
        public_event("b" * 40, "2026-11-10T10:05:00Z"),
    )
    gh = Pipeline(first)
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(403, json={}))
    enqueue(first)
    await drain()
    assert run_of(db_session, "a" * 40).status == "ok"  # the review itself is fine
    assert [r.drop_reason for r in rows(db_session)] == [PRIVATE_FAILED]
    assert TITLE not in public_text(gh)

    advisory.mock(return_value=httpx.Response(201, json={}))
    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": "b" * 40}}))
    enqueue(second)
    await drain()
    assert advisory.call_count == 2  # not "known" yet: delivered this time
    assert [r.drop_reason for r in rows(db_session)][-1] == PRIVATE_DELIVERED
    assert TITLE not in public_text(gh)


@respx.mock
async def test_a_delivered_finding_is_not_sent_again_on_the_next_push(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory")
    use_reviewers(monkeypatch, [finding()])
    first, second = (
        public_event("a" * 40, "2026-11-10T10:00:00Z"),
        public_event("b" * 40, "2026-11-10T10:05:00Z"),
    )
    gh = Pipeline(first)
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    enqueue(first)
    await drain()
    gh.head.mock(return_value=httpx.Response(200, json={"head": {"sha": "b" * 40}}))
    enqueue(second)
    await drain()
    assert advisory.call_count == 1


@respx.mock
async def test_only_the_dangerous_findings_are_diverted_the_rest_stay_inline(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory")
    use_reviewers(
        monkeypatch,
        security=[
            finding(Severity.CRITICAL, Category.INJECTION, line=2),
            finding(Severity.MEDIUM, Category.CRYPTO, line=3, title="weak hash md5"),
        ],
        quality=[finding(Severity.HIGH, Category.BUG, line=1, title="off by one loop")],
    )
    gh = Pipeline(EVENT)
    other = {"filename": "app/other.py", "status": "modified", "patch": None}
    del other  # one reviewed file is enough
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    enqueue(EVENT)
    await drain()

    inline = " ".join(c["body"] for c in gh.sent_review()["comments"])
    assert "weak hash md5" in inline and "off by one loop" in inline  # still inline
    assert TITLE not in public_text(gh) and "critical" not in public_text(gh).lower()
    assert advisory.call_count == 1


@respx.mock
async def test_the_per_review_cap_keeps_the_rest_in_the_database(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory", max_private_per_run="1")
    use_reviewers(
        monkeypatch,
        [finding(line=2), finding(line=3, title="second UNIQUE-TITLE-7731 one")],
    )
    gh = Pipeline(EVENT)
    advisory = respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    enqueue(EVENT)
    await drain()
    assert advisory.call_count == 1
    assert sorted(r.drop_reason or "" for r in rows(db_session)) == [
        PRIVATE_DELIVERED,
        PRIVATE_FAILED,
    ]
    assert "UNIQUE" not in public_text(gh)


@respx.mock
async def test_the_422_fallback_summary_cannot_leak_a_private_finding_either(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel(monkeypatch, "advisory")
    use_reviewers(
        monkeypatch,
        [
            finding(),
            finding(Severity.LOW, Category.CRYPTO, line=3, title="weak hash md5").model_copy(
                update={"rationale": "md5 is fine for checksums only"}
            ),
        ],
    )
    gh = Pipeline(EVENT)
    respx.post(ADVISORIES).mock(return_value=httpx.Response(201, json={}))
    gh.reviews.mock(side_effect=[httpx.Response(422, json={}), httpx.Response(200, json={})])
    enqueue(EVENT)
    await drain()
    assert gh.reviews.call_count == 2
    assert "weak hash md5" in gh.sent_review(1)["body"]  # the low finding is listed
    assert "UNIQUE" not in public_text(gh)


@respx.mock
async def test_a_public_hold_asks_for_confirmation_without_naming_the_severity(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_confirm_job import Saver

    monkeypatch.setattr(handlers, "open_checkpointer", Saver())
    monkeypatch.setenv("CONFIRMATION_ENABLED", "true")
    get_settings.cache_clear()
    # a CRITICAL code-quality finding is not a vulnerability detail: it is held, not diverted
    use_reviewers(
        monkeypatch, [], [finding(Severity.CRITICAL, Category.BUG, title="data loss bug")]
    )
    gh = Pipeline(EVENT)
    enqueue(EVENT)
    await drain()

    bodies = [json.loads(c.request.content)["body"] for c in gh.comments.calls]
    assert len(bodies) == 1 and "/confirm" in bodies[0]
    assert "critical" not in bodies[0].lower() and "data loss" not in bodies[0]
    assert gh.reviews.call_count == 0


@respx.mock
async def test_private_repos_behave_exactly_as_before(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_jobs import make_event

    channel(monkeypatch, "advisory")
    use_reviewers(monkeypatch, [finding()])
    event = make_event()  # the fixture repo is private
    gh = Pipeline(event)
    advisory = respx.post(
        f"https://api.github.com/repos/{event.owner}/{event.repo}/security-advisories"
    ).mock(return_value=httpx.Response(201, json={}))
    enqueue(event)
    await drain()

    assert advisory.call_count == 0
    comment = gh.sent_review()["comments"][0]["body"]
    assert TITLE in comment and "[HIGH]" in comment
    assert PUBLIC_CLOSING_LINE not in gh.sent_review()["body"]
    assert rows(db_session)[0].posted is True


@respx.mock
async def test_an_unknown_visibility_is_treated_as_public(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = copy.deepcopy(RAW)
    raw["repository"].pop("private", None)
    event = PullRequestEvent.model_validate(raw)
    use_reviewers(monkeypatch, [finding()])
    gh = Pipeline(event)
    enqueue(event)
    await drain()
    assert gh.sent_review()["comments"] == [] and TITLE not in public_text(gh)
