"""Current static client, real HTTP channel and independent chat operations together."""

from contextlib import closing
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from phase1_agent.graph_service import GraphWorkflowService
from test_frontend_node_http import owner, serving
from test_workflow_chat_service import chat, counter, graph_fixture


def test_real_http_logical_chat_candidates_receipts_and_floor_fork(tmp_path):
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node.js is required for offline real-HTTP frontend-node checks")
    with closing(GraphWorkflowService(tmp_path / "chat-http.sqlite")) as service:
        graph, initial, calls, _, frontend = graph_fixture(service)
        binding = owner(graph, frontend, initial)
        with serving(service) as (_, port):
            completed = subprocess.run(
                [executable, str(Path(__file__).with_name("frontend_node_chat_http_bridge.cjs")),
                 f"http://127.0.0.1:{port}", json.dumps(binding)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        (tmp_path / "http-chat.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (tmp_path / "http-chat.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        report = json.loads(completed.stdout)
        assert report["done"] is True and calls == [0, 0, 0, 1]
        source = service.get_session(report["source_session_id"])
        target = service.get_session(report["target_session_id"])
        assert counter(source) == 5 and counter(target) == 2
        assert len(chat(source)["rounds"]) == 2 and len(chat(target)["rounds"]) == 1
        source_floor, target_floor = chat(source)["rounds"][0], chat(target)["rounds"][0]
        assert len(source_floor["candidates"]) == len(target_floor["candidates"]) == 3
        assert source_floor["selected_candidate_id"] != report["second_candidate_id"]
        assert target_floor["selected_candidate_id"] == report["second_candidate_id"]
        assert len(source["chains"]) == 4 and target["chains"] == []
