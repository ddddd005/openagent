"""Bounded, single-pass preparation nodes, separate from workflow execution."""

from __future__ import annotations

import copy
import json
import re
import hashlib
from typing import Any
from uuid import UUID

from .contract_json import canonical_bytes, content_digest, validate_json_value
from .prompt_errors import PromptProcessingError
from .prompt_regex import RegexLimits, regex_replace_many, regex_rule_from_dict
from .prompt_values import validate_context_view, validate_prompt_collection
from .workbench_resources import (
    session_data_entry, validate_data_definition, validate_data_value, validate_session_data,
    validate_global_content,
)


MAX_PROGRAM_NODES = 128
MAX_PROGRAM_CHARS = 1_000_000
_NAME = re.compile(r"(?:[^\W\d]|_)\w*", re.UNICODE)
_MACRO = re.compile(r"\{\{((?:[^\W\d]|_)\w*)\}\}", re.UNICODE)
_TYPES = {"string", "integer", "number", "boolean"}
_PRESETS = {"workflow_session_id", "node_binding_id"}
_KINDS = {
    "text", "prompt-source", "context-source", "prompt-collector",
    "text-to-prompt", "prompt-to-text", "regex", "variable-register",
    "variable-assign", "variable-replace",
    "session-data-read", "session-data-write", "json-to-text",
    "global-source",
}


def _fail(code: str, message: str, node_id: str | None = None) -> None:
    raise PromptProcessingError(code, message, node_id=node_id)


def _require(condition: bool, message: str, node_id: str | None = None) -> None:
    if not condition:
        _fail("invalid_preparation_program", message, node_id)


