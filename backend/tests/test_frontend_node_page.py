"""Node-owned page host checks with isolated HTTP and EventSource doubles."""
from fnmatch import fnmatch
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.server import _STATIC_FILES
from test_server import call, running_server


STATIC = Path(__file__).resolve().parents[1] / "src" / "phase1_agent" / "static"
NODE_PAGE_ASSETS = {
    "/static/vendor/lucide/lucide.min.js": (
        "vendor/lucide/lucide.min.js", "text/javascript; charset=utf-8"),
    "/static/vendor/lucide/LICENSE": ("vendor/lucide/LICENSE", "text/plain; charset=utf-8"),
    "/static/assets/user-avatar.svg": ("assets/user-avatar.svg", "image/svg+xml; charset=utf-8"),
    "/static/assets/assistant-avatar.svg": (
        "assets/assistant-avatar.svg", "image/svg+xml; charset=utf-8"),
}


def test_frontend_node_page_host():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required for offline frontend-node page tests")
    completed = subprocess.run(
        [executable, "--test", str(Path(__file__).with_name("frontend_node_page.test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_node_page_assets_are_project_owned_and_packaged():
    html = (STATIC / "frontend-node.html").read_text(encoding="utf-8")
    entry = (STATIC / "frontend-node-entry.js").read_text(encoding="utf-8")
    for name, source in (("frontend-node.html", html), ("frontend-node-entry.js", entry)):
        assert "/tavern/" not in source, name + " must not depend on tavern assets"
    assert "/static/vendor/lucide/lucide.min.js" in html
    assert "/static/assets/assistant-avatar.svg" in html
    assert "/static/assets/user-avatar.svg" in entry
    assert "/static/assets/assistant-avatar.svg" in entry
    tomllib = pytest.importorskip("tomllib")
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    patterns = tomllib.loads(project.read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["package-data"]["phase1_agent"]
    for path in NODE_PAGE_ASSETS:
        relative = NODE_PAGE_ASSETS[path][0]
        assert (STATIC / relative).is_file(), relative
        assert any(fnmatch("static/" + relative, pattern) for pattern in patterns), relative


def test_node_page_assets_are_served_without_runtime_or_tavern():
    with running_server() as (service, port):
        for path, (relative, content_type) in NODE_PAGE_ASSETS.items():
            status, headers, body = call(port, "GET", path)
            assert _STATIC_FILES[path] == (relative, content_type)
            assert status == 200, path
            assert headers["Content-Type"] == content_type
            assert body == (STATIC / relative).read_bytes()
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["Cache-Control"] == "no-store"
        assert service.calls == []
