from enum import StrEnum

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = structlog.get_logger()

MAX_TITLE_CHARS = 200


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(StrEnum):
    # security agent
    INJECTION = "injection"
    SECRETS = "secrets"
    AUTHZ = "authz"
    CRYPTO = "crypto"
    UNSAFE_EXEC = "unsafe_exec"
    OTHER = "other"
    # quality agent
    BUG = "bug"
    ERROR_HANDLING = "error_handling"
    RESOURCE_LEAK = "resource_leak"


class Agent(StrEnum):
    SECURITY = "security"
    QUALITY = "quality"


# Which categories each agent may report. The validator drops anything else, so an agent that is
# talked into another agent's territory (or invents a category) cannot get it posted.
AGENT_CATEGORIES: dict[Agent, frozenset[Category]] = {
    Agent.SECURITY: frozenset(
        {
            Category.INJECTION,
            Category.SECRETS,
            Category.AUTHZ,
            Category.CRYPTO,
            Category.UNSAFE_EXEC,
            Category.OTHER,
        }
    ),
    Agent.QUALITY: frozenset({Category.BUG, Category.ERROR_HANDLING, Category.RESOURCE_LEAK}),
}


def agent_for_category(category: Category) -> Agent:
    return Agent.QUALITY if category in AGENT_CATEGORIES[Agent.QUALITY] else Agent.SECURITY


class Finding(BaseModel):
    """One issue on one line of the NEW version of a file. Model output is untrusted."""

    file_path: str
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    rationale: str = Field(min_length=1)
    suggestion: str | None = None
    # Exact replacement for the commented line (10.27). Rendered as a ```suggestion block, which
    # GitHub turns into a one-click "Commit suggestion" button, so it is policy-checked.
    replacement_code: str | None = None


class ReviewFinding(Finding):
    """A finding plus which agent produced it.

    `agent` is set by OUR graph node, never by the model: Finding (the schema the model fills in)
    has no such field, so the model cannot claim to be the other agent.
    """

    agent: Agent


class ReviewResult(BaseModel):
    """What the model returns for one file.

    Findings are parsed ONE AT A TIME: a single malformed finding (bad severity, line 0, empty
    text) is skipped and logged, it does not throw away the file's other findings. A title that is
    merely too long is shortened (the output policy still checks the shortened text).
    """

    findings: list[Finding] = Field(default_factory=list)

    @field_validator("findings", mode="before")
    @classmethod
    def _parse_each(cls, items: object) -> object:
        if not isinstance(items, list):
            return items  # wrong shape altogether: a real validation error (the caller retries)
        parsed: list[Finding] = []
        skipped = 0
        for item in items:
            if isinstance(item, dict):
                title = item.get("title")
                if isinstance(title, str) and len(title) > MAX_TITLE_CHARS:
                    item = {**item, "title": title[: MAX_TITLE_CHARS - 3] + "..."}
            try:
                parsed.append(Finding.model_validate(item))
            except ValidationError:
                skipped += 1
        if skipped:
            logger.warning("malformed_findings_skipped", skipped=skipped, kept=len(parsed))
        return parsed
