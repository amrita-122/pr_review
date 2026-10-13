from pydantic import SecretStr

from app.config import Settings


def test_settings_load_from_environment(monkeypatch):
    # Pretend these environment variables exist on the machine.
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "super-secret-webhook")
    monkeypatch.setenv(
        "GITHUB_PRIVATE_KEY_BASE64",
        "super-secret-private-key",
    )
    monkeypatch.setenv("SMEE_URL", "https://smee.io/example")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:password@localhost/testdb",
    )

    # Load the settings just like the real application would.
    settings = Settings()

    # Normal values should load correctly.
    assert settings.github_app_id == "123456"
    assert settings.smee_url == "https://smee.io/example"
    assert settings.database_url == "postgresql://user:password@localhost/testdb"

    # Secret values should also load correctly.
    assert settings.github_webhook_secret.get_secret_value() == "super-secret-webhook"
    assert settings.github_private_key_base64.get_secret_value() == "super-secret-private-key"

    # The secrets should be SecretStr objects.
    assert isinstance(settings.github_webhook_secret, SecretStr)
    assert isinstance(settings.github_private_key_base64, SecretStr)

    # Their actual values should NOT appear when Settings is represented/printed.
    settings_repr = repr(settings)

    assert "super-secret-webhook" not in settings_repr
    assert "super-secret-private-key" not in settings_repr
    assert "**********" in settings_repr


def test_llm_settings_defaults_and_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "w")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_BASE64", "k")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    monkeypatch.setenv("MODEL_REVIEW", "some-model")

    settings = Settings()

    assert settings.model_review == "some-model"
    assert settings.max_files_per_pr == 30
    assert settings.max_chars_per_file == 20000
    assert settings.model_timeout_seconds == 60.0
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-very-secret"
    assert "sk-ant-very-secret" not in repr(settings)
