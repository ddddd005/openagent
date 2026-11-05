import io
import json
import unittest
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from openagent_cli import jsonio
from openagent_cli.client import ApiError, GraphClient, TransportError


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data


class ClientTests(unittest.TestCase):
    def test_routes_and_origin(self):
        calls = []
        def open_request(request, timeout):
            calls.append((request, timeout))
            return Response(b'{"result":{}}')
        with patch("openagent_cli.client.urlopen", open_request):
            client = GraphClient("http://127.0.0.1:4321", 7)
            client.describe()
            client.query("definition.read", {"identity": "x"})
            client.command({"operation": "definition.update", "parameters": {}})
        self.assertEqual([r.full_url for r, _ in calls], [
            "http://127.0.0.1:4321/api/graph/application",
            "http://127.0.0.1:4321/api/graph/queries",
            "http://127.0.0.1:4321/api/graph/commands"])
        self.assertEqual(calls[1][0].get_header("Origin"), "http://127.0.0.1:4321")
        self.assertEqual(json.loads(calls[1][0].data), {
            "operation": "definition.read", "parameters": {"identity": "x"}})

    def test_error_and_unknown_write(self):
        error = HTTPError("http://localhost", 409, "Conflict", {}, io.BytesIO(
            b'{"error":{"code":"stale","diagnostics":[1]}}'))
        with patch("openagent_cli.client.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as caught:
                GraphClient("http://localhost:42").describe()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload["error"]["diagnostics"], [1])
        with patch("openagent_cli.client.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(TransportError) as caught:
                GraphClient("http://localhost:42").command({"operation": "x", "parameters": {}})
        self.assertTrue(caught.exception.write_unknown)

    def test_strict_json(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                jsonio.loads(text)

    def test_query_can_return_native_array(self):
        with patch("openagent_cli.client.urlopen", return_value=Response(b'[{"id":"x"}]')):
            self.assertEqual(GraphClient("http://localhost:42").query("definition.list"), [{"id": "x"}])

    def test_truncated_write_response_is_unknown(self):
        response = Response(b"")
        response.read = lambda: (_ for _ in ()).throw(IncompleteRead(b'{"result":'))
        with patch("openagent_cli.client.urlopen", return_value=response):
            with self.assertRaises(TransportError) as caught:
                GraphClient("http://localhost:42").command({"operation": "x", "parameters": {}})
        self.assertTrue(caught.exception.write_unknown)


if __name__ == "__main__":
    unittest.main()
