"""Production workbench assets share the current API and consumer origin."""

from contextlib import contextmanager
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from phase1_agent.server import create_server, main
from test_server import FakeService, call, command


@contextmanager
def running_workbench(directory):
    service = FakeService()
    with patch("phase1_agent.graph_http.dispatch_graph",
               side_effect=lambda target, method, path, data: target.dispatch(method, path, data)):
        server = create_server(service, port=0, graph_service=service, workbench_dist=directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield service, server.server_address[1]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


@pytest.fixture
def build(tmp_path):
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(
        '<!doctype html><script type="module" src="/workbench/assets/index-test.js"></script>',
        encoding="utf-8",
    )
    (directory / "assets" / "index-test.js").write_text("window.currentBuild = true;", encoding="utf-8")
    (directory / "assets" / "index-test.css").write_text("body { color: black; }", encoding="utf-8")
    (directory / "assets" / "index-test.js.map").write_text('{"sources":["private"]}', encoding="utf-8")
    (directory / "release.sqlite").write_bytes(b"private")
    return directory


def test_production_workbench_and_current_pages_api_share_one_origin(build):
    with running_workbench(build) as (service, port):
        for path, content_type, marker in (
            ("/workbench/", "text/html", b"/workbench/assets/index-test.js"),
            ("/workbench/assets/index-test.js", "text/javascript", b"currentBuild"),
            ("/workbench/assets/index-test.css", "text/css", b"color: black"),
            ("/", "text/html", b"/static/chat-entry.js"),
            ("/static/index.html", "text/html", b"/static/chat-entry.js"),
        ):
            status, headers, body = call(port, "GET", path)
            assert status == 200 and headers["Content-Type"].startswith(content_type) and marker in body
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert service.calls == []
        assert call(port, "GET", "/api/health")[0] == 200
        assert command(port, {"operation": "example.current", "parameters": {}})[0] == 200
        assert len(service.calls) == 1


@pytest.mark.parametrize("path", [
    "/workbench", "/workbench/index.html", "/workbench/assets",
    "/workbench/assets/", "/workbench/release.sqlite",
    "/workbench/assets/index-test.js.map", "/workbench/assets/missing.js",
    "/workbench/assets/../index.html", "/workbench/assets/%2e%2e/index.html",
    "/workbench/assets/%69ndex-test.js", "/workbench/assets/index-test.js?download=1",
    "/workbench/?session=00000000-0000-4000-8000-000000000922",
])
def test_build_directory_is_not_an_arbitrary_file_or_spa_fallback(build, path):
    with running_workbench(build) as (service, port):
        assert call(port, "GET", path)[0] == 404
        assert service.calls == []


def test_workbench_does_not_weaken_host_origin_or_write_boundaries(build):
    with running_workbench(build) as (service, port):
        assert call(port, "GET", "/workbench/", headers={"Host": "evil.example"})[0] == 403
        assert call(port, "GET", "/workbench/", headers={"Origin": "http://evil.example"})[0] == 403
        assert call(port, "POST", "/workbench/", b"{}", {"Content-Type": "application/json"})[0] == 404
        assert command(port, {"operation": "example.current", "parameters": {}},
                       include_origin=False)[0] == 403
        assert service.calls == []


def test_workbench_build_is_a_startup_snapshot(build):
    with running_workbench(build) as (_, port):
        (build / "assets" / "index-test.js").write_text("changed", encoding="utf-8")
        (build / "assets" / "new.js").write_text("new", encoding="utf-8")
        assert b"currentBuild" in call(port, "GET", "/workbench/assets/index-test.js")[2]
        assert call(port, "GET", "/workbench/assets/new.js")[0] == 404


@pytest.mark.parametrize("directory", ["absent", "file", "no-index"])
def test_invalid_build_fails_before_binding_or_graph_initialization(tmp_path, directory):
    target = tmp_path / directory
    if directory == "file":
        target.write_bytes(b"not a directory")
    elif directory == "no-index":
        target.mkdir()
    with pytest.raises((ValueError, FileNotFoundError, NotADirectoryError)):
        create_server(FakeService(), port=0, workbench_dist=target)


def test_resolved_asset_outside_build_is_rejected(build, monkeypatch):
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == "index-test.js":
            return build.parent / "outside.js"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="linked files"):
        create_server(FakeService(), port=0, workbench_dist=build)


def test_cli_reports_invalid_build_without_creating_database(tmp_path, capsys):
    database = tmp_path / "demo.sqlite"
    with pytest.raises(SystemExit) as error:
        main(["--port", "0", "--database", str(database),
              "--workbench-dist", str(tmp_path / "missing")])
    assert error.value.code == 2
    assert "Invalid workbench build" in capsys.readouterr().err
    assert not database.exists()


def test_cli_prints_both_entry_points_without_initializing_database(build, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("phase1_agent.server.LocalHTTPServer.serve_forever", lambda server: None)
    database = tmp_path / "cli.sqlite"
    main(["--port", "0", "--database", str(database), "--workbench-dist", str(build)])
    urls = capsys.readouterr().out.splitlines()
    assert len(urls) == 2 and urls[1] == urls[0] + "workbench/"
    assert urls[0].startswith("http://127.0.0.1:") and urls[0].endswith("/")
    assert not database.exists()
