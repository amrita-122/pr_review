from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    github_app_id: str
    github_webhook_secret: SecretStr
    github_private_key_base64: SecretStr
    smee_url: str | None = None
    database_url: str

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
