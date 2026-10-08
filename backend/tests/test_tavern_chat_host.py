"""Finite tavern assets do not open a filesystem or weaken the generic page."""

from contextlib import closing
from importlib import resources
import threading

import pytest

from phase1_agent.server import _TAVERN_STATIC_FILES, create_server
from phase1_agent.workflow_host import WorkflowHost
from test_server import call, running_server


WORKFLOW = "00000000-0000-4000-8000-000000000921"
SESSION = "00000000-0000-4000-8000-000000000922"


def test_tavern_finite_assets_use_isolated_csp_and_preserve_generic_page():
    with running_server() as (service, port):
        for path, (name, content_type) in _TAVERN_STATIC_FILES.items():
            status, headers, body = call(port, "GET", path)
            expected = resources.files("phase1_agent.tavern").joinpath("frontend", name).read_bytes()
            assert status == 200 and body == expected
            assert headers["Content-Type"] == content_type
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            csp = headers["Content-Security-Policy"]
            assert "img-src 'self'" in csp and "font-src 'self'" in csp
            assert "'unsafe-inline'" not in csp and "data:" not in csp
            assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
        status, headers, body = call(port, "GET", "/")
        assert status == 200 and b"/static/chat-entry.js" in body and b"/tavern/entry.js" not in body
        assert "img-src" not in headers["Content-Security-Policy"]
        assert service.calls == []


def test_tavern_identity_queries_are_exact_and_passive():
    with running_server() as (service, port):
        for page in ("/tavern/", "/tavern/index.html"):
            for query in ("", f"?graph_workflow={WORKFLOW}",
                          f"?graph_workflow={WORKFLOW}&graph_session={SESSION}"):
                status, _, body = call(port, "GET", page + query)
                assert status == 200 and b"<!doctype html>" in body
        assert service.calls == []


@pytest.mark.parametrize("path", [
    "/tavern", "/tavern/private.sqlite", "/tavern/chat.js.map", "/tavern/styles/",
    "/tavern/../server.py", "/tavern/%2e%2e/server.py", "/tavern/%65ntry.js",
    "/tavern/vendor/sillytavern/public/script.js", "/tavern/entry.js?download=1",
    "/tavern/?", "/tavern/?graph_workflow=", "/tavern/?graph_workflow=bad",
    f"/tavern/?graph_session={SESSION}", f"/tavern/?graph_workflow={WORKFLOW}&unknown=1",
    f"/tavern/?graph_workflow={WORKFLOW}&graph_workflow={WORKFLOW}",
    f"/tavern/?graph_workflow={WORKFLOW}&graph_session={SESSION}&graph_session={SESSION}",
    f"/tavern/?graph_workflow={WORKFLOW}&", f"/tavern/?graph_workflow={WORKFLOW}#fragment",
    "/tavern/?graph_workflow=%FF",
])
def test_tavern_does_not_have_spa_directory_query_or_traversal_fallback(path):
    with running_server() as (service, port):
        assert call(port, "GET", path)[0] == 404
        assert service.calls == []


def test_tavern_preserves_host_origin_and_write_boundaries():
    with running_server() as (service, port):
        assert call(port, "GET", "/tavern/", headers={"Host": "evil.example"})[0] == 403
        assert call(port, "GET", "/tavern/", headers={"Origin": "http://evil.example"})[0] == 403
        assert call(port, "POST", "/tavern/", b"{}", {"Content-Type": "application/json"})[0] == 404
        assert service.calls == []


def test_tavern_page_and_assets_do_not_initialize_lazy_runtime(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Static tavern requests cannot construct a graph runtime")

    monkeypatch.setattr("phase1_agent.graph_service.GraphWorkflowService", forbidden)
    database = tmp_path / "tavern-static.sqlite"
    with closing(WorkflowHost(database)) as host:
        server = create_server(host, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for path in _TAVERN_STATIC_FILES:
                assert call(port, "GET", path)[0] == 200
            assert call(port, "GET", f"/tavern/?graph_workflow={WORKFLOW}")[0] == 200
            assert host._graph is None and not database.exists()
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


def test_tavern_declared_assets_have_package_data_patterns():
    from pathlib import Path
    from fnmatch import fnmatch

    tomllib = pytest.importorskip("tomllib")
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    patterns = tomllib.loads(project.read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["phase1_agent.tavern"]
    assert all(any(fnmatch("frontend/" + filename, pattern) for pattern in patterns)
               for filename, _ in _TAVERN_STATIC_FILES.values())
