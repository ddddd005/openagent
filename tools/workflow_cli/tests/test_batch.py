"""Offline fixtures mirror graph_contracts and catalog.node-types protocol v2."""

import copy
import unittest
from uuid import uuid4

from openagent_cli.batch import BatchError, prepare_batch


def uid():
    return str(uuid4())


DESCRIPTOR = {
    "component_id": "test.node", "component_version": "1", "display_name": "Node",
    "default_config": {"nested": {"a": 1, "b": 2}, "body": "@source"},
    "default_configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"},
    "config_node_references": [{"config_field": "source_ref", "multiple": False,
                                "allow_empty": True, "target_types": []}],
}


def document():
    return {"schema_version": 3, "workflow_definition_id": uid(), "revision": 2,
            "edit_sequence": 5, "name": "Example", "nodes": [], "edges": [],
            "object_bindings": [], "package_lock": [{"package_id": "test", "version": "1"}]}


def baseline(doc):
    return {"id": doc["workflow_definition_id"], "revision": doc["revision"],
            "edit_sequence": doc["edit_sequence"]}


class Client:
    def __init__(self, doc):
        self.doc = doc
        self.commands = []
        self.queries = []

    def query(self, operation, parameters=None):
        self.queries.append((operation, parameters))
        if operation == "definition.read":
            return copy.deepcopy(self.doc)
        if operation == "definition.summaries":
            return [{"workflow_definition_id": self.doc["workflow_definition_id"], "archived": True}]
        if operation == "catalog.node-types":
            return {"schema_version": 2, "node_types": [copy.deepcopy(DESCRIPTOR)],
                    "execution_package_lock": [{"package_id": "test", "version": "1"}]}
        raise AssertionError(operation)

    def command(self, request):
        self.commands.append(request)
        raise AssertionError("batch preparation must not write")


