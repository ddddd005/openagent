"""Registered variables use explicit HTTP events, reads, saves, and fork values."""

from copy import deepcopy
from uuid import uuid4

from phase1_agent.host_sdk import ObjectBinding
from phase1_agent.runtime_checkpoints import CHECKPOINT_SCHEMA_VERSION
from phase1_agent.variable_contract import VARIABLE_SCHEMA_VERSION, VARIABLE_TYPE

from test_graph_fresh_http import command, query, request, serve
from test_stage4_model_request_installation import _edge, _node
from test_success_checkpoints import checkpoint_history


def _event(provider):
    return {
        "event_id": "tools.variable-register/" + provider["node_binding_id"],
        "schema_version": 1, "display_name": "Register variable", "audience": "management",
        "target_node_ids": [provider["node_binding_id"]],
        "payload_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def _control(source, target):
    return {"edge_id": str(uuid4()), "source_node_id": source["node_binding_id"],
            "target_node_id": target["node_binding_id"]}


def _document(service):
    first = _node(service, "tools.variable-register", "2", name="count", value_type="integer",
                  has_initial=True, initial_value=0)
    second = _node(service, "tools.variable-register", "2", name="flag", value_type="boolean",
                   has_initial=True, initial_value=False)
    count_key, flag_key = ["variable-" + node["node_binding_id"] for node in (first, second)]
    first["config"]["object_key"], second["config"]["object_key"] = count_key, flag_key
    before = _node(service, "tools.variable-to-content", "1", object_key=count_key, mode="json")
    before["public_outputs"] = ["output"]
    assign = _node(service, "tools.variable-assign", "1", object_key=count_key, operation="add", value=1)
    after = _node(service, "tools.variable-to-content", "1", object_key=count_key, mode="json")
    flag = _node(service, "tools.variable-to-content", "1", object_key=flag_key)
    count_output = _node(service, "tools.output", "1", mode="json")
    flag_output = _node(service, "tools.output", "1")
    nodes = [first, second, before, assign, after, flag, count_output, flag_output]
    bindings = [
        ObjectBinding(
            count_key, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
            readers=tuple(node["node_binding_id"] for node in (first, before, assign, after)),
            writers=tuple(node["node_binding_id"] for node in (first, assign)),
            registrar_node_id=first["node_binding_id"],
        ).to_dict(),
        ObjectBinding(
            flag_key, VARIABLE_TYPE, VARIABLE_SCHEMA_VERSION, "shared",
            readers=tuple(node["node_binding_id"] for node in (second, flag)),
            writers=(second["node_binding_id"],), registrar_node_id=second["node_binding_id"],
        ).to_dict(),
    ]
    return {
        "schema_version": 3, "workflow_definition_id": str(uuid4()), "revision": 1,
        "name": "Explicit variable HTTP acceptance", "nodes": nodes,
        "edges": [_edge(after, count_output), _edge(flag, flag_output)],
        "control_edges": [_control(before, assign), _control(assign, after)],
        "object_bindings": bindings, "event_bindings": [_event(first), _event(second)],
        "package_lock": deepcopy(list(service.registry.execution_package_lock)),
    }, {"first": first, "second": second, "before": before, "after": after, "flag": flag,
        "count_key": count_key, "flag_key": flag_key}


def _accept(port, operation, expected_status, **parameters):
    status, packet = command(port, operation, **parameters)
    assert status == expected_status, packet
    assert packet["receipt"]["operation"] == operation
    assert packet["receipt"]["idempotency_key"] == parameters["idempotency_key"]
    return packet["result"]


def _create(port, document):
    return _accept(port, "session.create", 201,
                   workflow_definition_id=document["workflow_definition_id"],
                   definition_revision=document["revision"], idempotency_key=str(uuid4()))


def _register(host, port, session, document, provider):
    parameters = {
        "session_id": session["workflow_session_id"],
        "workflow_definition_id": document["workflow_definition_id"],
        "definition_revision": document["revision"],
        "event_id": _event(provider)["event_id"], "event_schema_version": 1, "payload": {},
        "expected_revision": session["revision"], "idempotency_key": str(uuid4()),
    }
    accepted = _accept(port, "event.submit", 202, **parameters)
    chain = accepted["active_chain_run_id"]
    host.graph_service.wait(chain)
    event = query(port, "event.read", session_id=session["workflow_session_id"], chain_id=chain)
    assert event["status"] == "succeeded", event
    assert event["ordered_nodes"] == [provider["node_binding_id"]]
    assert event["completed_nodes"] == event["ordered_nodes"]
    assert query(port, "checkpoint.list", session_id=session["workflow_session_id"]) == []
    status, receipt = request(
        port, "/api/graph/queries", "receipt.read",
        {"operation": "event.submit", "parameters": parameters},
    )
    assert status == 200 and receipt["outcome"] == "matched"
    return query(port, "session.read", session_id=session["workflow_session_id"])


def _run(host, port, session):
    started = _accept(port, "run.start", 202, session_id=session["workflow_session_id"],
                      expected_revision=session["revision"], idempotency_key=str(uuid4()))
    host.graph_service.wait(started["active_chain_run_id"])
    current = query(port, "session.read", session_id=session["workflow_session_id"])
    assert current["status"] == "succeeded", current["chains"]
    return current


def _output(session, node):
    return next(row["outputs"]["output"] for row in session["nodes"]
                if row["node_binding_id"] == node["node_binding_id"])


def test_http_variable_events_isolate_items_and_sessions_and_preserve_read_values(tmp_path):
    with serve(tmp_path / "variables-http.sqlite") as (host, port):
        document, branch = _document(host.graph_service)
        saved = _accept(
            port, "definition.update", 200,
            workflow_definition_id=document["workflow_definition_id"], document=document,
            expected_edit_sequence=0, idempotency_key=str(uuid4()),
        )
        assert saved["outcome"] == "saved"
        assert query(port, "definition.read", identity=document["workflow_definition_id"]) == saved["document"]
        source, other = _create(port, document), _create(port, document)
        other_before = deepcopy(other)
        empty_second = deepcopy(source["objects"][branch["flag_key"]])
        source = _register(host, port, source, document, branch["first"])
        assert source["objects"][branch["count_key"]]["value"]["value"] == 0
        assert source["objects"][branch["flag_key"]] == empty_second
        assert query(port, "session.read", session_id=other["workflow_session_id"]) == other_before
        source = _register(host, port, source, document, branch["second"])
        assert source["objects"][branch["flag_key"]]["value"]["value"] is False
        first = _run(host, port, source)
        assert _output(first, branch["before"])["value"] == 0
        assert _output(first, branch["after"])["value"] == 1
        assert _output(first, branch["flag"])["text"] == "false"
        chain = query(port, "run.read", session_id=first["workflow_session_id"],
                      chain_id=first["selected_chain_run_id"])["chain"]
        providers = {branch[name]["node_binding_id"] for name in ("first", "second")}
        assert not providers.intersection(chain["ordered_nodes"])
        assert chain["ordered_nodes"] == chain["completed_nodes"]
        assert query(port, "session.read", session_id=other["workflow_session_id"]) == other_before
        frozen = deepcopy(first)
        second = _run(host, port, first)
        assert first == frozen
        assert _output(second, branch["before"])["value"] == 1
        assert _output(second, branch["after"])["value"] == 2


def test_http_selected_checkpoint_initializes_fork_and_repeat_registration_never_resets(tmp_path):
    database = tmp_path / "variable-fork-http.sqlite"
    with serve(database) as (host, port):
        document, branch = _document(host.graph_service)
        _accept(
            port, "definition.update", 200,
            workflow_definition_id=document["workflow_definition_id"], document=document,
            expected_edit_sequence=0, idempotency_key=str(uuid4()),
        )
        source = _create(port, document)
        source = _register(host, port, source, document, branch["first"])
        source = _register(host, port, source, document, branch["second"])
        first = _run(host, port, source)
        first_history = checkpoint_history(host.graph_service, first)
        saved_state = deepcopy(first_history["state"])
        assert first_history["latest"]["schema_version"] == CHECKPOINT_SCHEMA_VERSION
        assert saved_state["variables"][branch["count_key"]]["value"]["value"] == 1
        assert saved_state["variables"][branch["flag_key"]]["value"]["value"] is False
        source = _run(host, port, first)
        source_before = deepcopy(source)
        fork = _accept(
            port, "checkpoint.fork", 201, session_id=source["workflow_session_id"],
            checkpoint_id=first_history["latest"]["checkpoint_id"],
            expected_session_revision=source["revision"],
            expected_data_revision=source["data_revision"],
            expected_head_revision=source["head_revision"], idempotency_key=str(uuid4()),
        )
        target = query(port, "session.read", session_id=fork["target_session_id"])
        assert target["objects"][branch["count_key"]]["value"]["value"] == 1
        assert target["objects"][branch["flag_key"]]["value"]["value"] is False
        assert checkpoint_history(host.graph_service, target)["state"] == saved_state
        target_value = deepcopy(target["objects"][branch["count_key"]])
        parameters = {
            "session_id": target["workflow_session_id"],
            "workflow_definition_id": document["workflow_definition_id"], "definition_revision": 1,
            "event_id": _event(branch["first"])["event_id"], "event_schema_version": 1,
            "payload": {}, "expected_revision": target["revision"], "idempotency_key": str(uuid4()),
        }
        accepted = _accept(port, "event.submit", 202, **parameters)
        host.graph_service.wait(accepted["active_chain_run_id"])
        target = query(port, "session.read", session_id=target["workflow_session_id"])
        assert target["objects"][branch["count_key"]] == target_value
        assert len(query(port, "checkpoint.list", session_id=target["workflow_session_id"])) == 1
        target = _run(host, port, target)
        assert _output(target, branch["before"])["value"] == 1
        assert target["objects"][branch["count_key"]]["value"]["value"] == 2
        assert len(query(port, "checkpoint.list", session_id=target["workflow_session_id"])) == 2
        assert query(port, "session.read", session_id=source["workflow_session_id"]) == source_before
        assert checkpoint_history(host.graph_service, first)["path"][0] == first_history["path"][0]
    with serve(database) as (host, port):
        assert host._graph is None
        assert query(port, "session.read", session_id=source["workflow_session_id"]) == source_before
        assert query(port, "session.read", session_id=target["workflow_session_id"])["objects"] == target["objects"]
        assert host._graph is None
