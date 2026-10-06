import pytest

from app.diff.parse import DiffParseError, parse_patch

PATCH = (
    "@@ -1,3 +1,4 @@\n a\n-b\n+B\n+new\n c\n"
    "@@ -20,2 +21,2 @@\n x\n-y\n+z\n"
    "\\ No newline at end of file"
)


def test_commentable_lines_are_added_and_context() -> None:
    parsed = parse_patch("src/f.py", PATCH)
    assert parsed.commentable_lines == {1, 2, 3, 4, 21, 22}


def test_numbered_rendering() -> None:
    parsed = parse_patch("src/f.py", PATCH)
    assert parsed.numbered.splitlines() == [
        "L1   a",
        "     - b",
        "L2 + B",
        "L3 + new",
        "L4   c",
        "...",
        "L21   x",
        "     - y",
        "L22 + z",
    ]


def test_content_that_looks_like_diff_headers() -> None:
    patch = "@@ -1,2 +1,3 @@\n a\n--- b\n+++ c\n+new"
    assert parse_patch("f.py", patch).commentable_lines == {1, 2, 3}


def test_path_with_spaces() -> None:
    assert parse_patch("my dir/f.py", "@@ -0,0 +1 @@\n+x").commentable_lines == {1}


def test_malformed_patch_raises() -> None:
    with pytest.raises(DiffParseError):
        parse_patch("f.py", "@@ -1,5 +1,5 @@\n only one line")


def test_newline_in_path_rejected() -> None:
    with pytest.raises(DiffParseError):
        parse_patch("a.py\n+++ b/evil", "@@ -0,0 +1 @@\n+x")


def test_real_sandbox_diff_fixture() -> None:
    from pathlib import Path

    text = Path("tests/fixtures/diffs/pr1_divide_function.diff").read_text()
    patch = text[text.index("@@") :]
    parsed = parse_patch("main.py", patch)
    assert parsed.commentable_lines == set(range(4, 11))
    assert "L10 +     return a * b" in parsed.numbered
