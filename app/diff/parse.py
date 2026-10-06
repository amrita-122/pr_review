from dataclasses import dataclass

from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet


class DiffParseError(ValueError):
    """The patch text could not be parsed. Callers skip that one file."""


@dataclass(frozen=True)
class ParsedFile:
    path: str
    commentable_lines: frozenset[int]  # new-file line numbers: added + context lines
    numbered: str  # what the reviewer reads: "L42 + code" / "L43   code"


def parse_patch(path: str, patch: str) -> ParsedFile:
    """Parse one file's `patch` text from the GitHub files API.

    GitHub's patch starts at the first `@@` line, but unidiff wants file headers, so add them.
    """
    if "\n" in path or "\r" in path:
        # The path is attacker-controlled; a newline would let it forge extra diff headers.
        raise DiffParseError("file path contains a newline")
    try:
        patch_set = PatchSet(f"--- a/{path}\n+++ b/{path}\n{patch}")
    except UnidiffParseError as exc:
        raise DiffParseError(str(exc)) from exc

    commentable: set[int] = set()
    rendered: list[str] = []
    for patched_file in patch_set:
        for index, hunk in enumerate(patched_file):
            if index > 0:
                rendered.append("...")  # unchanged lines between hunks are not shown
            for line in hunk:
                text = line.value.rstrip("\r\n")
                if line.is_added and line.target_line_no is not None:
                    commentable.add(line.target_line_no)
                    rendered.append(f"L{line.target_line_no} + {text}")
                elif line.is_context and line.target_line_no is not None:
                    commentable.add(line.target_line_no)
                    rendered.append(f"L{line.target_line_no}   {text}")
                elif line.is_removed:
                    rendered.append(f"     - {text}")  # no new-file number: not commentable
                # "\ No newline at end of file" markers are dropped

    return ParsedFile(path, frozenset(commentable), "\n".join(rendered))
