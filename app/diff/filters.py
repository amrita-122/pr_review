"""Rules for files we never send to a reviewer. All patterns live in SKIP_PATTERNS."""

import re

# (compiled pattern, reason). Matched against the full path with forward slashes, case-insensitive.
SKIP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), reason)
    for p, reason in [
        (
            r"(^|/)(uv\.lock|poetry\.lock|Pipfile\.lock|package-lock\.json|yarn\.lock"
            r"|pnpm-lock\.yaml|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum)$",
            "lockfile",
        ),
        (r"\.min\.(js|css)$|\.(js|css)\.map$", "minified or source map"),
        (
            r"(^|/)(node_modules|vendor|third_party|dist|build|__pycache__|\.venv|venv)/",
            "vendored or generated folder",
        ),
        (r"_pb2(_grpc)?\.pyi?$|\.pb\.go$|\.generated\.\w+$", "generated code"),
        (
            r"\.(png|jpe?g|gif|bmp|ico|webp|pdf|zip|gz|tar|tgz|jar|exe|dll|so|dylib"
            r"|woff2?|ttf|eot|mp[34]|mov|pyc|class|bin)$",
            "binary file",
        ),
    ]
]


def skip_reason(filename: str, status: str) -> str | None:
    """Why a file is not reviewed, or None if it should be."""
    if status == "removed":
        return "deleted file"
    path = filename.replace("\\", "/")
    for pattern, reason in SKIP_PATTERNS:
        if pattern.search(path):
            return reason
    return None