def _fields(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    _require(type(value) is dict and required <= set(value)
             and set(value) <= required | (optional or set()), "Preparation fields are invalid")


def validate_variable_name(name: Any) -> str:
    _require(type(name) is str and 0 < len(name) <= 128
             and (name[0].isalpha() or name[0] == "_")
             and all(character.isalnum() or character == "_" for character in name),
             "Variable name must be a Unicode identifier")
    _require(name not in _PRESETS, "Preset variables are read-only")
    return name


def validate_typed_value(value_type: str, value: Any) -> None:
    accepted = {
        "string": type(value) is str, "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
    }
    if value_type not in _TYPES or not accepted[value_type]:
        _fail("variable_type_mismatch", "Variable value differs from its declared type")
    if value_type == "integer" and not -(2**53 - 1) <= value <= 2**53 - 1:
        _fail("variable_type_mismatch", "Integer exceeds the interoperable safe range")
    validate_json_value(value)
    if type(value) is str and len(value) > MAX_PROGRAM_CHARS:
        _fail("variable_value_limit", "Variable text exceeds the preparation limit")


def _uuid(value: Any) -> None:
    try:
        parsed = UUID(value) if type(value) is str else None
    except ValueError:
        parsed = None
    _require(parsed is not None and parsed.version == 4 and str(parsed) == value,
             "Prompt instance must use a canonical UUID4")


def validate_preparation_program(value: Any) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, {"schema_version", "kind", "nodes", "outputs"})
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1
             and value["kind"] == "prompt_preparation_program", "Unknown preparation program version")
    _require(type(value["nodes"]) is list and len(value["nodes"]) <= MAX_PROGRAM_NODES,
             "Preparation program must be a bounded ordered node array")
    seen: set[str] = set()
    kinds: dict[str, str] = {}
    declarations: dict[str, str] = {}
    for node in value["nodes"]:
        _fields(node, {"node_id", "kind", "inputs", "config"}, {"public_outputs", "output_ports"})
        node_id, kind, inputs, config = (node[key] for key in ("node_id", "kind", "inputs", "config"))
        _require(type(node_id) is str and 0 < len(node_id) <= 128 and node_id not in seen,
                 "Preparation node identity is missing or repeated")
        _require(type(kind) is str and kind in _KINDS, "Unknown preparation node kind", node_id)
        _require(type(inputs) is dict and all(type(port) is str and port for port in inputs)
                 and all(type(target) is str and target in seen for target in inputs.values()),
                 "Preparation inputs must refer to earlier nodes", node_id)
        _require(type(config) is dict, "Preparation node config must be an object", node_id)
        if "output_ports" in node:
            ports = node["output_ports"]
            _require(type(ports) is list and 0 < len(ports) <= 4
                     and all(type(identity) is str for identity in ports)
                     and len(ports) == len(set(ports))
                     and set(ports) <= set(_supported_node_output_ids(node)),
                     "Output ports differ from the node's material type", node_id)
        if "public_outputs" in node:
            outputs = node["public_outputs"]
            _require(type(outputs) is list and len(outputs) <= 4
                     and all(type(identity) is str for identity in outputs)
                     and len(outputs) == len(set(outputs))
                     and set(outputs) <= set(node_output_ids(node)),
                     "Public outputs differ from the node's typed ports", node_id)
        if kind == "text":
            _require(not inputs and (set(config) == {"text"} and type(config["text"]) is str
                                     or config == {"source": "root_input"}),
                     "Text node requires literal text or explicit root input", node_id)
        elif kind == "prompt-source":
            _fields(config, {"instances"})
            _require(not inputs and type(config["instances"]) is list,
                     "Prompt source requires exact instances", node_id)
            for instance in config["instances"]:
                _fields(instance, {"group_instance_id", "item_instance_id"})
                _uuid(instance["item_instance_id"])
                if instance["group_instance_id"] is not None:
                    _uuid(instance["group_instance_id"])
        elif kind == "context-source":
            _fields(config, set(), {"binding_id"})
            _require(not inputs, "Context source cannot accept arbitrary messages", node_id)
            if "binding_id" in config:
                _uuid(config["binding_id"])
        elif kind == "global-source":
            _fields(config, {"resource_id"}, {"record"})
            _uuid(config["resource_id"])
            _require(not inputs, "Global source cannot accept arbitrary inputs", node_id)
            if "record" in config:
                record = validate_global_content(config["record"])
                _require(record["resource_id"] == config["resource_id"] and record["enabled"],
                         "Resolved global content differs from its reference", node_id)
        elif kind in ("session-data-read", "session-data-write"):
            _fields(config, {"definition"} if kind == "session-data-read" else {"definition", "value"})
            definition = validate_data_definition(config["definition"])
            _require(not inputs if kind == "session-data-read" else set(inputs) <= {"text"},
                     "Session data node input is invalid", node_id)
            if kind == "session-data-write":
                _require(definition["writable"], "Session data is read-only", node_id)
                if not inputs:
                    validate_data_value(definition, config["value"])
        elif kind == "json-to-text":
            _require(set(inputs) == {"input"} and not config, "JSON conversion requires one input", node_id)
        elif kind == "prompt-collector":
            _fields(config, set(), {"input_order"})
            if "input_order" in config:
                order = config["input_order"]
                _require(type(order) is list and all(type(port) is str for port in order)
                         and len(order) == len(set(order)) and set(order) == set(inputs),
                         "Collector order must cover each input once", node_id)
        elif kind == "text-to-prompt":
            _fields(config, {"item_instance_id", "role", "placement", "depth", "order"}, {"enabled"})
            _uuid(config["item_instance_id"])
            _require(set(inputs) == {"input"} and config["role"] in ("system", "user", "assistant")
                     and config["placement"] in ("before", "middle", "after")
                     and type(config["order"]) is int
                     and (type(config["depth"]) is int and config["depth"] >= 0
                          if config["placement"] == "middle" else config["depth"] is None),
                     "Text conversion placement is invalid", node_id)
            _require("enabled" not in config or type(config["enabled"]) is bool,
                     "Text conversion enabled flag is invalid", node_id)
        elif kind == "prompt-to-text":
            _fields(config, {"separator"})
            _require(set(inputs) == {"input"} and type(config["separator"]) is str,
                     "Prompt text conversion is invalid", node_id)
        elif kind in ("regex", "variable-replace"):
            _fields(config, {"mode", "rule"} if kind == "regex" else {"mode"})
            _require(set(inputs) == {"input"} and config["mode"] in ("text", "prompt"),
                     "Processing node mode or input is invalid", node_id)
            if kind == "regex":
                regex_rule_from_dict(config["rule"])
        elif kind == "variable-register":
            _fields(config, {"name", "type"}, {"initial"})
            name = validate_variable_name(config["name"])
            _require(type(config["type"]) is str and config["type"] in _TYPES and set(inputs) <= {"text"},
                     "Variable registration type or source is invalid", node_id)
            _require(name not in declarations or declarations[name] == config["type"],
                     "Variable declarations have different types", node_id)
            declarations[name] = config["type"]
            if "initial" in config:
                validate_typed_value(config["type"], config["initial"])
            _require(not inputs or config["type"] == "string",
                     "Explicit text registration requires a string variable", node_id)
        else:
            _fields(config, {"name", "operation"}, {"value"})
            validate_variable_name(config["name"])
            _require(config["operation"] in ("set", "add", "subtract") and set(inputs) <= {"text"}
                     and (bool(inputs) != ("value" in config)),
                     "Assignment requires one explicit value source", node_id)
        seen.add(node_id)
        kinds[node_id] = kind
    _fields(value["outputs"], {"prompt", "context"})
    _require(all(target is None or type(target) is str and target in seen
                 for target in value["outputs"].values()), "Program output is unavailable")
    _require(len(canonical_bytes(value)) <= 2_000_000, "Preparation program exceeds its wire limit")
    return copy.deepcopy(value)


