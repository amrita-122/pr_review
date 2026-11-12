import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.review.cost import Prices, PriceTable
from app.review.schemas import Severity

_PROMPT_DIR = Path(__file__).parent / "review" / "prompts"
_PROMPT_NAME = re.compile(r"[a-z]+_v\d+")  # also keeps "../x" out of a file path


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
    retention_deliveries_days: int = 30  # webhook_deliveries older than this are deleted
    retention_jobs_days: int = 14  # finished (done/failed) jobs older than this are deleted
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
    # /dismiss in a finding's thread (needs the App subscribed to "Pull request review comment").
    feedback_enabled: bool = True
    # Public repos (ADR 0008): where high/critical security findings go instead of the PR.
    # "none" = nowhere but the database (fail closed); "advisory" = a draft security advisory
    # (needs the App permission "Repository security advisories: write").
    private_channel: Literal["none", "advisory"] = "none"
    max_private_per_run: int = 5  # advisories created per review; the rest stay in the database
    # Architecture agent (ADR 0009): one extra call per PR that also sees related repo files.
    architecture_agent_enabled: bool = False
    architecture_prompt_version: str = "architecture_v1"
    architecture_max_files: int = 8  # related files fetched through the Contents API
    architecture_max_chars_per_file: int = 8_000
    architecture_max_total_chars: int = 40_000  # all related files together
    architecture_max_findings: int = 5
    # Deterministic secret scan of added lines before validation (10.32). Off until its false-alarm
    # rate is measured on real diffs.
    secret_scan_enabled: bool = False
    # Spend and abuse limits (10.34). 0 = no limit. The per-PR cost is bounded by the size caps
    # above (max_total_chars_per_pr, max_files_per_pr), so there is no separate per-PR budget.
    max_cost_per_day_usd: float = Field(default=0, ge=0)  # all runs started since 00:00 UTC
    max_runs_per_author_per_hour: int = Field(default=0, ge=0)  # reviews queued per PR author
    # A Slack-style incoming webhook told when a review job fails for good. A secret.
    alert_webhook_url: SecretStr | None = None
    review_command_enabled: bool = True  # /review re-runs a failed, skipped or rate-limited review
    # Review only the files changed since the PR's last reviewed commit (10.31). Off by default:
    # a file that failed review earlier is not retried by a later incremental run.
    incremental_review_enabled: bool = False
    # A neutral Check Run with annotations after a posted review (ADR 0010). Needs the App
    # permission `checks: write`; without it the call fails, is logged and nothing else changes.
    check_run_enabled: bool = False
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
        """Force the psycopg 3 driver; hosted-database/.env URLs are often plain postgres(ql)://."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @model_validator(mode="after")
    def _validate_at_startup(self) -> "Settings":
        """A typo in ENABLED_AGENTS, CONFIRM_SEVERITIES or a prompt version must stop the app when
        it starts, not fail every review (or worse, silently review with the wrong agents)."""
        _ = self.agent_names
        _ = self.held_severities
        versions = {"PROMPT_VERSION": self.prompt_version}
        if "quality" in self.agent_names:
            versions["QUALITY_PROMPT_VERSION"] = self.quality_prompt_version
        if self.triage_enabled:
            versions["TRIAGE_PROMPT_VERSION"] = self.triage_prompt_version
        if self.architecture_agent_enabled:
            versions["ARCHITECTURE_PROMPT_VERSION"] = self.architecture_prompt_version
        for name, version in versions.items():
            if not _PROMPT_NAME.fullmatch(version) or not (_PROMPT_DIR / f"{version}.md").is_file():
                raise ValueError(f"{name}={version!r} does not match a file in app/review/prompts")
        caps = (
            self.architecture_max_files,
            self.architecture_max_chars_per_file,
            self.architecture_max_total_chars,
            self.architecture_max_findings,
        )
        if min(caps) < 1:
            raise ValueError("the ARCHITECTURE_MAX_* caps must be at least 1")
        if self.confirm_ttl_hours <= 0:
            raise ValueError("CONFIRM_TTL_HOURS must be positive")
        return self

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
