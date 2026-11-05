import argparse
import json
import sys

from . import jsonio
from .batch import BatchError, prepare_batch
from .client import ApiError, GraphClient, TransportError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    p = Parser(prog="oa")
    p.add_argument("--base-url", required=True, help="HTTP origin of the existing service")
    p.add_argument("--timeout", type=float, default=30)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("describe")
    q = sub.add_parser("query")
    q.add_argument("operation")
    q.add_argument("--params")
    c = sub.add_parser("command")
    c.add_argument("--request", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("--component")
    c.add_argument("--version")
    r = sub.add_parser("receipt")
    r.add_argument("--request", required=True)
    w = sub.add_parser("workflow").add_subparsers(dest="workflow_action", required=True)
    r = w.add_parser("read")
    r.add_argument("id")
    r.add_argument("--node")
    r.add_argument("--full", action="store_true")
    e = w.add_parser("edit")
    e.add_argument("--batch", required=True)
    e.add_argument("--request-out", required=True)
    e.add_argument("--apply", action="store_true")
    a = w.add_parser("apply")
    a.add_argument("--request", required=True)
    return p


def _request(path, *, update=False):
    value = jsonio.read(path)
    if (not isinstance(value, dict) or set(value) != {"operation", "parameters"}
            or not isinstance(value["operation"], str) or not isinstance(value["parameters"], dict)):
        raise ValueError("request must contain only operation and parameters")
    if update and value["operation"] != "definition.update":
        raise ValueError("workflow apply requires a definition.update request")
    return value


def _short_document(document, node_id=None):
    result = {key: document[key] for key in (
        "workflow_definition_id", "name", "revision", "edit_sequence",
        "schema_version", "edges", "object_bindings") if key in document}
    nodes = document.get("nodes", [])
    if node_id is not None:
        nodes = [node for node in nodes if node.get("node_binding_id") == node_id]
        if not nodes:
            raise ValueError("node not found")
        result["nodes"] = nodes
        result["edges"] = [edge for edge in document.get("edges", [])
                           if node_id in (edge.get("source_node_id"), edge.get("target_node_id"))]
        result["object_bindings"] = [binding for binding in document.get("object_bindings", [])
                                     if (node_id in binding.get("readers", [])
                                         or node_id in binding.get("writers", [])
                                         or node_id in (binding.get("owner_node_id"),
                                                        binding.get("registrar_node_id")))]
    else:
        result["nodes"] = [{key: node[key] for key in (
            "node_binding_id", "component_id", "component_version", "title") if key in node}
                           for node in nodes]
    return result


def _redact(value, secrets):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if "secret" in key.lower() else _redact(item, secrets))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    return value


def _secrets(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if "secret" in key.lower() and isinstance(item, str) and item:
                found.append(item)
            else:
                found.extend(_secrets(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_secrets(item))
    return found


def _summary_response(response):
    result = response.get("result")
    if isinstance(result, dict) and "document" in result:
        return {**response, "result": {key: value for key, value in result.items() if key != "document"}}
    return response


def _apply(client, request, request_path):
    try:
        response = client.command(request)
    except TransportError as error:
        if error.write_unknown:
            return {"status": "unknown", "message": str(error),
                    "receipt_request_file": request_path}, 1
        raise
    except ApiError as error:
        if error.status >= 500:
            return {"status": "unknown", "http_status": error.status,
                    "response": error.payload, "receipt_request_file": request_path}, 1
        raise
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        return {"status": "unknown", "receipt_request_file": request_path,
                "message": "Command response has no result object"}, 1
    result = response["result"]
    response = _summary_response(response)
    outcome = result.get("outcome")
    if outcome == "confirmation_required":
        return {"status": "confirmation_required", "response": response}, 1
    if outcome not in ("saved", "unchanged", "derived"):
        return {"status": "unknown", "response": response,
                "receipt_request_file": request_path}, 1
    identity = result.get("workflow_definition_id") or request["parameters"]["workflow_definition_id"]
    try:
        document = client.query("definition.read", {"identity": identity})
        if not isinstance(document, dict):
            raise TransportError("Invalid definition.read response")
        try:
            summary = _short_document(document)
        except (TypeError, AttributeError):
            raise TransportError("Invalid definition.read response") from None
    except (ApiError, TransportError) as error:
        details = _error(error)
        return {"status": "saved_read_failed", "response": response, "read_error": details}, 1
    return {"status": "saved", "response": response, "workflow": summary}, 0


def _error(error):
    if isinstance(error, ApiError):
        return {"code": "http_error", "http_status": error.status, "response": error.payload}
    if isinstance(error, TransportError):
        return {"code": "transport_error", "message": str(error),
                "write_result": "unknown" if error.write_unknown else None}
    if isinstance(error, BatchError):
        return {"code": error.code, "message": str(error), "details": error.details}
    return {"code": "invalid_input", "message": str(error)}


def run(args, client):
    if args.action == "describe":
        return client.describe(), 0
    if args.action == "query":
        parameters = jsonio.read(args.params) if args.params else {}
        if not isinstance(parameters, dict):
            raise ValueError("query parameters must be an object")
        return client.query(args.operation, parameters), 0
    if args.action == "command":
        request = _request(args.request)
        secrets = _secrets(request)
        try:
            return _redact(client.command(request), secrets), 0
        except ApiError as error:
            if error.status >= 500:
                return {"status": "unknown", "http_status": error.status,
                        "response": _redact(error.payload, secrets),
                        "receipt_request_file": args.request}, 1
            raise ApiError(error.status, _redact(error.payload, secrets)) from None
        except TransportError as error:
            return {"status": "unknown", "error": _error(error),
                    "receipt_request_file": args.request}, 1
    if args.action == "receipt":
        return client.query("receipt.read", _request(args.request)), 0
    if args.action == "catalog":
        catalog = client.query("catalog.node-types", {"protocol_version": 2})
        items = [item for item in catalog["node_types"]
                 if (args.component is None or item.get("component_id") == args.component)
                 and (args.version is None or item.get("component_version") == args.version)]
        if args.component is None:
            items = [{key: item[key] for key in ("component_id", "component_version", "display_name", "title", "name", "category")
                      if key in item} for item in items]
        return {**catalog, "node_types": items}, 0
    if args.workflow_action == "read":
        document = client.query("definition.read", {"identity": args.id})
        return (document if args.full else _short_document(document, args.node)), 0
    if args.workflow_action == "apply":
        return _apply(client, _request(args.request, update=True), args.request)
    prepared = prepare_batch(client, jsonio.read(args.batch))
    request = prepared["request"]
    jsonio.write_new(args.request_out, request)
    parameters = request["parameters"]
    try:
        preview = client.query("definition.preview", {
            "workflow_definition_id": parameters["workflow_definition_id"],
            "document": parameters["document"],
            "expected_edit_sequence": parameters["expected_edit_sequence"]})
    except (ApiError, TransportError) as error:
        return {"status": "preview_failed", "request_file": args.request_out,
                "aliases": prepared["aliases"], "changes": prepared["changes"],
                "error": _error(error)}, 1
    output = {"status": "prepared", "request_file": args.request_out,
              "aliases": prepared["aliases"], "changes": prepared["changes"], "preview": preview}
    if args.apply:
        output["apply"], code = _apply(client, request, args.request_out)
        return output, code
    return output, 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        result, code = run(args, GraphClient(args.base_url, args.timeout))
    except (ValueError, OSError, json.JSONDecodeError, ApiError, TransportError, BatchError) as error:
        result, code = {"error": _error(error)}, 1
    print(jsonio.dumps(result))
    return code
