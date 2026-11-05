"""Build a complete definition.update request without performing a command."""

from copy import deepcopy
from uuid import UUID, uuid4


class BatchError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def fail(code, message, **details):
    raise BatchError(code, message, details)


def fields(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        fail("invalid_batch", "Fields differ from the batch contract", required=list(required))


def identity(value):
    try:
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        fail("invalid_identity", "Expected a canonical UUID4", value=value)
    return value


def text(value):
    if type(value) is not str or not value.strip() or len(value) > 128:
        fail("invalid_batch", "Expected nonempty bounded text")
    return value


def prepare_batch(client, batch):
    fields(batch, ("schema_version", "workflow", "operations"), ("idempotency_key", "confirm_derivation"))
    if type(batch["schema_version"]) is not int or batch["schema_version"] != 1:
        fail("invalid_batch", "Unsupported batch schema version")
    workflow = batch["workflow"]
    if type(workflow) is not dict:
        fail("invalid_batch", "workflow must be an object")
    creating = "name" in workflow
    if creating:
        fields(workflow, ("name",), ("id",))
        workflow_id = identity(workflow["id"]) if "id" in workflow else str(uuid4())
        rows = client.query("definition.summaries", {"include_archived": True})
        if any(row["workflow_definition_id"] == workflow_id for row in rows):
            fail("workflow_exists", "Workflow identity already exists", workflow_id=workflow_id)
        catalog = client.query("catalog.node-types", {"protocol_version": 2})
        document = {"schema_version": 3, "workflow_definition_id": workflow_id,
                    "revision": 1, "name": text(workflow["name"]), "nodes": [], "edges": [],
                    "object_bindings": [], "package_lock": deepcopy(catalog["execution_package_lock"])}
        expected = 0
    else:
        fields(workflow, ("id", "revision", "edit_sequence"))
        workflow_id = identity(workflow["id"])
        if (type(workflow["revision"]) is not int or workflow["revision"] < 1
                or type(workflow["edit_sequence"]) is not int or workflow["edit_sequence"] < 0):
            fail("invalid_batch", "Invalid workflow baseline")
        document = deepcopy(client.query("definition.read", {"identity": workflow_id}))
        if (document.get("workflow_definition_id") != workflow_id
                or document.get("revision") != workflow["revision"]
                or document.get("edit_sequence", 0) != workflow["edit_sequence"]):
            fail("stale_baseline", "Workflow revision or edit sequence differs from the current document",
                 expected=deepcopy(workflow),
                 actual={"id": document.get("workflow_definition_id"), "revision": document.get("revision"),
                         "edit_sequence": document.get("edit_sequence", 0)})
        expected = workflow["edit_sequence"]
        catalog = client.query("catalog.node-types", {"protocol_version": 2})
    descriptors = {(row["component_id"], row["component_version"]): row for row in catalog["node_types"]}
    if type(batch["operations"]) is not list or (not creating and not batch["operations"]):
        fail("invalid_batch", "operations must be an array and must be nonempty for an existing workflow")
    aliases, changes, removed = {}, [], set()

    def resolve(value):
        if type(value) is str and value.startswith("@"):
            if value[1:] not in aliases:
                fail("unknown_alias", "Alias is not defined", alias=value)
            value = aliases[value[1:]]
        return identity(value)

    def node(value):
        node_id = resolve(value)
        found = next ( (row for row in document["nodes"] if row["node_binding_id"] == node_id), None )
        if found is None:
            fail("missing_node", "Node does not exist", node_id=node_id)
        return found

    def descriptor(row):
        key = row["component_id"], row["component_version"]
        if key not in descriptors:
            fail("missing_component", "Component version is absent from the current catalog",
                 component_id=key[0], component_version=key[1])
        return descriptors[key]

    def references(row):
        for ref in descriptor(row).get("config_node_references", []):
            key = ref["config_field"]
            if key not in row["config"] or row["config"][key] is None:
                continue
            value = row["config"][key]
            if ref["multiple"]:
                if type(value) is not list:
                    fail("invalid_batch", "Declared multiple node reference must be an array", field=key)
                row["config"][key] = [resolve(item) for item in value]
            elif value != "":
                row["config"][key] = resolve(value)

    for index, action in enumerate(batch["operations"]):
        if type(action) is not dict or type(action.get("operation")) is not str:
            fail("invalid_batch", "Each operation needs an operation name", index=index)
        op = action["operation"]
        if op == "node.add":
            fields(action, ("operation", "component_id", "component_version"),
                   ("as", "node_binding_id", "title", "position", "config", "configuration_policy"))
            template = descriptor(action)
            node_id = identity(action["node_binding_id"]) if "node_binding_id" in action else str(uuid4())
            if node_id in removed or any(row["node_binding_id"] == node_id for row in document["nodes"]):
                fail("duplicate_node", "Node identity already exists", node_id=node_id)
            if "as" in action:
                alias = text(action["as"])
                if alias.startswith("@") or alias in aliases:
                    fail("invalid_alias", "Alias must be unique and omit @", alias=alias)
                aliases[alias] = node_id
            config = deepcopy(template["default_config"])
            if type(action.get("config", {})) is not dict:
                fail("invalid_batch", "Node config must be an object")
            config.update(deepcopy(action.get("config", {})))
            row = {"node_binding_id": node_id, "component_id": action["component_id"],
                   "component_version": action["component_version"],
                   "title": action.get("title", template["display_name"]),
                   "position": deepcopy(action.get("position", {"x": 0, "y": 0})), "config": config}
            if document["schema_version"] == 3:
                row["configuration_policy"] = deepcopy(action.get("configuration_policy", template["default_configuration_policy"]))
            references(row)
            document["nodes"].append(row)
            changes.append({"operation": op, "node_binding_id": node_id})
        elif op == "node.remove":
            fields(action, ("operation", "node_binding_id"))
            row = node(action["node_binding_id"])
            node_id = row["node_binding_id"]
            document["nodes"].remove(row)
            document["edges"] = [edge for edge in document["edges"]
                                 if node_id not in (edge["source_node_id"], edge["target_node_id"])]
            removed.add(node_id)
            changes.append({"operation": op, "node_binding_id": node_id})
        elif op == "node.configure":
            fields(action, ("operation", "node_binding_id"), ("changes", "replace"))
            if ("changes" in action) == ("replace" in action):
                fail("invalid_batch", "Specify exactly one of changes and replace")
            row = node(action["node_binding_id"])
            value = action.get("changes", action.get("replace"))
            if type(value) is not dict:
                fail("invalid_batch", "Configuration must be an object")
            if "replace" in action:
                row["config"] = deepcopy(value)
            else:
                row["config"].update(deepcopy(value))
            references(row)
            changes.append({"operation": op, "node_binding_id": row["node_binding_id"]})
        elif op == "edge.add":
            required = ("operation", "source_node_id", "source_port_id", "target_node_id", "target_port_id", "order")
            fields(action, required, ("edge_id",))
            edge_id = identity(action["edge_id"]) if "edge_id" in action else str(uuid4())
            if any(row["edge_id"] == edge_id for row in document["edges"] + document.get("control_edges", [])):
                fail("duplicate_edge", "Edge identity already exists", edge_id=edge_id)
            if type(action["order"]) is not int or action["order"] < 0:
                fail("invalid_batch", "Edge order must be a nonnegative integer")
            edge = {"edge_id": edge_id, "source_node_id": node(action["source_node_id"])["node_binding_id"],
                    "target_node_id": node(action["target_node_id"])["node_binding_id"],
                    "source_port_id": text(action["source_port_id"]), "target_port_id": text(action["target_port_id"]),
                    "order": action["order"]}
            document["edges"].append(edge)
            changes.append({"operation": op, "edge_id": edge_id})
        elif op == "edge.remove":
            fields(action, ("operation", "edge_id"))
            edge_id = identity(action["edge_id"])
            match = next((row for row in document["edges"] if row["edge_id"] == edge_id), None)
            if match is None:
                fail("missing_edge", "Ordinary edge does not exist", edge_id=edge_id)
            document["edges"].remove(match)
            changes.append({"operation": op, "edge_id": edge_id})
        elif op == "binding.put":
            fields(action, ("operation", "binding"))
            binding = deepcopy(action["binding"])
            fields(binding, ("object_key", "type_id", "schema_version", "scope", "owner_node_id",
                             "registrar_node_id", "readers", "writers"), ("default_value",))
            text(binding["object_key"])
            text(binding["type_id"])
            if (type(binding["schema_version"]) is not int or binding["schema_version"] < 1
                    or binding["scope"] not in ("shared", "private")):
                fail("invalid_binding", "Invalid binding type version or scope")
            for key in ("owner_node_id", "registrar_node_id"):
                if binding[key] is not None:
                    binding[key] = node(binding[key])["node_binding_id"]
            for key in ("readers", "writers"):
                if type(binding[key]) is not list:
                    fail("invalid_binding", "Binding permissions must be arrays")
                binding[key] = [node(value)["node_binding_id"] for value in binding[key]]
                if len(set(binding[key])) != len(binding[key]):
                    fail("invalid_binding", "Binding permissions must be unique")
            owner = binding["owner_node_id"]
            if ((binding["scope"] == "shared" and owner is not None)
                    or (binding["scope"] == "private" and
                        (owner is None or not set(binding["readers"] + binding["writers"]) <= {owner}))):
                fail("invalid_binding", "Binding owner or private permissions are invalid")
            bindings = document["object_bindings"]
            match = next((row for row in bindings if row["object_key"] == binding["object_key"]), None)
            if match is None:
                bindings.append(binding)
            else:
                bindings[bindings.index(match)] = binding
            changes.append({"operation": op, "object_key": binding["object_key"]})
        elif op == "binding.remove":
            fields(action, ("operation", "object_key"))
            key = text(action["object_key"])
            match = next((row for row in document["object_bindings"] if row["object_key"] == key), None)
            if match is None:
                fail("missing_binding", "Binding does not exist", object_key=key)
            document["object_bindings"].remove(match)
            changes.append({"operation": op, "object_key": key})
        else:
            fail("unknown_operation", "Unknown batch operation", operation=op, index=index)

    dependencies = []
    for edge in document.get("control_edges", []):
        if removed.intersection((edge["source_node_id"], edge["target_node_id"])):
            dependencies.append({"kind": "control_edge", "edge_id": edge["edge_id"]})
    for event in document.get("event_bindings", []):
        if removed.intersection(event["target_node_ids"]):
            dependencies.append({"kind": "event_binding", "event_id": event["event_id"]})
    for binding in document.get("object_bindings", []):
        involved = set(binding["readers"] + binding["writers"]) | {binding["owner_node_id"], binding.get("registrar_node_id")}
        if removed.intersection(involved):
            dependencies.append({"kind": "object_binding", "object_key": binding["object_key"]})
    if removed:
        for row in document["nodes"]:
            for ref in descriptor(row).get("config_node_references", []):
                value = row["config"].get(ref["config_field"])
                values = value if ref["multiple"] and type(value) is list else [value]
                if any(value in removed for value in values if type(value) is str):
                    dependencies.append({"kind": "config_reference", "node_binding_id": row["node_binding_id"],
                                         "config_field": ref["config_field"]})
    keys = {binding["object_key"] for binding in document.get("object_bindings", [])}
    for preload in document.get("session_preloads", []):
        if preload["object_key"] not in keys:
            dependencies.append({"kind": "session_preload", "object_key": preload["object_key"]})
    if dependencies:
        fail("remaining_dependencies", "Batch leaves dependencies that require explicit resolution",
             dependencies=dependencies)
    key = batch.get("idempotency_key", str(uuid4()))
    if type(key) is not str or not key.strip():
        fail("invalid_batch", "idempotency_key must be nonempty text")
    if "confirm_derivation" in batch and batch["confirm_derivation"] is not True:
        fail("invalid_batch", "confirm_derivation must be explicitly true")
    parameters = {"workflow_definition_id": workflow_id, "document": document,
                  "expected_edit_sequence": expected, "idempotency_key": key}
    if batch.get("confirm_derivation") is True:
        parameters["confirm_derivation"] = True
    return {"request": {"operation": "definition.update", "parameters": parameters},
            "aliases": aliases, "changes": changes}