def _supported_node_output_ids(node):
    kind = node["kind"]
    if kind == "context-source":
        return ("context",)
    if kind in ("session-data-read", "session-data-write"):
        return ("json",)
    if kind in ("regex", "variable-replace"):
        return (node["config"].get("mode", ""),)
    if kind == "prompt-source":
        return ("prompt", "tool-descriptions", "tool-schemas")
    if kind == "prompt-collector":
        return ("prompt", "tool-descriptions", "tool-schemas")
    if kind in ("global-source", "text-to-prompt"):
        return ("prompt",)
    if kind == "text" and node["config"].get("source") == "root_input":
        return ("current-input",)
    return ("text",)


def node_output_ids(node):
    return tuple(node.get("output_ports", _supported_node_output_ids(node)))


def project_node_outputs(node, material, identities=None, *, program=None):
    """Canvas ports and session reads share the frozen producer material."""
    result = {}
    for identity in identities if identities is not None else node_output_ids(node):
        output = copy.deepcopy(material)
        if identity.startswith("tool-") and material["kind"] == "prompt":
            by_id = {entry["node_id"]: entry for entry in (program or {}).get("nodes", [])}

            def tool_instances(producer):
                if producer["kind"] == "prompt-source":
                    offset = 0 if identity == "tool-descriptions" else 1
                    return {entry["item_instance_id"] for entry in producer["config"]["instances"][offset::2]}
                return set().union(*(tool_instances(by_id[source])
                                     for source in producer["inputs"].values() if source in by_id))

            selected = tool_instances(node)
            output["items"] = [item for item in output["items"] if item["item_instance_id"] in selected]
        result[identity] = output
    return result


def selected_context_binding(program: dict, default: str) -> str:
    """Find the archive reaching assembly, not every source on the canvas."""
    by_id = {node["node_id"]: node for node in program["nodes"]}
    found, seen = set(), set()

    def visit(identity):
        if identity is None or identity in seen:
            return
        seen.add(identity)
        node = by_id[identity]
        if node["kind"] == "context-source":
            found.add(node["config"].get("binding_id", default))
        for source in node["inputs"].values():
            visit(source)

    for identity in program["outputs"].values():
        visit(identity)
    _require(len(found) <= 1, "An assembly cannot merge two canonical Agent archives")
    return next(iter(found), default)


def program_definitions(program: dict[str, Any]) -> list[dict[str, Any]]:
    program = validate_preparation_program(program)
    definitions: dict[str, dict[str, Any]] = {}
    for node in program["nodes"]:
        if node["kind"] != "variable-register":
            continue
        config = node["config"]
        definition = {"name": config["name"], "type": config["type"]}
        if "initial" in config and not node["inputs"]:
            definition["default"] = config["initial"]
        if config["name"] in definitions:
            _require(canonical_bytes(definitions[config["name"]]) == canonical_bytes(definition),
                     "Repeated variable declarations differ", node["node_id"])
        definitions[config["name"]] = definition
    return list(definitions.values())


