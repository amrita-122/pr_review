import pytest

from app.review.sanitize import count_invisible, strip_invisible


@pytest.mark.parametrize(
    "char",
    [
        "​",  # zero-width space
        "‍",  # zero-width joiner
        "‮",  # right-to-left override
        "⁦",  # left-to-right isolate
        "⁠",  # word joiner
        "﻿",  # BOM / zero-width no-break space
        "­",  # soft hyphen
        chr(0xE0041),  # tag character 'A'
    ],
)
def test_invisible_characters_are_removed(char: str) -> None:
    assert strip_invisible(f"a{char}b") == "ab"
    assert count_invisible(f"a{char}b") == 1


def test_visible_text_newlines_tabs_and_unicode_letters_survive() -> None:
    text = "L1 + naïve = 'é'\n\tL2   日本語 \U0001f600"
    assert strip_invisible(text) == text
    assert count_invisible(text) == 0


def test_line_count_is_unchanged_so_line_numbers_hold() -> None:
    text = "L1 + a​\nL2 + ‮b\nL3 + c"
    assert strip_invisible(text).count("\n") == text.count("\n")
