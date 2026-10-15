"""Finite tavern assets do not open a filesystem or weaken the generic page."""

from contextlib import closing
from hashlib import sha256
from importlib import resources
import json
import threading

import pytest

from phase1_agent.server import _TAVERN_STATIC_FILES, create_server
from phase1_agent.workflow_host import WorkflowHost
from test_server import call, running_server


WORKFLOW = "00000000-0000-4000-8000-000000000921"
SESSION = "00000000-0000-4000-8000-000000000922"


def test_tavern_finite_assets_use_isolated_csp_and_old_root_is_retired():
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
        assert status == 410 and json.loads(body)["error"]["code"] == "legacy_frontend_entry_retired"
        assert b"/static/chat-entry.js" not in body and b"/tavern/entry.js" not in body
        assert "Location" not in headers
        assert "img-src" not in headers["Content-Security-Policy"]
        assert service.calls == []


def test_tavern_old_identity_queries_are_retired_without_guessing_a_node_binding():
    with running_server() as (service, port):
        for page in ("/tavern/", "/tavern/index.html"):
            for query in ("", f"?graph_workflow={WORKFLOW}",
                          f"?graph_workflow={WORKFLOW}&graph_session={SESSION}"):
                status, _, body = call(port, "GET", page + query)
                assert status == 410
                assert json.loads(body)["error"]["code"] == "legacy_frontend_entry_retired"
                assert b"frontend-node/" not in body and b"<script" not in body
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
        assert call(port, "POST", "/tavern/", b"{}", {"Content-Type": "application/json"})[0] == 410
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
            assert call(port, "GET", f"/tavern/?graph_workflow={WORKFLOW}")[0] == 410
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


@pytest.mark.parametrize("path", [
    "/tavern/source/index.html", "/tavern/entry.js", "/tavern/adapter.js", "/tavern/chat.js",
    "/tavern/vendor/showdown/showdown.min.js", "/tavern/vendor/dompurify/purify.min.js",
    "/static/source/index.html", "/static/chat-entry.js", "/static/graph-chat-core.js",
    "/static/graph-chat.js", "/static/frontend-package-host.js", "/static/frontend-package.js",
    "/static/style.css",
])
def test_old_frontend_sources_are_readable_plain_text_and_not_executable(path):
    with running_server() as (service, port):
        status, headers, body = call(port, "GET", path)
        assert status == 200 and body
        assert headers["Content-Type"] == "text/plain; charset=utf-8"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Cache-Control"] == "no-store"
        assert service.calls == []


def test_license_page_links_to_retained_sources_and_licenses_not_the_retired_entry():
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attributes):
            if tag == "a":
                self.paths.append(dict(attributes)["href"])

    with running_server() as (service, port):
        status, headers, body = call(port, "GET", "/tavern/legal.html")
        assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8"
        parser = Links()
        parser.feed(body.decode("utf-8"))
        assert "/tavern/source/index.html" in parser.paths
        assert "/tavern/index.html" not in parser.paths
        for path in parser.paths:
            assert call(port, "GET", path)[0] == 200
        assert service.calls == []


def test_reused_assets_css_and_vendor_licenses_keep_the_recorded_bytes():
    root = resources.files("phase1_agent.tavern").joinpath("frontend")
    manifest = json.loads(root.joinpath("vendor/sillytavern/COPY-MANIFEST.json").read_text(encoding="utf-8"))
    retained = {
        "styles/chat.css", "assets/user-default.png", "assets/assistant.png",
        "vendor/sillytavern/LICENSE", "vendor/showdown/showdown.min.js", "vendor/showdown/LICENSE",
        "vendor/dompurify/purify.min.js", "vendor/dompurify/LICENSE", "vendor/dompurify/LICENSE-MPL",
        "vendor/lucide/lucide.min.js", "vendor/lucide/LICENSE",
    }
    copies = {row["destination"]: row for row in manifest["copies"]}
    for path in retained:
        assert sha256(root.joinpath(path).read_bytes()).hexdigest() == copies[path]["destination_sha256"]
