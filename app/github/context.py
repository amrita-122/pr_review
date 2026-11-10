"""Read related repository files through the Contents API. Read-only; the App already holds the
"Contents: read" permission. Everything returned is UNTRUSTED data (ADR 0009)."""

import base64
import binascii

import httpx
import structlog

from app.github.client import _headers
from app.github.http import get_with_retry, github_client
from app.review.context import ContextFile, is_safe_path

logger = structlog.get_logger()

MAX_ATTEMPTS = 20  # network lookups per review, however many candidates there are
MAX_FILE_BYTES = 200_000  # GitHub inlines up to 1 MB; we never want to carry that much


async def fetch_context_files(
    token: str,
    owner: str,
    repo: str,
    ref: str,
    candidates: list[str],
    *,
    max_files: int,
    max_chars_per_file: int,
    max_total_chars: int,
) -> list[ContextFile]:
    """The first `max_files` candidates that exist, are plain text files and fit the caps.

    A missing file (404), a directory, a binary, an oversized file or any error on one candidate is
    skipped: retrieval is best effort and never fails a review. Long files are cut at the cap.
    """
    files: list[ContextFile] = []
    total = 0
    attempts = 0
    async with github_client() as client:
        for path in candidates:
            if len(files) >= max_files or total >= max_total_chars or attempts >= MAX_ATTEMPTS:
                break
            if not is_safe_path(path):  # defence in depth: the selector already checked
                continue
            attempts += 1
            content = await _read(client, token, owner, repo, ref, path)
            if content is None:
                continue
            room = min(max_chars_per_file, max_total_chars - total)
            content = content[:room]
            files.append(ContextFile(path=path, content=content))
            total += len(content)
    logger.info("context_files_fetched", count=len(files), attempts=attempts, chars=total)
    return files


async def _read(
    client: httpx.AsyncClient, token: str, owner: str, repo: str, ref: str, path: str
) -> str | None:
    try:
        response = await get_with_retry(
            client,
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers=_headers(token),
            attempts=2,
        )
        data = response.json()
    except Exception as exc:  # 404 is the normal case; anything else is also just "no context"
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if status not in (404, None):
            logger.warning("context_fetch_failed", status=status)
        return None
    if not isinstance(data, dict) or data.get("type") != "file":
        return None  # a directory listing is a JSON array; a symlink or submodule has another type
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        return None  # too large for the API to inline, or an unknown encoding
    if int(data.get("size") or 0) > MAX_FILE_BYTES:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None  # a binary file
