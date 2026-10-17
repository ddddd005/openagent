"""Named client transport keeps the existing execution and read authorities."""

from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_http import dispatch_graph

from test_graph_service import service


def query(service, operation, parameters=None, *, consumer=False):
    path = "/api/graph/consumer/queries" if consumer else "/api/graph/queries"
    return dispatch_graph(service, "POST", path, {
        "operation": operation, "parameters": deepcopy(parameters or {}),
    })[1]


def rejected(code, callback):
    with pytest.raises(ContractValidationError) as caught:
        callback()
    assert caught.value.reason_code == code


@pytest.mark.parametrize("operation", [
    "object.artifact.read", "object.read", "session.read", "manifest.read",
])
def test_named_consumer_scope_rejects_management_before_dispatch(service, operation):
    rejected("application_scope_denied", lambda: query(service, operation, {}, consumer=True))
    rejected("invalid_request", lambda: dispatch_graph(
        service, "POST", "/api/graph/consumer/queries",
        {"operation": operation, "parameters": {}, "scope": "management"}))
