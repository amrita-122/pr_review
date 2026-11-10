from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.review.cost import Prices, PriceTable
from app.review.schemas import Severity


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
    max_webhook_body_bytes: int = 2_000_000  # larger bodies are refused before the HMAC
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
    # Cost routing (Phase 7). The cheap model labels files high/low risk and reviews the low ones.
    model_triage: str = "claude-haiku-5-5"
    triage_enabled: bool = False
    triage_prompt_version: str = "triage_v1"
    # Claude Haiku 5.5 list prices (prompts up to 100K tokens). Estimates only.
    price_triage_input_per_mtok: Decimal = Decimal("0.10")
    price_triage_output_per_mtok: Decimal = Decimal("0.50")
    prompt_version: str = "security_v5"
    quality_prompt_version: str = "quality_v2"
    # Which review agents run, comma separated: security, quality.
    enabled_agents: str = "security,quality"
    review_drafts: bool = False  # draft PRs are skipped until they are marked ready for review
    # Human confirmation (Phase 8): a review with a held finding waits for a maintainer /confirm.
    confirmation_enabled: bool = False
    confirm_ttl_hours: float = 72.0
    confirm_severities: str = "critical"  # comma separated Severity values
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "pr-review-agent"

    @property
    def price_table(self) -> PriceTable:
        """Per-model prices: usage is reported per model, so each is priced at its own rate."""
        return PriceTable(
            default=Prices(self.price_input_per_mtok, self.price_output_per_mtok),
            by_model={
                self.model_triage: Prices(
                    self.price_triage_input_per_mtok, self.price_triage_output_per_mtok
                ),
                self.model_review: Prices(self.price_input_per_mtok, self.price_output_per_mtok),
            },
        )

    @property
    def agent_names(self) -> list[str]:
        names = [n.strip().lower() for n in self.enabled_agents.split(",") if n.strip()]
        unknown = set(names) - {"security", "quality"}
        if unknown or not names:
            raise ValueError(f"ENABLED_AGENTS must list security and/or quality, got {names}")
        return list(dict.fromkeys(names))

    @property
    def held_severities(self) -> frozenset[Severity]:
        names = [n.strip().lower() for n in self.confirm_severities.split(",") if n.strip()]
        try:
            held = frozenset(Severity(n) for n in names)
        except ValueError:
            raise ValueError(f"CONFIRM_SEVERITIES has an unknown severity: {names}") from None
        if not held:
            raise ValueError("CONFIRM_SEVERITIES must not be empty")
        return held

    @property
    def psycopg_url(self) -> str:
        """Plain libpq URL for the LangGraph checkpointer (it uses psycopg, not SQLAlchemy)."""
        return self.sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)

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
