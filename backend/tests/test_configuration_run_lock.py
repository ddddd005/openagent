"""G21: user configuration edits are locked while a run is unfinished.

The approved rule (118 号 Q21/G21) scopes the lock to the workflow definition
identity: while any persisted session of that definition still holds an active
chain (prepared/running/paused/failed/archive_failed and other not-closed
states), every new complete save or node-page narrow patch is rejected with one
shared business error.  Succeeded or explicitly closed runs release the lock.

These tests exercise the shared rule from both real configuration sources and
the end states the old tests previously assumed editable.  The former intent
"patch adjustable configuration while a run is active" is intentionally
replaced per G21; its original expectation remains described here as history.
"""

from contextlib import closing
from copy import deepcopy
from threading import Event
from uuid import uuid4

import pytest

from phase1_agent.capability_registry import create_package_registry
from phase1_agent.chat_contracts import CHAT_CONTEXT_TYPE
from phase1_agent.content_contracts import text_content
from phase1_agent.content_contracts import text_content
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.frontend_node_operations import FrontendNodeOperations
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService

from test_graph_service import document, edge, node, text_graph, uid


@pytest.fixture
def service(tmp_path):
    registry = create_package_registry().registry.detached()
    instance = GraphWorkflowService(tmp_path / "configuration-run-lock.sqlite", registry=registry)
    try:
        yield instance
    finally:
        instance.close()


def _rejected(action):
    with pytest.raises(ContractValidationError) as error:
        action()
    assert error.value.reason_code == "configuration_run_locked", error.value.reason_code
    assert error.value.status_code == 409
    return error.value


def _save(service, graph, key=None):
    return service.update_definition(
        graph, workflow_definition_id=graph["workflow_definition_id"],
        expected_edit_sequence=0, idempotency_key=key or str(uuid4()))


def _editable(service, graph):
    """Promote a schema-2 fixture into one complete schema-3 editable document."""
    graph["schema_version"] = 3
    graph.setdefault("package_lock", list(service.registry.execution_package_lock))
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    return graph


def _receipt(service, key):
    with closing(service._store()) as store:
        return store.read_receipt("graph.definition.update", key)


