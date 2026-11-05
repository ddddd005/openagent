import argparse
import tempfile
import unittest
from pathlib import Path

from openagent_cli import cli, jsonio
from openagent_cli.client import TransportError


class Stub:
    def __init__(self, result=None, error=None):
        self.result = result or {"result": {"outcome": "saved", "workflow_definition_id": "id"}}
        self.error = error
        self.requests = []

    def command(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.result

    def query(self, operation, parameters=None):
        self.requests.append((operation, parameters))
        return {"workflow_definition_id": "id", "revision": 1, "edit_sequence": 2,
                "nodes": [], "edges": [], "object_bindings": []}


class CliTests(unittest.TestCase):
    def test_apply_and_confirmation(self):
        request = {"operation": "definition.update", "parameters": {"workflow_definition_id": "id"}}
        client = Stub()
        result, code = cli._apply(client, request, "request.json")
        self.assertEqual((code, result["status"]), (0, "saved"))
        self.assertIs(client.requests[0], request)
        self.assertEqual(client.requests[1], ("definition.read", {"identity": "id"}))
        client = Stub({"result": {"outcome": "confirmation_required"}})
        result, code = cli._apply(client, request, "request.json")
        self.assertEqual((code, result["status"], len(client.requests)), (1, "confirmation_required", 1))
        client = Stub(error=TransportError("offline", write_unknown=True))
        result, code = cli._apply(client, request, "request.json")
        self.assertEqual((code, result["status"], result["receipt_request_file"]), (1, "unknown", "request.json"))

    def test_node_references(self):
        document = {"nodes": [{"node_binding_id": "n", "config": {"x": 1}}],
                    "edges": [{"source_node_id": "n", "target_node_id": "m"}],
                    "object_bindings": [{"readers": ["n"]}, {"writers": ["n"]},
                                        {"owner_node_id": "n"}, {"registrar_node_id": "n"},
                                        {"readers": ["m"]}]}
        output = cli._short_document(document, "n")
        self.assertEqual(len(output["object_bindings"]), 4)
        self.assertEqual(output["nodes"][0]["config"], {"x": 1})

    def test_saved_then_read_failure_is_not_reported_as_failed_write(self):
        class ReadFailure(Stub):
            def query(self, operation, parameters=None):
                raise TransportError("read unavailable")
        request = {"operation": "definition.update", "parameters": {"workflow_definition_id": "id"}}
        result, code = cli._apply(ReadFailure(), request, "request.json")
        self.assertEqual((code, result["status"]), (1, "saved_read_failed"))
        self.assertEqual(result["response"]["result"]["outcome"], "saved")

    def test_non_object_read_after_save_reports_read_failure(self):
        class InvalidRead(Stub):
            def query(self, operation, parameters=None):
                return []
        request = {"operation": "definition.update", "parameters": {"workflow_definition_id": "id"}}
        result, code = cli._apply(InvalidRead(), request, "request.json")
        self.assertEqual((code, result["status"]), (1, "saved_read_failed"))
        self.assertEqual(result["response"]["result"]["outcome"], "saved")

    def test_generic_command_transport_unknown_has_top_level_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "request.json")
            jsonio.write_new(path, {"operation": "test", "parameters": {}})
            args = argparse.Namespace(action="command", request=path)
            result, code = cli.run(args, Stub(error=TransportError("offline", write_unknown=True)))
            self.assertEqual((code, result["status"], result["receipt_request_file"]),
                             (1, "unknown", path))

    def test_unexpected_command_response_is_unknown(self):
        request = {"operation": "definition.update", "parameters": {"workflow_definition_id": "id"}}
        for response in ([], {"result": None}, {"other": 1}):
            client = Stub()
            client.result = response
            result, code = cli._apply(client, request, "request.json")
            self.assertEqual((code, result["status"]), (1, "unknown"))
            self.assertEqual(len(client.requests), 1)

    def test_catalog_summary_includes_display_name(self):
        class Catalog:
            def query(self, operation, parameters):
                return {"node_types": [{"component_id": "x", "component_version": "1",
                                        "display_name": "Example", "config_schema": {}}]}
        args = argparse.Namespace(action="catalog", component=None, version=None)
        output, code = cli.run(args, Catalog())
        self.assertEqual(code, 0)
        self.assertEqual(output["node_types"][0]["display_name"], "Example")
        self.assertNotIn("config_schema", output["node_types"][0])

    def test_request_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "request.json")
            jsonio.write_new(path, {"first": True})
            with self.assertRaises(FileExistsError):
                jsonio.write_new(path, {"second": True})
            self.assertEqual(jsonio.read(path), {"first": True})

    def test_secret_redaction(self):
        self.assertEqual(cli._redact({"error": "oops password", "secret": "password"},
                                     ["password"]),
                         {"error": "oops [REDACTED]", "secret": "[REDACTED]"})


if __name__ == "__main__":
    unittest.main()
