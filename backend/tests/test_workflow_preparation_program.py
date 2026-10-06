"""Public v2 publication, preview, session values and repeatable mixed runs."""

from contextlib import closing
from copy import deepcopy
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import httpx
import pytest

from phase1_agent import preparation_program
from phase1_agent.prepared_context import validate_frozen_preparation
from phase1_agent.program_variable_store import ProgramVariableStore
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService

from test_preparation_program import COUNTDOWN, countdown_program, node, program
from test_server_prompt_configs import request, running_server
from test_workflow_prepared_context import bundle, factory, uid
from test_workflow_prepared_control import frozen_for_chain, reroll
from test_workflow_prompt_selection import CONFIG, catalog_records, choice


@pytest.fixture
def database():
    directory = Path(__file__).resolve().parents[1] / "tmp"
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="increment-program-", dir=directory) as folder:
        yield Path(folder) / "workflow.sqlite"


def draft():
    items, config = catalog_records()
    items[0]["text"] = "fixed {{literal}}"
    processing = countdown_program()
    processing["nodes"].extend([
        node("fixed-source", "prompt-source", {"instances": [{
            "group_instance_id": None, "item_instance_id": uid(8300),
        }]}),
        node("mixed", "prompt-collector", {"input_order": ["fixed", "dynamic"]},
             fixed="fixed-source", dynamic="convert"),
    ])
    processing["outputs"]["prompt"] = "mixed"
    config.update(schema_version=2, preparation=processing)
    return {"items": items, "groups": [], "config": config}


def publish(service, value=None):
    value = draft() if value is None else value
    for item in value["items"]:
        service.save_prompt_config("item", item, expected_revision=0,
                                   idempotency_key="item:" + item["item_id"])
    service.save_prompt_config("config", value["config"], expected_revision=0, idempotency_key="config")
    return value


def fence(service, sid):
    current = service.get_session(sid)
    return {"expected_session_revision": current["revision"],
            "expected_ref_revision": current["ref_revision"],
            "expected_head_commit_id": current["head_commit_id"]}


def read_values(service, sid):
    return service.read_session_variables(sid, **fence(service, sid), prompt_selection=choice("A", "B"))


def current_state(database, sid):
    with closing(SqliteStore(database)) as store:
        return ProgramVariableStore(store).current(sid)


def test_http_v2_publication_mixed_three_rounds_reopen_and_session_isolation(database):
    calls, sid = [], None
    for first_round, last_round in ((1, 2), (3, 3)):
        with closing(WorkflowService(database, model_factory=factory(calls))) as service:
            if sid is None:
                sid = service.create_session()["workflow_session_id"]
                publish(service)
            with running_server(service) as port:
                for number in range(first_round, last_round + 1):
                    payload = {"text": f"root {number}", "idempotency_key": f"run:{number}",
                               "prompt_selection": choice("A", "B")}
                    status, receipt = request(port, "POST", f"/api/sessions/{sid}/inputs", payload)
                    assert status == 202
                    service.wait_for_idle(sid)
                    assert service.get_session(sid)["error"] is None
                    for frozen in frozen_for_chain(database, receipt["chain_run_id"]):
                        evidence = validate_frozen_preparation(frozen)
                        assert evidence["schema_version"] == 2
                        assert evidence["config"]["schema_version"] == 3
                        assert [item["text"] for item in evidence["collection"]["items"]] == [
                            "fixed {{literal}}", f"10/{10 - number}",
                        ]
                        assert all(message["source"]["kind"] != "prompt"
                                   for message in evidence["canonical_messages"])
                        assert len(evidence["program"]["stages"]) == len(draft()["config"]["preparation"]["nodes"])
                    assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 10 - number
            if last_round == 3:
                other = service.create_session()["workflow_session_id"]
                other_values = service.read_session_variables(
                    other, **fence(service, other), prompt_selection=choice("A", "B"),
                )
                assert next(entry["value"] for entry in other_values["values"] if entry["name"] == COUNTDOWN) == 10
                assert current_state(database, other) == {"revision": 0, "values": {}}
    assert len(calls) == 6