class BatchTests(unittest.TestCase):
    def test_alias_connection_configuration_and_binding(self):
        doc = document()
        client = Client(doc)
        batch = {"schema_version": 1, "workflow": baseline(doc), "operations": [
            {"operation": "node.add", "component_id": "test.node", "component_version": "1", "as": "source"},
            {"operation": "node.add", "component_id": "test.node", "component_version": "1", "as": "sink",
             "config": {"source_ref": "@source", "nested": {"c": 3}}},
            {"operation": "edge.add", "source_node_id": "@source", "source_port_id": "out",
             "target_node_id": "@sink", "target_port_id": "in", "order": 0},
            {"operation": "binding.put", "binding": {
                "object_key": "item", "type_id": "test.item", "schema_version": 1, "scope": "shared",
                "owner_node_id": None, "registrar_node_id": "@source",
                "readers": ["@sink"], "writers": ["@source"]}},
        ]}
        original = copy.deepcopy(batch)
        result = prepare_batch(client, batch)
        graph = result["request"]["parameters"]["document"]
        source, sink = result["aliases"]["source"], result["aliases"]["sink"]
        self.assertEqual(graph["nodes"][1]["config"], {"source_ref": source, "nested": {"c": 3}, "body": "@source"})
        self.assertEqual(graph["edges"][0]["source_node_id"], source)
        self.assertEqual(graph["object_bindings"][0]["readers"], [sink])
        self.assertEqual(graph["package_lock"], doc["package_lock"])
        self.assertEqual(batch, original)
        self.assertEqual(client.commands, [])

    def test_failure_has_no_partial_mutation_or_command(self):
        doc = document()
        client = Client(doc)
        batch = {"schema_version": 1, "workflow": baseline(doc), "operations": [
            {"operation": "node.add", "component_id": "test.node", "component_version": "1", "as": "a"},
            {"operation": "node.remove", "node_binding_id": uid()}]}
        original = copy.deepcopy(batch)
        with self.assertRaises(BatchError) as caught:
            prepare_batch(client, batch)
        self.assertEqual(caught.exception.code, "missing_node")
        self.assertEqual(batch, original)
        self.assertEqual(doc["nodes"], [])
        self.assertEqual(client.commands, [])

    def test_stale_baseline(self):
        doc = document()
        batch = {"schema_version": 1, "workflow": baseline(doc), "operations": [
            {"operation": "node.remove", "node_binding_id": uid()}]}
        batch["workflow"]["edit_sequence"] -= 1
        with self.assertRaises(BatchError) as caught:
            prepare_batch(Client(doc), batch)
        self.assertEqual(caught.exception.code, "stale_baseline")

    def test_removal_preserves_unrelated_and_checks_dependencies_at_end(self):
        doc = document()
        a, b, c = uid(), uid(), uid()
        for node_id in (a, b, c):
            doc["nodes"].append({"node_binding_id": node_id, "component_id": "test.node",
                                 "component_version": "1", "title": "", "position": {"x": 0, "y": 0},
                                 "config": {}, "configuration_policy": {"schema_version": 1, "prompt_mode": "fixed"}})
        first, second = uid(), uid()
        doc["edges"] = [{"edge_id": first, "source_node_id": a, "target_node_id": b,
                         "source_port_id": "o", "target_port_id": "i", "order": 0},
                        {"edge_id": second, "source_node_id": b, "target_node_id": c,
                         "source_port_id": "o", "target_port_id": "i", "order": 0}]
        doc["nodes"][2]["config"]["source_ref"] = a
        batch = {"schema_version": 1, "workflow": baseline(doc), "operations": [
            {"operation": "node.remove", "node_binding_id": a}]}
        with self.assertRaises(BatchError) as caught:
            prepare_batch(Client(doc), batch)
        self.assertEqual(caught.exception.code, "remaining_dependencies")
        batch["operations"].append({"operation": "node.configure", "node_binding_id": c,
                                    "changes": {"source_ref": ""}})
        graph = prepare_batch(Client(doc), batch)["request"]["parameters"]["document"]
        self.assertEqual([row["node_binding_id"] for row in graph["nodes"]], [b, c])
        self.assertEqual([row["edge_id"] for row in graph["edges"]], [second])

    def test_new_workflow_and_replacement(self):
        client = Client(document())
        batch = {"schema_version": 1, "workflow": {"name": "New"}, "operations": [
            {"operation": "node.add", "component_id": "test.node", "component_version": "1", "as": "n"},
            {"operation": "node.configure", "node_binding_id": "@n", "changes": {"nested": {"x": 1}}}]}
        result = prepare_batch(client, batch)
        graph = result["request"]["parameters"]["document"]
        self.assertEqual(graph["nodes"][0]["config"]["nested"], {"x": 1})
        self.assertEqual(graph["nodes"][0]["config"]["body"], "@source")
        self.assertEqual(result["request"]["parameters"]["expected_edit_sequence"], 0)
        batch["operations"][-1] = {"operation": "node.configure", "node_binding_id": "@n", "replace": {"z": 2}}
        graph = prepare_batch(client, batch)["request"]["parameters"]["document"]
        self.assertEqual(graph["nodes"][0]["config"], {"z": 2})

    def test_new_id_cannot_overwrite_archived_or_current_definition(self):
        doc = document()
        batch = {"schema_version": 1, "workflow": {"name": "New", "id": doc["workflow_definition_id"]},
                 "operations": [{"operation": "node.remove", "node_binding_id": uid()}]}
        with self.assertRaises(BatchError) as caught:
            prepare_batch(Client(doc), batch)
        self.assertEqual(caught.exception.code, "workflow_exists")

    def test_empty_new_workflow_builds_request_without_writing(self):
        client = Client(document())
        workflow_id = uid()
        batch = {"schema_version": 1, "workflow": {"name": "Empty", "id": workflow_id},
                 "operations": []}
        result = prepare_batch(client, batch)
        request = result["request"]
        graph = request["parameters"]["document"]
        self.assertEqual(request["operation"], "definition.update")
        self.assertEqual(request["parameters"]["workflow_definition_id"], workflow_id)
        self.assertEqual(request["parameters"]["expected_edit_sequence"], 0)
        self.assertEqual(graph["revision"], 1)
        self.assertEqual(graph["name"], "Empty")
        self.assertEqual((graph["nodes"], graph["edges"], graph["object_bindings"]), ([], [], []))
        self.assertEqual(graph["package_lock"], [{"package_id": "test", "version": "1"}])
        self.assertEqual((result["aliases"], result["changes"]), ({}, []))
        self.assertIn(("definition.summaries", {"include_archived": True}), client.queries)
        self.assertEqual(client.commands, [])

    def test_empty_existing_batch_is_rejected(self):
        doc = document()
        client = Client(doc)
        with self.assertRaises(BatchError) as caught:
            prepare_batch(client, {"schema_version": 1, "workflow": baseline(doc), "operations": []})
        self.assertEqual(caught.exception.code, "invalid_batch")
        self.assertEqual(client.commands, [])


if __name__ == "__main__":
    unittest.main()