def validate_program_state(value: Any) -> dict[str, Any]:
    validate_json_value(value)
    _fields(value, {"revision", "values"}, {"data"})
    _require(type(value["revision"]) is int and value["revision"] >= 0
             and type(value["values"]) is dict and len(value["values"]) <= 512,
             "Program variable state is invalid")
    for name, entry in value["values"].items():
        validate_variable_name(name)
        _fields(entry, {"type", "source"}, {"value"})
        _require(type(entry["type"]) is str and entry["type"] in _TYPES
                 and entry["source"] in ("default", "assignment", "unassigned"),
                 "Program variable entry is invalid")
        _require(("value" in entry) == (entry["source"] != "unassigned"),
                 "Program variable assignment status differs")
        if "value" in entry:
            validate_typed_value(entry["type"], entry["value"])
    _require(sum(len(entry["value"]) for entry in value["values"].values()
                 if type(entry.get("value")) is str) <= MAX_PROGRAM_CHARS,
             "Session variables exceed their total text limit")
    if "data" in value:
        validate_session_data(value["data"])
    return copy.deepcopy(value)


def merge_program_definitions(state: Any, definitions: list[dict[str, Any]]) -> dict[str, Any]:
    result = validate_program_state(state)
    for definition in definitions:
        name, value_type = definition["name"], definition["type"]
        existing = result["values"].get(name)
        if existing is not None:
            if existing["type"] != value_type:
                _fail("variable_type_conflict", "Current variable has another declared type")
            continue
        result["values"][name] = {
            "type": value_type, "source": "default" if "default" in definition else "unassigned",
            **({"value": definition["default"]} if "default" in definition else {}),
        }
    return validate_program_state(result)


def variable_text(state: dict[str, Any], name: str, node_id: str) -> str:
    entry = state["values"].get(name)
    if entry is None or "value" not in entry:
        _fail("missing_macro_variable", "Variable is unknown or unassigned", node_id)
    value = entry["value"]
    return value if type(value) is str else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _replace(text: str, state: dict, presets: dict, node_id: str) -> str:
    parts, position, size, count = [], 0, 0, 0
    for match in _MACRO.finditer(text):
        name = match.group(1)
        replacement = presets[name] if name in presets else variable_text(state, name, node_id)
        literal = text[position:match.start()]
        size += len(literal) + len(replacement)
        count += 1
        if size > MAX_PROGRAM_CHARS or count > 10_000:
            _fail("macro_output_limit", "Macro output exceeds the preparation limit", node_id)
        parts.extend((literal, replacement))
        position = match.end()
    tail = text[position:]
    if size + len(tail) > MAX_PROGRAM_CHARS:
        _fail("macro_output_limit", "Macro output exceeds the preparation limit", node_id)
    parts.append(tail)
    return "".join(parts)


def _material(items: list[dict] | None = None, view: dict | None = None) -> dict:
    return {"kind": "prompt", "items": [] if items is None else copy.deepcopy(items),
            "view": copy.deepcopy(view)}


def _targets(view: dict) -> list[tuple[str, int]]:
    protected = {(entry["message_id"], entry["block_index"]) for entry in view["protected_blocks"]}
    source_roles = {"human": "user", "upstream_node": "user", "model": "assistant"}
    return [
        (message["message_id"], index)
        for message in view["messages"]
        if message["role"] == source_roles.get(message["source"]["kind"])
        and all(block["kind"] == "text" for block in message["blocks"])
        for index in range(len(message["blocks"]))
        if (message["message_id"], index) not in protected
    ]


def _view_texts(view: dict) -> tuple[list[tuple[str, int]], list[str]]:
    selected = _targets(view)
    overrides = {(entry["message_id"], entry["block_index"]): entry["text"] for entry in view["overrides"]}
    original = {(message["message_id"], index): block.get("text", "")
                for message in view["messages"] for index, block in enumerate(message["blocks"])}
    return selected, [overrides.get(target, original[target]) for target in selected]


def _set_view_texts(view: dict, targets: list[tuple[str, int]], texts: list[str]) -> dict:
    result = copy.deepcopy(view)
    overrides = {(entry["message_id"], entry["block_index"]): entry["text"] for entry in view["overrides"]}
    overrides.update(zip(targets, texts))
    result["overrides"] = [
        {"message_id": message["message_id"], "block_index": index,
         "text": overrides[(message["message_id"], index)]}
        for message in view["messages"] for index in range(len(message["blocks"]))
        if (message["message_id"], index) in overrides
    ]
    return validate_context_view(result)


