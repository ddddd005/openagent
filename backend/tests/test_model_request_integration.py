"""M2 composition against existing SQLite resource and immutable fact stores."""

from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import pytest

from phase1_agent.contract_errors import ContractValidationError, require
from phase1_agent.contract_json import canonical_bytes
from phase1_agent.runtime_fact_store import RuntimeFactStore
from phase1_agent.storage import SqliteStore
from test_model_request_resources import ResourceHarness, identity
from test_model_request_service import ServiceHarness


class IntegratedHost:
    def __init__(self, directory):
        self.resources = ResourceHarness(directory)
        self.mock = ServiceHarness()
        self.mock.context = self.resources.context
        self.mock.binding = self.resources.binding
        self.mock.output_id = self.resources.evidence["binding_output"]["output_id"]
        self.mock.input_refs = self.resources.evidence["consumer_run"]["input_refs"]
        self.read_count = 0
        self.fail_outcome = self.drop_outcome_ack = False
        self.resources.provider["update_sequence"] += 1
        self.resources.provider["value"]["credential_ref"] = "local:" + str(uuid4())
        self.resources.save(self.resources.provider)
        self.mock.broker.save(self.resources.provider["value"]["credential_ref"], "synthetic-integration-key")
        self.mock.service = self.mock.make_service(
            authorize_binding=self.resources.reader.authorize_binding,
            read_resource_bundle=self.read_bundle,
            check_resource_bundle=self.resources.reader.check_resource_bundle,
            accept_fact=self.accept_fact,
        )
        with closing(SqliteStore(self.resources.path)) as store:
            RuntimeFactStore(store)

    def read_bundle(self, *arguments):
        self.read_count += 1
        return self.resources.reader.read_resource_bundle(*arguments)

    def accept_fact(self, context, fact):
        owner = {key: getattr(context, key) for key in (
            "workflow_session_id", "chain_run_id", "node_binding_id", "node_run_id")}
        require(all(fact[field] == owner[key] for field, key in (
            ("session_id", "workflow_session_id"), ("chain_run_id", "chain_run_id"),
            ("node_binding_id", "node_binding_id"), ("node_run_id", "node_run_id"))),
            "runtime_fact_owner_mismatch", "Original model fact has another runtime owner")
        if fact["stage"] == "outcome" and self.fail_outcome:
            self.fail_outcome = False
            raise OSError("Synthetic outcome commit failure")
        with closing(SqliteStore(self.resources.path)) as store:
            facts = RuntimeFactStore(store)
            store._connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = facts.accept(
                    owner=owner, stream_id="service:workflow.models@2.0.0:model:" + fact["request_id"], fact=fact)
                store._connection.execute("COMMIT")
            except BaseException:
                store._connection.execute("ROLLBACK")
                raise
        if fact["stage"] == "outcome" and self.drop_outcome_ack:
            self.drop_outcome_ack = False
            raise OSError("Synthetic accepted-outcome acknowledgment loss")
        return receipt

    def facts(self):
        with closing(SqliteStore(self.resources.path)) as store:
            return RuntimeFactStore(store).list(
                self.mock.context.chain_run_id, node_run_id=self.mock.context.node_run_id)


@pytest.fixture
def integrated(tmp_path):
    host = IntegratedHost(tmp_path)
    yield host
    host.mock.close()


def test_new_request_reads_current_provider_dependency_and_preserves_first_facts(integrated):
    host, mock = integrated.resources, integrated.mock
    first = mock.prepare()
    assert integrated.read_count == 1 and integrated.facts() == []
    original = deepcopy(first)
    first_result = mock.dispatch(first)
    first_facts = deepcopy(integrated.facts())
    assert [fact["stage"] for fact in first_facts] == ["request", "attempt", "outcome"]
    assert first_result["fact_refs"] == [{"fact_id": fact["fact_id"]} for fact in first_facts]

    new_provider = deepcopy(host.provider)
    new_provider["resource_id"], new_provider["update_sequence"] = str(uuid4()), 1
    new_provider["value"]["base_url"] = "https://new-provider.example.invalid"
    new_provider["value"]["credential_ref"] = "local:" + str(uuid4())
    host.save(new_provider)
    host.profile["update_sequence"] += 1
    host.profile["value"]["provider"] = identity(new_provider)
    host.profile["value"]["runtime"]["output_limit_tokens"] = 16000
    host.save(host.profile)
    mock.broker.save(new_provider["value"]["credential_ref"], "synthetic-new-provider-key")

    second = mock.prepare(key="normal:2")
    second_result = mock.dispatch(second, key="normal:2")
    assert second["settings"]["provider"]["reference"] == identity(new_provider)
    assert second["settings"]["parameters"]["max_tokens"] == 16000
    assert second_result["settings_id"] != first_result["settings_id"]
    assert mock.communications[0]["parameters"]["max_tokens"] == 32000
    assert mock.communications[1]["provider"]["base_url"] == new_provider["value"]["base_url"]
    assert mock.communications[1]["api_key"] == "synthetic-new-provider-key"
    assert first == original and integrated.facts()[:3] == first_facts
    assert len(mock.communications) == 2 and integrated.read_count == 2
    assert mock.prepare() == first
    assert mock.dispatch(first) == first_result
    assert integrated.read_count == 2 and len(mock.communications) == 2
    public = canonical_bytes({"preparations": [first, second], "results": [first_result, second_result],
                              "facts": integrated.facts()})
    assert b"synthetic-integration-key" not in public and b"synthetic-new-provider-key" not in public