def _gated(service, *, save_key=None, before_start=None):
    """Save one fixed graph and start a run blocked inside its gate node."""
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "configuration run-lock gate timed out"
        return {"output": inputs["input"]}

    service.registry.register(NodeDefinition(
        "test.configuration-run-lock-gate", "1", "Run lock gate", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), gate)
    first = node(service.registry, "tools.text", 1)
    middle = node(service.registry, "test.configuration-run-lock-gate", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([first, middle, output], [edge(first, middle, 1), edge(middle, output, 2)])
    graph["schema_version"] = 3
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = _save(service, graph, save_key)
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    if before_start is not None:
        before_start(service, saved, view)
    started = service.start(
        view["workflow_session_id"], expected_revision=view["revision"],
        idempotency_key=str(uuid4()))
    assert entered.wait(5), "gated run did not reach its gate"
    return {
        "graph": graph, "saved": saved, "view": view, "started": started,
        "sid": view["workflow_session_id"], "chain_id": started["active_chain_run_id"],
        "release": release,
    }


def _finish_gated(service, gated):
    gated["release"].set()
    service.wait(gated["chain_id"])


def test_complete_save_is_locked_while_run_unfinished_and_writes_nothing(service):
    gated = _gated(service)
    try:
        identity = gated["saved"]["workflow_definition_id"]
        before = service.get_definition(identity)
        changed = deepcopy(before)
        changed["name"] = "rename while the run is active"
        key = str(uuid4())
        _rejected(lambda: service.update_definition(
            changed, workflow_definition_id=identity,
            expected_edit_sequence=before["edit_sequence"], idempotency_key=key))
        after = service.get_definition(identity)
        assert after["edit_sequence"] == before["edit_sequence"]
        assert after["name"] == before["name"]
        assert _receipt(service, key) is None
        session = service.get_session(gated["sid"])
        assert session["active_chain_run_id"] == gated["chain_id"]
    finally:
        _finish_gated(service, gated)


def test_no_change_submission_is_rejected_like_any_new_submission(service):
    # G21: "新配置提交统一拒绝，包括无变化提交" — the lock is not a no-op saver.
    gated = _gated(service)
    try:
        identity = gated["saved"]["workflow_definition_id"]
        current = service.get_definition(identity)
        key = str(uuid4())
        _rejected(lambda: service.update_definition(
            deepcopy(current), workflow_definition_id=identity,
            expected_edit_sequence=current["edit_sequence"], idempotency_key=key))
        assert _receipt(service, key) is None
    finally:
        _finish_gated(service, gated)


def test_exact_receipt_replay_is_allowed_while_run_is_locked(service):
    # G21: "原请求的精确回执重放、只读核实仍允许" — replay short-circuits before
    # the shared save rule, so it returns the original accepted result.
    gated = _gated(service, save_key="configuration-run-lock-initial")
    try:
        replay = service.update_definition(
            deepcopy(gated["graph"]),
            workflow_definition_id=gated["graph"]["workflow_definition_id"],
            expected_edit_sequence=0, idempotency_key="configuration-run-lock-initial")
        assert replay == gated["saved"]
    finally:
        _finish_gated(service, gated)


def test_confirmation_required_submission_is_locked_before_asking(service):
    # G21: a new submission is rejected while locked, including the submission
    # that would otherwise ask for fixed-change confirmation.  The same request
    # asks for confirmation once the run has finished (no definition write).
    gated = _gated(service)
    identity = gated["saved"]["workflow_definition_id"]
    current = service.get_definition(identity)
    proposed = deepcopy(current)
    proposed["nodes"][0]["config"]["text"] = "fixed change while locked"
    key = str(uuid4())
    try:
        _rejected(lambda: service.update_definition(
            deepcopy(proposed), workflow_definition_id=identity,
            expected_edit_sequence=current["edit_sequence"], idempotency_key=key))
        assert _receipt(service, key) is None
    finally:
        _finish_gated(service, gated)
    asked = service.update_definition(
        deepcopy(proposed), workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
    assert asked["outcome"] == "confirmation_required", asked
    assert service.get_definition(identity)["revision"] == current["revision"]


def test_succeeded_run_releases_the_lock(service):
    graph = _editable(service, text_graph(service.registry))
    saved = _save(service, graph)
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    started = service.start(view["workflow_session_id"], expected_revision=view["revision"],
                            idempotency_key=str(uuid4()))
    service.wait(started["active_chain_run_id"])
    finished = service.get_session(view["workflow_session_id"])
    assert finished["status"] == "succeeded", finished
    assert finished["active_chain_run_id"] is None
    current = service.get_definition(saved["workflow_definition_id"])
    renamed = deepcopy(current)
    renamed["name"] = "rename after success"
    saved_again = service.update_definition(
        renamed, workflow_definition_id=saved["workflow_definition_id"],
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
    assert saved_again["outcome"] == "saved", saved_again


def test_paused_run_stays_locked_until_close_then_allows_save(service):
    gated = _gated(service)
    try:
        running = service.get_session(gated["sid"])
        service.control(gated["sid"], action="pause", expected_revision=running["revision"],
                        idempotency_key=str(uuid4()))
    finally:
        gated["release"].set()
    service.wait(gated["chain_id"])
    paused = service.get_session(gated["sid"])
    assert paused["status"] == "paused", paused
    assert paused["active_chain_run_id"] == gated["chain_id"]
    identity = gated["saved"]["workflow_definition_id"]
    current = service.get_definition(identity)
    changed = deepcopy(current)
    changed["name"] = "rename while paused"
    _rejected(lambda: service.update_definition(
        changed, workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4())))
    assert service.get_definition(identity)["name"] == current["name"]

    service.control(gated["sid"], action="close", expected_revision=paused["revision"],
                    idempotency_key=str(uuid4()))
    closed = service.get_session(gated["sid"])
    assert closed["status"] == "closed", closed
    assert closed["active_chain_run_id"] is None
    reopened = deepcopy(current)
    reopened["name"] = "rename after close"
    result = service.update_definition(
        reopened, workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
    assert result["outcome"] == "saved", result
    assert service.get_definition(identity)["name"] == "rename after close"


def test_failed_unclosed_run_stays_locked_until_close(service):
    def fail(config, inputs, context):
        raise ValueError("configuration run-lock failure probe")

    service.registry.register(NodeDefinition(
        "test.configuration-run-lock-failure", "1", "Failure", "Test", {}, {"type": "object"},
        inputs=(NodePort("input", "TEXT", data_schema_version=2),),
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), fail)
    source = node(service.registry, "tools.text", 1)
    middle = node(service.registry, "test.configuration-run-lock-failure", 2)
    output = node(service.registry, "tools.output", 3)
    graph = document([source, middle, output], [edge(source, middle, 1), edge(middle, output, 2)])
    graph["schema_version"] = 3
    graph["package_lock"] = list(service.registry.execution_package_lock)
    for entry in graph["nodes"]:
        entry["configuration_policy"] = {"schema_version": 1, "prompt_mode": "fixed"}
    saved = _save(service, graph)
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    service.start(view["workflow_session_id"], expected_revision=view["revision"],
                  idempotency_key=str(uuid4()))
    service.wait(service.get_session(view["workflow_session_id"])["active_chain_run_id"])
    failed = service.get_session(view["workflow_session_id"])
    assert failed["status"] == "failed", failed
    assert failed["active_chain_run_id"] is not None
    identity = saved["workflow_definition_id"]
    current = service.get_definition(identity)
    changed = deepcopy(current)
    changed["name"] = "rename after failure without close"
    _rejected(lambda: service.update_definition(
        changed, workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4())))
    service.control(view["workflow_session_id"], action="close",
                    expected_revision=failed["revision"], idempotency_key=str(uuid4()))
    unlocked = deepcopy(current)
    unlocked["name"] = "rename after closing the failure"
    result = service.update_definition(
        unlocked, workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
    assert result["outcome"] == "saved", result


def test_lock_is_scoped_to_one_workflow_definition(service):
    gated = _gated(service)
    try:
        other = _editable(service, text_graph(service.registry))
        saved = _save(service, other)
        assert saved["outcome"] == "saved"
        current = service.get_definition(saved["workflow_definition_id"])
        renamed = deepcopy(current)
        renamed["name"] = "unrelated workflow edit"
        result = service.update_definition(
            renamed, workflow_definition_id=saved["workflow_definition_id"],
            expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
        assert result["outcome"] == "saved", result
    finally:
        _finish_gated(service, gated)


def test_second_run_of_same_definition_keeps_lock_after_first_succeeds(service):
    # G21: all runs under the definition must exit before the configuration unlocks.
    registry = service.registry
    gated = _gated(service)
    identity = gated["saved"]["workflow_definition_id"]
    second = service.create_session(identity, 1, idempotency_key=str(uuid4()))
    second_started = service.start(
        second["workflow_session_id"], expected_revision=second["revision"],
        idempotency_key=str(uuid4()))
    try:
        assert second_started["active_chain_run_id"] is not None
        current = service.get_definition(identity)
        changed = deepcopy(current)
        changed["name"] = "rename while two runs are unfinished"
        _rejected(lambda: service.update_definition(
            changed, workflow_definition_id=identity,
            expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4())))
    finally:
        gated["release"].set()
        for sid in (gated["sid"], second["workflow_session_id"]):
            for chain_id in [row["chain_run_id"] for row in service.get_session(sid)["chains"]]:
                try:
                    service.wait(chain_id)
                except Exception:
                    pass
    # Close both runs explicitly; the definition stays editable afterwards.
    for sid in (gated["sid"], second["workflow_session_id"]):
        session = service.get_session(sid)
        if session["active_chain_run_id"] is not None:
            service.control(sid, action="close", expected_revision=session["revision"],
                            idempotency_key=str(uuid4()))
    current = service.get_definition(identity)
    changed = deepcopy(current)
    changed["name"] = "rename after both runs closed"
    result = service.update_definition(
        changed, workflow_definition_id=identity,
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4()))
    assert result["outcome"] == "saved", result


def test_real_node_page_patch_is_locked_while_run_is_active(service):
    """The narrow node-page source obeys the same shared business rule."""
    entered, release = Event(), Event()

    def gate(config, inputs, context):
        entered.set()
        assert release.wait(10), "node-page run-lock gate timed out"
        return {"output": text_content("gate")}

    service.registry.register(NodeDefinition(
        "test.node-page-run-lock-gate", "1", "Gate", "Test", {}, {"type": "object"},
        outputs=(NodePort("output", "TEXT", data_schema_version=2),),
    ), gate)
    wait_node = node(service.registry, "test.node-page-run-lock-gate", 18001)
    source = node(service.registry, "tools.editable-input", 18002, text="initial")
    output = node(service.registry, "tools.output", 18003)
    frontend_entry = service.registry.get("frontend.node", "2")
    frontend = {
        "node_binding_id": uid(18004), "component_id": "frontend.node", "component_version": "2",
        "title": "frontend.node", "position": {"x": 0, "y": 200},
        "config": {
            **deepcopy(frontend_entry.definition.default_config),
            "object_key": "chat-0", "input_node_id": source["node_binding_id"],
        },
    }
    graph = document([wait_node, source, output, frontend], [
        edge(source, output, 18001),
        edge(source, frontend, 18002, target_port="content"),
    ])
    graph.update(
        schema_version=3, package_lock=list(service.registry.execution_package_lock),
        control_edges=[{
            "edge_id": str(uuid4()), "source_node_id": wait_node["node_binding_id"],
            "target_node_id": source["node_binding_id"],
        }],
        object_bindings=[{
            "object_key": "chat-0", "type_id": CHAT_CONTEXT_TYPE, "schema_version": 1,
            "scope": "shared", "readers": [frontend["node_binding_id"]],
            "writers": [frontend["node_binding_id"]], "owner_node_id": None,
            "registrar_node_id": frontend["node_binding_id"],
        }],
    )
    for entry in graph["nodes"]:
        entry["configuration_policy"] = deepcopy(service.registry.get(
            entry["component_id"], entry["component_version"],
        ).definition.to_dict()["default_configuration_policy"])
    saved = _save(service, graph)
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    sid = view["workflow_session_id"]
    started = service.start(sid, expected_revision=view["revision"], idempotency_key=str(uuid4()))
    try:
        assert entered.wait(5)
        bound = FrontendNodeOperations(service).bind(
            workflow_definition_id=saved["workflow_definition_id"],
            definition_revision=saved["definition_revision"],
            node_binding_id=frontend["node_binding_id"], session_id=sid)
        current = service.get_definition(saved["workflow_definition_id"])
        key = str(uuid4())
        _rejected(lambda: bound.patch_input(
            "locked input", expected_edit_sequence=current["edit_sequence"], idempotency_key=key))
        assert _text(service.get_definition(saved["workflow_definition_id"]),
                     source["node_binding_id"]) == "initial"
        with closing(service._store()) as store:
            assert store.read_receipt("graph.frontend-node.patch-input", key) is None
    finally:
        release.set()
    service.wait(started["active_chain_run_id"])
    finished = service.get_session(sid)
    import json
    assert finished["status"] == "succeeded", json.dumps(finished["chains"], ensure_ascii=False, indent=2)
    assert finished["active_chain_run_id"] is None

    bound = FrontendNodeOperations(service).bind(
        workflow_definition_id=saved["workflow_definition_id"],
        definition_revision=saved["definition_revision"],
        node_binding_id=frontend["node_binding_id"], session_id=sid)
    current = service.get_definition(saved["workflow_definition_id"])
    patched = bound.patch_input(
        "unlocked input", expected_edit_sequence=current["edit_sequence"],
        idempotency_key=str(uuid4()))
    assert patched["outcome"] == "saved", patched
    assert _text(service.get_definition(saved["workflow_definition_id"]),
                 source["node_binding_id"]) == "unlocked input"


def test_prepared_only_run_locks_new_configuration_saves(service, monkeypatch):
    """The prepared state (before the executor starts a node) already locks."""
    submitted = []
    monkeypatch.setattr(service._executor, "submit",
                        lambda *args, **kwargs: submitted.append(args) or None)
    graph = _editable(service, text_graph(service.registry))
    saved = _save(service, graph)
    view = service.create_session(
        saved["workflow_definition_id"], saved["definition_revision"],
        idempotency_key=str(uuid4()))
    started = service.start(
        view["workflow_session_id"], expected_revision=view["revision"],
        idempotency_key=str(uuid4()))
    assert submitted
    prepared = service.get_session(view["workflow_session_id"])
    assert prepared["status"] == "prepared", prepared["chains"]
    assert prepared["active_chain_run_id"] == started["active_chain_run_id"]
    current = service.get_definition(saved["workflow_definition_id"])
    changed = deepcopy(current)
    changed["name"] = "rename while prepared"
    _rejected(lambda: service.update_definition(
        changed, workflow_definition_id=saved["workflow_definition_id"],
        expected_edit_sequence=current["edit_sequence"], idempotency_key=str(uuid4())))
    assert service.get_definition(saved["workflow_definition_id"])["name"] == current["name"]


def test_confirmed_resubmission_after_confirmation_is_rejected_by_the_run_lock(service):
    """G21: confirmation cannot be used to bypass the source workflow's lock."""
    asked = {}

    def before_start(service, saved, view):
        changed = deepcopy(saved["document"])
        changed["nodes"][0]["config"]["text"] = "fixed change needing confirmation"
        asked["result"] = service.update_definition(
            changed, workflow_definition_id=saved["workflow_definition_id"],
            expected_edit_sequence=saved["edit_sequence"], idempotency_key=str(uuid4()))
        assert asked["result"]["outcome"] == "confirmation_required", asked["result"]

    gated = _gated(service, before_start=before_start)
    identity = gated["saved"]["workflow_definition_id"]
    try:
        document = deepcopy(asked["result"]["document"])
        key = str(uuid4())
        _rejected(lambda: service.update_definition(
            deepcopy(document), workflow_definition_id=identity,
            expected_edit_sequence=asked["result"]["edit_sequence"],
            idempotency_key=key, confirm_derivation=True))
        assert _receipt(service, key) is None
    finally:
        _finish_gated(service, gated)
    derived = service.update_definition(
        deepcopy(document), workflow_definition_id=identity,
        expected_edit_sequence=asked["result"]["edit_sequence"],
        idempotency_key=str(uuid4()), confirm_derivation=True)
    assert derived["outcome"] == "derived", derived
    assert derived["workflow_definition_id"] != identity
    assert service.get_definition(identity)["revision"] == gated["saved"]["definition_revision"]


def _text(document_value, node_binding_id):
    node_value = next(item for item in document_value["nodes"]
                      if item["node_binding_id"] == node_binding_id)
    return node_value["config"]["text"]