def test_preview_executes_same_program_without_persisting_variables_or_dispatch(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        before = current_state(database, sid)
        with running_server(service) as port:
            status, response = request(port, "POST", f"/api/sessions/{sid}/nodes/{A_BINDING}/context/preview", {
                **fence(service, sid), "prompt_config": draft(), "text": "preview",
            })
            assert status == 200 and response["status"] == "ready"
            assert [item["text"] for item in response["preparation"]["collection"]["items"]] == [
                "fixed {{literal}}", "10/9",
            ]
            status, pending = request(port, "POST", f"/api/sessions/{sid}/nodes/{B_BINDING}/context/preview", {
                **fence(service, sid), "prompt_config": draft(), "text": None,
            })
            assert status == 200 and pending["status"] == "pending"
        assert current_state(database, sid) == before and calls == []


def test_edit_is_persistent_typed_and_replays_after_new_head_without_duplicate_write(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        reading = read_values(service, sid)
        payload = {**fence(service, sid), "prompt_selection": choice("A", "B"),
                   "name": COUNTDOWN, "value": 20, "expected_variable_revision": reading["revision"],
                   "idempotency_key": "edit:countdown"}
        with running_server(service) as port:
            status, edited = request(port, "POST", f"/api/sessions/{sid}/variables/write", payload)
            assert status == 200
            assert next(entry["value"] for entry in edited["values"] if entry["name"] == COUNTDOWN) == 20
            submitted = service.submit(sid, "after edit", "edited-run", prompt_selection=choice("A", "B"))
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
            a, b = frozen_for_chain(database, submitted["chain_run_id"])
            assert a["config"]["payload"]["context_preparation"]["collection"]["items"][-1]["text"] == "20/19"
            assert b["config"]["payload"]["context_preparation"]["collection"]["items"][-1]["text"] == "20/19"
            state_before = current_state(database, sid)
            assert request(port, "POST", f"/api/sessions/{sid}/variables/write", payload) == (200, edited)
            assert current_state(database, sid) == state_before
            conflict = {**payload, "value": 21}
            status, rejected = request(port, "POST", f"/api/sessions/{sid}/variables/write", conflict)
            assert status == 409 and rejected["error"]["reason_code"] == "idempotency_conflict"
            invalid = {**fence(service, sid), "prompt_selection": choice("A", "B"), "name": COUNTDOWN,
                       "value": "wrong type", "expected_variable_revision": state_before["revision"],
                       "idempotency_key": "edit:invalid"}
            status, rejected = request(port, "POST", f"/api/sessions/{sid}/variables/write", invalid)
            assert status == 400 and rejected["error"]["reason_code"] == "variable_type_mismatch"
            assert current_state(database, sid) == state_before


def test_unknown_variable_preview_reports_locatable_redacted_diagnostic(database):
    value = draft()
    value["config"]["preparation"] = program([
        node("source", "text", {"text": "{{unknown}}"}),
        node("replace", "variable-replace", {"mode": "text"}, input="source"),
    ])
    with closing(WorkflowService(database, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        with running_server(service) as port:
            status, rejected = request(port, "POST", f"/api/sessions/{sid}/nodes/{A_BINDING}/context/preview", {
                **fence(service, sid), "prompt_config": value, "text": "preview",
            })
        assert status == 400 and rejected["error"]["reason_code"] == "missing_macro_variable"
        assert rejected["error"]["diagnostic"]["node_id"] == "replace"
        assert "unknown" not in rejected["error"]["message"]
        assert current_state(database, sid) == {"revision": 0, "values": {}}


def test_read_inline_defaults_and_unassigned_without_writing(database):
    with closing(WorkflowService(database, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        reading = service.read_session_variables(sid, **fence(service, sid), prompt_config=draft())
        assert reading["revision"] == 0
        entries = {entry["name"]: entry for entry in reading["values"]}
        assert entries[COUNTDOWN]["value"] == 10 and entries[COUNTDOWN]["source"] == "default"
        assert entries["b"]["assigned"] is False and "value" not in entries["b"]
        assert current_state(database, sid)["revision"] == 0


def test_http_inline_variable_read_is_post_only_and_has_no_side_effects(database):
    with closing(WorkflowService(database, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        before_fence, before_state = fence(service, sid), current_state(database, sid)
        with running_server(service) as port:
            status, reading = request(port, "POST", f"/api/sessions/{sid}/variables/read", {
                **before_fence, "prompt_config": draft(),
            })
            assert status == 200
            assert reading["revision"] == 0 and "write_receipt" not in reading
            entries = {entry["name"]: entry for entry in reading["values"]}
            assert entries[COUNTDOWN]["value"] == 10
            assert entries["b"]["assigned"] is False
            assert request(port, "GET", f"/api/sessions/{sid}/variables/read")[0] == 404
        assert current_state(database, sid) == before_state
        assert fence(service, sid) == before_fence


def test_http_variable_write_rejects_get_with_valid_body_and_post_without_origin(database):
    with closing(WorkflowService(database, model_factory=factory([]))) as service:
        sid = service.create_session()["workflow_session_id"]
        payload = {
            **fence(service, sid), "prompt_config": draft(), "name": COUNTDOWN, "value": 999,
            "expected_variable_revision": 0, "idempotency_key": "must-not-write",
        }
        before_fence, before_state = fence(service, sid), current_state(database, sid)
        with running_server(service) as port:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(
                    "GET", f"/api/sessions/{sid}/variables/write",
                    body=json.dumps(payload).encode("utf-8"),
                    headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                rejected = json.loads(response.read())
                assert response.status == 404 and rejected["error"]["code"] == "not_found"
            finally:
                connection.close()
            status, rejected = request(
                port, "POST", f"/api/sessions/{sid}/variables/write", payload, origin=False,
            )
            assert status == 403
        assert current_state(database, sid) == before_state
        assert fence(service, sid) == before_fence


def edit_value(service, sid, value, key):
    reading = read_values(service, sid)
    return service.write_session_variable(
        sid, **fence(service, sid), prompt_selection=choice("A", "B"),
        name=COUNTDOWN, value=value, expected_variable_revision=reading["revision"], idempotency_key=key,
    )


def test_same_run_resume_reuses_preparation_and_keeps_variable_edit(database, monkeypatch):
    calls, regex_calls, broken = [], [], True
    original_regex = preparation_program.regex_replace_many

    def counted_regex(*args, **kwargs):
        regex_calls.append(kwargs["node_id"])
        return original_regex(*args, **kwargs)

    class RecoveringModel:
        def __init__(self, stage):
            self.stage = stage

        def generate(self, messages, tools):
            if self.stage == "A" and broken:
                calls.append((self.stage, deepcopy(messages), deepcopy(tools)))
                raise httpx.ReadTimeout("injected transient fixture")
            return factory(calls)(self.stage).generate(messages, tools)

    value = draft()
    processing = value["config"]["preparation"]
    processing["nodes"].insert(-2, node("regex-once", "regex", {
        "mode": "prompt",
        "rule": {"pattern": r"\d", "replacement": r"\g<0>\g<0>", "flags": "", "mode": "all"},
    }, input="convert"))
    processing["nodes"][-1]["inputs"]["dynamic"] = "regex-once"
    monkeypatch.setattr(preparation_program, "regex_replace_many", counted_regex)

    with closing(WorkflowService(database, model_factory=RecoveringModel)) as service:
        service._resolved["A"][1].implementation._backoff = lambda retry: None
        sid = service.create_session()["workflow_session_id"]
        publish(service, value)
        submitted = service.submit(sid, "same root", "original", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        failed = service.get_session(sid)
        assert failed["error"]["code"] == "model_retry_exhausted"
        run_id = failed["nodes"][0]["run_id"]
        original_a = deepcopy(frozen_for_chain(database, submitted["chain_run_id"])[0])
        original_evidence = validate_frozen_preparation(original_a)
        assert value["config"]["schema_version"] == 2
        assert original_evidence["config"]["schema_version"] == 3
        assert len(calls) == 4 and regex_calls == ["regex-once"]
        stages = {stage["node_id"]: stage for stage in original_evidence["program"]["stages"]}
        assert stages["update"]["state_after"]["values"][COUNTDOWN]["value"] == 9
        assert not stages["update"]["cached"] and not stages["regex-once"]["cached"]
        assert [item["text"] for item in original_evidence["collection"]["items"]] == [
            "fixed {{literal}}", "1100/99",
        ]
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 9

        edit_value(service, sid, 50, "while-paused")
        before_resume = current_state(database, sid)
        resume_view = service.get_session(sid)
        broken = False
        service.resume(
            sid, run_id, idempotency_key="resume-same-run",
            expected_session_revision=resume_view["revision"],
            expected_run_revision=resume_view["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        completed = service.get_session(sid)
        assert completed["error"] is None
        assert completed["nodes"][0]["run_id"] == run_id
        assert completed["chains"] == [{"chain_run_id": submitted["chain_run_id"], "status": "succeeded"}]
        resumed_a, prepared_b = frozen_for_chain(database, submitted["chain_run_id"])
        assert resumed_a == original_a
        a_calls = [messages for stage, messages, _tools in calls if stage == "A"]
        assert len(a_calls) == 5 and all(messages == original_a["s0"] for messages in a_calls)
        b_evidence = validate_frozen_preparation(prepared_b)
        b_stages = {stage["node_id"]: stage for stage in b_evidence["program"]["stages"]}
        assert all(stage["cached"] for stage in b_stages.values())
        assert b_stages["update"]["state_before"]["values"][COUNTDOWN]["value"] == 50
        assert b_stages["update"]["state_after"]["values"][COUNTDOWN]["value"] == 50
        assert [item["text"] for item in b_evidence["collection"]["items"]] == [
            "fixed {{literal}}", "1100/99",
        ]
        assert regex_calls == ["regex-once"]
        after_resume = current_state(database, sid)
        assert after_resume["values"] == before_resume["values"]
        assert after_resume["revision"] >= before_resume["revision"]


def test_reroll_keeps_definite_prompt_without_reversing_later_variable_edit(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        first = service.submit(sid, "same root", "original", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        old_a, old_b = frozen_for_chain(database, first["chain_run_id"])
        edit_value(service, sid, 50, "after-original")
        before = current_state(database, sid)
        rolled = reroll(service, sid)
        assert service.get_session(sid)["error"] is None
        new_a, new_b = frozen_for_chain(database, rolled["chain_run_id"])
        assert new_a["s0"] == old_a["s0"]
        assert validate_frozen_preparation(new_a) == validate_frozen_preparation(old_a)
        assert [item["text"] for item in validate_frozen_preparation(new_b)["collection"]["items"]] == [
            "fixed {{literal}}", "10/9",
        ]
        assert [item["text"] for item in validate_frozen_preparation(old_b)["collection"]["items"]] == [
            "fixed {{literal}}", "10/9",
        ]
        assert current_state(database, sid) == before
        assert next(entry["value"] for entry in read_values(service, sid)["values"]
                    if entry["name"] == COUNTDOWN) == 50
        candidates = {row["chain_run_id"]: row for row in bundle(database)["workflow_candidate"]}
        calls_before_selection = deepcopy(calls)
        committed_values = validate_frozen_preparation(old_b)["program"]["state"]["values"]
        edit_value(service, sid, 75, "after-reroll")
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 75
        selected_view = service.get_session(sid)
        service.select_candidate(
            sid, candidates[first["chain_run_id"]]["candidate_id"], idempotency_key="select-original",
            expected_session_revision=selected_view["revision"],
            expected_ref_revision=selected_view["ref_revision"],
            expected_head_commit_id=selected_view["head_commit_id"],
        )
        assert current_state(database, sid)["values"] == committed_values
        assert calls == calls_before_selection
        assert frozen_for_chain(database, first["chain_run_id"]) == [old_a, old_b]


def test_v2_values_survive_v1_fixed_commit_fork_and_later_source_edit(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        service.submit(sid, "dynamic", "dynamic-run", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        edit_value(service, sid, 50, "before-fixed")
        fixed = deepcopy(draft()["config"])
        fixed.update(schema_version=1, config_id=uid(9700))
        fixed.pop("preparation")
        service.save_prompt_config("config", fixed, expected_revision=0, idempotency_key="fixed-only")
        selection = choice("A", "B")
        for selected in selection["nodes"].values():
            selected["config_id"] = fixed["config_id"]
        submitted = service.submit(sid, "fixed", "fixed-run", prompt_selection=selection)
        service.wait_for_idle(sid)
        fixed_view = service.get_session(sid)
        assert fixed_view["error"] is None
        assert all(snapshot["config"]["payload"].get("program_variable_preparation") is None
                   for snapshot in frozen_for_chain(database, submitted["chain_run_id"]))
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 50
        assistant_id = fixed_view["messages"][-1]["visible_message_id"]
        edit_value(service, sid, 75, "after-fixed")
        child = service.create_branch(
            sid, assistant_id, idempotency_key="fork-fixed",
            expected_source_revision=service.get_session(sid)["revision"],
        )["workflow_session_id"]
        assert next(entry["value"] for entry in read_values(service, child)["values"]
                    if entry["name"] == COUNTDOWN) == 50
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 75
        service.submit(child, "dynamic child", "child-run", prompt_selection=choice("A", "B"))
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        assert current_state(database, child)["values"][COUNTDOWN]["value"] == 49
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 75


def test_fork_inherits_chosen_commit_values_not_later_source_edits(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        service.submit(sid, "original", "original", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        original = service.get_session(sid)
        assert original["error"] is None
        assistant_id = original["messages"][-1]["visible_message_id"]
        edit_value(service, sid, 50, "source-edit")
        child = service.create_branch(
            sid, assistant_id, idempotency_key="fork-original",
            expected_source_revision=service.get_session(sid)["revision"],
        )["workflow_session_id"]
        assert next(entry["value"] for entry in read_values(service, child)["values"]
                    if entry["name"] == COUNTDOWN) == 9
        service.submit(child, "child", "child-run", prompt_selection=choice("A", "B"))
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        assert current_state(database, child)["values"][COUNTDOWN]["value"] == 8
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 50


def test_new_b_replacement_observes_edit_after_a_has_started(database):
    calls, entered, release = [], Event(), Event()
    with closing(WorkflowService(database, model_factory=factory(calls, entered=entered, release=release))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        b_config = deepcopy(draft()["config"])
        b_config["config_id"] = uid(9400)
        b_config["preparation"] = program([
            node("register", "variable-register", {"name": COUNTDOWN, "type": "integer", "initial": 10}),
            node("b-source", "text", {"text": "{{" + COUNTDOWN + "}}"}),
            node("b-replace", "variable-replace", {"mode": "text"}, input="b-source"),
            node("b-convert", "text-to-prompt", {
                "item_instance_id": uid(9401), "role": "system", "placement": "before",
                "depth": None, "order": 0,
            }, input="b-replace"),
        ], prompt="b-convert")
        service.save_prompt_config("config", b_config, expected_revision=0, idempotency_key="config:b")
        selection = choice("A", "B")
        selection["nodes"]["B"]["config_id"] = b_config["config_id"]
        submitted = service.submit(sid, "pending A", "dynamic-run", prompt_selection=selection)
        try:
            assert entered.wait(10)
            reading = service.read_session_variables(sid, **fence(service, sid), prompt_selection=selection)
            service.write_session_variable(
                sid, **fence(service, sid), prompt_selection=selection, name=COUNTDOWN, value=50,
                expected_variable_revision=reading["revision"], idempotency_key="mid-run-edit",
            )
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a, b = frozen_for_chain(database, submitted["chain_run_id"])
        assert validate_frozen_preparation(a)["collection"]["items"][-1]["text"] == "10/9"
        assert validate_frozen_preparation(b)["collection"]["items"][0]["text"] == "50"
        assert current_state(database, sid)["values"][COUNTDOWN]["value"] == 50


def test_a_b_disagreeing_defaults_are_rejected_before_run_and_before_read(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        second = deepcopy(draft()["config"])
        second["config_id"] = uid(9500)
        second["preparation"]["nodes"][0]["config"]["initial"] = 20
        service.save_prompt_config("config", second, expected_revision=0, idempotency_key="different-default")
        selected = choice("A", "B")
        selected["nodes"]["B"]["config_id"] = second["config_id"]
        with running_server(service) as port:
            status, rejected = request(port, "POST", f"/api/sessions/{sid}/variables/read", {
                **fence(service, sid), "prompt_selection": selected,
            })
            assert status == 400
            status, rejected = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "must not run", "idempotency_key": "different-default",
                "prompt_selection": selected,
            })
            assert status == 400
        assert calls == [] and current_state(database, sid)["revision"] == 0


def test_write_after_readonly_reroll_returns_exact_receipt_with_monotonic_revision_gap(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        publish(service)
        service.submit(sid, "same root", "original", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        edit_value(service, sid, 50, "before-reroll")
        before = current_state(database, sid)
        reroll(service, sid)
        assert current_state(database, sid) == before
        payload = {
            **fence(service, sid), "prompt_selection": choice("A", "B"), "name": COUNTDOWN,
            "value": 55, "expected_variable_revision": before["revision"], "idempotency_key": "after-reroll",
        }
        with running_server(service) as port:
            status, edited = request(port, "POST", f"/api/sessions/{sid}/variables/write", payload)
            assert status == 200
            assert edited["revision"] > before["revision"] + 1
            assert edited["write_receipt"] == {
                "idempotency_key": "after-reroll", "name": COUNTDOWN,
                "expected_variable_revision": before["revision"],
            }
            assert next(entry["value"] for entry in edited["values"] if entry["name"] == COUNTDOWN) == 55
            reading = service.read_session_variables(
                sid, **fence(service, sid), prompt_selection=choice("A", "B"),
            )
            assert "write_receipt" not in reading and reading["revision"] == edited["revision"]
            service.submit(sid, "next", "after-gap", prompt_selection=choice("A", "B"))
            service.wait_for_idle(sid)
            assert service.get_session(sid)["error"] is None
            before_replay = current_state(database, sid)
            assert request(port, "POST", f"/api/sessions/{sid}/variables/write", payload) == (200, edited)
            assert current_state(database, sid) == before_replay
