from fastapi import FastAPI, Header, HTTPException, Request

from app.config import get_settings
from app.github.webhooks import verify_signature

app = FastAPI()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/github")
async def handle_webhook(
    request: Request, signature: str | None = Header(None, alias="X-Hub-Signature-256")
) -> dict[str, str]:
    # Load the secret from your settings or environment variables
    settings = get_settings()
    secret = settings.github_webhook_secret.get_secret_value()
    body = await request.body()
    if not verify_signature(secret, body, signature):
        raise HTTPException(status_code=401)

    return {"status": "ok"}