def _validate_material(value: Any) -> None:
    _require(type(value) is dict, "Preparation output must be an object")
    if value.get("kind") == "json":
        _fields(value, {"kind", "value"})
        validate_json_value(value["value"])
        _require(len(canonical_bytes(value)) <= MAX_PROGRAM_CHARS, "JSON output exceeds its limit")
        return
    _fields(value, {"kind", "items", "view"} if value.get("kind") == "prompt" else {"kind", "text"})
    if value["kind"] == "text":
        _require(value["text"] is None or type(value["text"]) is str, "Program text output is invalid")
        size = len(value["text"] or "")
    else:
        _require(value["kind"] == "prompt", "Unknown preparation material")
        validate_prompt_collection({"schema_version": 1, "kind": "prompt_collection", "items": value["items"]})
        if value["view"] is not None:
            validate_context_view(value["view"])
        size = sum(len(item["text"]) for item in value["items"])
        if value["view"] is not None:
            size += sum(len(text) for text in _view_texts(value["view"])[1])
    _require(size <= MAX_PROGRAM_CHARS, "Program material exceeds its character limit")


def _text_input(outputs: dict, node: dict, port: str = "input") -> str:
    value = outputs[node["inputs"][port]]
    _require(value["kind"] == "text", "Node requires text input", node["node_id"])
    if value["text"] is None:
        _fail("missing_macro_variable", "Unassigned variable cannot produce text", node["node_id"])
    return value["text"]


def _prompt_input(outputs: dict, node: dict) -> dict:
    value = outputs[node["inputs"]["input"]]
    _require(value["kind"] == "prompt", "Node requires prompt input", node["node_id"])
    return copy.deepcopy(value)


