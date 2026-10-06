"""Prompt catalog management is separate from workflow execution and history."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.storage import SqliteStore
from phase1_agent.workflow import WorkflowService


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def item(revision=1):
    return {
        "schema_version": 1, "kind": "item", "item_id": uid(1), "revision": revision,
        "name": "Instructions", "text": "Same text", "role": "assistant", "enabled": True,
        "placement": "before", "depth": None, "order": 10, "interpolation": "literal",
        "source": {"kind": "configuration"},
    }


def group():
    return {
        "schema_version": 1, "kind": "group", "group_id": uid(2), "revision": 1,
        "name": "Reusable", "members": [{
            "item_instance_id": uid(10), "item_id": uid(1), "revision": 1, "overrides": {},
        }],
    }


def config():
    return {
        "schema_version": 1, "kind": "config", "config_id": uid(3), "revision": 1,
        "name": "Two references", "inputs": [{
            "name": name, "kind": "group", "group_instance_id": uid(identity),
            "group_id": uid(2), "revision": 1, "enabled": True, "member_overrides": [],
        } for name, identity in (("first", 20), ("second", 21))],
    }


def save(service, kind, record, key=None, expected=0):
    return service.save_prompt_config(
        kind, record, expected_revision=expected, idempotency_key=key or kind,
    )


def durable(database):
    with closing(SqliteStore(database)) as store:
        return store.read_bundle()


@pytest.fixture
def database():
    with TemporaryDirectory(prefix="prompt-service-", dir=Path(__file__).parent) as folder:
        yield Path(folder) / "workflow.sqlite"


def test_exact_revisions_repeated_group_hidden_definition_and_reopen(database):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: calls.append(stage))) as service:
        sid = service.create_session()["workflow_session_id"]
        before = durable(database)
        saved = save(service, "item", item())
        save(service, "group", group())
        save(service, "config", config())
        original = service.resolve_prompt_config(uid(3), 1)
        assert len(original["items"]) == 2
        assert {entry["group_instance_id"] for entry in original["items"]} == {uid(20), uid(21)}
        assert {entry["item_instance_id"] for entry in original["items"]} == {uid(10)}
        assert [entry["text"] for entry in original["items"]] == ["Same text", "Same text"]
        revised = item(2)
        revised["text"] = "New text"
        save(service, "item", revised, key="edit", expected=1)
        hidden = service.delete_prompt_config(
            "item", uid(1), expected_revision=2, idempotency_key="hide",
        )
        assert hidden["head"]["catalog_revision"] == 3
        assert not hidden["head"]["selectable"]
        assert service.list_prompt_configs("item") == []
        assert service.get_prompt_config_revision("item", uid(1), 1) == item()
        assert service.resolve_prompt_config(uid(3), 1) == original
        assert save(service, "item", item()) == saved
        original["items"][0]["text"] = "Local mutation"
        assert service.resolve_prompt_config(uid(3), 1)["items"][0]["text"] == "Same text"
        assert calls == [] and durable(database) == before
        assert service.get_session(sid)["can_submit"]
    with closing(WorkflowService(database)) as reopened:
        assert reopened.resolve_prompt_config(uid(3), 1)["items"][0]["text"] == "Same text"
        assert reopened.get_prompt_config_head("item", uid(1)) == hidden["head"]
        assert save(reopened, "item", item()) == saved


def test_concurrent_replays_and_conflicts_never_dispatch(database):
    calls = []
    with closing(WorkflowService(database, model_factory=lambda stage: calls.append(stage))) as service:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: save(service, "item", item()), range(3)))
        assert results == [results[0]] * 3
        assert len(service.list_prompt_configs("item")) == 1
        changed = item()
        changed["text"] = "Private conflicting text"
        with pytest.raises(ContractValidationError) as conflict:
            save(service, "item", changed)
        assert conflict.value.status_code == 409
        assert conflict.value.reason_code == "idempotency_conflict"
        with pytest.raises(ContractValidationError) as stale:
            save(service, "item", item(2), key="stale", expected=0)
        assert stale.value.status_code == 409
        assert calls == []


def test_missing_and_wrong_reference_never_create_config_or_workflow_history(database):
    with closing(WorkflowService(database)) as service:
        before = durable(database)
        with pytest.raises(ContractValidationError):
            save(service, "config", config())
        assert service.list_prompt_configs("config") == []
        assert durable(database) == before
        for operation in (
            lambda: service.get_prompt_config_revision("item", uid(1), 1),
            lambda: service.resolve_prompt_config(uid(3), 1),
        ):
            with pytest.raises(ContractValidationError) as missing:
                operation()
            assert missing.value.status_code == 404


def test_config_edits_cannot_mutate_existing_frozen_history(database):
    with closing(WorkflowService(database)) as service:
        sid = service.create_session()["workflow_session_id"]
        service.submit(sid, "Stable facts", "input")
        service.wait_for_idle(sid)
        before = durable(database)
        assert len(before["turn"]) == 2
        save(service, "item", item())
        changed = deepcopy(item(2))
        changed.update(enabled=False, order=-10)
        save(service, "item", changed, key="edit", expected=1)
        service.delete_prompt_config("item", uid(1), expected_revision=2, idempotency_key="hide")
        assert durable(database) == before
        assert service.get_session(sid)["error"] is None
