from pathlib import Path

from evals.lint_dataset import lint

DIFF = "diff --git a/{p} b/{p}\n--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"


def _build(tmp: Path, expected: str, pr_files: int = 2) -> Path:
    (tmp / "cases").mkdir()
    (tmp / "cases" / "one.diff").write_text(DIFF.format(p="app/x.py"))
    pr = tmp / "prs" / "pr_a"
    pr.mkdir(parents=True)
    for i in range(pr_files):
        (pr / f"f{i}.diff").write_text(DIFF.format(p=f"app/f{i}.py"))
    (tmp / "expected.jsonl").write_text(expected)
    return tmp


def test_valid_labels_pass(tmp_path: Path) -> None:
    row = '{"case_id":"one","file":"app/x.py","line_start":1,"line_end":2,"category":"bug"}\n'
    problems, stats = lint(_build(tmp_path, row))
    assert problems == []
    assert stats["single_cases"] == 1 and stats["pr_cases"] == 1


def test_label_outside_diff_is_reported(tmp_path: Path) -> None:
    row = '{"case_id":"one","file":"app/x.py","line_start":9,"line_end":9,"category":"bug"}\n'
    problems, _ = lint(_build(tmp_path, row))
    assert any("not in the diff" in p for p in problems)


def test_pr_label_for_missing_file_is_reported(tmp_path: Path) -> None:
    import pytest

    row = '{"case_id":"pr_a","file":"nope.py","line_start":1,"line_end":1,"category":"bug"}\n'
    with pytest.raises(ValueError, match="unknown file"):
        lint(_build(tmp_path, row))


def test_single_file_pr_is_reported(tmp_path: Path) -> None:
    problems, _ = lint(_build(tmp_path, "", pr_files=1))
    assert any("at least 2 files" in p for p in problems)
