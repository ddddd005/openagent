"""Run-order rejection also protects event and chat reroll shared entry paths."""

import pytest

from phase1_agent.contract_errors import ContractValidationError
from test_graph_application_receipt_custody import raw_database
from test_graph_events import event_service, event_document, submit as submit_event
from test_graph_service import create
from test_workflow_chat_integration import (
    context_chat, create_with_context, context_value, submit, guards,
)
from test_workflow_run_order import _reject_background


def test_event_declaration_rejection_has_no_records_or_frames(event_service, monkeypatch):
    service, harness = event_service
    document = event_document(service)
    initial = create(service, document)
    before = raw_database(service.database)
    _reject_background(monkeypatch)
    with pytest.raises(ContractValidationError, match="declaration and bindings differ"):
        submit_event(service, initial, document)
    assert raw_database(service.database) == before
    assert service.get_session(initial["workflow_session_id"]) == initial
    assert service._resource_frames == {}
    assert service._execution_registries == {}
    assert service._futures == {}


def test_chat_reroll_declaration_rejection_preserves_original_round(context_chat, monkeypatch):
    service, document, _, observed = context_chat
    initial = create_with_context(service, document, context_value("original", 7))
    completed = submit(service, initial)
    round_id = completed["objects"]["chat-0"]["value"]["rounds"][0]["round_id"]
    before = raw_database(service.database)
    calls = list(observed)
    _reject_background(monkeypatch)
    with pytest.raises(ContractValidationError, match="declaration and bindings differ"):
        service.chat_reroll(
            completed["workflow_session_id"], round_id=round_id, **guards(completed))
    assert raw_database(service.database) == before
    assert service.get_session(completed["workflow_session_id"]) == completed
    assert observed == calls
    assert service._resource_frames == {}
    assert service._execution_registries == {}
