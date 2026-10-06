"""Workflow variable plans freeze atomically and inherit stable selected values."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import content_digest
from phase1_agent.prepared_context import validate_frozen_preparation
from phase1_agent.prompt_preparation import make_prompt_context_config
from phase1_agent.prompt_variables import create_variable_registry, create_variable_snapshot
from phase1_agent.storage import SqliteStore
from phase1_agent.variable_preparation import (
    make_variable_assignment_plan, prepare_variable_assignments,
    rederive_root_variable_assignments,
)
from phase1_agent.variable_store import VariableStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService

from test_workflow_prepared_context import (
    PROFILE, collection, components, factory, macro, run_turn, snapshots, uid,
)
from test_workflow_prepared_control import (
    TrackedContext, frozen_for_chain, reroll, variant_factory,
)


@pytest.fixture
def tmp_path():
    directory = Path(__file__).resolve().parents[1] / "tmp"
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="workflow-variables-", dir=directory) as folder:
        yield Path(folder)


def constant(name, value, node):
    return {"node_id": node, "name": name, "source": {"kind": "constant", "value": value}}


def root(name="value", node="root-input"):
    return {"node_id": node, "name": name, "source": {"kind": "root_input_text"}}


def variable_configs(*, a_assignments=None, b_assignments=None):
    registry = create_variable_registry(
        workflow_id=PROFILE, revision=1, definitions=[
            {"name": "value", "type": "string", "default": "default-root"},
            {"name": "fixed", "type": "string", "default": "default-fixed"},
            {"name": "counter", "type": "integer", "default": 0},
        ],
    )
    variables = create_variable_snapshot(
        registry, workflow_session_id=uid(9900), node_binding_id=uid(9901),
    )
    a_assignments = [
        constant("counter", 1, "A-counter-first"),
        constant("counter", 2, "A-counter-last"),
        constant("fixed", "A-fixed", "A-fixed"),
        root(node="A-root"),
    ] if a_assignments is None else a_assignments
    b_assignments = [
        constant("fixed", "B-fixed", "B-fixed"),
        root(node="B-root"),
    ] if b_assignments is None else b_assignments
    return [
        make_prompt_context_config(
            collection("{{value}} / {{fixed}} / {{counter}}"), steps=[macro()],
            variables=variables, variable_plan=make_variable_assignment_plan(assignments),
        )
        for assignments in (a_assignments, b_assignments)
    ]


def selected_components(*, context=None, a_assignments=None, b_assignments=None):
    a_config, b_config = variable_configs(
        a_assignments=a_assignments, b_assignments=b_assignments,
    )
    selected = components(a_config, context=context)
    b_selection = selected.contexts["B"]
    selected.contexts["B"] = replace(b_selection, config={
        **deepcopy(b_selection.config), "payload": b_config,
    })
    return selected


def durable(path):
    with closing(SqliteStore(path)) as store:
        return store.read_bundle()


def current(path, sid):
    with closing(SqliteStore(path)) as store:
        return VariableStore(store).get_current(
            workflow_session_id=sid, workflow_id=PROFILE, registry_revision=1,
        )


def selected_variable_state(path, sid):
    with closing(SqliteStore(path)) as store:
        head = next(row for row in store.list_records("workflow_ref")
                    if row["workflow_session_id"] == sid)
        variables = VariableStore(store)
        reference = variables.get_bound_state_ref("workflow_commit", head["head_commit_id"])
        return variables.get_state(reference)


def variable_counts(path):
    with closing(SqliteStore(path)) as store:
        return {
            table: store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "variable_registries", "variable_states", "variable_heads",
                "variable_preparations", "variable_bindings",
            )
        }


def variable_evidence(snapshot):
    return snapshot["config"]["payload"]["variable_preparation"]


def refs_equal_variables(path, frozen):
    evidence = variable_evidence(frozen)
    with closing(SqliteStore(path)) as store:
        variables = VariableStore(store)
        assert variables.get_bound_state_ref(
            "input_snapshot", frozen["snapshot_id"],
        ) == evidence["state_ref"]
        saved = variables.read_snapshot(evidence["state_ref"], node_binding_id=frozen["node_binding_id"])
        assert saved == frozen["config"]["payload"]["context"]["variables"]
        receipt = variables.read_preparation(evidence["receipt_key"])
        assert receipt["after"]["revision"] == evidence["state_ref"]["revision"]
        return receipt


def test_a_b_share_registry_and_session_values_but_presets_are_per_consumer(tmp_path):
    calls, path = [], tmp_path / "shared.sqlite"
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "first root", "first")
        frozen = snapshots(path)
        a_snapshot = next(row for row in frozen if row["node_binding_id"] == A_BINDING)
        b_snapshot = next(row for row in frozen if row["node_binding_id"] == B_BINDING)
        a_variables = a_snapshot["config"]["payload"]["context"]["variables"]
        b_variables = b_snapshot["config"]["payload"]["context"]["variables"]
        assert a_variables["registry"] == b_variables["registry"]
        assert a_variables["workflow_session_id"] == b_variables["workflow_session_id"] == sid
        assert a_variables["node_binding_id"] == A_BINDING
        assert b_variables["values"]["node_binding_id"]["value"] == B_BINDING
        assert a_variables["values"]["counter"]["value"] == 2
        assert b_variables["values"]["counter"]["value"] == 2
        assert a_variables["values"]["fixed"]["value"] == "A-fixed"
        assert b_variables["values"]["fixed"]["value"] == "B-fixed"
        assert a_variables["values"]["value"]["value"] == "first root"
        assert b_variables["values"]["value"]["value"] == "first rootA"
        assert variable_evidence(b_snapshot)["frozen"]["basis_snapshot"]["values"]["counter"]["value"] == 2
        a_receipt = refs_equal_variables(path, a_snapshot)
        b_receipt = refs_equal_variables(path, b_snapshot)
        assert [entry["value"] for entry in a_receipt["request"]["assignments"][:2]] == [1, 2]
        assert b_receipt["before"] == a_receipt["after"]
        assert current(path, sid)["values"] == b_receipt["after"]["values"]
        assert calls[0][1][0]["blocks"][0]["text"] == "first root / A-fixed / 2"
        assert calls[1][1][0]["blocks"][0]["text"] == "first rootA / B-fixed / 2"
        counts = variable_counts(path)
        assert counts["variable_registries"] == 1
        assert counts["variable_heads"] == 1
        assert counts["variable_states"] == counts["variable_preparations"] == 3


def test_same_submit_key_replays_receipt_without_reassignment_or_new_dispatch(tmp_path):
    calls, path = [], tmp_path / "replay.sqlite"
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        first = service.submit(sid, "same", "same-key")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        before, before_counts = durable(path), variable_counts(path)
        assert service.submit(sid, "same", "same-key") == first
        service.wait_for_idle(sid)
        assert durable(path) == before
        assert variable_counts(path) == before_counts
        assert len(calls) == 2
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=lambda _: pytest.fail("Replay dispatched"),
    )) as service:
        assert service.submit(sid, "same", "same-key") == first
        assert variable_counts(path) == before_counts


@pytest.mark.parametrize("point", [
    "variable_after_state_write", "after_first_write",
    "variable_after_binding_write", "before_commit",
])
def test_initial_preparation_fault_rolls_back_all_variables_and_start_records(tmp_path, point):
    armed = False

    def fault(observed):
        if armed and observed == point:
            raise RuntimeError("workflow variable preparation failure")

    calls, path = [], tmp_path / f"fault-{point}.sqlite"
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=factory(calls), fault_injector=fault,
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        before, before_counts = durable(path), variable_counts(path)
        armed = True
        with pytest.raises(RuntimeError, match="workflow variable preparation failure"):
            service.submit(sid, "nothing dispatched", "fault")
        assert calls == []
        assert durable(path) == before
        assert variable_counts(path) == before_counts
        assert snapshots(path) == []
        armed = False
        run_turn(service, sid, "nothing dispatched", "fault")
        assert len(calls) == 2
        assert variable_counts(path)["variable_states"] == 3


def test_reroll_original_a_is_not_prepared_again_and_new_b_uses_only_frozen_root_rules(tmp_path):
    calls, path, context = [], tmp_path / "reroll.sqlite", TrackedContext()
    with closing(WorkflowService(
        path, components=selected_components(context=context),
        model_factory=variant_factory(calls, "first A", "new A"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        first = service.submit(sid, "root", "original")
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        old_a, old_b = frozen_for_chain(path, first["chain_run_id"])
        old_b_values = old_b["config"]["payload"]["context"]["variables"]
        before_prepares = list(context.prepares)
        with closing(SqliteStore(path)) as store:
            variables = VariableStore(store)
            head = current(path, sid)
            variables.prepare(
                workflow_session_id=sid, workflow_id=PROFILE, registry_revision=1,
                expected_revision=head["revision"], idempotency_key="fixture-live-edit",
                assignments=[
                    {"node_id": "edit-counter", "name": "counter", "value": 999},
                    {"node_id": "edit-fixed", "name": "fixed", "value": "live drift"},
                ],
            )
        live_context, kernel, adapter, mutable = service._resolved["B"]
        changed = deepcopy(mutable)
        changed["payload"]["context"]["variable_plan"]["assignments"][0]["source"]["value"] = "new constant"
        service._resolved["B"] = live_context, kernel, adapter, changed
        replacement = reroll(service, sid, "replace")
        new_a, new_b = frozen_for_chain(path, replacement["chain_run_id"])
        assert new_a["s0"] == old_a["s0"]
        assert variable_evidence(new_a) == variable_evidence(old_a)
        assert context.prepares == [*before_prepares, B_BINDING]
        new_values = new_b["config"]["payload"]["context"]["variables"]
        assert new_values["values"]["value"]["value"] == "new A"
        assert new_values["values"]["fixed"] == old_b_values["values"]["fixed"]
        assert new_values["values"]["counter"] == old_b_values["values"]["counter"]
        assert new_values["values"]["fixed"]["value"] == "B-fixed"
        assert new_values["values"]["counter"]["value"] == 2
        receipt = refs_equal_variables(path, new_b)
        assert receipt["request"]["operation"] == "prepare_snapshot"
        assert receipt["after"]["source"]["kind"] == "frozen_snapshot"
        assert variable_evidence(new_b)["rederivation"]["assignments"] == [
            {"node_id": "B-root", "name": "value", "value": "new A"},
        ]
        assert variable_counts(path)["variable_states"] == 5


def test_later_b_constant_masks_root_dependency_on_reroll(tmp_path):
    calls, path = [], tmp_path / "masked.sqlite"
    selected = selected_components(b_assignments=[
        root(node="B-root"), constant("value", "masked root", "B-value-final"),
    ])
    with closing(WorkflowService(
        path, components=selected, model_factory=variant_factory(calls, "first A", "new A"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "root")
        replacement = reroll(service, sid)
        _, new_b = frozen_for_chain(path, replacement["chain_run_id"])
        evidence = variable_evidence(new_b)
        assert evidence["frozen"]["dependencies"][0]["active"] is False
        assert evidence["rederivation"]["assignments"] == []
        assert new_b["config"]["payload"]["context"]["variables"]["values"]["value"]["value"] == "masked root"


@pytest.mark.parametrize("replacement", [False, True])
def test_saved_masked_rule_proof_cannot_be_forged_even_when_final_values_match(
    tmp_path, replacement,
):
    calls, path = [], tmp_path / "masked-proof.sqlite"
    selected = selected_components(b_assignments=[
        root(node="B-root"), constant("value", "masked root", "B-value-final"),
    ])
    with closing(WorkflowService(
        path, components=selected, model_factory=variant_factory(calls, "first A", "new A"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "root")
        chain_id = (reroll(service, sid)["chain_run_id"] if replacement
                    else durable(path)["chain_run"][0]["chain_run_id"])
        _, original = frozen_for_chain(path, chain_id)
        changed = deepcopy(original)
        payload = changed["config"]["payload"]
        evidence = payload["variable_preparation"]
        old_frozen = evidence["frozen"]
        forged_plan = deepcopy(old_frozen["plan"])
        forged_plan["assignments"][0]["source"] = {
            "kind": "constant", "value": "forged but covered value",
        }
        evidence["frozen"] = prepare_variable_assignments(
            old_frozen["basis_snapshot"], forged_plan, old_frozen["node_input"],
        )
        assert evidence["frozen"]["snapshot"] == old_frozen["snapshot"]
        if evidence["rederivation"] is not None:
            evidence["rederivation"] = rederive_root_variable_assignments(
                evidence["frozen"], evidence["rederivation"]["node_input"],
                workflow_session_id=changed["workflow_session_id"],
                node_binding_id=changed["node_binding_id"],
            )
        payload["context"]["variable_plan"] = forged_plan
        preparation = payload["context_preparation"]
        preparation["config"]["variable_plan"] = deepcopy(forged_plan)
        preparation["evidence_digest"] = content_digest({
            key: value for key, value in preparation.items() if key != "evidence_digest"
        })
        assert validate_frozen_preparation(changed) is not None
        before, before_variables, before_calls = durable(path), variable_counts(path), deepcopy(calls)
        with pytest.raises(ContractValidationError, match="durable preparation proof"):
            service._snapshot_components(changed)
        assert durable(path) == before
        assert variable_counts(path) == before_variables
        assert calls == before_calls


def test_historical_fork_copies_anchor_values_not_latest_then_is_independent(tmp_path):
    calls, path = [], tmp_path / "fork.sqlite"
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=factory(calls),
    )) as service:
        parent = service.create_session()["workflow_session_id"]
        run_turn(service, parent, "first root", "first")
        first_reply = service.get_session(parent)["messages"][-1]
        anchored = deepcopy(current(path, parent))
        run_turn(service, parent, "latest root", "second")
        latest = current(path, parent)
        assert anchored["values"]["value"]["value"] != latest["values"]["value"]["value"]
        before = durable(path)
        child = service.create_branch(
            parent, first_reply["visible_message_id"], idempotency_key="historical-fork",
            expected_source_revision=service.get_session(parent)["revision"],
        )["workflow_session_id"]
        inherited = current(path, child)
        assert inherited is not None
        assert inherited["values"] == anchored["values"]
        assert inherited["values"] != latest["values"]
        child_state = service.get_session(child)
        with closing(SqliteStore(path)) as store:
            variables = VariableStore(store)
            head_ref = next(row for row in store.list_records("workflow_ref")
                            if row["workflow_session_id"] == child)
            bound = variables.get_bound_state_ref("workflow_commit", head_ref["head_commit_id"])
            assert bound["workflow_session_id"] == child
            assert variables.get_state(bound) == inherited
            source_anchor = store.get_record("workflow_session", {"workflow_session_id": child})[
                "source"
            ]["fork_anchor_id"]
            anchored_ref = variables.get_bound_state_ref("fork_anchor", source_anchor)
            assert variables.get_state(anchored_ref)["values"] == anchored["values"]
        run_turn(service, child, "child root", "child-next")
        assert current(path, child)["values"]["value"]["value"] == "child rootA"
        assert current(path, parent) == latest
        assert all(row in durable(path)["turn"] for row in before["turn"])
        assert child_state["messages"] == service.get_session(parent)["messages"][:2]


def test_inherited_candidate_selection_replays_reopens_and_next_a_uses_selected_values(tmp_path):
    calls, path = [], tmp_path / "inherited-selection.sqlite"
    models = variant_factory(calls, "original A", "rolled A", "child continued")
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=models,
    )) as service:
        parent = service.create_session()["workflow_session_id"]
        run_turn(service, parent, "root", "original")
        original_candidate = durable(path)["workflow_candidate"][0]
        original_values = deepcopy(current(path, parent)["values"])
        reroll(service, parent, "second")
        parent_values = deepcopy(current(path, parent)["values"])
        view = service.get_session(parent)
        child = service.create_branch(
            parent, view["messages"][-1]["visible_message_id"],
            idempotency_key="fork", expected_source_revision=view["revision"],
        )["workflow_session_id"]
        assert current(path, child)["values"] == parent_values
        child_view = service.get_session(child)
        request = {
            "idempotency_key": "select-inherited",
            "expected_session_revision": child_view["revision"],
            "expected_ref_revision": child_view["ref_revision"],
            "expected_head_commit_id": child_view["head_commit_id"],
        }
        receipt = service.select_candidate(child, original_candidate["candidate_id"], **request)
        assert selected_variable_state(path, child)["values"] == original_values
        assert current(path, parent)["values"] == parent_values
        saved, counts = durable(path), variable_counts(path)
        assert service.select_candidate(child, original_candidate["candidate_id"], **request) == receipt
        assert durable(path) == saved
        assert variable_counts(path) == counts
        assert len(calls) == 4
    with closing(WorkflowService(
        path, components=selected_components(), model_factory=models,
    )) as service:
        assert service.select_candidate(child, original_candidate["candidate_id"], **request) == receipt
        assert variable_counts(path) == counts
        with closing(SqliteStore(path)) as store:
            head = current(path, child)
            VariableStore(store).prepare(
                workflow_session_id=child, workflow_id=PROFILE, registry_revision=1,
                expected_revision=head["revision"], idempotency_key="child-unselected-live-edit",
                assignments=[
                    {"node_id": "drift", "name": "value", "value": "unselected live value"},
                    {"node_id": "drift-counter", "name": "counter", "value": 999},
                ],
            )
        continued = service.submit(child, "child root", "child-next")
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        new_a, _new_b = frozen_for_chain(path, continued["chain_run_id"])
        basis = variable_evidence(new_a)["frozen"]["basis_snapshot"]["values"]
        assert {name: basis[name] for name in original_values} == original_values
        assert current(path, parent)["values"] == parent_values
        assert original_candidate in durable(path)["workflow_candidate"]


def test_second_fork_of_specific_ancestor_candidate_keeps_each_selected_variable_basis(tmp_path):
    calls, path = [], tmp_path / "second-fork.sqlite"
    with closing(WorkflowService(
        path, components=selected_components(),
        model_factory=variant_factory(
            calls, "original A", "rolled A", "child continued", "grandchild continued",
        ),
    )) as service:
        parent = service.create_session()["workflow_session_id"]
        run_turn(service, parent, "root", "original")
        original_candidate = durable(path)["workflow_candidate"][0]
        original_values = deepcopy(current(path, parent)["values"])
        reroll(service, parent, "second")
        rolled_values = deepcopy(current(path, parent)["values"])
        parent_floors = service.list_reply_candidate_floors(parent)
        view = service.get_session(parent)
        child = service.create_branch(
            parent, view["messages"][-1]["visible_message_id"],
            idempotency_key="first-fork", expected_source_revision=view["revision"],
        )["workflow_session_id"]
        child_view = service.get_session(child)
        grandchild = service.create_branch(
            child, child_view["messages"][-1]["visible_message_id"],
            candidate_id=original_candidate["candidate_id"],
            idempotency_key="second-specific-fork",
            expected_source_revision=child_view["revision"],
        )["workflow_session_id"]
        assert current(path, grandchild)["values"] == original_values
        assert selected_variable_state(path, grandchild)["values"] == original_values
        assert selected_variable_state(path, child)["values"] == rolled_values
        assert service.list_reply_candidate_floors(child) == parent_floors
        inherited = service.list_reply_candidate_floors(grandchild)[0]
        assert inherited["candidate_ids"] == parent_floors[0]["candidate_ids"]
        assert inherited["selected_candidate_id"] == original_candidate["candidate_id"]
        assert len(calls) == 4
        child_submit = service.submit(child, "child root", "child-next")
        service.wait_for_idle(child)
        assert service.get_session(child)["error"] is None
        child_a, _ = frozen_for_chain(path, child_submit["chain_run_id"])
        child_basis = variable_evidence(child_a)["frozen"]["basis_snapshot"]["values"]
        assert {name: child_basis[name] for name in rolled_values} == rolled_values
        grandchild_submit = service.submit(grandchild, "grandchild root", "grandchild-next")
        service.wait_for_idle(grandchild)
        assert service.get_session(grandchild)["error"] is None
        grandchild_a, _ = frozen_for_chain(path, grandchild_submit["chain_run_id"])
        grandchild_basis = variable_evidence(grandchild_a)["frozen"]["basis_snapshot"]["values"]
        assert {name: grandchild_basis[name] for name in original_values} == original_values
        assert current(path, parent)["values"] == rolled_values
        assert len(durable(path)["workflow_candidate"]) == 4


def test_grandchild_inherited_reroll_only_rederives_b_root_and_uses_ancestor_frozen_values(tmp_path):
    calls, path, context = [], tmp_path / "grandchild-reroll.sqlite", TrackedContext()
    with closing(WorkflowService(
        path, components=selected_components(context=context),
        model_factory=variant_factory(calls, "original A", "rolled A", "grandchild new A"),
    )) as service:
        parent = service.create_session()["workflow_session_id"]
        first = service.submit(parent, "root", "original")
        service.wait_for_idle(parent)
        assert service.get_session(parent)["error"] is None
        original_candidate = durable(path)["workflow_candidate"][0]
        original_a, original_b = frozen_for_chain(path, first["chain_run_id"])
        reroll(service, parent, "second")
        parent_values = deepcopy(current(path, parent)["values"])
        view = service.get_session(parent)
        child = service.create_branch(
            parent, view["messages"][-1]["visible_message_id"],
            idempotency_key="first-fork", expected_source_revision=view["revision"],
        )["workflow_session_id"]
        child_view = service.get_session(child)
        grandchild = service.create_branch(
            child, child_view["messages"][-1]["visible_message_id"],
            candidate_id=original_candidate["candidate_id"],
            idempotency_key="second-specific-fork",
            expected_source_revision=child_view["revision"],
        )["workflow_session_id"]
        with closing(SqliteStore(path)) as store:
            head = current(path, grandchild)
            VariableStore(store).prepare(
                workflow_session_id=grandchild, workflow_id=PROFILE, registry_revision=1,
                expected_revision=head["revision"], idempotency_key="grandchild-live-edit",
                assignments=[
                    {"node_id": "drift", "name": "counter", "value": 999},
                    {"node_id": "drift-fixed", "name": "fixed", "value": "unselected live fixed"},
                ],
            )
        prepares_before = list(context.prepares)
        replacement = reroll(service, grandchild, "grandchild-reroll")
        new_a, new_b = frozen_for_chain(path, replacement["chain_run_id"])
        assert new_a["s0"] == original_a["s0"]
        assert variable_evidence(new_a) == variable_evidence(original_a)
        assert context.prepares == [*prepares_before, B_BINDING]
        assert new_b["workflow_session_id"] == grandchild
        new_variables = new_b["config"]["payload"]["context"]["variables"]
        assert new_variables["workflow_session_id"] == grandchild
        assert new_variables["values"]["workflow_session_id"]["value"] == grandchild
        assert new_variables["values"]["value"]["value"] == "grandchild new A"
        frozen_values = original_b["config"]["payload"]["context"]["variables"]["values"]
        assert new_variables["values"]["counter"] == frozen_values["counter"]
        assert new_variables["values"]["fixed"] == frozen_values["fixed"]
        receipt = refs_equal_variables(path, new_b)
        assert receipt["request"]["source_ref"] == variable_evidence(original_b)["state_ref"]
        assert current(path, parent)["values"] == parent_values
        assert selected_variable_state(path, child)["values"] == parent_values
        assert len(service.list_reply_candidate_floors(grandchild)[0]["candidate_ids"]) == 3


def test_static_v1_variable_config_does_not_create_persistent_variable_catalog(tmp_path):
    config = variable_configs()[0]
    config["schema_version"] = 1
    config.pop("variable_plan")
    calls, path = [], tmp_path / "static.sqlite"
    with closing(WorkflowService(
        path, components=components(config), model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid)
        assert all(value == 0 for value in variable_counts(path).values())
        assert all("variable_preparation" not in row["config"]["payload"] for row in snapshots(path))
        assert len(calls) == 2
