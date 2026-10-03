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
