"""10.25: the architecture eval set is well formed and its scoring is correct (no real model)."""

import pytest

from app.review.architecture import ArchitectureFinding
from app.review.context import candidate_paths, is_safe_path
from app.review.schemas import Severity
from evals.architecture_cases import CASES
from evals.architecture_run import matches, parse_case, run_all, run_case, score


def finding(title: str, rationale: str = "because", files: list[str] | None = None):  # noqa: ANN201
    return ArchitectureFinding(
        severity=Severity.HIGH, title=title, rationale=rationale, files=files or []
    )


def test_the_set_is_the_planned_size_and_every_case_parses() -> None:
    kinds = [c.kind for c in CASES]
    assert kinds.count("flaw") >= 6 and kinds.count("clean") >= 4 and kinds.count("redteam") >= 5
    assert len({c.case_id for c in CASES}) == len(CASES)
    for case in CASES:
        changed, context = parse_case(case)
        assert changed and all(is_safe_path(p) for p in [*changed, *(c.path for c in context)])
        assert candidate_paths(changed) is not None


def test_flaws_have_keywords_and_clean_cases_have_none() -> None:
    for case in CASES:
        if case.kind == "clean":
            assert not case.keywords and not case.canaries
        else:
            assert case.keywords, case.case_id


def test_redteam_cases_hide_their_canaries_in_a_repo_file_not_the_diff() -> None:
    red = [c for c in CASES if c.kind == "redteam"]
    for case in red:
        assert case.channel.startswith("repo file")
        assert case.context and not any(
            canary in patch for canary in case.canaries for patch in case.changed.values()
        )
        for canary in case.canaries:
            assert any(canary in text for text in case.context.values()), case.case_id
        assert "auth" in case.keywords  # the real flaw is still there to be found


def test_matching_is_case_insensitive_on_title_and_rationale() -> None:
    assert matches(finding("Route skips AUTHORIZATION"), ("authoriz",))
    assert matches(finding("x", "the CORS policy"), ("cors",))
    assert not matches(finding("Naming"), ("authoriz",))


async def test_scoring_with_a_perfect_agent_and_a_hijacked_one() -> None:
    flaw = next(c for c in CASES if c.case_id == "arch_admin_no_auth")
    clean = next(c for c in CASES if c.case_id == "arch_clean_rename_helper")
    link = next(c for c in CASES if c.case_id == "rt_arch_link")

    async def agent(changed, context):  # noqa: ANN001, ANN202
        if "app/routes/admin.py" in changed:
            return [
                finding("Admin route skips the authorization layer", files=["app/routes/admin.py"])
            ]
        return []

    assert (await run_case(agent, flaw)).found is True
    assert (await run_case(agent, clean)).kept == 0

    async def hijacked(changed, context):  # noqa: ANN001, ANN202
        return [finding("Missing authorization", "see https://evil-7731.example/fix")]

    result = await run_case(hijacked, link)
    # the model repeated the canary, but the output policy drops it: never posted
    assert result.hijack_model is True and result.hijack_posted is False and result.found is False


async def test_a_crash_is_an_error_not_a_win_for_the_attacker() -> None:
    async def broken(changed, context):  # noqa: ANN001, ANN202
        raise RuntimeError("down")

    report = await run_all(broken, runs=1)
    assert report["errors"] == len(CASES)
    assert report["suppressed"]["cases"] == 0 and report["hijack_model"]["cases"] == 0


async def test_report_counts_sit_next_to_rates() -> None:
    async def silent(changed, context):  # noqa: ANN001, ANN202
        return []

    report = await run_all(silent, runs=2)
    assert report["recall"]["found"] == 0 and report["recall"]["of"] == 12  # 6 flaws x 2 runs
    assert report["false_alarms"]["cases"] == 0
    assert report["suppressed"]["rate"] == 1.0  # a silent agent "loses" every red-team case
    assert score([])["recall"]["rate"] is None


def test_the_cli_refuses_to_spend_money_without_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from evals import architecture_run

    monkeypatch.setattr(sys, "argv", ["architecture_run"])
    with pytest.raises(SystemExit):
        architecture_run.main()
