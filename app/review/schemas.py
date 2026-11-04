from enum import StrEnum

from pydantic import BaseModel, Field


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
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1)
    suggestion: str | None = None


class ReviewFinding(Finding):
    """A finding plus which agent produced it.

    `agent` is set by OUR graph node, never by the model: Finding (the schema the model fills in)
    has no such field, so the model cannot claim to be the other agent.
    """

    agent: Agent


class ReviewResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
