import pytest

from app.diff.filters import skip_reason


@pytest.mark.parametrize(
    ("filename", "status", "reason"),
    [
        ("uv.lock", "modified", "lockfile"),
        ("web/package-lock.json", "modified", "lockfile"),
        ("static/app.min.js", "added", "minified or source map"),
        ("static/app.js.map", "added", "minified or source map"),
        ("node_modules/x/index.js", "added", "vendored or generated folder"),
        ("pkg/vendor/lib.go", "added", "vendored or generated folder"),
        ("api/service_pb2.py", "added", "generated code"),
        ("logo.PNG", "added", "binary file"),
        ("src/app.py", "removed", "deleted file"),
    ],
)
def test_skipped(filename: str, status: str, reason: str) -> None:
    assert skip_reason(filename, status) == reason


@pytest.mark.parametrize(
    "filename",
    ["app/main.py", "README.md", "src/vendored_utils.py", "docs/build-notes.md", "lockfile.py"],
)
def test_kept(filename: str) -> None:
    assert skip_reason(filename, "modified") is None
