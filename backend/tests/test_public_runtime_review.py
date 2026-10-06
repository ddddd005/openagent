"""Bounded regressions for public host locks and durable fact authority."""

import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

from phase1_agent.contracts import ModelResponse
from test_model_package import BoundaryFixture, uid


def test_starting_second_session_during_model_response_cannot_deadlock_host(tmp_path):
    """Use a child process so a regression cannot leave pytest's workers stuck."""
    script = r'''
import json
import os
import sys
from threading import Event, Thread
from uuid import uuid4

from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.contracts import ModelResponse
from test_models_service_integration import ModelDatabaseFixture
from phase1_agent.model_host_service import ModelHostService

entered, release, preparing = Event(), Event(), Event()

class Transport:
    max_retries = 0
    def generate(self, messages, tools):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release response")
        return ModelResponse("stop", "response")
    def close(self):
        pass

service = GraphWorkflowService(sys.argv[1], public_model_factory=lambda **kwargs: Transport())
fixture = ModelDatabaseFixture()
fixture.write(service, 1)
doc = fixture.document(service)
first = fixture.create(service, doc)
second = service.create_session(doc["workflow_definition_id"], 1, idempotency_key=str(uuid4()))
started = service.start(first["workflow_session_id"], expected_revision=first["revision"],
                        idempotency_key=str(uuid4()))
if not entered.wait(5):
    print(json.dumps({"setup_error": "first request did not enter transport"}), flush=True)
    os._exit(0)

original_prepare = ModelHostService.prepare_run
def prepare_second(self, **kwargs):
    preparing.set()
    return original_prepare(self, **kwargs)
ModelHostService.prepare_run = prepare_second
errors = []
def start_second():
    try:
        service.start(second["workflow_session_id"], expected_revision=second["revision"],
                      idempotency_key=str(uuid4()))
    except Exception as error:
        errors.append(type(error).__name__)
thread = Thread(target=start_second, daemon=True)
thread.start()
if not preparing.wait(5):
    print(json.dumps({"setup_error": "second start did not enter model preparation"}), flush=True)
    os._exit(0)
release.set()
thread.join(1.5)
try:
    service._futures[started["active_chain_run_id"]].result(timeout=1.5)
    first_done = True
except Exception:
    first_done = False
print(json.dumps({"second_start_blocked": thread.is_alive(),
                  "first_worker_finished": first_done, "errors": errors}), flush=True)
os._exit(0)
'''
    environment = os.environ.copy()
    environment["DEEPSEEK_API_KEY"] = "local-review-key"
    project = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = os.pathsep.join((str(project / "src"), str(project / "tests")))
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "locks.sqlite")],
        capture_output=True, text=True, timeout=15, env=environment,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {"second_start_blocked": False, "first_worker_finished": True, "errors": []}, observed


def test_same_logical_request_concurrent_reentry_uses_one_transport_response():
    entered, release, second_started = Event(), Event(), Event()
    results, errors = [], []

    def transport(messages, tools):
        entered.set()
        assert release.wait(5)
        return ModelResponse("stop", "retained response")

    fixture = BoundaryFixture(transport=transport)

    def call(*, second=False):
        if second:
            second_started.set()
        try:
            results.append(fixture.chat())
        except Exception as error:
            errors.append(error)

    first = Thread(target=call, daemon=True)
    second = Thread(target=lambda: call(second=True), daemon=True)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert second_started.wait(5)
    finally:
        release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert len(results) == 2 and results[0] == results[1]
    assert fixture.calls == 1 and len(fixture.facts) == 3
    fixture.service.close()


def test_release_does_not_wait_for_inflight_model_or_close_transport_before_return():
    entered, proceed, released = Event(), Event(), Event()
    errors = []

    def transport(messages, tools):
        entered.set()
        assert proceed.wait(5)
        return ModelResponse("stop", "late response")

    fixture = BoundaryFixture(transport=transport)

    def call():
        try:
            fixture.chat()
        except Exception as error:
            errors.append(error)

    def release():
        fixture.service.release_run(uid(2), uid(3))
        released.set()

    worker = Thread(target=call, daemon=True)
    disposer = Thread(target=release, daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        disposer.start()
        assert released.wait(1), "Release waited for the in-flight transport"
        assert fixture.closes == 0
        assert fixture.service.active_frame_count == 0
    finally:
        proceed.set()
    worker.join(5)
    disposer.join(5)
    assert not worker.is_alive() and not disposer.is_alive()
    assert not errors
    assert fixture.closes == 1 and fixture.calls == 1
    assert not fixture.service._requests
