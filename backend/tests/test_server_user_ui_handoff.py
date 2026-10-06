"""Only user UI page GETs accept strict, non-executing workbench handoffs."""

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.workflow import WorkflowService
from test_server import call, running_server as fake_server
from test_server_prompt_configs import running_server


SESSION = "00000000-0000-4000-8000-000000000921"
CONFIG_A = "00000000-0000-4000-8000-000000000922"
CONFIG_B = "00000000-0000-4000-8000-000000000923"
PAGE_PATHS = ("/", "/static/index.html")
SESSION_QUERY = "?session=" + SESSION
A_QUERY = f"{SESSION_QUERY}&prompt_a={CONFIG_A}&prompt_a_revision=1"
B_QUERY = f"{SESSION_QUERY}&prompt_b={CONFIG_B}&prompt_b_revision=2"
FULL_QUERY = f"{A_QUERY}&prompt_b={CONFIG_B}&prompt_b_revision=2"


def test_valid_handoff_page_queries_serve_html_without_service_calls():
    with fake_server() as (service, port):
        for path in PAGE_PATHS:
            for query in (
                SESSION_QUERY, A_QUERY, B_QUERY, FULL_QUERY,
                f"{FULL_QUERY}&model_config={CONFIG_A}&model_revision=3",
                f"{FULL_QUERY}&exposure_config={CONFIG_A}&exposure_revision=3",
                A_QUERY.replace("revision=1", "revision=9007199254740991"),
                f"?graph_workflow={CONFIG_A}",
                f"?graph_workflow={CONFIG_A}&graph_session={SESSION}",
            ):
                status, headers, body = call(port, "GET", path + query)
                assert status == 200 and b"<!doctype html>" in body
                assert headers["Content-Type"] == "text/html; charset=utf-8"
                assert headers["Cache-Control"] == "no-store"
        assert service.calls == []


def test_invalid_handoffs_and_non_page_queries_stay_not_found_without_service_calls():
    invalid = [
        "?", "?session=", "?session=invalid",
        "?session=" + SESSION.replace("-4000-", "-1000-"),
        "?session=" + SESSION.replace("-8000-", "-0000-"),
        f"?prompt_a={CONFIG_A}&prompt_a_revision=1",
        f"{SESSION_QUERY}&prompt_a={CONFIG_A}",
        f"{SESSION_QUERY}&prompt_a_revision=1",
        f"{SESSION_QUERY}&prompt_b={CONFIG_B}",
        f"{SESSION_QUERY}&model_config={CONFIG_A}",
        f"{SESSION_QUERY}&model_revision=1",
        f"{SESSION_QUERY}&exposure_config={CONFIG_A}",
        f"{SESSION_QUERY}&exposure_revision=1",
        f"{SESSION_QUERY}&exposure_config={CONFIG_A}&exposure_revision=0",
        f"{SESSION_QUERY}&model_config={CONFIG_A}&model_revision=0",
        f"{SESSION_QUERY}&model_config={CONFIG_A}&model_revision=9007199254740992",
        f"{SESSION_QUERY}&prompt_a=invalid&prompt_a_revision=1",
        f"{SESSION_QUERY}&session={SESSION}",
        f"{A_QUERY}&prompt_a_revision=1",
        f"{SESSION_QUERY}&config=https%3A%2F%2Funtrusted.invalid",
        SESSION_QUERY + "&", SESSION_QUERY + "#fragment",
        "?session=%FF", SESSION_QUERY + "&unknown",
        f"?graph_session={SESSION}", f"?graph_workflow=bad&graph_session={SESSION}",
        f"?graph_workflow={CONFIG_A}&graph_session=bad",
        f"?graph_workflow={CONFIG_A}&session={SESSION}",
        f"?graph_workflow={CONFIG_A}&prompt_a={CONFIG_A}&prompt_a_revision=1",
        f"?graph_workflow={CONFIG_A}&graph_workflow={CONFIG_A}",
    ]
    invalid.extend(
        f"{SESSION_QUERY}&prompt_a={CONFIG_A}&prompt_a_revision={revision}"
        for revision in ("", "0", "-1", "01", "+1", "1.0", "1e1", "latest", "9007199254740992")
    )
    with fake_server() as (service, port):
        for path in PAGE_PATHS:
            for query in invalid:
                assert call(port, "GET", path + query)[0] == 404
            assert call(port, "POST", path + FULL_QUERY, b"{}", {
                "Content-Type": "application/json",
            })[0] == 404
        for path in (
            "/static/app.js", "/static/style.css", "/static/other",
            "/api/health", "/api/sessions", "/api/active-session",
            "/api/prompt-configs/config", f"/api/sessions/{SESSION}",
        ):
            assert call(port, "GET", path + FULL_QUERY)[0] == 404
        assert call(port, "POST", "/api/sessions" + FULL_QUERY, b"{}", {
            "Content-Type": "application/json",
        })[0] == 404
        assert service.calls == []


def test_real_database_page_handoff_does_not_create_switch_or_execute():
    with TemporaryDirectory(prefix="ui-handoff-", dir=Path(__file__).parent) as folder:
        database = Path(folder) / "workflow.sqlite"
        with closing(WorkflowService(
            database, model_factory=lambda _stage: pytest.fail("A page GET cannot construct a model"),
        )) as service:
            with closing(sqlite3.connect(database)) as raw:
                before = tuple(raw.iterdump())
            with running_server(service) as port:
                for path in PAGE_PATHS:
                    assert call(port, "GET", path + FULL_QUERY)[0] == 200
            with closing(sqlite3.connect(database)) as raw:
                assert tuple(raw.iterdump()) == before
