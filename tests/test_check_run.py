"""Check Run with annotations (ADR 0010): only what is already posted inline, always neutral."""

import json

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.review.render import MAX_ANNOTATIONS, PUBLIC_CLOSING_LINE, render_check_output
from app.review.schemas import Category, Finding, Severity
from tests.helpers import Pipeline
from tests.test_jobs import RAW, Drain, Enqueue, make_event, settings_env
from tests.test_private_delivery import EVENT, TITLE, finding, use_reviewers


def _f(line: int = 2, severity: Severity = Severity.MEDIUM, **kw: object) -> Finding:
    return Finding(
        file_path="a.py",
        line=line,
        severity=severity,
        category=Category.BUG,
        title=f"title-{line}",
        rationale=f"why-{line}",
        **kw,  # type: ignore[arg-type]
    )


def test_annotations_mirror_the_inline_findings() -> None:
    out = render_check_output([_f(3, start_line=2), _f(5, Severity.LOW)], public=False)
    first, second = out["annotations"]  # type: ignore[misc]
    assert (first["path"], first["start_line"], first["end_line"]) == ("a.py", 2, 3)
    assert (first["annotation_level"], second["annotation_level"]) == ("warning", "notice")
    assert first["title"] == "[MEDIUM] title-3" and first["message"] == "why-3"
    assert out["summary"] == "2 finding(s) posted inline."


def test_deleted_line_findings_and_the_50_cap() -> None:
    assert render_check_output([_f(2, side="LEFT")], public=False)["annotations"] == []
    many = [_f(i) for i in range(1, 80)]
    assert len(render_check_output(many, public=False)["annotations"]) == MAX_ANNOTATIONS  # type: ignore[arg-type]


def test_public_summary_is_the_neutral_line_whatever_the_findings() -> None:
    assert render_check_output([_f()], public=True)["summary"] == PUBLIC_CLOSING_LINE
    assert render_check_output([], public=True)["summary"] == PUBLIC_CLOSING_LINE


@respx.mock
async def test_off_by_default_no_check_run_is_created(
    db_session: Session, enqueue: Enqueue, drain: Drain
) -> None:
    event = make_event()
    Pipeline(event)
    route = respx.post(url__regex=r".*/check-runs").mock(return_value=httpx.Response(201))
    enqueue(event)
    await drain()
    assert route.call_count == 0


@respx.mock
async def test_enabled_creates_one_neutral_check_on_the_head_commit(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, CHECK_RUN_ENABLED="true")
    event = make_event()
    Pipeline(event)
    route = respx.post(url__regex=r".*/check-runs").mock(return_value=httpx.Response(201))
    enqueue(event)
    await drain()
    sent = json.loads(route.calls[0].request.content)
    assert route.call_count == 1
    assert (sent["head_sha"], sent["conclusion"], sent["status"]) == (
        event.head_sha,
        "neutral",
        "completed",
    )
    assert [a["path"] for a in sent["output"]["annotations"]] == ["app/db.py"]


@respx.mock
async def test_a_missing_permission_never_fails_the_review(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, CHECK_RUN_ENABLED="true")
    event = make_event()
    gh = Pipeline(event)
    respx.post(url__regex=r".*/check-runs").mock(return_value=httpx.Response(403))
    enqueue(event)
    await drain()
    assert gh.reviews.call_count == 1  # posted once, no retry storm
    from tests.test_jobs import run_of

    assert run_of(db_session).status == "ok"


def test_the_fixture_is_still_a_real_payload() -> None:
    assert RAW["pull_request"]["head"]["sha"]


@respx.mock
async def test_public_repo_check_never_carries_a_diverted_security_finding(
    db_session: Session, enqueue: Enqueue, drain: Drain, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_env(monkeypatch, CHECK_RUN_ENABLED="true")
    # one HIGH security finding (must stay private) and one MEDIUM quality-style finding
    use_reviewers(
        monkeypatch,
        [finding(), finding(Severity.MEDIUM, Category.OTHER, line=3, title="plain MEDIUM-note")],
    )
    Pipeline(EVENT)
    route = respx.post(url__regex=r".*/check-runs").mock(return_value=httpx.Response(201))
    enqueue(EVENT)
    await drain()
    body = route.calls[0].request.content.decode()
    # (the medium finding shares RATIONALE, so only the high finding's title can be checked)
    for secret in (TITLE, "UNIQUE-TITLE", "injection", "[HIGH]"):
        assert secret not in body, secret
    assert "MEDIUM-note" in body  # what is posted inline may be annotated
