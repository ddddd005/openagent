"""Whole feature-package scenarios using real SQLite/HTTP and offline model wires."""

from contextlib import closing
from copy import deepcopy
from threading import Event

from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService
from test_exposure_configuration import configuration
from test_workflow_context_view import fence, persistent_data
from test_workflow_model_selection import database, provider_factory, save_model, selected
from test_workflow_prompt_selection import save_catalog, choice, assert_selected
from test_workflow_prepared_control import frozen_for_chain
from test_workflow_interrupt import accepted_interrupt
from test_server_prompt_configs import request, running_server


def post_turn(service, port, sid, number, *, revision=1):
    body = {
        "text": f"matrix round {number}", "idempotency_key": f"matrix:{sid}:{number}",
        "prompt_selection": choice("A", "B", revision=revision),
        "model_selection": selected(revision),
    }
    status, receipt = request(port, "POST", f"/api/sessions/{sid}/inputs", body)
    assert status == 202
    service.wait_for_idle(sid)
    return body, receipt


def test_three_round_http_restart_matrix_preserves_exact_models_prompts_history_and_public_reads(database):
    calls, sid, previous_runs = [], None, set()
    for number in range(1, 4):
        with closing(WorkflowService(database, mode="deepseek", model_factory=provider_factory(calls))) as service:
            if sid is None:
                sid = service.create_session()["workflow_session_id"]
                save_catalog(service)
                save_model(service)
                service.save_exposure_configuration(
                    record=configuration(), expected_revision=0, idempotency_key="declarations",
                )
            with running_server(service) as port:
                body, receipt = post_turn(service, port, sid, number)
                before, count = persistent_data(database), len(calls)
                # A lost receipt can be reconciled with precisely the original body.
                status, replay = request(port, "POST", f"/api/sessions/{sid}/inputs", body)
                assert status == 202 and replay == receipt and len(calls) == count
                public_path = f"/api/sessions/{sid}/exposures/{configuration()['config_id']}/revisions/1"
                status, public = request(port, "GET", public_path)
                assert status == 200 and public["availability"] == "available"
                assert persistent_data(database) == before
            view = service.get_session(sid)
            observed = view["observation"]
            assert view["can_submit"] and view["error"] is None
            assert len(view["messages"]) == number * 2
            assert view["model_selection"] == selected()
            assert observed["chain_run_id"] == receipt["chain_run_id"]
            current_runs = {node["run_id"] for node in observed["nodes"]}
            assert not current_runs.intersection(previous_runs)
            previous_runs.update(current_runs)
            for stage, frozen in zip(("A", "B"), frozen_for_chain(database, receipt["chain_run_id"])):
                assert_selected(frozen)
                assert frozen["config"]["payload"]["model_selection_plan"]["selection"] == selected()
                context = service.read_node_context(sid, A_BINDING if stage == "A" else B_BINDING, **fence(service, sid))
                assert len(context["turns"]) == number
                message_ids = [message["message_id"] for message in context["messages"]]
                assert len(message_ids) == len(set(message_ids))
            assert public["observations"][0]["runId"] == observed["nodes"][0]["run_id"]
            assert public["observations"][0]["value"] == {"result_text": observed["nodes"][0]["result"]["text"]}
            assert public["observations"][1]["value"] == {"delivered_text": observed["nodes"][1]["result"]["text"]}
            if number == 1:
                save_catalog(service, 2)
                save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="later-draft"))
    assert [stage for stage, *_ in calls] == ["A", "B"] * 3
    assert all(body["model"] != "later-draft" for _, _, body in calls)


