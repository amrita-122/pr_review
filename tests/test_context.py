"""10.23: related repo files are chosen from the diff and fetched read-only under hard caps."""

import base64

import httpx
import pytest
import respx

from app.diff.parse import parse_patch
from app.github.context import MAX_ATTEMPTS, fetch_context_files
from app.review.context import FIXED_CANDIDATES, ContextFile, candidate_paths, is_safe_path

BASE = "https://api.github.com/repos/o/r/contents"


def changed(path: str, *added: str) -> dict:
    body = "\n".join(f"+{line}" for line in added)
    patch = f"@@ -0,0 +1,{len(added)} @@\n{body}"
    return {path: parse_patch(path, patch)}


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("app/db/session.py", True),
        ("a-b_c/d.e/f.py", True),
        ("../etc/passwd", False),
        ("app/../../x.py", False),
        ("/abs/path.py", False),
        ("app//x.py", False),
        ("app/dir/", False),
        ("app/x y.py", False),
        ("app/x\n.py", False),
        ("app/%2e%2e/x.py", False),
        ("app/x?ref=evil", False),
        ("a" * 300, False),
        ("", False),
    ],
)
def test_only_plain_repo_relative_paths_are_candidates(path: str, ok: bool) -> None:
    assert is_safe_path(path) is ok


def test_imports_in_added_lines_become_candidates_before_the_fixed_names() -> None:
    files = changed(
        "app/api/users.py",
        "from app.auth import require_user",
        "import app.db.session, os",
        "from . import local_thing",
        "x = 1",
    )
    got = candidate_paths(files)
    assert got[:2] == ["app/auth.py", "app/auth/__init__.py"]
    assert "app/db/session.py" in got and "os.py" in got
    assert got.index("app/auth.py") < got.index("app/config.py")
    assert len(got) == len(set(got))  # de-duplicated
    assert all(is_safe_path(p) for p in got)


def test_removed_and_context_lines_and_other_languages_are_not_mined() -> None:
    patch = "@@ -1,2 +1,2 @@\n import secret_context\n-import secret_removed\n+x = 1"
    files = {
        "a.py": parse_patch("a.py", patch),
        "b.js": parse_patch("b.js", "@@ -0,0 +1 @@\n+import x from 'y'"),
    }
    got = candidate_paths(files)
    assert not any("secret_" in p for p in got)
    assert got == [p for p in FIXED_CANDIDATES]


def test_files_already_in_the_diff_are_not_fetched_again() -> None:
    files = changed("app/main.py", "from app.config import settings")
    got = candidate_paths(files)
    assert "app/main.py" not in got and "app/config.py" in got


def test_a_hostile_import_cannot_smuggle_a_path_out_of_the_repo() -> None:
    files = changed("app/x.py", "from ..evil import y", "import a.b/../../etc", "import ok_mod")
    got = candidate_paths(files)
    assert "ok_mod.py" in got and not any(".." in p or p.startswith("/") for p in got)


def _file(path: str, text: str, **extra: object) -> httpx.Response:
    body = {
        "type": "file",
        "encoding": "base64",
        "size": len(text),
        "content": base64.b64encode(text.encode()).decode(),
        **extra,
    }
    return httpx.Response(200, json=body)


CAPS = {"max_files": 3, "max_chars_per_file": 10, "max_total_chars": 25}


@respx.mock
async def test_fetches_read_only_at_the_head_sha_and_skips_what_it_cannot_use() -> None:
    sha = "a" * 40
    route_ok = respx.get(f"{BASE}/app/auth.py", params={"ref": sha}).mock(
        return_value=_file("app/auth.py", "def require_user(): ...")
    )
    respx.get(f"{BASE}/missing.py").mock(return_value=httpx.Response(404))
    respx.get(f"{BASE}/app/dir.py").mock(return_value=httpx.Response(200, json=[{"name": "x"}]))
    respx.get(f"{BASE}/app/link.py").mock(return_value=_file("x", "x", type="symlink"))
    respx.get(f"{BASE}/app/big.py").mock(return_value=_file("x", "x", size=500_000))
    respx.get(f"{BASE}/app/bin.py").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": 4,
                "content": base64.b64encode(b"\xff\xfe\x00\x01").decode(),
            },
        )  # fmt: skip
    )
    respx.get(f"{BASE}/app/err.py").mock(return_value=httpx.Response(403))
    got = await fetch_context_files(
        "tok", "o", "r", sha,
        [
            "missing.py", "app/dir.py", "app/link.py", "app/big.py", "app/bin.py", "app/err.py",
            "app/auth.py",
        ],
        **CAPS,
    )  # fmt: skip
    assert [f.path for f in got] == ["app/auth.py"]
    assert route_ok.call_count == 1
    assert got[0].content == "def requir"  # cut at max_chars_per_file (10)


@respx.mock
async def test_caps_on_files_characters_and_total() -> None:
    for name in "abcd":
        respx.get(f"{BASE}/{name}.py").mock(return_value=_file(name, name * 100))
    got = await fetch_context_files(
        "tok", "o", "r", "s", [f"{n}.py" for n in "abcd"], max_files=3, max_chars_per_file=10,
        max_total_chars=25,
    )  # fmt: skip
    assert [len(f.content) for f in got] == [10, 10, 5]  # per-file cap, then the total cap
    assert [f.path for f in got] == ["a.py", "b.py", "c.py"]  # d.py never requested
    assert not respx.calls.last.request.url.path.endswith("d.py")


@respx.mock
async def test_a_huge_candidate_list_costs_a_bounded_number_of_lookups() -> None:
    respx.get(url__regex=r".*/contents/.*").mock(return_value=httpx.Response(404))
    got = await fetch_context_files(
        "tok", "o", "r", "s", [f"m{i}.py" for i in range(200)], **{**CAPS, "max_files": 8}
    )
    assert got == [] and respx.calls.call_count == MAX_ATTEMPTS  # a 404 is not retried


@respx.mock
async def test_unsafe_candidates_are_never_requested() -> None:
    route = respx.get(url__regex=r".*/contents/.*").mock(return_value=httpx.Response(404))
    await fetch_context_files("tok", "o", "r", "s", ["../x.py", "/etc/passwd", "a b.py"], **CAPS)
    assert route.call_count == 0


def test_context_file_is_plain_data() -> None:
    assert ContextFile("a.py", "x") == ContextFile("a.py", "x")
