"""Static checks on the held-out eval set (evals/holdout/). No model, no network.

These tests exist to keep the set unbiased: labels must be reachable, clean cases must stay clean,
cases must not carry hints, and architecture cases must only use context that production fetches.
"""

import base64
import re
from pathlib import Path

import pytest

from app.diff.parse import parse_patch
from app.review.architecture import ArchitectureFinding
from app.review.context import candidate_paths
from app.review.schemas import Category, Finding, Severity
from evals.architecture_cases import CASES
from evals.architecture_cases_holdout import HOLDOUT_CASES
from evals.architecture_run import CaseResult, matches, parse_case, score
from evals.dataset import load_cases, load_pr_cases
from evals.lint_dataset import lint
from evals.redteam import _model_text, load_redteam_cases

HOLDOUT = Path(__file__).parent.parent / "evals" / "holdout"
DATA = HOLDOUT / "data"

# Words that tell a model what to find. A case may not contain them in its added or context lines
# (red-team injections are the exception: they are attacks, not hints, and live in redteam/).
_HINTS = re.compile(
    r"vulnerab|insecure|exploit|\bbug\b|\bleak|FIXME|\bTODO\b|\bhack\b|not safe|unsafe|"
    r"\bBAD\b|\bwrong\b|should be|intentionally|deliberately",
    re.IGNORECASE,
)


def test_holdout_lints_clean_and_has_the_planned_size() -> None:
    problems, stats = lint(DATA)
    assert problems == []
    assert stats["single_cases"] >= 28 and stats["pr_cases"] >= 6
    assert stats["single_clean"] >= 11  # was 12; hn_literal_eval relabelled (RELABEL_LOG.md)


def test_every_holdout_case_is_python_that_compiles_or_not_python() -> None:
    """A syntax error would be reported by the quality agent as a bug nobody labelled."""
    cases = load_cases(DATA)
    prs = load_pr_cases(DATA)
    files = {c.path: c.parsed for c in cases} | {
        f"{p.case_id}/{path}": parsed for p in prs for path, parsed in p.files.items()
    }
    checked = 0
    for name, parsed in files.items():
        if not name.endswith(".py"):
            continue
        if not parsed.numbered.startswith("L1 ") or "..." in parsed.numbered.splitlines():
            continue  # a patch that does not start at line 1 is not a whole file
        source = "\n".join(
            re.sub(r"^L\d+ [ +] ", "", line, count=1)
            for line in parsed.numbered.splitlines()
            if re.match(r"^L\d+ [ +] ", line)
        )
        compile(source, name, "exec")
        checked += 1
    assert checked >= 30


def test_holdout_cases_carry_no_hints() -> None:
    offenders: list[str] = []
    cases = load_cases(DATA)
    prs = load_pr_cases(DATA)
    texts = {c.case_id: c.parsed.numbered for c in cases}
    texts |= {f"{p.case_id}/{path}": f.numbered for p in prs for path, f in p.files.items()}
    for name, numbered in texts.items():
        for line in numbered.splitlines():
            if re.match(r"^L\d+ [ +] ", line) and _HINTS.search(line):
                offenders.append(f"{name}: {line.strip()}")
    assert offenders == []


def test_no_label_marker_leaked_into_a_diff() -> None:
    for path in HOLDOUT.rglob("*.diff"):
        assert "«" not in path.read_text(encoding="utf-8"), path


def test_holdout_redteam_pairs_share_the_planted_line() -> None:
    cases = load_redteam_cases(HOLDOUT / "redteam")
    assert len(cases) >= 10
    channels = [c.row.channel for c in cases]
    assert len(set(channels)) == len(channels)  # ten different attack styles, not one repeated
    for case in cases:
        assert case.injected.expected[0].line_start == case.control.expected[0].line_start
        assert (
            case.injected.parsed.numbered != case.control.parsed.numbered
            or case.injected.path != case.control.path
        )


def test_replacement_code_is_part_of_the_text_the_model_wrote() -> None:
    finding = Finding(
        file_path="a.py",
        line=1,
        severity=Severity.LOW,
        category=Category.INJECTION,
        title="t",
        rationale="r",
        replacement_code='os.system("echo PWNED-7731")',
    )
    assert "PWNED-7731" in _model_text(finding)


def test_holdout_ids_are_unique_and_do_not_clash_with_the_original_set() -> None:
    ids = [c.case_id for c in HOLDOUT_CASES]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {c.case_id for c in CASES}


def test_holdout_architecture_set_has_the_planned_size() -> None:
    kinds = [c.kind for c in HOLDOUT_CASES]
    assert kinds.count("flaw") >= 6 and kinds.count("clean") >= 6 and kinds.count("redteam") >= 3


@pytest.mark.parametrize("case", HOLDOUT_CASES, ids=lambda c: c.case_id)
def test_architecture_context_is_only_files_production_would_fetch(case) -> None:  # noqa: ANN001
    """The original set hands the model files that candidate_paths can never select. Here every
    related file must be one the real selector would pick for this diff."""
    changed, context = parse_case(case)
    selectable = set(candidate_paths(changed))
    unreachable = [c.path for c in context if c.path not in selectable]
    assert unreachable == [], f"{case.case_id}: production would never fetch {unreachable}"


@pytest.mark.parametrize("case", HOLDOUT_CASES, ids=lambda c: c.case_id)
def test_architecture_case_is_well_formed(case) -> None:  # noqa: ANN001
    if case.kind == "clean":
        assert not case.keywords and not case.canaries and not case.flaw_files
    else:
        assert case.keywords and case.flaw_files, case.case_id
        assert set(case.flaw_files) & (set(case.changed) | set(case.context)), case.case_id
    for path, patch in case.changed.items():
        assert parse_patch(path, patch).commentable_lines
    for canary in case.canaries:
        plain = any(canary in text for text in case.context.values())
        encoded = any(
            canary.encode() in base64.b64decode(token)
            for text in case.context.values()
            for token in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", text)
            if _is_b64(token)
        )
        assert plain or encoded, case.case_id
        assert not any(canary in patch for patch in case.changed.values()), case.case_id


def _is_b64(token: str) -> bool:
    try:
        base64.b64decode(token, validate=True)
    except ValueError:
        return False
    return True


def _finding(files: list[str], text: str = "title") -> ArchitectureFinding:
    return ArchitectureFinding(severity=Severity.HIGH, title=text, rationale="r", files=files)


def test_flaw_files_stops_a_keyword_hit_on_the_wrong_file_from_counting() -> None:
    keywords = ("require_admin",)
    on_target = _finding(["app/routes/exports.py"], "skips require_admin")
    off_target = _finding(["app/auth.py"], "skips require_admin")
    assert matches(on_target, keywords, ("app/routes/exports.py",))
    assert not matches(off_target, keywords, ("app/routes/exports.py",))
    assert matches(off_target, keywords)  # old behaviour is unchanged when flaw_files is empty


def test_suppression_only_redteam_cases_do_not_pad_the_hijack_denominator() -> None:
    def result(case_id: str, has_canaries: bool) -> CaseResult:
        return CaseResult(case_id, "redteam", False, True, 1, False, False, has_canaries)

    report = score([result("a", True), result("b", False), result("rt_arch_suppress", False)])
    assert report["hijack_model"]["of"] == 1 and report["hijack_posted"]["of"] == 1
