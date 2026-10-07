"""Only strict current graph identities can bind the static consumer page."""

from contextlib import closing
import threading

import pytest

from phase1_agent.server import create_server
from phase1_agent.workflow_host import WorkflowHost
from test_server import call, running_server


WORKFLOW = "00000000-0000-4000-8000-000000000921"
SESSION = "00000000-0000-4000-8000-000000000922"
PAGE_PATHS = ("/", "/static/index.html")


def test_current_handoff_page_queries_do_not_dispatch_or_create_sessions():
    with running_server() as (service, port):
        for path in PAGE_PATHS:
            for query in (
                "", f"?graph_workflow={WORKFLOW}",
                f"?graph_workflow={WORKFLOW}&graph_session={SESSION}",
                f"?graph_workflow={WORKFLOW.replace('-', '%2D')}",
            ):
                status, headers, body = call(port, "GET", path + query)
                assert status == 200 and b"<!doctype html>" in body
                assert headers["Cache-Control"] == "no-store"
                assert b"A \xe8\x8d\x89\xe7\xa8\xbf" not in body
                assert b'src="/static/app.js"' not in body
        assert service.calls == []


@pytest.mark.parametrize("query", [
    "?", "?graph_workflow=", "?graph_workflow=bad", "?graph_workflow=%FF",
    f"?graph_session={SESSION}", f"?session={SESSION}",
    f"?graph_workflow={WORKFLOW}&session={SESSION}",
    f"?graph_workflow={WORKFLOW}&graph_session=bad",
    f"?graph_workflow={WORKFLOW}&graph_session=",
    f"?graph_workflow={WORKFLOW}&graph_session",
    f"?graph_workflow={WORKFLOW}&graph_session={SESSION}&graph_session={SESSION}",
    f"?graph_workflow={WORKFLOW}&graph_workflow={WORKFLOW}",
    f"?graph_workflow={WORKFLOW}&unknown=value",
    f"?graph_workflow={WORKFLOW}&", f"?graph_workflow={WORKFLOW}&&graph_session={SESSION}",
    f"?graph_workflow={WORKFLOW}#fragment",
    "?graph_workflow=" + WORKFLOW.replace("-4000-", "-1000-"),
    "?graph_workflow=" + WORKFLOW.replace("-8000-", "-0000-"),
    f"?session={SESSION}&prompt_a={WORKFLOW}&prompt_a_revision=1",
])
def test_invalid_or_retired_page_handoffs_have_no_fallback(query):
    with running_server() as (service, port):
        for path in PAGE_PATHS:
            assert call(port, "GET", path + query)[0] == 404
        assert service.calls == []


def test_non_page_queries_remain_not_found():
    with running_server() as (service, port):
        query = f"?graph_workflow={WORKFLOW}"
        for path in ("/api/health", "/static/chat-entry.js", "/static/graph-chat.js", "/api/graph/node-types"):
            assert call(port, "GET", path + query)[0] == 404
        for path in PAGE_PATHS:
            assert call(port, "POST", path + query, b"{}", {"Content-Type": "application/json"})[0] == 404
        assert service.calls == []


def test_pages_static_and_health_do_not_initialize_lazy_graph_host(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Static page GET cannot construct a graph runtime")

    monkeypatch.setattr("phase1_agent.graph_service.GraphWorkflowService", forbidden)
    database = tmp_path / "page-only.sqlite"
    with closing(WorkflowHost(database)) as host:
        server = create_server(host, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            for path in PAGE_PATHS:
                for query in ("", f"?graph_workflow={WORKFLOW}&graph_session={SESSION}"):
                    assert call(port, "GET", path + query)[0] == 200
            for path in ("/api/health", "/static/chat-entry.js", "/static/graph-chat-core.js",
                         "/static/graph-chat.js", "/static/frontend-package-host.js",
                         "/static/frontend-package.js", "/static/style.css"):
                assert call(port, "GET", path)[0] == 200
            assert host._graph is None and not hasattr(host, "_legacy")
            assert not database.exists()
            with pytest.raises(AttributeError):
                host.create_session()
            assert host._graph is None
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
    assert not database.exists()
