"""Golden-set loader. Cases are trusted repo files, but they are still validated on load."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.diff.parse import ParsedFile, parse_patch
from app.review.schemas import Category

DATA_DIR = Path(__file__).parent / "data"


class Expected(BaseModel):
    case_id: str
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    category: Category

    @model_validator(mode="after")
    def _ordered(self) -> "Expected":
        if self.line_end < self.line_start:
            raise ValueError("line_end before line_start")
        return self


class Case(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    case_id: str
    path: str
    parsed: ParsedFile
    expected: list[Expected]

    @property
    def is_clean(self) -> bool:
        return not self.expected


def _split_diff(text: str) -> tuple[str, str]:
    """Return (new file path, patch text starting at the first @@), like GitHub's files API."""
    lines = text.splitlines()
    path = next(line[6:] for line in lines if line.startswith("+++ b/"))
    first_hunk = next(i for i, line in enumerate(lines) if line.startswith("@@"))
    return path, "\n".join(lines[first_hunk:])


def load_cases(data_dir: Path = DATA_DIR) -> list[Case]:
    expected_by_case: dict[str, list[Expected]] = {}
    expected_file = data_dir / "expected.jsonl"
    for line in expected_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = Expected.model_validate_json(line)
            expected_by_case.setdefault(row.case_id, []).append(row)

    cases: list[Case] = []
    for diff_file in sorted((data_dir / "cases").glob("*.diff")):
        case_id = diff_file.stem
        path, patch = _split_diff(diff_file.read_text(encoding="utf-8"))
        cases.append(
            Case(
                case_id=case_id,
                path=path,
                parsed=parse_patch(path, patch),
                expected=expected_by_case.pop(case_id, []),
            )
        )
    if expected_by_case:
        raise ValueError(f"expected.jsonl references unknown cases: {sorted(expected_by_case)}")
    return cases
