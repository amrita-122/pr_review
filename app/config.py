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
    model_timeout_seconds: float = 60.0
    # USD per million tokens; defaults are Claude Sonnet 5.5 list prices. Estimates only.
    price_input_per_mtok: Decimal = Decimal("2")
    price_output_per_mtok: Decimal = Decimal("10")
    prompt_version: str = "security_v2"
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "pr-review-agent"

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