def test_candidate_fork_and_session_switch_matrix_keeps_formal_public_result_and_frozen_origin(database):
    calls = []
    with closing(WorkflowService(database, mode="deepseek", model_factory=provider_factory(calls))) as service:
        parent = service.create_session()["workflow_session_id"]
        save_catalog(service)
        save_model(service)
        service.save_exposure_configuration(record=configuration(), expected_revision=0, idempotency_key="declarations")
        with running_server(service) as port:
            _, original = post_turn(service, port, parent, 1)
        first = service.get_session(parent)
        original_nodes = deepcopy(first["observation"]["nodes"])
        service.reroll(parent, original["chain_run_id"], idempotency_key="reroll", **fence(service, parent))
        service.wait_for_idle(parent)
        view = service.get_session(parent)
        candidate = next(row for row in view["messages"][-1]["reply_candidates"]
                         if row["chain_run_id"] == original["chain_run_id"])
        service.select_candidate(parent, candidate["candidate_id"], idempotency_key="select", **fence(service, parent))
        selected_view = service.get_session(parent)
        assert selected_view["observation"]["nodes"] == original_nodes
        child = service.create_branch(
            parent, selected_view["messages"][-1]["visible_message_id"],
            expected_source_revision=selected_view["revision"], idempotency_key="fork",
        )["workflow_session_id"]
        selection = service.get_active_session()
        service.switch_session(child, idempotency_key="switch-child", expected_selection_revision=selection["revision"])
        inherited = service.get_session(child)
        assert inherited["model_selection"] == selected()
        assert inherited["observation"]["nodes"][0]["source_workflow_session_id"] == parent
        assert inherited["observation"]["nodes"][0]["result"] == original_nodes[0]["result"]
        save_catalog(service, 2)
        save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="parent-next"))
        with running_server(service) as port:
            post_turn(service, port, parent, 2, revision=2)
            before, count = persistent_data(database), len(calls)
            for sid in (parent, child, parent, child):
                status, public = request(
                    port, "GET", f"/api/sessions/{sid}/exposures/{configuration()['config_id']}/revisions/1",
                )
                assert status == 200 and public["workflow_session_id"] == sid
                if sid == child:
                    assert public["observations"][0]["value"] == {"result_text": original_nodes[0]["result"]["text"]}
            assert persistent_data(database) == before and len(calls) == count
            _, child_turn = post_turn(service, port, child, 1)
        assert service.get_session(child)["model_selection"] == selected()
        assert service.get_session(parent)["model_selection"] == selected(2)
        for frozen in frozen_for_chain(database, child_turn["chain_run_id"]):
            assert_selected(frozen, 1)
            assert frozen["config"]["payload"]["model_selection_plan"]["selection"] == selected()


def test_pause_refresh_resume_matrix_keeps_the_same_run_and_frozen_selection(database):
    calls, started, release = [], Event(), Event()
    first = True

    def handler(stage):
        nonlocal first
        if stage == "A" and first:
            first = False
            started.set()
            assert release.wait(60)

    with closing(WorkflowService(
        database, mode="deepseek", model_factory=provider_factory(calls, handler),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        save_model(service)
        receipt = service.submit(
            sid, "pause matrix", "start", prompt_selection=choice("A", "B"), model_selection=selected(),
        )
        try:
            assert started.wait(20)
            view = service.get_session(sid)
            run = view["nodes"][0]
            interrupt = accepted_interrupt(
                service, sid, run["run_id"], view["revision"], run["revision"], release, "pause",
            )
            assert interrupt["status"] == "pausing"
        finally:
            release.set()
            service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["observation"]["nodes"][0]["status"] == "paused", paused["error"]
        before, count = persistent_data(database), len(calls)
        for _ in range(3):
            assert service.get_session(sid)["observation"]["nodes"][0]["run_id"] == run["run_id"]
        assert persistent_data(database) == before and len(calls) == count
        save_catalog(service, 2)
        save_model(service, 2, changes=lambda record: record["nodes"][0]["parameters"].update(model="later-draft"))
        service.resume(
            sid, run["run_id"], idempotency_key="resume",
            expected_session_revision=paused["revision"], expected_run_revision=paused["nodes"][0]["revision"],
        )
        service.wait_for_idle(sid)
        finished = service.get_session(sid)
        assert finished["error"] is None and finished["can_submit"]
        assert finished["observation"]["nodes"][0]["run_id"] == run["run_id"]
        assert finished["model_selection"] == selected()
        for frozen in frozen_for_chain(database, receipt["chain_run_id"]):
            assert_selected(frozen, 1)
            assert frozen["config"]["payload"]["model_selection_plan"]["selection"] == selected()
        assert [stage for stage, *_ in calls] == ["A", "A", "B"]
        assert all(body["model"] == "deepseek-flash" for _, _, body in calls)
