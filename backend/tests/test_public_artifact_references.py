"""Public artifact roots retain direct references without granting nested reads."""

from contextlib import closing

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.graph_contracts import NodeDefinition, NodePort
from phase1_agent.graph_service import GraphWorkflowService
from phase1_agent.host_sdk import DataTypeDefinition, HostContractError, TypeRegistry
from phase1_agent.graph_application import GraphApplication
from test_graph_service import create, document, edge, node, run, uid


def test_public_reference_hooks_are_direct_retained_exports_and_survive_detach():
    refs = [{"scope": "artifact", "output_id": uid(1310)}, {"scope": "artifact", "output_id": uid(1311)}]
    value = {"schema_version": 1, "direct": refs[0], "private": refs[1]}
    types = TypeRegistry()
    types.register(DataTypeDefinition(
        "sample.exports", 1, {"type": "object"}, scope="content",
        references=lambda payload: [payload["direct"], payload["private"]],
        public_references=lambda payload: [payload["direct"]]))
    frozen = types.detached(frozen=True)
    assert frozen.get("sample.exports", 1, scope="content").to_dict()["has_public_references"] is True
    assert frozen.public_artifact_references("sample.exports", 1, value) == [refs[0]]
    types.register(DataTypeDefinition(
        "sample.retention", 1, {"type": "object"}, scope="content", references=lambda payload: refs))
    with pytest.raises(HostContractError) as denied:
        types.public_artifact_references("sample.retention", 1, value)
    assert denied.value.reason_code == "output_reference_denied"
    types.register(DataTypeDefinition(
        "sample.invalid-export", 1, {"type": "object"}, scope="content",
        references=lambda payload: [refs[0]], public_references=lambda payload: [refs[1]]))
    with pytest.raises(HostContractError) as denied:
        types.public_artifact_references("sample.invalid-export", 1, value)
    assert denied.value.reason_code == "host_invalid_references"


def test_artifact_read_does_not_expand_nested_reference_retention(tmp_path):
    with closing(GraphWorkflowService(tmp_path / "public-nested-pointer.sqlite")) as service:
        service.registry.data_types.register(DataTypeDefinition(
            "sample.public-pointer", 1, {"type": "object"}, scope="content",
            references=lambda value: [value["reference"]],
            public_references=lambda value: [value["reference"]]))
        service.registry.register(NodeDefinition(
            "sample.pointer", "1", "Pointer", "test", {}, {"type": "object"},
            inputs=(NodePort("input", "TEXT", data_schema_version=2),),
            outputs=(NodePort("output", "sample.public-pointer"),),
            input_storage="references"), lambda config, inputs, context: {
                "output": {"schema_version": 1, "reference": {
                    "scope": "artifact", "output_id": context.input_artifact_refs("input")[0]["output_id"]}}})
        service.registry.register(NodeDefinition(
            "sample.outer", "1", "Outer", "test", {}, {"type": "object"},
            inputs=(NodePort("input", "sample.public-pointer"),),
            outputs=(NodePort("output", "sample.public-pointer"),),
            is_output=True, input_storage="references"), lambda config, inputs, context: {
                "output": {"schema_version": 1, "reference": {
                    "scope": "artifact", "output_id": context.input_artifact_refs("input")[0]["output_id"]}}})
        source = node(service.registry, "tools.text", 1320, text="private nested")
        inner = node(service.registry, "sample.pointer", 1321)
        outer = node(service.registry, "sample.outer", 1322)
        outer["public_outputs"] = ["output"]
        graph = document([source, inner, outer], [edge(source, inner, 1320), edge(inner, outer, 1321)])
        final = run(service, create(service, graph))
        response = service.read_public_output(
            final["workflow_session_id"], workflow_definition_id=graph["workflow_definition_id"],
            definition_revision=graph["revision"], node_id=outer["node_binding_id"],
            port_id="output")["output"]
        parameters = {
            "session_id": final["workflow_session_id"], "workflow_definition_id": graph["workflow_definition_id"],
            "definition_revision": graph["revision"], "node_id": outer["node_binding_id"],
            "port_id": "output", "run_id": response["run_id"],
        }
        app = GraphApplication(service)
        direct = app.query("output.artifact.read", {
            **parameters, "reference": response["payload"]["reference"]})
        assert direct["data_type"] == "sample.public-pointer"
        with pytest.raises(ContractValidationError) as denied:
            app.query("output.artifact.read", {
                **parameters, "reference": direct["value"]["reference"]})
        assert denied.value.reason_code == "output_reference_denied"