def _execute_node(node: dict, outputs: dict, collection: dict, view: dict, state: dict,
                  presets: dict, limits: RegexLimits, node_input: dict,
                  declared_instances: set[tuple], context_views: dict | None = None) -> dict:
    node_id, kind, config = node["node_id"], node["kind"], node["config"]
    if kind == "text":
        text = config.get("text")
        if config.get("source") == "root_input":
            text = node_input["payload"].get("text")
            _require(type(text) is str, "Root input has no explicit text field", node_id)
        return {"kind": "text", "text": text}
    if kind == "context-source":
        binding = config.get("binding_id")
        _require(binding is None or binding in (context_views or {}),
                 "Context source binding was not resolved", node_id)
        return _material(view=view if binding is None else context_views[binding])
    if kind == "global-source":
        _require("record" in config, "Global content was not resolved", node_id)
        items = []
        for member in config["record"]["members"]:
            if not member["enabled"]:
                continue
            identity = str(UUID(bytes=hashlib.sha256(
                (node_id + ":" + member["id"]).encode("utf-8"),
            ).digest()[:16], version=4))
            items.append({
                "item_id": member["id"], "revision": config["record"]["revision"],
                "item_instance_id": identity, "group_id": None, "group_revision": None,
                "group_instance_id": None, "input_name": node_id,
                "declaration_index": len(items), "name": member["name"],
                **{key: member[key] for key in ("text", "role", "enabled", "placement", "depth", "order")},
                "interpolation": "literal", "source": {"kind": "configuration"},
            })
        return _material(items)
    if kind in ("session-data-read", "session-data-write"):
        definition = config["definition"]
        data = state.setdefault("data", {})
        entry = data.setdefault(definition["key"], session_data_entry(definition))
        _require(canonical_bytes(entry["definition"]) == canonical_bytes(definition),
                 "Session data definition changed", node_id)
        if kind == "session-data-write":
            value = config["value"]
            if node["inputs"]:
                text = _text_input(outputs, node, "text")
                try:
                    value = text if definition["schema"].get("type") == "string" else json.loads(text)
                except ValueError:
                    _fail("session_data_type_mismatch", "Input is not valid JSON", node_id)
            validate_data_value(definition, value)
            entry = session_data_entry(definition, value, assigned=True)
            data[definition["key"]] = entry
        if "value" not in entry:
            _fail("session_data_unassigned", "Session data has not been assigned", node_id)
        return {"kind": "json", "value": copy.deepcopy(entry["value"])}
    if kind == "json-to-text":
        material = outputs[node["inputs"]["input"]]
        _require(material["kind"] == "json", "JSON conversion requires JSON input", node_id)
        value = material["value"]
        return {"kind": "text", "text": value if type(value) is str else json.dumps(value, ensure_ascii=False)}
    if kind == "prompt-source":
        identities = [(instance["group_instance_id"], instance["item_instance_id"])
                      for instance in config["instances"]]
        available = {(item["group_instance_id"], item["item_instance_id"]): item for item in collection["items"]}
        _require(all(identity in declared_instances for identity in identities), "Prompt source is unavailable", node_id)
        return _material([available[identity] for identity in identities if identity in available])
    if kind == "prompt-collector":
        items, selected_view = [], None
        for port in config.get("input_order", sorted(node["inputs"])):
            target = node["inputs"][port]
            material = outputs[target]
            _require(material["kind"] == "prompt", "Collector requires prompt material", node_id)
            items.extend(material["items"])
            if material["view"] is not None:
                _require(selected_view is None, "Context cannot be collected twice", node_id)
                selected_view = material["view"]
        identities = [(item["group_instance_id"], item["item_instance_id"]) for item in items]
        _require(len(identities) == len(set(identities)), "Collector repeats a prompt instance", node_id)
        for index, item in enumerate(items):
            item = copy.deepcopy(item)
            item["declaration_index"] = index
            items[index] = item
        return _material(items, selected_view)
    if kind == "prompt-to-text":
        material = _prompt_input(outputs, node)
        _require(material["view"] is None, "Protected context cannot be flattened to text", node_id)
        return {"kind": "text", "text": config["separator"].join(item["text"] for item in material["items"])}
    if kind == "text-to-prompt":
        identity = config["item_instance_id"]
        if not config.get("enabled", True):
            return _material()
        return _material([{
            "item_id": identity, "revision": 1, "item_instance_id": identity,
            "group_id": None, "group_revision": None, "group_instance_id": None,
            "input_name": node_id, "declaration_index": 0, "name": node_id,
            "text": _text_input(outputs, node), "role": config["role"],
            "enabled": True, "placement": config["placement"], "depth": config["depth"],
            "order": config["order"], "interpolation": "literal",
            "source": {"kind": "configuration"},
        }])
    if kind in ("variable-register", "variable-assign"):
        name = config["name"]
        if kind == "variable-register":
            state.update(merge_program_definitions(state, program_definitions({
                "schema_version": 1, "kind": "prompt_preparation_program",
                "nodes": [{**node, "inputs": {}}], "outputs": {"prompt": None, "context": None},
            })))
            if node["inputs"] and "value" not in state["values"][name]:
                state["values"][name] = {"type": "string", "source": "assignment",
                                         "value": _text_input(outputs, node, "text")}
        else:
            if name not in state["values"]:
                _fail("variable_not_registered", "Assignment target is not registered", node_id)
            entry = state["values"][name]
            value = _text_input(outputs, node, "text") if node["inputs"] else config["value"]
            if config["operation"] != "set":
                _require(entry["type"] in ("integer", "number") and "value" in entry,
                         "Numeric update requires an assigned numeric variable", node_id)
                validate_typed_value(entry["type"], value)
                value = entry["value"] + value if config["operation"] == "add" else entry["value"] - value
            validate_typed_value(entry["type"], value)
            state["values"][name] = {"type": entry["type"], "source": "assignment", "value": value}
        return {"kind": "text", "text": variable_text(state, name, node_id)
                if "value" in state["values"][name] else None}
    material = ({"kind": "text", "text": _text_input(outputs, node)}
                if config["mode"] == "text" else _prompt_input(outputs, node))
    if material["kind"] == "text":
        texts, targets, item_count = [material["text"]], [], 0
    else:
        texts = [item["text"] for item in material["items"]]
        item_count = len(texts)
        targets = []
        if material["view"] is not None:
            targets, context_texts = _view_texts(material["view"])
            texts.extend(context_texts)
    processed = (regex_replace_many(texts, regex_rule_from_dict(config["rule"]), limits=limits, node_id=node_id)
                 if kind == "regex" else [_replace(text, state, presets, node_id) for text in texts])
    if material["kind"] == "text":
        material["text"] = processed[0]
    else:
        for item, text in zip(material["items"], processed[:item_count]):
            item["text"] = text
        if material["view"] is not None:
            material["view"] = _set_view_texts(material["view"], targets, processed[item_count:])
    return material


