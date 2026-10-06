"""Typed regex drafts preserve every connection, including erroneous ones."""

import copy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent.prompt_ports import diagnose_regex_ports, require_regex_port_mode


def uid(number):
    return f"00000000-0000-4000-8000-{number:012x}"


def edge(number=1, schema_id="text", direction="input", version=1):
    return {
        "connection_id": uid(number), "direction": direction, "port_id": direction,
        "schema_ref": {"schema_id": schema_id, "version": version},
    }


@pytest.mark.parametrize("kind", ["text", "prompt_item", "prompt_collection", "context_view"])
def test_matching_constraints_infer_without_changing_unspecified_mode(kind):
    edges = [edge(schema_id=kind), edge(2, kind, "output")]
    original = copy.deepcopy(edges)
    result = diagnose_regex_ports(uid(100), None, edges)
    assert result["executable"]
    assert result["mode"] is None
    assert result["resolved_mode"] == kind
    assert require_regex_port_mode(uid(100), None, edges) == kind
    assert edges == original


def test_conflicting_directions_never_pick_latest_input_or_remove_connections():
    edges = [edge(schema_id="text"), edge(2, "context_view", "output")]
    original = copy.deepcopy(edges)
    first = diagnose_regex_ports(uid(100), None, edges)
    second = diagnose_regex_ports(uid(100), None, list(reversed(edges)))
    assert first["resolved_mode"] is second["resolved_mode"] is None
    assert not first["executable"]
    assert {item["connection_id"] for item in first["diagnostics"]} == {uid(1), uid(2)}
    assert edges == original
    with pytest.raises(PromptProcessingError, match="resolved") as caught:
        require_regex_port_mode(uid(100), None, edges)
    assert caught.value.node_id == uid(100)


def test_explicit_mode_is_retained_and_only_mismatched_ports_are_diagnosed():
    edges = [edge(schema_id="prompt_collection"), edge(2, "text", "output")]
    result = diagnose_regex_ports(uid(100), "prompt_collection", edges)
    assert result["mode"] == result["resolved_mode"] == "prompt_collection"
    assert not result["executable"]
    assert result["diagnostics"] == [{
        "code": "regex_port_type_mismatch", "node_id": uid(100),
        "connection_id": uid(2), "port_id": "output", "direction": "output",
        "expected": {"schema_id": "prompt_collection", "version": 1},
        "actual": {"schema_id": "text", "version": 1},
    }]
    result["diagnostics"][0]["actual"]["schema_id"] = "changed"
    assert edges[1]["schema_ref"]["schema_id"] == "text"


@pytest.mark.parametrize("kind,version", [("variable_snapshot", 1), ("text", 2)])
def test_unsupported_types_and_versions_are_editable_diagnostics(kind, version):
    result = diagnose_regex_ports(uid(100), None, [edge(schema_id=kind, version=version)])
    assert not result["executable"]
    assert result["diagnostics"][0]["code"] == "regex_port_unsupported_type"


def test_unconnected_mode_is_unresolved_until_explicitly_selected():
    result = diagnose_regex_ports(uid(100), None, [])
    assert not result["executable"]
    assert result["diagnostics"][0]["code"] == "regex_port_type_unresolved"
    assert require_regex_port_mode(uid(100), "text", []) == "text"


@pytest.mark.parametrize("mutate", [
    lambda edges: edges.append(copy.deepcopy(edges[0])),
    lambda edges: edges[0].update(direction="latest_input"),
    lambda edges: edges[0].update(connection_id="invalid"),
    lambda edges: edges[0].update(port_id=""),
    lambda edges: edges[0].update(extra=True),
    lambda edges: edges[0]["schema_ref"].update(version=True),
    lambda edges: edges[0]["schema_ref"].update(version=0),
    lambda edges: edges[0]["schema_ref"].update(extra=True),
])
def test_malformed_connections_fail_explicitly_without_mutation(mutate):
    edges = [edge()]
    mutate(edges)
    original = copy.deepcopy(edges)
    with pytest.raises(ContractValidationError):
        diagnose_regex_ports(uid(100), "text", edges)
    assert edges == original
