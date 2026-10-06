from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(StrEnum):
    INJECTION = "injection"
    SECRETS = "secrets"
    AUTHZ = "authz"
    CRYPTO = "crypto"
    UNSAFE_EXEC = "unsafe_exec"
    OTHER = "other"


class Finding(BaseModel):
    """One issue on one line of the NEW version of a file. Model output is untrusted."""

    file_path: str
    line: int = Field(ge=1)
    severity: Severity
    category: Category
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1)
    suggestion: str | None = None


class ReviewResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