def execute_preparation_program(program: Any, collection: dict, view: dict, state: Any,
                                *, limits: RegexLimits, node_input: dict,
                                cached: dict[str, dict] | None = None,
                                declared_instances: set[tuple] | None = None,
                                context_views: dict | None = None) -> dict[str, Any]:
    program = validate_preparation_program(program)
    collection = validate_prompt_collection(collection)
    view = validate_context_view(view)
    basis = validate_program_state(state)
    current = copy.deepcopy(basis)
    outputs, stages = {}, []
    evidence_bytes = 0
    presets = {name: view[name] for name in _PRESETS}
    for node in program["nodes"]:
        before = copy.deepcopy(current)
        try:
            cache = (cached or {}).get(node["node_id"])
            cacheable = node["kind"] not in ("context-source",) and not (
                any(outputs[target]["kind"] == "prompt" and outputs[target]["view"] is not None
                    for target in node["inputs"].values())
            )
            if cache is not None and cacheable:
                _require(cache["config_digest"] == content_digest(node),
                         "Shared node configuration changed within one chain", node["node_id"])
                output = copy.deepcopy(cache["output"])
            else:
                output = _execute_node(
                    node, outputs, collection, view, current, presets, limits, node_input,
                    declared_instances if declared_instances is not None else {
                        (item["group_instance_id"], item["item_instance_id"]) for item in collection["items"]
                    },
                    context_views,
                )
            _validate_material(output)
            validate_program_state(current)
        except PromptProcessingError as exc:
            if exc.node_id is None:
                exc.node_id = node["node_id"]
            raise
        outputs[node["node_id"]] = output
        stages.append({
            "node_id": node["node_id"], "inputs": {
                port: content_digest(outputs[target]) for port, target in node["inputs"].items()
            }, "output": copy.deepcopy(output), "state_before": before,
            "state_after": copy.deepcopy(current),
            "cached": cache is not None and cacheable,
        })
        evidence_bytes += len(canonical_bytes(stages[-1]))
        if evidence_bytes > 16_000_000:
            _fail("preparation_evidence_limit", "Preparation evidence exceeds its size limit", node["node_id"])
    final_collection, final_view = _program_outputs(program, outputs, collection, view)
    result = {
        "schema_version": 1, "kind": "preparation_program_result", "program": program,
        "basis_state": basis, "state": current, "stages": stages,
        "collection": final_collection, "view": final_view,
    }
    result["evidence_digest"] = content_digest(result)
    return result


def _program_outputs(program: dict, outputs: dict, collection: dict, view: dict) -> tuple[dict, dict]:
    prompt_id, context_id = program["outputs"]["prompt"], program["outputs"]["context"]
    final_collection, final_view = copy.deepcopy(collection), copy.deepcopy(view)
    if prompt_id is not None:
        material = outputs[prompt_id]
        _require(material["kind"] == "prompt", "Program prompt output is not prompt material")
        final_collection["items"] = copy.deepcopy(material["items"])
        if material["view"] is not None:
            final_view = copy.deepcopy(material["view"])
    if context_id is not None:
        material = outputs[context_id]
        _require(material["kind"] == "prompt" and material["view"] is not None
                 and not material["items"], "Program context output is not exclusively context")
        if prompt_id is not None and outputs[prompt_id]["view"] is not None:
            _require(canonical_bytes(final_view) == canonical_bytes(material["view"]),
                     "Program has conflicting context outputs")
        final_view = copy.deepcopy(material["view"])
    return validate_prompt_collection(final_collection), validate_context_view(final_view)


