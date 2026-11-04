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


class PRCase(BaseModel):
    """A whole pull request: several changed files reviewed together (cost per PR, triage)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    case_id: str
    files: dict[str, ParsedFile]
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
    pr_ids = {p.name for p in (data_dir / "prs").glob("*") if p.is_dir()}
    leftover = set(expected_by_case) - pr_ids
    if leftover:
        raise ValueError(f"expected.jsonl references unknown cases: {sorted(leftover)}")
    return cases


def load_pr_cases(data_dir: Path = DATA_DIR) -> list[PRCase]:
    """Multi-file cases: evals/data/prs/<case_id>/*.diff (one diff per changed file).

    Labels live in the same expected.jsonl, with case_id = the directory name and `file` = the
    path inside that PR. Single-file cases in cases/ are not touched.
    """
    expected_by_case: dict[str, list[Expected]] = {}
    for line in (data_dir / "expected.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = Expected.model_validate_json(line)
            expected_by_case.setdefault(row.case_id, []).append(row)
    single = {p.stem for p in (data_dir / "cases").glob("*.diff")}

    cases: list[PRCase] = []
    prs_dir = data_dir / "prs"
    for pr_dir in sorted(p for p in prs_dir.glob("*") if p.is_dir()) if prs_dir.exists() else []:
        files: dict[str, ParsedFile] = {}
        for diff_file in sorted(pr_dir.glob("*.diff")):
            path, patch = _split_diff(diff_file.read_text(encoding="utf-8"))
            if path in files:
                raise ValueError(f"{pr_dir.name}: two diffs for {path}")
            files[path] = parse_patch(path, patch)
        if not files:
            raise ValueError(f"{pr_dir.name}: no .diff files")
        expected = expected_by_case.get(pr_dir.name, [])
        for exp in expected:
            if exp.file not in files:
                raise ValueError(f"{pr_dir.name}: expected bug in unknown file {exp.file}")
        cases.append(PRCase(case_id=pr_dir.name, files=files, expected=expected))
    names = {c.case_id for c in cases}
    unknown = set(expected_by_case) - single - names
    if unknown:
        raise ValueError(f"expected.jsonl references unknown cases: {sorted(unknown)}")
    return cases
