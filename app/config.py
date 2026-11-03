from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    github_app_id: str
    github_webhook_secret: SecretStr
    github_private_key_base64: SecretStr
    smee_url: str | None = None
    database_url: str
    anthropic_api_key: SecretStr
    model_review: str
    max_files_per_pr: int = 30
    max_chars_per_file: int = 20000
    # Cost-exhaustion caps: the PR author controls the size of everything we send to the model.
    max_total_chars_per_pr: int = 150_000
    max_output_tokens: int = 2048  # per model call; bounds output spend and runaway generations
    max_findings_per_file: int = 10
    max_comments_per_review: int = 25
    max_listed_pages: int = 3  # pages of 100 files fetched from GitHub; the rest are not listed
    # Job queue (Phase 6). The job deadline must stay below the lease or a live job is reaped.
    job_max_attempts: int = 3
    job_poll_seconds: float = 2.0
    job_lease_seconds: float = 600.0
    job_deadline_seconds: float = 480.0
    job_backoff_base_seconds: float = 30.0
    shutdown_grace_seconds: float = 30.0
    db_connect_timeout_seconds: int = 5
    db_statement_timeout_ms: int = 15_000
    model_timeout_seconds: float = 60.0
    # USD per million tokens; defaults are Claude Sonnet 5.5 list prices. Estimates only.
    price_input_per_mtok: Decimal = Decimal("2")
    price_output_per_mtok: Decimal = Decimal("10")
    prompt_version: str = "security_v5"
    quality_prompt_version: str = "quality_v1"
    # Which review agents run, comma separated: security, quality.
    enabled_agents: str = "security,quality"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "pr-review-agent"

    @property
    def agent_names(self) -> list[str]:
        names = [n.strip().lower() for n in self.enabled_agents.split(",") if n.strip()]
        unknown = set(names) - {"security", "quality"}
        if unknown or not names:
            raise ValueError(f"ENABLED_AGENTS must list security and/or quality, got {names}")
        return list(dict.fromkeys(names))

    @property
    def sqlalchemy_url(self) -> str:
        """Force the psycopg 3 driver; Railway/.env URLs are often plain postgres(ql)://."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
