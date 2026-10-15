"""The private client calls the real node channel without an HTTP service."""

from contextlib import closing
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from threading import Thread
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_channel import FrontendNodeChannel
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.graph_store import GraphRecordStore

from test_frontend_node_runtime import _graph, _save


@pytest.mark.parametrize("mode", ["normal", "unknown"])
def test_client_to_real_channel_saves_input_then_runs_and_reads_stored_messages(tmp_path, mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    with closing(GraphWorkflowService(tmp_path / "channel-client.sqlite")) as service:
        graph, messages, frontends = _graph(service, bound=True)
        graph = _save(service, graph)
        service.patch_node_configuration(
            workflow_definition_id=graph["workflow_definition_id"],
            node_binding_id=messages[0]["node_binding_id"],
            component_id="tools.editable-input", component_version="1",
            changes={"text": "current B"}, expected_edit_sequence=1,
            idempotency_key=str(uuid4()),
        )
        owner = {
            "workflow_definition_id": graph["workflow_definition_id"], "definition_revision": 1,
            "node_binding_id": frontends[0]["node_binding_id"], "session_id": None,
        }
        script = Path(__file__).with_name("frontend_node_channel_bridge.cjs")
        process = subprocess.Popen(
            [node, str(script), json.dumps(owner), mode], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        received = Queue()

        def collect():
            for line in process.stdout:
                received.put(line)
            received.put(None)

        reader = Thread(target=collect, daemon=True)
        reader.start()
        calls, channels, completed = [], {}, None
        try:
            while completed is None:
                try:
                    line = received.get(timeout=15)
                except Empty:
                    raise AssertionError("Private client/channel exchange timed out") from None
                if line is None:
                    break
                message = json.loads(line)
                if message.get("done"):
                    completed = message
                    break
                request = message["request"]
                calls.append(request["operation"])
                binding = request["owner"]
                key = json.dumps(binding, sort_keys=True)
                if key not in channels:
                    channels[key] = FrontendNodeChannel(service, binding)
                channel = channels[key]
                original_resolve = channel._evidence._resolve
                if mode == "unknown" and request["operation"] == "patch_input":
                    def fail_metadata_ack(record, resolution):
                        raise OSError("Simulated response loss after actual input save")
                    channel._evidence._resolve = fail_metadata_ack
                try:
                    value = channel.handle(request)
                    if request["operation"] == "state" and binding["session_id"] is None:
                        with closing(service._store()) as store:
                            assert GraphRecordStore(store).rows("workflow_session") == []
                    if request["operation"] == "start" and value["outcome"] == "succeeded":
                        service.wait(value["result"]["start_result"]["active_chain_run_id"])
                    reply = {"id": message["id"], "value": value}
                except ContractValidationError as failure:
                    reply = {"id": message["id"], "error": {
                        "message": str(failure), "code": getattr(failure, "reason_code", "invalid_request"),
                    }}
                except OSError as failure:
                    reply = {"id": message["id"], "error": {"message": str(failure)}}
                finally:
                    channel._evidence._resolve = original_resolve
                process.stdin.write(json.dumps(reply) + "\n")
                process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=15)
            diagnostic = process.stderr.read()
            assert process.returncode == 0, diagnostic
            assert completed is not None, diagnostic
            assert calls == (["state", "patch_input", "start", "state"] if mode == "normal" else [
                "state", "patch_input", "state", "receipt", "accept_receipt", "start", "state",
            ])
            session = service.get_session(completed["session_id"])
            assert session["status"] == "succeeded"
            assert len(session["objects"]["chat-0"]["value"]["entries"]) == 1
            with closing(service._store()) as store:
                assert len(GraphRecordStore(store).rows("workflow_session")) == 1
                assert store._connection.execute(
                    "SELECT count(*) FROM pending_commands WHERE resolution='pending'",
                ).fetchone()[0] == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
