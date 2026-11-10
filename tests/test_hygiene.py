"""10.13: padding order, config validated at startup, retention, de-duplicated skip list."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, WebhookDelivery
from app.db.retention import prune_old_rows
from app.diff.parse import parse_patch
from app.review.graph import ReviewLimits, review_diff
from app.review.schemas import Category, Finding, Severity

BASE = {
    "github_app_id": "1",
    "github_webhook_secret": "s",
    "github_private_key_base64": "x",
    "database_url": "sqlite://",
    "anthropic_api_key": "x",
    "model_review": "m",
}


def settings(**changes: object) -> Settings:
    return Settings(**{**BASE, **changes})  # type: ignore[arg-type]


# --- config is validated when the app starts ---------------------------------------------------


def test_defaults_are_valid() -> None:
    assert settings().agent_names == ["security", "quality"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled_agents", "security,style", "ENABLED_AGENTS"),
        ("enabled_agents", " , ", "ENABLED_AGENTS"),
        ("confirm_severities", "critical,catastrophic", "CONFIRM_SEVERITIES"),
        ("confirm_severities", "", "CONFIRM_SEVERITIES"),
        ("prompt_version", "security_v999", "PROMPT_VERSION"),
        ("prompt_version", "../../etc/passwd", "PROMPT_VERSION"),
        ("quality_prompt_version", "quality_v999", "QUALITY_PROMPT_VERSION"),
        ("confirm_ttl_hours", 0, "CONFIRM_TTL_HOURS"),
    ],
)
def test_a_bad_setting_stops_the_app_at_startup(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        settings(**{field: value})


def test_a_missing_quality_prompt_is_fine_when_the_quality_agent_is_off() -> None:
    assert settings(enabled_agents="security", quality_prompt_version="nope_v1")


# --- padding: risky files are reviewed first, before the caps cut --------------------------

PATCH = "@@ -0,0 +1,3 @@\n+import os\n+x = 1\n+y = 2"
RISKY = "@@ -0,0 +1,3 @@\n+import os\n+password = 'hunter2'\n+y = 2"


async def test_padding_cannot_push_the_risky_file_out_of_the_review() -> None:
    seen: list[str] = []

    async def reviewer(path: str, numbered: str) -> list[Finding]:
        seen.append(path)
        return []

    # 5 harmless files come FIRST in API order, the risky one last; only 3 files fit.
    files = {f"util/pad{i}.py": parse_patch(f"util/pad{i}.py", PATCH) for i in range(5)}
    files["app/payments.py"] = parse_patch("app/payments.py", RISKY)
    outcome = await review_diff(files, reviewer, ReviewLimits(max_files=3, max_chars_per_file=9999))
    assert "app/payments.py" in seen
    assert "app/payments.py" in outcome.reviewed
    assert len(outcome.skipped) == 3 and all("file limit" in why for _, why in outcome.skipped)


async def test_low_risk_files_are_the_first_to_be_cut() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return []

    files = {
        "docs/guide.md": parse_patch("docs/guide.md", PATCH),
        "lib/thing.py": parse_patch("lib/thing.py", PATCH),
        "app/auth/login.py": parse_patch("app/auth/login.py", PATCH),
    }
    outcome = await review_diff(files, reviewer, ReviewLimits(max_files=2, max_chars_per_file=9999))
    assert set(outcome.reviewed) == {"app/auth/login.py", "lib/thing.py"}
    assert [p for p, _ in outcome.skipped] == ["docs/guide.md"]


async def test_the_skipped_list_has_no_duplicates() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [
            Finding(
                file_path=path,
                line=2,
                severity=Severity.LOW,
                category=Category.OTHER,
                title="t",
                rationale="r",
            )
        ]

    files = {"a.py": parse_patch("a.py", PATCH)}
    pre = [("uv.lock", "lockfile"), ("uv.lock", "lockfile"), ("logo.png", "binary file")]
    outcome = await review_diff(
        files, reviewer, ReviewLimits(max_files=5, max_chars_per_file=9999), pre_skipped=pre
    )
    assert outcome.skipped == [("uv.lock", "lockfile"), ("logo.png", "binary file")]


# --- retention ---------------------------------------------------------------------------------


def test_old_deliveries_and_finished_jobs_are_pruned_but_live_work_is_not(
    db_session: Session,
) -> None:
    now = datetime.now(UTC)
    old, recent = now - timedelta(days=40), now - timedelta(days=1)
    db_session.add_all(
        [
            WebhookDelivery(
                delivery_id="old", event="pull_request", action="opened", received_at=old
            ),
            WebhookDelivery(
                delivery_id="new", event="pull_request", action="opened", received_at=recent
            ),
            Job(kind="review_pr", payload={}, status="done", created_at=old),
            Job(kind="review_pr", payload={}, status="failed", created_at=old),
            Job(kind="review_pr", payload={}, status="done", created_at=recent),
            Job(kind="review_pr", payload={}, status="queued", created_at=old),
            Job(kind="review_pr", payload={}, status="running", created_at=old),
        ]
    )
    db_session.commit()

    assert prune_old_rows(db_session, now, settings()) == (1, 2)
    assert [d.delivery_id for d in db_session.scalars(select(WebhookDelivery))] == ["new"]
    assert sorted(j.status for j in db_session.scalars(select(Job))) == [
        "done",
        "queued",
        "running",
    ]
    assert prune_old_rows(db_session, now, settings()) == (0, 0)


# --- 10.22: architecture agent settings --------------------------------------------------------


def test_the_architecture_agent_is_off_by_default_and_validated_when_on() -> None:
    assert settings().architecture_agent_enabled is False
    # while it is off, its prompt file need not exist yet
    assert settings(architecture_prompt_version="nope_v9")
    with pytest.raises(ValueError, match="ARCHITECTURE_MAX"):
        settings(architecture_max_files=0)
