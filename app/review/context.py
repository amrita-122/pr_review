"""Which repository files give the architecture agent useful context. Pure: no IO.

The candidates come from UNTRUSTED diffs (an import line is attacker-written text), so a candidate
is only ever a repo-relative path made of safe characters. The caller fetches them read-only,
under hard caps, and treats what it gets back as untrusted data again.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.diff.parse import ParsedFile

# Fixed names worth showing next to almost any change: where trust boundaries usually live.
FIXED_CANDIDATES = (
    "app/config.py",
    "app/settings.py",
    "config.py",
    "settings.py",
    "app/auth.py",
    "auth.py",
    "app/security.py",
    "app/main.py",
    "main.py",
    "app/middleware.py",
    "app/routes.py",
    "app/db/session.py",
)

_SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}")
_ADDED = re.compile(r"^L\d+ \+ (.*)$")
_FROM = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+")
_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w., ]*)")


@dataclass(frozen=True)
class ContextFile:
    """A related file from the repository: untrusted data, like the diff."""

    path: str
    content: str


def is_safe_path(path: str) -> bool:
    """A repo-relative path we are willing to put into a URL."""
    return (
        _SAFE_PATH.fullmatch(path) is not None
        and ".." not in path.split("/")
        and not path.endswith("/")
        and "//" not in path
    )


def _modules(parsed: ParsedFile) -> list[str]:
    found: list[str] = []
    for line in parsed.numbered.splitlines():
        added = _ADDED.match(line)
        if added is None:
            continue
        code = added.group(1)
        if (m := _FROM.match(code)) is not None:
            found.append(m.group(1))
        elif (m := _IMPORT.match(code)) is not None:
            found += [part.strip().split(" ")[0] for part in m.group(1).split(",") if part.strip()]
    return found


def _module_paths(module: str, current_dir: str) -> list[str]:
    """Where `a.b.c` might live in the repo: a/b/c.py or a/b/c/__init__.py, from the repo root
    and from the importing file's own directory (a relative import written without dots)."""
    rel = module.replace(".", "/")
    bases = ["", f"{current_dir}/"] if current_dir else [""]
    out: list[str] = []
    for base in bases:
        out += [f"{base}{rel}.py", f"{base}{rel}/__init__.py"]
    return out


def candidate_paths(changed: Mapping[str, ParsedFile], exclude: Iterable[str] = ()) -> list[str]:
    """Ordered, de-duplicated, safe candidate paths: imports of the changed Python files first
    (most relevant), then the fixed names. Files already in the diff are left out."""
    skip = set(exclude) | set(changed)
    ordered: list[str] = []
    for path, parsed in changed.items():
        if not path.endswith(".py"):
            continue
        current_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        for module in _modules(parsed):
            ordered += _module_paths(module, current_dir)
    ordered += FIXED_CANDIDATES
    seen: set[str] = set()
    result: list[str] = []
    for candidate in ordered:
        if candidate in skip or candidate in seen or not is_safe_path(candidate):
            continue
        seen.add(candidate)
        result.append(candidate)
    return result
