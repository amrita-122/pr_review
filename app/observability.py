import os

from app.config import Settings


def configure_tracing(settings: Settings) -> bool:
    """Export LangSmith settings to os.environ: the SDK reads the process environment, not pydantic.

    Tracing turns on only when it is enabled AND a key is set. Returns whether it is on.
    """
    if not settings.langsmith_tracing or settings.langsmith_api_key is None:
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    return True
