"""Directory cursors retain exact query scope even for identical catalog pages."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_application import GraphApplication
from phase1_agent.runtime_information import paginate_registrations

from test_graph_application import public_document
from test_graph_service import create, service


def test_identical_session_catalogs_do_not_share_cursors(service):
    application = GraphApplication(service)
    document = public_document(service)
    first = create(service, document)
    second = service.create_session(document["workflow_definition_id"], document["revision"],
                                    idempotency_key="other-catalog-session")
    basis = {"session_id": first["workflow_session_id"], "limit": 1}
    first_page = application.query("registration.list", basis)
    second_page = application.query("registration.list", {
        **basis, "session_id": second["workflow_session_id"],
    })
    assert first_page["items"] == second_page["items"]
    cursor = first_page["next_cursor"]
    assert cursor is not None
    assert application.query("registration.list", {**basis, "cursor": cursor})["items"]
    for changed in ({**basis, "session_id": second["workflow_session_id"]}, {"limit": 1}):
        with pytest.raises(ContractValidationError) as failure:
            application.query("registration.list", {**changed, "cursor": cursor})
        assert failure.value.reason_code == "information_invalid_cursor"


@pytest.mark.parametrize("coordinate", ["session_id", "chain_id", "node_id"])
def test_scope_binding_does_not_filter_static_entries(coordinate):
    entries = [{
        "kind": "node", "registration_ref": {"component_id": identity, "component_version": "1"},
        "declaration": {}, "discover_public": True,
    } for identity in ("example.first", "example.second")]
    original = deepcopy(entries)
    basis = {coordinate: "original"}
    page = paginate_registrations(entries, limit=1, query_scope=basis)
    assert page["items"] == entries[:1] and page["next_cursor"] is not None
    assert paginate_registrations(entries, limit=1, query_scope=basis,
                                  cursor=page["next_cursor"])["items"] == entries[1:]
    with pytest.raises(ContractValidationError) as failure:
        paginate_registrations(entries, limit=1, query_scope={coordinate: "changed"},
                               cursor=page["next_cursor"])
    assert failure.value.reason_code == "information_invalid_cursor"
    assert entries == original
