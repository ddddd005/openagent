"""Exact public choices freeze before dispatch and survive later catalog edits."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, dumps_pretty, loads_strict
from phase1_agent.prepared_context import validate_frozen_preparation
from phase1_agent.prompt_preparation import validate_prompt_context_config
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService

from test_server_prompt_configs import request, running_server
from test_prompt_preparation import (
    context_regex as configured_context_regex, lorebook as configured_lorebook,
    regex as configured_regex,
)
from test_workflow_prepared_context import bundle, factory, run_turn, snapshots, uid
from test_workflow_prepared_control import (
    VariantModel, frozen_for_chain, reroll, variant_factory,
)
from test_workflow_variable_preparation import (
    selected_components as variable_components, variable_counts,
)


CONFIG = uid(8200)
ITEM_IDS = [uid(8100 + index) for index in range(3)]
ROLES = ("system", "user", "assistant")


@pytest.fixture
def database():
    directory = Path(__file__).resolve().parents[1] / "tmp"
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="prompt-selection-", dir=directory) as folder:
        yield Path(folder) / "workflow.sqlite"


def catalog_records(revision=1):
    items = [{
        "schema_version": 1, "kind": "item", "item_id": identity,
        "revision": revision, "name": "A title not sent: " + role,
        "text": f"catalog:r{revision}:{role}", "role": role, "enabled": True,
        "placement": placement, "depth": 0 if placement == "middle" else None,
        "order": index, "interpolation": "literal",
        "source": {"kind": "configuration"},
    } for index, (identity, role, placement) in enumerate(zip(
        ITEM_IDS, ROLES, ("before", "middle", "after"),
    ))]
    config = {
        "schema_version": 1, "kind": "config", "config_id": CONFIG,
        "revision": revision, "name": "Selected three roles",
        "inputs": [{
            "name": role, "kind": "item", "item_instance_id": uid(8300 + index),
            "item_id": identity, "revision": revision, "overrides": {},
        } for index, (identity, role) in enumerate(zip(ITEM_IDS, ROLES))],
    }
    return items, config


def save_catalog(service, revision=1):
    items, config = catalog_records(revision)
    for item in items:
        service.save_prompt_config(
            "item", item, expected_revision=revision - 1,
            idempotency_key=f"item:{item['item_id']}:r{revision}",
        )
    service.save_prompt_config(
        "config", config, expected_revision=revision - 1,
        idempotency_key=f"config:r{revision}",
    )
    return config


def choice(*stages, revision=1):
    return {
        "schema_version": 1, "kind": "workflow_prompt_selection",
        "nodes": {stage: {"config_id": CONFIG, "revision": revision} for stage in stages},
    }


def prompts(messages):
    return [message for message in messages if message["source"]["kind"] == "prompt"]


def assert_selected(frozen, revision=1):
    evidence = validate_frozen_preparation(frozen)
    assert evidence is not None and frozen["projection_version"] == 2
    materialized = evidence["config"]["prompt_config"]
    assert materialized["config"] == catalog_records(revision)[1]
    assert materialized["items"] == catalog_records(revision)[0]
    selected = prompts(frozen["s0"])
    assert [message["role"] for message in selected] == list(ROLES)
    assert [message["blocks"][0]["text"] for message in selected] == [
        f"catalog:r{revision}:{role}" for role in ROLES
    ]
    assert all(message["schema_version"] == 3 for message in selected)
    assert "A title not sent" not in dumps_pretty(frozen["s0"])
    return evidence


def test_real_http_selection_runs_a_b_with_three_prompt_roles_and_exact_saved_material(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        with running_server(service) as port:
            status, submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "selected root", "idempotency_key": "http-start",
                "prompt_selection": choice("A", "B"),
            })
        assert status == 202
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a, b = frozen_for_chain(database, submitted["chain_run_id"])
        assert [a["node_binding_id"], b["node_binding_id"]] == [A_BINDING, B_BINDING]
        for stage, frozen in zip(("A", "B"), (a, b)):
            evidence = assert_selected(frozen)
            assert next(messages for owner, messages, _ in calls if owner == stage) == frozen["s0"]
            assert evidence["variables"]["workflow_session_id"] == sid
            assert evidence["variables"]["node_binding_id"] == frozen["node_binding_id"]
            turn = next(row for row in bundle(database)["turn"] if row["snapshot_id"] == frozen["snapshot_id"])
            assert not prompts(turn["messages"])
        assert len(calls) == 2
        assert len({message["message_id"] for frozen in (a, b) for message in prompts(frozen["s0"])}) == 6
        assert set(a["config"]["payload"]["prompt_selection_plan"]["nodes"]) == {"A", "B"}


def test_real_http_fixed_selection_continues_after_reopen_without_accumulating_prompt_s0(database):
    calls, selected, previous = [], choice("A", "B"), {A_BINDING: [], B_BINDING: []}

    def submit_turn(service, port, number):
        payload = {
            "text": f"continuous root {number}", "idempotency_key": f"http-turn:{number}",
            "prompt_selection": selected,
        }
        status, receipt = request(port, "POST", f"/api/sessions/{sid}/inputs", payload)
        assert status == 202
        service.wait_for_idle(sid)
        status, view = request(port, "GET", f"/api/sessions/{sid}")
        assert status == 200 and view["error"] is None and view["can_submit"]
        assert len(view["messages"]) == number * 2
        fence = {
            "expected_session_revision": view["revision"],
            "expected_ref_revision": view["ref_revision"],
            "expected_head_commit_id": view["head_commit_id"],
        }
        for stage, frozen in zip(("A", "B"), frozen_for_chain(database, receipt["chain_run_id"])):
            binding = A_BINDING if stage == "A" else B_BINDING
            evidence = assert_selected(frozen)
            assert evidence["canonical_messages"][:-1] == previous[binding]
            assert all(message["source"]["kind"] != "prompt"
                       for message in evidence["canonical_messages"])
            assert calls[-2 if stage == "A" else -1][1] == frozen["s0"]
            status, archived = request(
                port, "POST", f"/api/sessions/{sid}/nodes/{binding}/context/read", fence,
            )
            assert status == 200 and len(archived["turns"]) == number
            assert len(archived["logical_floors"]) == number * 2
            identities = [message["message_id"] for message in archived["messages"]]
            assert len(identities) == len(set(identities))
            assert all(message["source"]["kind"] != "prompt" for message in archived["messages"])
            assert archived["messages"][:len(previous[binding])] == previous[binding]
            previous[binding] = archived["messages"]
        before = bundle(database)
        assert request(port, "POST", f"/api/sessions/{sid}/inputs", payload) == (202, receipt)
        assert bundle(database) == before and len(calls) == number * 2

    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        with running_server(service) as port:
            submit_turn(service, port, 1)
            save_catalog(service, revision=2)
            submit_turn(service, port, 2)
        saved = snapshots(database)
    with closing(WorkflowService(database, model_factory=factory(calls))) as reopened:
        with running_server(reopened) as port:
            assert request(port, "GET", f"/api/prompt-configs/config/{CONFIG}/revisions/1") == (
                200, catalog_records(1)[1],
            )
            submit_turn(reopened, port, 3)
        assert all(frozen in snapshots(database) for frozen in saved)
        assert len(calls) == 6
        assert "catalog:r2:" not in dumps_pretty([list(call) for call in calls])


def test_same_submit_key_with_different_prompt_choice_is_409_without_reexecution(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        service.submit(sid, "same root", "same", prompt_selection=choice("A"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        before = bundle(database)
        with running_server(service) as port:
            status, body = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "same root", "idempotency_key": "same",
                "prompt_selection": choice("B"),
            })
        assert status == 409 and body["error"]["reason_code"] == "idempotency_conflict"
        assert bundle(database) == before and len(calls) == 2


@pytest.mark.parametrize("missing", ["config", "revision", "stored_item"])
def test_missing_exact_selection_or_corrupt_reference_has_zero_partial_start_or_dispatch(database, missing):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        saved_config = save_catalog(service)
        selected = choice("A", "B")
        if missing == "config":
            selected["nodes"]["A"]["config_id"] = uid(8999)
        elif missing == "revision":
            selected["nodes"]["A"]["revision"] = 999
        else:
            saved_config["inputs"][0]["item_id"] = uid(8999)
            with closing(SqliteStore(database)) as store:
                store._connection.execute(
                    "UPDATE prompt_revisions SET payload = ? "
                    "WHERE kind = 'config' AND definition_id = ? AND revision = 1",
                    (canonical_bytes(saved_config).decode("utf-8"), CONFIG),
                )
        before = bundle(database)
        with pytest.raises(ContractValidationError) as caught:
            service.submit(sid, "not dispatched", "bad", prompt_selection=selected)
        assert caught.value.status_code == (500 if missing == "stored_item" else 404)
        assert caught.value.reason_code == ("storage_contract_violation" if missing == "stored_item" else "not_found")
        assert bundle(database) == before
        assert snapshots(database) == [] and calls == []
        assert service.get_session(sid)["can_submit"]


def test_catalog_edit_and_hide_during_active_a_do_not_change_first_b_material(database):
    calls, entered, release = [], Event(), Event()
    with closing(WorkflowService(
        database, model_factory=factory(calls, entered=entered, release=release),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        submitted = service.submit(sid, "active root", "active", prompt_selection=choice("A", "B"))
        try:
            assert entered.wait(10)
            original_a = deepcopy(snapshots(database)[0])
            save_catalog(service, revision=2)
            service.delete_prompt_config(
                "config", CONFIG, expected_revision=2, idempotency_key="hide-new-config",
            )
            assert service.list_prompt_configs("config") == []
            assert snapshots(database) == [original_a]
            assert service.get_prompt_config_revision("config", CONFIG, 1) == catalog_records(1)[1]
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a, b = frozen_for_chain(database, submitted["chain_run_id"])
        assert a == original_a
        assert_selected(a)
        assert_selected(b)
        assert "catalog:r2:" not in dumps_pretty([list(call) for call in calls])


def test_b_only_choice_preserves_basic_a_and_reads_legacy_b_history_without_prompt_accumulation(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "legacy root", "legacy")
        before = bundle(database)
        old_b = next(row for row in before["input_snapshot"] if row["node_binding_id"] == B_BINDING)
        assert old_b["projection_version"] == 1
        save_catalog(service)
        submitted = service.submit(sid, "next root", "selected-b", prompt_selection=choice("B"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a, b = frozen_for_chain(database, submitted["chain_run_id"])
        assert a["projection_version"] == 1 and validate_frozen_preparation(a) is None
        assert all("catalog:r1:" not in dumps_pretty(message) for message in a["s0"])
        evidence = assert_selected(b)
        old_turn = next(row for row in before["turn"] if row["snapshot_id"] == old_b["snapshot_id"])
        history = evidence["canonical_messages"][:-1]
        assert all(message in history for message in old_turn["messages"])
        assert any(message["source"]["kind"] == "upstream_node"
                   and loads_strict(message["blocks"][0]["text"]) == {"text": "legacy rootA"}
                   for message in history)
        assert all(message["source"]["kind"] != "prompt" for message in evidence["canonical_messages"])
        assert all(row in bundle(database)["turn"] for row in before["turn"])
        assert len(calls) == 4


def test_selection_replay_after_catalog_edit_and_service_reopen_keeps_original_receipt_and_freeze(database):
    calls, selected = [], choice("A", "B")
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        receipt = service.submit(sid, "replay root", "once", prompt_selection=selected)
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        save_catalog(service, revision=2)
        before = bundle(database)
        assert service.submit(sid, "replay root", "once", prompt_selection=selected) == receipt
        assert bundle(database) == before and len(calls) == 2
        for frozen in frozen_for_chain(database, receipt["chain_run_id"]):
            assert_selected(frozen)
    with closing(WorkflowService(
        database, model_factory=lambda _stage: pytest.fail("Receipt replay cannot construct a model"),
    )) as reopened:
        assert reopened.submit(sid, "replay root", "once", prompt_selection=selected) == receipt
        assert bundle(database) == before
        for frozen in frozen_for_chain(database, receipt["chain_run_id"]):
            assert_selected(frozen)


def test_selected_reroll_reuses_old_b_config_with_new_a_not_latest_catalog(database):
    calls = []
    with closing(WorkflowService(
        database, model_factory=variant_factory(calls, "original A", "replacement A"),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        original = service.submit(sid, "reroll root", "first", prompt_selection=choice("A", "B"))
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        old_a, old_b = frozen_for_chain(database, original["chain_run_id"])
        save_catalog(service, revision=2)
        replacement = reroll(service, sid)
        new_a, new_b = frozen_for_chain(database, replacement["chain_run_id"])
        assert new_a["s0"] == old_a["s0"]
        assert validate_frozen_preparation(new_a) == validate_frozen_preparation(old_a)
        new_b_evidence = assert_selected(new_b)
        assert new_b_evidence["config"] == validate_frozen_preparation(old_b)["config"]
        assert loads_strict(new_b_evidence["canonical_messages"][-1]["blocks"][0]["text"]) == {
            "text": "replacement A",
        }
        assert "original A" not in dumps_pretty(new_b_evidence["canonical_messages"])
        assert "catalog:r2:" not in dumps_pretty([list(call) for call in calls])
        assert len(bundle(database)["workflow_candidate"]) == 2
        assert old_a in snapshots(database) and old_b in snapshots(database)


def test_paused_a_reroll_with_no_b_snapshot_uses_original_complete_selection_plan(database):
    calls, entered, release = [], Event(), Event()

    class BlockFirstA(VariantModel):
        def generate(self, messages, tools):
            if self.stage == "A" and not calls:
                entered.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    def model_factory(stage):
        return BlockFirstA(stage, calls, ("discarded original A", "replacement A"))

    with closing(WorkflowService(database, model_factory=model_factory)) as service:
        sid = service.create_session()["workflow_session_id"]
        save_catalog(service)
        original = service.submit(sid, "paused root", "first", prompt_selection=choice("A", "B"))
        try:
            assert entered.wait(10)
            view = service.get_session(sid)
            run_id = view["nodes"][0]["run_id"]
            with closing(SqliteStore(database)) as store:
                run = store.get_record("run_record", {"run_id": run_id})
            service.interrupt(
                sid, run_id, idempotency_key="pause-a",
                expected_session_revision=view["revision"], expected_run_revision=run["revision"],
            )
        finally:
            release.set()
        service.wait_for_idle(sid)
        paused = service.get_session(sid)
        assert paused["available_actions"] == ["resume", "reroll"]
        old_frozen = frozen_for_chain(database, original["chain_run_id"])
        assert len(old_frozen) == 1 and old_frozen[0]["node_binding_id"] == A_BINDING
        plan = old_frozen[0]["config"]["payload"]["prompt_selection_plan"]
        assert plan["nodes"]["B"]["prompt_config"]["config"] == catalog_records(1)[1]
        save_catalog(service, revision=2)
        service.delete_prompt_config("config", CONFIG, expected_revision=2, idempotency_key="hide")
        replacement = service.reroll(
            sid, original["chain_run_id"], idempotency_key="new-full-chain",
            expected_session_revision=paused["revision"],
            expected_ref_revision=paused["ref_revision"], expected_head_commit_id=paused["head_commit_id"],
        )
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        new_a, new_b = frozen_for_chain(database, replacement["chain_run_id"])
        assert new_a["s0"] == old_frozen[0]["s0"]
        evidence = assert_selected(new_b)
        assert evidence["config"] == plan["nodes"]["B"]
        assert loads_strict(evidence["canonical_messages"][-1]["blocks"][0]["text"]) == {
            "text": "replacement A",
        }
        assert len(frozen_for_chain(database, original["chain_run_id"])) == 1
        assert [stage for stage, _messages, _tools in calls] == ["A", "A", "B"]
        assert "catalog:r2:" not in dumps_pretty([list(call) for call in calls])
        assert [message["role"] for message in service.get_session(sid)["messages"]] == ["user", "assistant"]


def processed_variable_components(*, incompatible_b=None):
    selected = variable_components()
    for stage, selection in list(selected.contexts.items()):
        config = deepcopy(selection.config["payload"])
        config["steps"].append(configured_regex(8500, "selected:", "processed:"))
        config["context_regex"] = [configured_context_regex(8600, "city", "town")]
        config["lorebooks"] = [configured_lorebook(8700, keyword="town", text="selected:lore")]
        config["limits"]["assembly"]["max_total_chars"] = 12345
        if stage == "B":
            if incompatible_b == "selector":
                original = config["collection"]["items"][0]
                config["steps"][0]["select"] = {"mode": "instances", "instances": [{
                    field: original[field] for field in ("group_instance_id", "item_instance_id")
                }]}
            elif incompatible_b == "assignment":
                config["variable_plan"]["assignments"][0]["name"] = "counter"
            elif incompatible_b == "capacity":
                config["limits"]["processing"]["max_items"] = 2
        selected.contexts[stage] = replace(selection, config={
            **deepcopy(selection.config), "payload": validate_prompt_context_config(config),
        })
    return selected


def save_processed_catalog(service, revision=1):
    items, config = catalog_records(revision)
    for item in items:
        item["text"] = (
            f"selected:r{revision}:{item['role']} "
            "{{value}} / {{fixed}} / {{counter}}"
        )
        item["interpolation"] = "variables"
        service.save_prompt_config(
            "item", item, expected_revision=revision - 1,
            idempotency_key=f"processed-item:{item['item_id']}:r{revision}",
        )
    service.save_prompt_config(
        "config", config, expected_revision=revision - 1,
        idempotency_key=f"processed-config:r{revision}",
    )


def test_real_http_selected_material_keeps_v2_variables_macro_regex_lorebook_and_capacity(database):
    calls = []
    configured = processed_variable_components()
    with closing(WorkflowService(
        database, components=configured, model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_processed_catalog(service)
        with running_server(service) as port:
            status, submitted = request(port, "POST", f"/api/sessions/{sid}/inputs", {
                "text": "city", "idempotency_key": "combined-http",
                "prompt_selection": choice("A", "B"),
            })
        assert status == 202
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        a, b = frozen_for_chain(database, submitted["chain_run_id"])
        for stage, frozen, root, fixed in (
            ("A", a, "city", "A-fixed"), ("B", b, "townA", "B-fixed"),
        ):
            evidence = validate_frozen_preparation(frozen)
            default = configured.contexts[stage].config["payload"]
            assert evidence["config"]["schema_version"] == 2
            assert evidence["config"]["variables"]["registry"] == default["variables"]["registry"]
            for field in ("variable_plan", "steps", "lorebooks", "context_regex", "limits"):
                assert evidence["config"][field] == default[field]
            assert evidence["variables"]["values"]["value"]["value"] == root
            assert evidence["variables"]["values"]["fixed"]["value"] == fixed
            assert evidence["variables"]["values"]["counter"]["value"] == 2
            texts = [message["blocks"][0]["text"] for message in prompts(frozen["s0"])]
            assert [text for text in texts if text.startswith("processed:r1:")] == [
                f"processed:r1:{role} {root} / {fixed} / 2" for role in ROLES
            ]
            assert "processed:lore" in texts
            assert all("{{value}}" not in text for text in texts)
            assert evidence["lorebook"][0]["trace"]["decisions"][0]["status"] == "activated"
            assert next(messages for owner, messages, _ in calls if owner == stage) == frozen["s0"]
        assert loads_strict(validate_frozen_preparation(a)["canonical_messages"][-1]["blocks"][0]["text"]) == {
            "text": "city",
        }
        assert loads_strict(next(
            message for message in a["s0"] if message["source"]["kind"] == "human"
        )["blocks"][0]["text"]) == {"text": "town"}
        plan = a["config"]["payload"]["prompt_selection_plan"]
        assert all(plan["nodes"][stage]["schema_version"] == 2 for stage in ("A", "B"))
        assert variable_counts(database)["variable_states"] == 3
        assert len(calls) == 2


@pytest.mark.parametrize("incompatibility", ["selector", "assignment", "capacity"])
def test_incompatible_selected_b_pipeline_refuses_entire_submit_before_a_dispatch(database, incompatibility):
    calls = []
    with closing(WorkflowService(
        database, components=processed_variable_components(incompatible_b=incompatibility),
        model_factory=factory(calls),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_processed_catalog(service)
        before, counts = bundle(database), variable_counts(database)
        with pytest.raises(ContractValidationError):
            service.submit(sid, "city", "incompatible", prompt_selection=choice("A", "B"))
        assert calls == []
        assert snapshots(database) == []
        assert bundle(database) == before
        assert variable_counts(database) == counts
        assert service.get_session(sid)["can_submit"]


def test_selected_b_pipeline_freezes_at_submit_and_reroll_ignores_live_pipeline_and_catalog_edits(database):
    calls, entered, release = [], Event(), Event()

    class BlockFirstA(VariantModel):
        def generate(self, messages, tools):
            if self.stage == "A" and not calls:
                entered.set()
                assert release.wait(20)
            return super().generate(messages, tools)

    with closing(WorkflowService(
        database, components=processed_variable_components(),
        model_factory=lambda stage: BlockFirstA(stage, calls, ("original city", "replacement city")),
    )) as service:
        sid = service.create_session()["workflow_session_id"]
        save_processed_catalog(service)
        first = service.submit(sid, "city", "first", prompt_selection=choice("A", "B"))
        try:
            assert entered.wait(10)
            old_a = deepcopy(snapshots(database)[0])
            plan_b = deepcopy(old_a["config"]["payload"]["prompt_selection_plan"]["nodes"]["B"])
            assert plan_b["schema_version"] == 2
            assert plan_b["variable_plan"]["assignments"][0]["source"]["value"] == "B-fixed"
            context, kernel, adapter, current = service._resolved["B"]
            changed = deepcopy(current)
            live = changed["payload"]["context"]
            live["variable_plan"]["assignments"][0]["source"]["value"] = "live B fixed"
            live["steps"][1]["rule"]["replacement"] = "live:"
            live["lorebooks"][0]["prompts"][0]["text"] = "live lore"
            live["context_regex"][0]["rule"]["replacement"] = "live town"
            live["limits"]["assembly"]["max_total_chars"] = 22222
            service._resolved["B"] = context, kernel, adapter, changed
            save_processed_catalog(service, revision=2)
        finally:
            release.set()
        service.wait_for_idle(sid)
        assert service.get_session(sid)["error"] is None
        saved_a, old_b = frozen_for_chain(database, first["chain_run_id"])
        assert saved_a == old_a
        for field in ("variable_plan", "steps", "lorebooks", "context_regex", "limits"):
            assert old_b["config"]["payload"]["context"][field] == plan_b[field]
        replacement = reroll(service, sid, "replacement")
        new_a, new_b = frozen_for_chain(database, replacement["chain_run_id"])
        assert new_a["s0"] == old_a["s0"]
        evidence = validate_frozen_preparation(new_b)
        for field in ("variable_plan", "steps", "lorebooks", "context_regex", "limits"):
            assert evidence["config"][field] == plan_b[field]
        assert evidence["variables"]["values"]["value"]["value"] == "replacement city"
        assert evidence["variables"]["values"]["fixed"]["value"] == "B-fixed"
        text = dumps_pretty(new_b["s0"])
        assert "processed:r1:system replacement city / B-fixed / 2" in text
        assert "processed:lore" in text
        assert "live" not in text and "selected:r2:" not in text
        assert len(calls) == 4
