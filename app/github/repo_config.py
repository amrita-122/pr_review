"""Per-repository settings from `.review-bot.yml` (10.29).

TRUST: the file is read from the PR's BASE commit, never from the PR's head. A PR can edit its own
copy of the file, and a config that the PR controls would let an attacker switch the review off.
The file is still repo content (untrusted text), so it is size-capped, parsed with `safe_load`,
refused if it uses YAML anchors/aliases (expansion bombs) and validated by a strict schema.

It can only TIGHTEN what the app does: choose fewer agents, skip more paths, lower the caps.
The global settings are the ceiling. It cannot turn on the private channel, raise a cap, or touch
the validator or the output policy. Any problem with the file means "use the defaults".
"""

import re
from dataclasses import replace
from fnmatch import fnmatchcase

import structlog
import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.github.context import _read
from app.github.http import github_client
from app.review.graph import ReviewLimits

logger = structlog.get_logger()

CONFIG_PATH = ".review-bot.yml"
MAX_CONFIG_CHARS = 10_000
IGNORED_REASON = "ignored by .review-bot.yml"
_ANCHOR_OR_ALIAS = re.compile(r"(?:^|[\s\[{,:-])[&*][A-Za-z0-9_-]")


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[str] | None = None  # a subset of the globally enabled agents
    ignore: list[str] = Field(default_factory=list, max_length=50)  # path globs to skip
    max_files: int | None = Field(default=None, ge=1)
    max_chars_per_file: int | None = Field(default=None, ge=1)
    max_comments: int | None = Field(default=None, ge=1)

    def ignores(self, path: str) -> bool:
        return any(fnmatchcase(path, pattern) for pattern in self.ignore)

    def tighten(self, limits: ReviewLimits) -> ReviewLimits:
        """The limits with each value lowered, never raised."""

        def lower(current: int, wanted: int | None) -> int:
            return current if wanted is None else min(current, wanted)

        return replace(
            limits,
            max_files=lower(limits.max_files, self.max_files),
            max_chars_per_file=lower(limits.max_chars_per_file, self.max_chars_per_file),
            max_comments=lower(limits.max_comments, self.max_comments),
        )

    def choose_agents(self, enabled: list[str]) -> list[str]:
        """The globally enabled agents, narrowed. An unknown or empty choice changes nothing: a
        config must not be able to switch every agent off by accident."""
        if not self.agents:
            return enabled
        chosen = [name for name in enabled if name in self.agents]
        return chosen or enabled


def parse_repo_config(text: str) -> RepoConfig:
    """The config in `text`, or the defaults if it is too big, unsafe or invalid."""
    if len(text) > MAX_CONFIG_CHARS or _ANCHOR_OR_ALIAS.search(text):
        logger.warning("repo_config_rejected", reason="too large or uses anchors")
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
        if data is None:
            return RepoConfig()
        return RepoConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError, ValueError):
        logger.warning("repo_config_rejected", reason="invalid")
        return RepoConfig()


async def fetch_repo_config(token: str, owner: str, repo: str, base_sha: str | None) -> RepoConfig:
    """The repo's config as of the base commit. No base sha, no file or any error = defaults."""
    if not base_sha:
        return RepoConfig()
    try:
        async with github_client() as client:
            text = await _read(client, token, owner, repo, base_sha, CONFIG_PATH)
    except Exception as exc:  # best effort: never fail a review over a config file
        logger.warning("repo_config_fetch_failed", error=type(exc).__name__)
        return RepoConfig()
    return RepoConfig() if text is None else parse_repo_config(text)