def validate_program_result(value: Any, collection: dict, view: dict, state: dict,
                            program: dict, *, node_input: dict | None = None,
                            cached: dict[str, dict] | None = None,
                            context_views: dict | None = None) -> dict[str, Any]:
    """Validate frozen correspondence without rerunning regex or variable nodes."""
    validate_json_value(value)
    _fields(value, {"schema_version", "kind", "program", "basis_state", "state", "stages",
                    "collection", "view", "evidence_digest"})
    _require(value["schema_version"] == 1 and value["kind"] == "preparation_program_result",
             "Unknown frozen program result")
    _require(value["evidence_digest"] == content_digest({
        key: entry for key, entry in value.items() if key != "evidence_digest"
    }), "Frozen preparation program digest differs")
    _require(canonical_bytes(value["program"]) == canonical_bytes(validate_preparation_program(program))
             and canonical_bytes(value["basis_state"]) == canonical_bytes(validate_program_state(state)),
             "Frozen program or basis differs")
    _require(type(value["stages"]) is list and len(value["stages"]) == len(program["nodes"]),
             "Frozen program stage count differs")
    outputs, current = {}, copy.deepcopy(state)
    for stage, node in zip(value["stages"], program["nodes"]):
        _fields(stage, {"node_id", "inputs", "output", "state_before", "state_after", "cached"})
        _require(type(stage["cached"]) is bool, "Frozen cache status is invalid")
        _validate_material(stage["output"])
        _require(stage["node_id"] == node["node_id"] and stage["inputs"] == {
            port: content_digest(outputs[target]) for port, target in node["inputs"].items()
        } and canonical_bytes(stage["state_before"]) == canonical_bytes(current),
                 "Frozen program input or state chain differs", node["node_id"])
        after = validate_program_state(stage["state_after"])
        if stage["cached"]:
            cache = (cached or {}).get(node["node_id"])
            _require(cache is not None and cache.get("config_digest") == content_digest(node)
                     and canonical_bytes(cache.get("output")) == canonical_bytes(stage["output"])
                     and canonical_bytes(after) == canonical_bytes(current),
                     "Frozen shared-node output differs from its exact cache")
        elif node["kind"] not in ("regex", "variable-replace") and (
            node["kind"] != "text" or "text" in node["config"] or node_input is not None
        ):
            expected_state = copy.deepcopy(current)
            expected_output = _execute_node(
                node, outputs, collection, view, expected_state,
                {name: view[name] for name in _PRESETS}, RegexLimits(),
                node_input or {"payload": {}},
                {(instance["group_instance_id"], instance["item_instance_id"])
                 for entry in program["nodes"] if entry["kind"] == "prompt-source"
                 for instance in entry["config"]["instances"]},
                context_views,
            )
            _require(canonical_bytes(expected_output) == canonical_bytes(stage["output"])
                     and canonical_bytes(expected_state) == canonical_bytes(after),
                     "Frozen node output differs from its declared inputs", node["node_id"])
        if node["kind"] not in ("variable-register", "variable-assign", "session-data-read", "session-data-write"):
            _require(canonical_bytes(after) == canonical_bytes(current), "Read-only node changed variables")
        material = stage["output"]
        if node["kind"] in ("regex", "variable-replace"):
            input_material = outputs[node["inputs"]["input"]]
            _require(material["kind"] == input_material["kind"], "Processing changed material type")
            if material["kind"] == "prompt":
                _require(len(material["items"]) == len(input_material["items"])
                         and all(canonical_bytes({key: entry for key, entry in before.items() if key != "text"})
                                 == canonical_bytes({key: entry for key, entry in after_item.items() if key != "text"})
                                 for before, after_item in zip(input_material["items"], material["items"])),
                         "Processing changed prompt metadata")
                _require((material["view"] is None) == (input_material["view"] is None),
                         "Processing changed context presence")
        if material["kind"] == "prompt" and material["view"] is not None:
            allowed = [view, *(context_views or {}).values()]
            _require(any(canonical_bytes({key: entry for key, entry in material["view"].items() if key != "overrides"})
                     == canonical_bytes({key: entry for key, entry in candidate.items() if key != "overrides"})
                     for candidate in allowed),
                     "Program changed canonical context or protection")
        outputs[node["node_id"]], current = copy.deepcopy(material), after
    final_collection, final_view = _program_outputs(program, outputs, collection, view)
    _require(all(canonical_bytes(value[key]) == canonical_bytes(expected) for key, expected in (
        ("collection", final_collection), ("view", final_view), ("state", current),
    )), "Frozen program output differs")
    return copy.deepcopy(value)
