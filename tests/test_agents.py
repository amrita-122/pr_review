"""Two agents (security, quality) on the same files, in one graph."""

import pytest

from app.config import Settings, get_settings
from app.diff.parse import parse_patch
from app.review.graph import ReviewLimits, review_diff
from app.review.llm import build_reviewers, load_prompt
from app.review.schemas import AGENT_CATEGORIES, Agent, Category, Finding, ReviewFinding, Severity

LIMITS = ReviewLimits(max_files=10, max_chars_per_file=1000)
PATCH = "@@ -0,0 +1,3 @@\n+import os\n+x = 1\n+y = 2"


def _finding(category: Category, line: int = 2, title: str = "t") -> Finding:
    return Finding(
        file_path="a.py",
        line=line,
        severity=Severity.HIGH,
        category=category,
        title=title,
        rationale="r",
    )


def _files() -> dict[str, object]:
    return {"a.py": parse_patch("a.py", PATCH)}


async def test_every_file_goes_to_every_agent_and_findings_are_tagged() -> None:
    calls: list[str] = []

    def reviewer(agent: str, category: Category):  # type: ignore[no-untyped-def]
        async def run(path: str, numbered: str) -> list[Finding]:
            calls.append(agent)
            return [_finding(category)]

        return run

    outcome = await review_diff(
        _files(),  # type: ignore[arg-type]
        {
            "security": reviewer("security", Category.INJECTION),
            "quality": reviewer("quality", Category.BUG),
        },
        LIMITS,
    )
    assert sorted(calls) == ["quality", "security"]
    tagged = {(f.agent, f.category) for f in outcome.findings if isinstance(f, ReviewFinding)}
    assert tagged == {(Agent.SECURITY, Category.INJECTION), (Agent.QUALITY, Category.BUG)}


async def test_the_model_cannot_choose_its_own_agent() -> None:
    # Finding (what the model fills in) has no agent field at all; the graph node sets it.
    assert "agent" not in Finding.model_fields

    async def quality(path: str, numbered: str) -> list[Finding]:
        return [_finding(Category.BUG)]

    outcome = await review_diff(_files(), {"quality": quality}, LIMITS)  # type: ignore[arg-type]
    assert [getattr(f, "agent", None) for f in outcome.findings] == [Agent.QUALITY]


@pytest.mark.parametrize(
    ("agent", "category"),
    [
        ("security", Category.BUG),
        ("security", Category.RESOURCE_LEAK),
        ("quality", Category.INJECTION),
        ("quality", Category.OTHER),
        ("quality", Category.SECRETS),
    ],
)
async def test_a_category_outside_the_agents_territory_is_dropped(
    agent: str, category: Category
) -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [_finding(category)]

    outcome = await review_diff(_files(), {agent: reviewer}, LIMITS)  # type: ignore[arg-type]
    assert outcome.findings == []
    assert [reason for _, reason in outcome.dropped] == ["category not allowed for agent"]


async def test_one_agent_failing_keeps_the_other_agents_findings() -> None:
    async def security(path: str, numbered: str) -> list[Finding]:
        return [_finding(Category.INJECTION)]

    async def quality(path: str, numbered: str) -> list[Finding]:
        raise RuntimeError("model down")

    outcome = await review_diff(
        _files(),  # type: ignore[arg-type]
        {"security": security, "quality": quality},
        LIMITS,
    )
    assert [f.category for f in outcome.findings] == [Category.INJECTION]
    assert outcome.skipped == [("a.py", "quality review failed")]
    assert outcome.reviewed == {}  # not fully reviewed: evals count this as an error


async def test_a_single_plain_reviewer_still_means_the_security_agent() -> None:
    async def reviewer(path: str, numbered: str) -> list[Finding]:
        return [_finding(Category.INJECTION)]

    outcome = await review_diff(_files(), reviewer, LIMITS)  # type: ignore[arg-type]
    assert [getattr(f, "agent", None) for f in outcome.findings] == [Agent.SECURITY]


def test_agent_territories_do_not_overlap_and_cover_every_category() -> None:
    security, quality = AGENT_CATEGORIES[Agent.SECURITY], AGENT_CATEGORIES[Agent.QUALITY]
    assert security.isdisjoint(quality)
    assert security | quality == set(Category)


def test_enabled_agents_setting() -> None:
    base = {
        "github_app_id": "1",
        "github_webhook_secret": "x",
        "github_private_key_base64": "x",
        "database_url": "sqlite://",
        "anthropic_api_key": "x",
        "model_review": "m",
    }
    assert Settings(**base).agent_names == ["security", "quality"]  # type: ignore[arg-type]
    assert Settings(**base, enabled_agents=" Quality ,quality").agent_names == ["quality"]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ENABLED_AGENTS"):
        _ = Settings(**base, enabled_agents="security,style").agent_names  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ENABLED_AGENTS"):
        _ = Settings(**base, enabled_agents=" , ").agent_names  # type: ignore[arg-type]


def test_build_reviewers_returns_one_reviewer_per_enabled_agent() -> None:
    settings = get_settings()
    assert set(build_reviewers(settings)) == {"security", "quality"}
    assert set(build_reviewers(settings, agents=["quality"])) == {"quality"}


def test_quality_prompt_carries_the_same_defences_as_the_security_prompt() -> None:
    quality = load_prompt("quality_v1")
    assert "random token" in quality and "untrusted" in quality.lower()
    assert "No links or hostnames" in quality
    for category in AGENT_CATEGORIES[Agent.QUALITY]:
        assert category.value in quality
    for category in AGENT_CATEGORIES[Agent.SECURITY] - {Category.OTHER}:
        assert f"`{category.value}`" not in quality  # it must not offer security categories
