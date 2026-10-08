"""Synthetic protocol markers test preservation, not valid supplier signatures."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from phase1_agent.agent_package import thinking_summary_page
from phase1_agent.context_compaction import is_compactable_text
from phase1_agent.context_regex import apply_context_regex
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contracts import ModelResponse, ModelToolCall
from phase1_agent.contracts_v2 import validate_record
from phase1_agent.provider_metadata import (
    GEMINI_PROTOCOL, remap_provider_metadata, validate_provider_metadata,
)
from phase1_agent.prompt_values import context_view_messages
from phase1_agent.runtime import CanonicalModelAdapter, KernelPaused, RunFailed, SnapshotKernel
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore

from test_context_regex import config as regex_config, view as regex_view
from test_runtime_pause import ScriptedAdapter, final, setup


MODEL = "gemini-2.5-flash"
SIGNATURE = "synthetic-fixture-signature-not-for-network"


def metadata(*calls, text="original", summary="Visible summary"):
    parts = [{"text": summary, "thought": True, "thoughtSignature": SIGNATURE},
             {"text": text, "thoughtSignature": SIGNATURE + "-text"}]
    bindings = []
    for identity, name, arguments in calls:
        bindings.append({"part_index": len(parts), "tool_call_id": identity})
        part = {"functionCall": {"id": "supplier-" + identity, "name": name, "args": arguments}}
        if len(bindings) == 1:
            part["thoughtSignature"] = SIGNATURE + "-function"
        parts.append(part)
    return {"schema_version": 1, "provider": "gemini", "protocol": GEMINI_PROTOCOL,
            "model": MODEL, "content": {"role": "model", "parts": parts},
            "tool_call_bindings": bindings}


def response(*calls, text="original"):
    return ModelResponse(
        "tool_calls" if calls else "stop", content=text,
        tool_calls=tuple(ModelToolCall(identity, name, json.dumps(arguments))
                         for identity, name, arguments in calls),
        model=MODEL, thinking_summary="Visible summary",
        provider_metadata=metadata(*calls, text=text),
    )


def canonical_text():
    return validate_record("agent_message", {
        "schema_version": 5, "message_id": str(uuid4()), "role": "assistant",
        "source": {"kind": "model", "request_id": str(uuid4())},
        "blocks": [{"kind": "text", "text": "original"}],
        "thinking_summary": "Visible summary", "provider_metadata": metadata(),
    })


def test_metadata_detaches_original_parts_and_maps_only_local_ids():
    source = metadata(("provider-1", "inspect_text", {"text": "one"}),
                      ("provider-2", "inspect_text", {"text": "two"}))
    calls = [{"id": "provider-" + str(index), "name": "inspect_text",
              "raw_arguments": json.dumps({"text": text})}
             for index, text in ((1, "one"), (2, "two"))]
    checked = validate_provider_metadata(source, calls=calls, content="original",
                                         thinking_summary="Visible summary", model=MODEL)
    remapped = remap_provider_metadata(checked, {"provider-1": "canonical-1",
                                                "provider-2": "canonical-2"})
    assert remapped["content"] == source["content"]
    assert [item["tool_call_id"] for item in remapped["tool_call_bindings"]] == [
        "canonical-1", "canonical-2",
    ]
    remapped["content"]["parts"][0]["text"] = "mutated"
    assert source["content"]["parts"][0]["text"] == "Visible summary"
    assert "thoughtSignature" not in source["content"]["parts"][-1]
    with pytest.raises(ContractValidationError):
        remap_provider_metadata(source, {"provider-1": "canonical-1"})
    alias = {**source, "response_model": "gemini-2.5-flash-actual-version"}
    assert validate_provider_metadata(alias, model=MODEL,
                                      response_model="gemini-2.5-flash-actual-version") == alias
    with pytest.raises(ContractValidationError):
        validate_provider_metadata(alias, response_model="wrong-actual-version")
    with pytest.raises(ContractValidationError):
        validate_provider_metadata(alias, response_model=None)
    absent_actual = {**source, "response_model": None}
    assert validate_provider_metadata(absent_actual, response_model=None) == absent_actual
    with pytest.raises(ContractValidationError):
        validate_provider_metadata(absent_actual, response_model=MODEL)
    repeated = deepcopy(source)
    repeated["content"]["parts"][-1]["functionCall"]["id"] = repeated["content"]["parts"][-2]["functionCall"]["id"]
    with pytest.raises(ContractValidationError):
        validate_provider_metadata(repeated)


@pytest.mark.parametrize("mutation", [
    lambda value: value["tool_call_bindings"][0].update(part_index=0),
    lambda value: value["content"]["parts"][-1]["functionCall"].update(args={"text": "wrong"}),
    lambda value: value.update(model="another-model"),
    lambda value: value["content"]["parts"][0].update(thoughtSignature="skip_thought_signature_validator"),
])
def test_metadata_rejects_binding_model_arguments_and_bypass_mismatch(mutation):
    value = metadata(("provider-1", "inspect_text", {"text": "one"}))
    mutation(value)
    with pytest.raises(ContractValidationError):
        validate_provider_metadata(
            value, calls=[{"id": "provider-1", "name": "inspect_text", "raw_arguments": '{"text":"one"}'}],
            content="original", thinking_summary="Visible summary", model=MODEL,
        )


def test_canonical_contract_version_retains_three_channels_and_old_version_stays_strict():
    message = canonical_text()
    assert message["blocks"] == [{"kind": "text", "text": "original"}]
    assert CanonicalModelAdapter._project([message])[0]["provider_metadata"] == message["provider_metadata"]
    incompatible = {**message, "schema_version": 1}
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", incompatible)
    rewritten = deepcopy(message)
    rewritten["blocks"][0]["text"] = "changed"
    with pytest.raises(ContractValidationError):
        validate_record("agent_message", rewritten)


def test_signed_text_is_neither_regex_editable_nor_compactable():
    message = canonical_text()
    assert not is_compactable_text(message)
    original = regex_view([message])
    result = apply_context_regex(original, regex_config())
    assert result["trace"]["processed_blocks"] == []
    assert context_view_messages(result["view"]) == [message]
    tampered = deepcopy(original)
    tampered["overrides"] = [{"message_id": message["message_id"], "block_index": 0, "text": "changed"}]
    with pytest.raises(ContractValidationError):
        context_view_messages(tampered)


def test_kernel_maps_both_same_named_calls_and_next_request_preserves_full_content():
    snapshot, tools = setup()
    signed = response(("provider-1", "inspect_text", {"text": "one"}),
                      ("provider-2", "inspect_text", {"text": "two"}))
    adapter = ScriptedAdapter(signed, final("done"))
    result = SnapshotKernel().run(snapshot, tools, adapter)
    accepted = result.messages[0]
    assert accepted["schema_version"] == 5
    assert accepted["provider_metadata"]["content"] == signed.provider_metadata["content"]
    ids = [block["tool_call_id"] for block in accepted["blocks"] if block["kind"] == "tool_call"]
    assert [binding["tool_call_id"] for binding in accepted["provider_metadata"]["tool_call_bindings"]] == ids
    assert ids != ["provider-1", "provider-2"]
    assert adapter.requests[1][-3]["provider_metadata"] == accepted["provider_metadata"]
    wire = CanonicalModelAdapter._project(adapter.requests[1])
    assert wire[-3]["provider_metadata"]["content"] == signed.provider_metadata["content"]
    assert [item["tool_call_id"] for item in wire[-2:]] == ids


def test_pause_before_acceptance_retains_signed_material_without_model_redispatch():
    snapshot, tools = setup()
    signed = response(("provider-final", "final_answer", {"answer": {"text": "done"}}))
    adapter = ScriptedAdapter(signed)
    with pytest.raises(KernelPaused) as paused:
        SnapshotKernel().run(snapshot, tools, adapter,
                             on_boundary=lambda kind: kind != "before_accept")
    checkpoint = paused.value.checkpoint
    assert checkpoint.pending_model_message["provider_metadata"]["content"] == signed.provider_metadata["content"]
    assert checkpoint.messages == checkpoint.pending_tools == ()
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=checkpoint)
    assert result.final["value"] == {"text": "done"}
    assert len(adapter.requests) == result.model_requests == result.attempts == 1
    assert result.messages[0]["provider_metadata"]["content"] == signed.provider_metadata["content"]
    changed = deepcopy(checkpoint.pending_model_message)
    changed["provider_metadata"]["content"]["parts"][0]["thoughtSignature"] = "modified"
    with pytest.raises(Exception):
        SnapshotKernel.validate_checkpoint(replace(checkpoint, pending_model_message=changed), snapshot)


def test_signed_pending_batch_pause_executes_each_original_call_once():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text) or {"text": text})
    signed = response(("provider-1", "inspect_text", {"text": "one"}),
                      ("provider-2", "inspect_text", {"text": "two"}))
    adapter = ScriptedAdapter(signed, final("done"))
    with pytest.raises(KernelPaused) as paused:
        SnapshotKernel().run(
            snapshot, tools, adapter,
            on_boundary=lambda kind: kind != "after_tool",
        )
    assert executed == ["one"]
    assert len(paused.value.checkpoint.pending_tools) == 1
    assert paused.value.checkpoint.messages[0]["provider_metadata"]["content"] == signed.provider_metadata["content"]
    result = SnapshotKernel().run(snapshot, tools, adapter, checkpoint=paused.value.checkpoint)
    assert executed == ["one", "two"]
    assert len(adapter.requests) == result.model_requests == 2
    assert result.messages[0]["provider_metadata"]["content"] == signed.provider_metadata["content"]


def test_malformed_response_metadata_is_rejected_before_any_tool_execution():
    executed = []
    snapshot, tools = setup(lambda text: executed.append(text))
    signed = response(("provider-1", "inspect_text", {"text": "one"}))
    signed.provider_metadata["content"]["parts"][-1]["functionCall"]["args"] = {"text": "wrong"}
    with pytest.raises(RunFailed) as error:
        SnapshotKernel().run(snapshot, tools, ScriptedAdapter(signed))
    assert error.value.code == "provider_metadata_invalid"
    assert executed == []


def test_sqlite_new_reader_history_projection_and_snapshot_keep_original_parts(tmp_path):
    snapshot, tools = setup()
    signed = response(("provider-1", "inspect_text", {"text": "one"}),
                      ("provider-2", "inspect_text", {"text": "two"}))
    events = []
    result = SnapshotKernel().run(snapshot, tools, ScriptedAdapter(signed, final("done")), on_fact=events.append)
    owner = dict(zip(("workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id"),
                     (str(uuid4()) for _ in range(4))))
    path = tmp_path / "signed-history.sqlite"
    reference = {"executor_id": "signed.fixture", "exact_version": "1"}
    with closing(SqliteStore(path)) as store:
        facts = RuntimeFactStore(store)
        store._connection.execute("BEGIN IMMEDIATE")
        for sequence, event in enumerate(events, 1):
            facts.accept(owner=owner, stream_id="executor:signed.fixture@1", fact={
                "fact_id": str(uuid4()), "sequence": sequence, "owner": owner,
                "generation": 1, "payload": event,
            })
        store._connection.execute("COMMIT")
    with closing(SqliteStore(path)) as store:
        facts = RuntimeFactStore(store).read_executor_page(owner, reference, 1)
        reread = [fact["payload"]["payload"]["message"] for fact in facts["items"]
                  if fact["payload"]["kind"] == "message_accepted"]
    assert reread == result.messages
    projection = CanonicalModelAdapter._project(reread)
    assert projection[0]["provider_metadata"]["content"] == signed.provider_metadata["content"]
    assert projection[0]["thinking_summary"] == "Visible summary"
    assert [binding["tool_call_id"] for binding in projection[0]["provider_metadata"]["tool_call_bindings"]] == [
        call["id"] for call in projection[0]["calls"]
    ]
    assert projection[1]["tool_call_id"] == projection[0]["calls"][0]["id"]
    assert projection[2]["tool_call_id"] == projection[0]["calls"][1]["id"]


def test_public_thinking_page_keeps_pagination_but_never_private_protocol_material():
    message = canonical_text()
    private = {"items": [
        {"payload": {"kind": "model_request", "payload": {"private": SIGNATURE}}},
        {"payload": {"kind": "message_accepted", "payload": {"message": message}}},
    ], "next_cursor": "opaque-next-page", "status": "ok"}
    actual = thinking_summary_page(lambda request: deepcopy(private), {"limit": 2})
    assert actual == {"items": [{"message_id": message["message_id"],
                                 "request_id": message["source"]["request_id"],
                                 "thinking_summary": "Visible summary"}],
                      "next_cursor": "opaque-next-page", "status": "ok"}
    assert SIGNATURE not in json.dumps(actual)
    assert "provider_metadata" not in json.dumps(actual)
    private["items"] = private["items"][:1]
    assert thinking_summary_page(lambda request: private, {}) == {
        "items": [], "next_cursor": "opaque-next-page", "status": "ok",
    }
