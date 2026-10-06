"""Selected Chat dependencies reach the wire without revision or credential drift."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx
import pytest

from phase1_agent.adapter import DeepSeekAdapter
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import dumps_pretty
from phase1_agent.model_configuration import DEFAULT_PROVIDER_ID, default_provider
from phase1_agent.model_selection import validate_model_selection
from phase1_agent.runtime import CanonicalModelAdapter
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService
from test_model_configuration import model_config, uid
from test_server_prompt_configs import mutation, request, running_server
from test_workflow_frozen_model import final_payload, model_settings
from test_workflow_prepared_control import frozen_for_chain


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "selected-fixture-credential")
    with TemporaryDirectory(prefix="model-selection-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def selected(revision=1):
    return {
        "schema_version": 1, "kind": "workflow_model_selection",
        "config_id": uid(1), "revision": revision,
    }


def save_model(service, revision=1, *, changes=None):
    record = model_config()
    record["revision"] = revision
    if changes is not None:
        changes(record)
    return service.save_model_configuration("model", **mutation(record, revision - 1, f"model:{revision}"))


def provider_factory(calls, handler=None):
    def factory(stage, parameters, provider):
        def transport(request):
            calls.append((stage, str(request.url), json.loads(request.content)))
            result = handler(stage) if handler else None
            return result or httpx.Response(200, json=final_payload(stage, len(calls)))

        return CanonicalModelAdapter(DeepSeekAdapter(
            "selected-fixture-credential", base_url=provider["base_url"],
            model=parameters["model"], max_tokens=parameters.get("max_tokens"),
            temperature=parameters.get("temperature"), max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        ))
    return factory


def sql_dump(database):
    with closing(SqliteStore(database)) as store:
        return tuple(store._connection.iterdump())


def reroll(service, sid, chain):
    view = service.get_session(sid)
    receipt = service.reroll(
        sid, chain, idempotency_key="reroll:" + chain,
        expected_session_revision=view["revision"], expected_ref_revision=view["ref_revision"],
        expected_head_commit_id=view["head_commit_id"],
    )
    service.wait_for_idle(sid)
    return receipt


def test_chat_provider_address_and_parameters_reach_real_adapter_with_mock_transport(database):
    calls = []
    with closing(WorkflowService(database, mode="deepseek", model_factory=provider_factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        record = save_model(service)
        with running_server(service) as port:
            status, receipt = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "selected chain", "idempotency_key": "first", "model_selection": selected(),
            })
        assert status == 202
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert [stage for stage, _, _ in calls] == ["A", "B"]
        for frozen in frozen_for_chain(database, receipt["chain_run_id"]):
            plan = frozen["config"]["payload"]["model_selection_plan"]
            assert set(plan["nodes"]) == {"A", "B"}
            assert plan["selection"] == selected()
            assert frozen["model_parameters"] == plan["nodes"]["A"]["parameters"]
            assert plan["nodes"]["A"]["provider"] == default_provider()
        assert all(url == "https://api.deepseek.com/chat/completions" for _, url, _ in calls)
        assert model_settings(calls[0][2]) == {
            **record["nodes"][0]["parameters"], "stream": False, "thinking": {"type": "disabled"},
        }
        assert "selected-fixture-credential" not in dumps_pretty(service.get_session(sid))
        assert "selected-fixture-credential" not in "\n".join(sql_dump(database))
        assert service.get_session(sid)["model_selection"] == selected()

def test_builtin_chat_path_uses_selected_address_optional_parameters_and_zero_sdk_retry(database, monkeypatch):
    calls, initialized = [], []
    original = DeepSeekAdapter
    class MockAdapter(original):
        def __init__(self, **kwargs):
            initialized.append({key: value for key, value in kwargs.items() if key != "api_key"})
            def transport(request):
                calls.append((str(request.url), json.loads(request.content)))
                return httpx.Response(200, json=final_payload("native", len(calls)))
            super().__init__(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr("phase1_agent.adapter.DeepSeekAdapter", MockAdapter)
    with closing(WorkflowService(database, mode="deepseek")) as service:
        provider = {**default_provider(), "revision": 2, "base_url": "https://selected.test/v1"}
        service.save_model_configuration("provider", **mutation(provider, 1, "address"))
        def changes(record):
            record["nodes"][0]["provider_ref"]["revision"] = 2
            record["nodes"][0]["parameters"] = {"model": "selected-optional"}
        save_model(service, changes=changes)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "native", "once", model_selection=selected())
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert len(calls) == len(initialized) == 2
        assert all(url == "https://selected.test/v1/chat/completions" for url, _ in calls)
        assert all(settings["max_retries"] == 0 for settings in initialized)
        assert all(body["model"] == "selected-optional" and "max_tokens" not in body and "temperature" not in body
                   for _, body in calls)


@pytest.mark.parametrize("value", [
    None, {}, {"schema_version": True, "kind": "workflow_model_selection", "config_id": uid(1), "revision": 1},
    {**selected(), "revision": True}, {**selected(), "revision": 0},
    {**selected(), "api_key": "PRIVATE"}, {**selected(), "config_id": "latest"},
])
def test_selection_contract_rejects_missing_unknown_or_mutable_references(value):
    with pytest.raises(ContractValidationError):
        validate_model_selection(value)


@pytest.mark.parametrize("problem,reason", [
    ("missing", "not_found"), ("disconnected", "model_dependency_missing"),
    ("provider_missing", "provider_missing"), ("disabled", "provider_unavailable"),
    ("no_credential", "credential_unavailable"), ("bad_b_parameters", "model_parameters_unsupported"),
    ("offline", "offline_model_unsupported"), ("old_factory", "model_factory_unsupported"),
])
def test_unexecutable_complete_plan_has_no_partial_start(database, monkeypatch, problem, reason):
    calls = []
    mode = "offline" if problem == "offline" else "deepseek"
    factory = None if problem == "offline" else (lambda stage: object()) if problem == "old_factory" else provider_factory(calls)
    with closing(WorkflowService(database, mode=mode, model_factory=factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        def changes(record):
            if problem == "disconnected":
                record["edges"].pop()
            if problem == "provider_missing":
                record["nodes"][0]["provider_ref"]["revision"] = 99
            if problem == "bad_b_parameters":
                node = deepcopy(record["nodes"][0])
                node["id"] = uid(90)
                node["parameters"]["max_tokens"] = 9000
                record["nodes"].append(node)
                record["edges"][1]["source"] = node["id"]
        save_model(service, changes=changes)
        if problem == "disabled":
            provider = {**default_provider(), "revision": 2, "enabled": False}
            service.save_model_configuration("provider", **mutation(provider, 1, "disable"))
        if problem == "no_credential":
            monkeypatch.delenv("DEEPSEEK_API_KEY")
        before = sql_dump(database)
        with running_server(service) as port:
            status, body = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "PRIVATE text", "idempotency_key": "bad",
                "model_selection": selected(99 if problem == "missing" else 1),
            })
        assert status == (404 if problem == "missing" else 409)
        assert body["error"]["reason_code"] == reason
        assert "PRIVATE" not in json.dumps(body)
        assert sql_dump(database) == before and calls == []
        assert service.get_session(sid)["can_submit"]


def test_frozen_provider_survives_active_edit_retry_and_first_b_then_reroll(database):
    calls, entered, release = [], Event(), Event()
    def handler(stage):
        if stage == "A" and len(calls) == 1:
            entered.set()
            assert release.wait(15)
            return httpx.Response(503, json={"error": {"message": "retry"}})
    with closing(WorkflowService(database, model_factory=provider_factory(calls, handler))) as service:
        service._resolved["A"][1].implementation._backoff = lambda _retry: None
        sid = service.create_session()["workflow_session_id"]
        save_model(service)
        first = service.submit(sid, "frozen", "first", model_selection=selected())
        try:
            assert entered.wait(10)
            provider = {**default_provider(), "revision": 2, "base_url": "https://other.test/v1"}
            service.save_model_configuration("provider", **mutation(provider, 1, "edit"))
            save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="new-model"))
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        replacement = reroll(service, sid, first["chain_run_id"])
        assert service.get_session(sid)["error"] is None
        for frozen in frozen_for_chain(database, replacement["chain_run_id"]):
            assert frozen["config"]["payload"]["model_binding"]["provider"] == default_provider()
        assert len(calls) == 5
        assert all(url == "https://api.deepseek.com/chat/completions" for _, url, _ in calls)
        assert all(body["model"] == "deepseek-flash" for _, _, body in calls)


def test_paused_a_without_b_snapshot_resume_and_reroll_keep_original_complete_plan(database):
    calls, entered, release = [], Event(), Event()
    def handler(stage):
        if stage == "A" and len(calls) == 1:
            entered.set()
            assert release.wait(15)
    with closing(WorkflowService(database, model_factory=provider_factory(calls, handler))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_model(service)
        first = service.submit(sid, "pause", "first", model_selection=selected())
        try:
            assert entered.wait(10)
            view = service.get_session(sid)
            node = view["nodes"][0]
            service.interrupt(sid, node["run_id"], idempotency_key="pause",
                             expected_session_revision=view["revision"], expected_run_revision=node["revision"])
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert len(frozen_for_chain(database, first["chain_run_id"])) == 1
        save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="later"))
        reroll(service, sid, first["chain_run_id"])
        assert service.get_session(sid)["error"] is None
        assert all(body["model"] == "deepseek-flash" for _, _, body in calls)


def test_retry_exhaustion_resume_refuses_changed_credential_before_mutation_then_resumes_same_run(database, monkeypatch):
    calls, broken = [], True
    def handler(stage):
        if stage == "A" and broken:
            return httpx.Response(503, json={"error": {"message": "retry"}})
    with closing(WorkflowService(database, model_factory=provider_factory(calls, handler))) as service:
        service._resolved["A"][1].implementation._backoff = lambda _retry: None
        sid = service.create_session()["workflow_session_id"]
        save_model(service)
        service.submit(sid, "resume", "first", model_selection=selected())
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        assert view["error"]["code"] == "model_retry_exhausted"
        node = view["nodes"][0]
        before = sql_dump(database)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "rotated-fixture-credential")
        with pytest.raises(ContractValidationError) as error:
            service.resume(sid, node["run_id"], idempotency_key="resume",
                           expected_session_revision=view["revision"], expected_run_revision=node["revision"])
        assert error.value.reason_code == "credential_changed"
        assert sql_dump(database) == before and len(calls) == 4
        monkeypatch.setenv("DEEPSEEK_API_KEY", "selected-fixture-credential")
        broken = False
        service.resume(sid, node["run_id"], idempotency_key="resume",
                       expected_session_revision=view["revision"], expected_run_revision=node["revision"])
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        assert service.get_session(sid)["nodes"][0]["run_id"] == node["run_id"]


def test_three_turns_reopen_exact_replay_and_historical_fork_preserve_choices(database):
    calls = []
    with closing(WorkflowService(database, model_factory=provider_factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_model(service)
        first = service.submit(sid, "first", "turn:1", model_selection=selected())
        service.wait_for_idle(sid)
        save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="second-model"))
        service.submit(sid, "second", "turn:2", model_selection=selected(2))
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        fork = service.create_branch(
            sid, view["messages"][1]["visible_message_id"], idempotency_key="fork",
            expected_source_revision=view["revision"],
        )
        child = fork["workflow_session_id"]
        assert service.get_session(child)["model_selection"] == selected()
        before = sql_dump(database)
        assert service.submit(sid, "first", "turn:1", model_selection=selected()) == first
        assert sql_dump(database) == before
        with pytest.raises(ContractValidationError) as error:
            service.submit(sid, "first", "turn:1", model_selection=selected(2))
        assert error.value.reason_code == "idempotency_conflict"
        with pytest.raises(ContractValidationError) as error:
            service.submit(child, "wrong fallback", "default")
        assert error.value.reason_code == "model_selection_required"
    with closing(WorkflowService(database, model_factory=provider_factory(calls))) as service:
        service.submit(sid, "third", "turn:3", model_selection=selected(2))
        service.wait_for_idle(sid)
        service.submit(child, "child", "child:1", model_selection=selected())
        service.wait_for_idle(child)
        assert service.get_session(sid)["error"] is None
        assert service.get_session(child)["error"] is None
        assert len(service.get_session(sid)["messages"]) == 6
        assert len(service.get_session(child)["messages"]) == 4
        assert len(calls) == 8
        assert calls[-2][2]["model"] == calls[-1][2]["model"] == "deepseek-flash"


def test_original_request_without_model_field_keeps_its_receipt(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "legacy", "once")
        service.wait_for_idle(sid)
        assert service.submit(sid, "legacy", "once") == receipt
    with closing(WorkflowService(database)) as service:
        assert service.submit(sid, "legacy", "once") == receipt


def test_selected_user_fork_pending_input_uses_anchor_model_not_latest_parent_choice(database):
    calls = []
    with closing(WorkflowService(database, model_factory=provider_factory(calls))) as service:
        save_model(service)
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "first", "first", model_selection=selected())
        service.wait_for_idle(sid)
        save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="latest-parent"))
        service.submit(sid, "second", "second", model_selection=selected(2))
        service.wait_for_idle(sid)
        view = service.get_session(sid)
        fork = service.create_branch(
            sid, view["messages"][0]["visible_message_id"], idempotency_key="user-fork",
            expected_source_revision=view["revision"],
        )
        child = fork["workflow_session_id"]
        child_view = service.get_session(child)
        assert child_view["model_selection"] == selected()
        service.continue_pending_input(
            child, child_view["pending_input_id"], idempotency_key="pending",
            expected_session_revision=child_view["revision"],
        )
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        assert all(body["model"] == "deepseek-flash" for _, _, body in calls[-2:])


def test_frozen_plan_must_match_exact_saved_graph_but_history_does_not_require_live_credential(database, monkeypatch):
    calls = []
    with closing(WorkflowService(database, model_factory=provider_factory(calls))) as service:
        save_model(service)
        sid = service.create_session()["workflow_session_id"]
        receipt = service.submit(sid, "intact", "once", model_selection=selected())
        service.wait_for_idle(sid)
        original = frozen_for_chain(database, receipt["chain_run_id"])[0]
        corrupt = deepcopy(original)
        payload = corrupt["config"]["payload"]
        payload["model_selection_plan"]["nodes"]["A"]["parameters"]["model"] = "wrong"
        payload["model_binding"]["parameters"]["model"] = "wrong"
        payload["adapter"]["model"] = corrupt["model_parameters"]["model"] = "wrong"
        with pytest.raises(ContractValidationError) as error:
            service._snapshot_components(corrupt)
        assert error.value.reason_code == "storage_contract_violation"
        monkeypatch.delenv("DEEPSEEK_API_KEY")
        assert service._snapshot_components(original)
        assert service.get_session(sid)["model_selection"] == selected()