@pytest.mark.parametrize("lost_ack", [False, True])
def test_original_outcome_acceptance_reuses_exact_sqlite_fact_ids_without_communication(integrated, lost_ack):
    mock = integrated.mock
    prepared = mock.prepare()
    if lost_ack:
        integrated.drop_outcome_ack = True
    else:
        integrated.fail_outcome = True
    with pytest.raises(ContractValidationError) as error:
        mock.dispatch(prepared)
    assert error.value.reason_code == "model_fact_acceptance_failed"
    assert len(mock.communications) == 1
    assert len(integrated.facts()) == (3 if lost_ack else 2)
    integrated.resources.profile["update_sequence"] += 1
    integrated.resources.profile["value"]["runtime"]["output_limit_tokens"] = 12000
    integrated.resources.save(integrated.resources.profile)
    mock.broker.save(integrated.resources.provider["value"]["credential_ref"], "synthetic-replaced-key")
    result = mock.dispatch(prepared)
    facts = integrated.facts()
    assert len(mock.communications) == 1 and integrated.read_count == 1
    assert result["fact_refs"] == [{"fact_id": fact["fact_id"]} for fact in facts]
    assert {fact["fact_id"] for fact in facts} == set(prepared["reserved_model_fact_ids"].values())
    assert facts[0]["details"]["settings"] == prepared["settings"]


def test_unknown_dispatch_remains_persisted_after_save_and_release(integrated):
    mock = integrated.mock
    mock.transport_error = TimeoutError("Synthetic network uncertainty")
    prepared = mock.prepare()
    with pytest.raises(ContractValidationError) as error:
        mock.dispatch(prepared)
    assert error.value.reason_code == "model_dispatch_unknown"
    before = deepcopy(integrated.facts())
    assert before[-1]["details"]["classification"] == "dispatch_unknown"
    integrated.resources.profile["update_sequence"] += 1
    integrated.resources.profile["value"]["runtime"]["output_limit_tokens"] = 16000
    integrated.resources.save(integrated.resources.profile)
    with pytest.raises(ContractValidationError) as error:
        mock.dispatch(prepared)
    assert error.value.reason_code == "model_dispatch_unknown"
    mock.close()
    assert integrated.facts() == before and len(mock.communications) == 1


def test_owned_post_dispatch_compaction_measurement_uses_no_new_resource_or_private_lease(integrated):
    mock = integrated.mock
    before_messages = [{"kind": "text", "role": "user", "content": "long context " * 80}]
    prepared = mock.prepare(key="compaction:1", purpose="compaction", messages=before_messages)
    mock.dispatch(prepared, key="compaction:1")
    counts = (integrated.read_count, len(mock.broker.freeze_calls), len(mock.broker.authorize_calls),
              len(mock.communications), len(integrated.facts()))
    integrated.resources.profile["update_sequence"] += 1
    integrated.resources.profile["value"]["runtime"]["output_limit_tokens"] = 8000
    integrated.resources.save(integrated.resources.profile)
    evidence = mock.measure(prepared, [{"kind": "text", "role": "user", "content": "summary"}])
    assert evidence["settings_id"] == prepared["settings"]["settings_id"]
    assert evidence["measurement"]["output_reserve_tokens"] == 32000
    assert (integrated.read_count, len(mock.broker.freeze_calls), len(mock.broker.authorize_calls),
            len(mock.communications), len(integrated.facts())) == counts


def test_actual_resource_change_during_lease_capture_rejects_without_reread(integrated):
    def save_during_freeze():
        integrated.resources.profile["update_sequence"] += 1
        integrated.resources.profile["value"]["runtime"]["output_limit_tokens"] = 16000
        integrated.resources.save(integrated.resources.profile)
    integrated.mock.broker.on_freeze = save_during_freeze
    with pytest.raises(ContractValidationError) as error:
        integrated.mock.prepare()
    assert error.value.reason_code == "model_request_configuration_changed_before_prepare"
    assert integrated.read_count == 1 and integrated.mock.communications == []
    assert integrated.facts() == []
