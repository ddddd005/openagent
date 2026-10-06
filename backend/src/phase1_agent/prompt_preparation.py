"""Versioned, detached prompt preparation and replay-safe structural evidence.

The caller owns selected history and final locators. This module does not read
live catalogs or session values and never persists or dispatches a model.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, fields
import re
from typing import Any, Mapping
from uuid import uuid4

from .context_regex import apply_context_regex, validate_context_regex
from .contract_errors import ContractValidationError
from .contract_graph import validate_message_history
from .contract_json import canonical_bytes, content_digest, dumps_pretty, validate_json_value
from .contracts_v2 import validate_record
from .lorebook import LorebookLimits, activate_lorebook, validate_lorebook, validate_lorebook_request
from .prompt_assembly import (
    PromptAssemblyLimits, assemble_prompt_collection, validate_prompt_assembly,
)
from .prompt_config import validate_prompt_record
from .prompt_errors import PromptProcessingError
from .prompt_macro import MACRO_SYNTAX, MacroLimits
from .prompt_processing import (
    PromptProcessingLimits, process_prompt_collection, validate_prompt_steps,
)
from .prompt_regex import REGEX_SYNTAX, RegexLimits, validate_regex_limits
from .prompt_values import (
    collect_prompt_inputs, collection_from_config, context_view_messages,
    make_context_view, validate_context_view, validate_prompt_collection,
)
from .prompt_variables import validate_variable_snapshot
from .variable_preparation import validate_variable_assignment_plan
from .preparation_program import (
    execute_preparation_program, validate_preparation_program, validate_program_result,
    validate_program_state,
)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_UNSET = object()
_CONFIG_FIELDS = {
    "schema_version", "kind", "collection", "prompt_config", "steps",
    "variables", "lorebooks", "context_regex", "limits",
}
_RESULT_FIELDS = {
    "schema_version", "kind", "node_input", "canonical_messages", "send_view",
    "display_view", "collection", "variables", "config", "root_locator",
    "logical_floors", "protected_blocks", "prompt_message_ids", "assembly",
    "processing", "lorebook", "context_regex", "s0", "evidence_digest",
}


def _require(condition: bool, description: str) -> None:
    if not condition:
        raise ContractValidationError(description)


def _object(value: Any, expected: set[str], description: str) -> None:
    _require(type(value) is dict and set(value) == expected, f"{description} fields must be exact")


def _same(left: Any, right: Any) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _uuid(value: Any, description: str) -> None:
    _require(
        type(value) is str and _UUID.fullmatch(value) is not None,
        f"{description} must be a canonical UUID4",
    )


def _profile(value: Any, cls: type, description: str) -> Any:
    _object(value, {field.name for field in fields(cls)}, description)
    return cls(**value)


def _limits(value: Any) -> tuple[PromptProcessingLimits, RegexLimits, LorebookLimits, PromptAssemblyLimits]:
    _object(value, {"processing", "context_regex", "lorebook", "assembly"}, "Preparation limits")
    processing = value["processing"]
    _object(
        processing, {field.name for field in fields(PromptProcessingLimits)}, "Processing limits",
    )
    processing = PromptProcessingLimits(
        **{key: processing[key] for key in ("max_nodes", "max_items", "max_total_chars")},
        macro=_profile(processing["macro"], MacroLimits, "Macro limits"),
        regex=_profile(processing["regex"], RegexLimits, "Prompt regex limits"),
    )
    context = _profile(value["context_regex"], RegexLimits, "Context regex limits")
    validate_regex_limits(context)
    lorebook = _profile(value["lorebook"], LorebookLimits, "Lorebook limits")
    assembly = _profile(value["assembly"], PromptAssemblyLimits, "Assembly limits")
    return processing, context, lorebook, assembly


def _definition_map(values: Any, kind: str) -> dict[tuple[str, int], dict[str, Any]]:
    _require(type(values) is list, "Materialized definitions must be a JSON array")
    identity = {"item": "item_id", "group": "group_id"}[kind]
    result = {}
    for value in values:
        value = validate_prompt_record(kind, value)
        key = value[identity], value["revision"]
        _require(key not in result, "Duplicate materialized definition")
        result[key] = value
    return result


def _resolver(definitions: dict[tuple[str, int], dict[str, Any]]):
    def resolve(identity: str, revision: int) -> dict[str, Any]:
        value = definitions.get((identity, revision))
        _require(value is not None, "Missing materialized exact prompt reference")
        return copy.deepcopy(value)
    return resolve


def validate_prompt_context_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate all static inputs before any processing; no latest references."""
    validate_json_value(value)
    version = value.get("schema_version") if type(value) is dict else None
    extra = ({"variable_plan"} if version == 2 else
             {"preparation", "preparation_state", "preparation_cache"} if version == 3 else set())
    if version == 3 and "context_sources" in value:
        extra |= {"context_sources"}
    _object(value, _CONFIG_FIELDS | extra,
            "Prompt context configuration")
    _require(
        type(version) is int and version in (1, 2, 3)
        and value["kind"] == "prompt_context_config",
        "Unknown prompt context configuration version or kind",
    )
    collection = validate_prompt_collection(value["collection"])
    materialized = value["prompt_config"]
    if materialized is not None:
        _object(materialized, {"config", "items", "groups"}, "Materialized prompt configuration")
        config = validate_prompt_record("config", materialized["config"])
        items = _definition_map(materialized["items"], "item")
        groups = _definition_map(materialized["groups"], "group")
        _require(
            _same(collection_from_config(config, _resolver(items), _resolver(groups)), collection),
            "Materialized configuration and prepared collection differ",
        )
    steps = validate_prompt_steps(value["steps"])
    variables = value["variables"]
    if variables is not None:
        validate_variable_snapshot(variables)
    if version == 2:
        _require(variables is not None, "Variable preparation requires an exact registry snapshot")
        validate_variable_assignment_plan(value["variable_plan"])
    if version == 3:
        validate_preparation_program(value["preparation"])
        validate_program_state(value["preparation_state"])
        _require(type(value["preparation_cache"]) is dict, "Preparation cache must be an object")
        _require(not steps and not value["context_regex"] and not value["lorebooks"],
                 "Versioned preparation programs cannot include a second legacy processing chain")
        sources = value.get("context_sources", {})
        _require(type(sources) is dict and len(sources) <= 2, "Context sources must be bounded")
        for binding, source in sources.items():
            _object(source, {"scope", "messages", "logical_floors", "protected_blocks"}, "Context source")
            _require(source["scope"]["node_binding_id"] == binding, "Context source binding differs")
            validate_message_history(source["messages"])
            _require(type(source["logical_floors"]) is list
                     and [identity for floor in source["logical_floors"] for identity in floor]
                     == [message["message_id"] for message in source["messages"]],
                     "Context source floor mapping differs")
    _require(type(value["lorebooks"]) is list, "Lorebook inputs must be an ordered JSON array")
    input_names, book_instances = set(), set()
    item_instances = {
        (item["group_instance_id"], item["item_instance_id"]) for item in collection["items"]
    }
    for entry in value["lorebooks"]:
        _object(entry, {"book", "request", "prompts"}, "Materialized Lorebook input")
        book = validate_lorebook(entry["book"])
        request = validate_lorebook_request(entry["request"])
        _require(request["input_name"] != "configured", "Lorebook input name is reserved")
        _require(
            request["input_name"] not in input_names
            and request["book_instance_id"] not in book_instances,
            "Duplicate Lorebook input name or book instance",
        )
        input_names.add(request["input_name"])
        book_instances.add(request["book_instance_id"])
        _require(
            {item["entry_id"] for item in request["entry_instances"]}
            == {item["entry_id"] for item in book["entries"]},
            "Lorebook instances must cover all declared entries",
        )
        for instance in request["entry_instances"]:
            identity = None, instance["item_instance_id"]
            _require(identity not in item_instances, "Duplicate scoped prompt instance in Lorebook inputs")
            item_instances.add(identity)
        prompts = _definition_map(entry["prompts"], "item")
        _require(
            {(item["prompt_ref"]["item_id"], item["prompt_ref"]["revision"]) for item in book["entries"]}
            <= prompts.keys(),
            "Missing materialized exact Lorebook prompt reference",
        )
    _require(type(value["context_regex"]) is list, "Context regex steps must be an ordered JSON array")
    context_steps = [validate_context_regex(step) for step in value["context_regex"]]
    node_ids = [step["node_id"] for step in steps + context_steps]
    node_ids.extend(entry["request"]["node_id"] for entry in value["lorebooks"])
    _require(len(set(node_ids)) == len(node_ids), "Duplicate preparation node identity")
    processing, _, _, _ = _limits(value["limits"])
    _require(
        len(node_ids) <= processing.max_nodes,
        "Preparation exceeds its explicit node limit",
    )
    _check_collection_size(collection, processing)
    return copy.deepcopy(value)


def make_prompt_context_config(
    collection: Mapping[str, Any], *, prompt_config: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None, variables: dict[str, Any] | None = None,
    lorebooks: list[dict[str, Any]] | None = None,
    context_regex: list[dict[str, Any]] | None = None,
    limits: dict[str, Any] | None = None,
    variable_plan: dict[str, Any] | None = None,
    preparation: dict[str, Any] | None = None,
    preparation_state: dict[str, Any] | None = None,
    preparation_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make defaults explicit in the saved configuration, not during replay."""
    if limits is None:
        limits = {
            "processing": asdict(PromptProcessingLimits()),
            "context_regex": asdict(RegexLimits()),
            "lorebook": asdict(LorebookLimits()),
            "assembly": asdict(PromptAssemblyLimits()),
        }
    return validate_prompt_context_config({
        "schema_version": 3 if preparation is not None else 1 if variable_plan is None else 2,
        "kind": "prompt_context_config",
        "collection": collection, "prompt_config": prompt_config,
        "steps": [] if steps is None else steps, "variables": variables,
        "lorebooks": [] if lorebooks is None else lorebooks,
        "context_regex": [] if context_regex is None else context_regex,
        "limits": limits,
        **({"variable_plan": variable_plan} if variable_plan is not None else {}),
        **({"preparation": preparation,
            "preparation_state": preparation_state or {"revision": 0, "values": {}},
            "preparation_cache": preparation_cache or {}} if preparation is not None else {}),
    })


def _check_collection_size(collection: dict[str, Any], limits: PromptProcessingLimits) -> None:
    if (
        len(collection["items"]) > limits.max_items
        or sum(len(item["text"]) for item in collection["items"]) > limits.max_total_chars
    ):
        raise PromptProcessingError("prompt_pipeline_limit", "Prepared collection exceeds its limits")


def _root_message(node_input: dict[str, Any], message_id: str) -> dict[str, Any]:
    _uuid(message_id, "Root message identity")
    source = node_input["source"]
    if source["kind"] == "visible_message":
        projected = {"kind": "human", "visible_message_id": source["visible_message_id"]}
    elif source["kind"] == "upstream_output":
        projected = {"kind": "upstream_node", "output_id": source["output_id"]}
    else:
        raise ContractValidationError("Prompt preparation requires an explicit root source")
    return validate_record("agent_message", {
        "schema_version": 1, "message_id": message_id, "role": "user",
        "source": projected, "blocks": [{"kind": "text", "text": dumps_pretty(node_input["payload"])}],
    })


def _canonical(node_input: dict, history: Any, message_id: str) -> list[dict[str, Any]]:
    _require(type(history) is list, "Selected canonical history must be a JSON array")
    messages = [validate_record("agent_message", message) for message in history]
    messages.append(_root_message(node_input, message_id))
    validate_message_history(messages)
    return messages


def _merge_lorebook_outputs(config: dict, results: list[dict]) -> dict[str, Any]:
    names = ["configured", *(entry["request"]["input_name"] for entry in config["lorebooks"])]
    inputs = {"configured": config["collection"]}
    inputs.update({
        entry["request"]["input_name"]: result["collection"]
        for entry, result in zip(config["lorebooks"], results)
    })
    return collect_prompt_inputs(names, inputs)


def _processing_stages(collection: dict, config: dict, limits: PromptProcessingLimits) -> dict:
    stages = []
    for step in config["steps"]:
        result = process_prompt_collection(
            collection, [step], variables=config["variables"], limits=limits,
        )
        collection = result["value"]
        stages.append({"collection": collection, "trace": result["trace"][0]})
    return {
        "schema_version": 1, "kind": "prompt_preparation_processing",
        "limits": asdict(limits), "syntax": {"macro": MACRO_SYNTAX, "regex": REGEX_SYNTAX},
        "stages": stages,
    }


def _context_stages(view: dict, config: dict, limits: RegexLimits) -> tuple[dict, list[dict]]:
    stages = []
    for step in config["context_regex"]:
        result = apply_context_regex(view, step, limits=limits)
        stages.append(result)
        view = result["view"]
    return view, stages


def prepare_prompt_context(
    node_input: dict, history: list[dict], *, workflow_session_id: str,
    node_binding_id: str, parent_turn_id: str | None, logical_floors: list[list[str]],
    protected_blocks: list[dict], config: dict, root_message_id: str,
    prompt_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Prepare one detached canonical/send/display result and its freeze evidence."""
    config = validate_prompt_context_config(config)
    node_input = validate_record("node_input", node_input)
    processing_limits, context_limits, lorebook_limits, assembly_limits = _limits(config["limits"])
    variables = config["variables"]
    if variables is not None:
        _require(
            variables["workflow_session_id"] == workflow_session_id
            and variables["node_binding_id"] == node_binding_id,
            "Variable snapshot belongs to another preparation scope",
        )
    messages = _canonical(node_input, history, root_message_id)
    _require(type(logical_floors) is list, "Selected logical floors must be a JSON array")
    logical_floors = copy.deepcopy(logical_floors) + [[root_message_id]]
    views = {
        purpose: make_context_view(
            messages, workflow_session_id=workflow_session_id,
            node_binding_id=node_binding_id, parent_turn_id=parent_turn_id,
            projection_version=1, purpose=purpose, protected_blocks=protected_blocks,
        )
        for purpose in ("send", "display")
    }
    # Validate boundaries and canonical capacity before launching processing workers.
    assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []},
        messages, logical_floors=logical_floors, prompt_message_ids=[], limits=assembly_limits,
    )
    context_traces = {}
    for purpose in ("send", "display"):
        views[purpose], context_traces[purpose] = _context_stages(views[purpose], config, context_limits)
    lorebook_results = []
    work_units = 0
    for entry in config["lorebooks"]:
        result = activate_lorebook(
            entry["book"], entry["request"], views["send"],
            _resolver(_definition_map(entry["prompts"], "item")), limits=lorebook_limits,
        )
        work_units += result["trace"]["work_units"]
        if work_units > lorebook_limits.max_work_units:
            raise PromptProcessingError(
                "lorebook_resource_limit", "Lorebook batch exceeds its shared work limit",
            )
        lorebook_results.append(result)
    collection = _merge_lorebook_outputs(config, lorebook_results)
    program_result = None
    if config["schema_version"] == 3:
        declared_instances = {
            (item["group_instance_id"], item["item_instance_id"]) for item in collection["items"]
        }
        if config["prompt_config"] is not None:
            materialized = config["prompt_config"]
            groups = {(group["group_id"], group["revision"]): group for group in materialized["groups"]}
            for entry in materialized["config"]["inputs"]:
                if entry["kind"] == "item":
                    declared_instances.add((None, entry["item_instance_id"]))
                else:
                    declared_instances.update(
                        (entry["group_instance_id"], member["item_instance_id"])
                        for member in groups[(entry["group_id"], entry["revision"])]["members"]
                    )
        program_result = execute_preparation_program(
            config["preparation"], collection, views["send"], config["preparation_state"],
            limits=context_limits, node_input=node_input, cached=config["preparation_cache"],
            declared_instances=declared_instances,
            context_views=_source_views(config, node_input, views["send"], root_message_id),
        )
        collection, views["send"] = program_result["collection"], program_result["view"]
    _check_collection_size(collection, processing_limits)
    processing = _processing_stages(collection, config, processing_limits)
    if processing["stages"]:
        collection = processing["stages"][-1]["collection"]
    if prompt_message_ids is None:
        prompt_message_ids = [str(uuid4()) for _ in collection["items"]]
    assembly = assemble_prompt_collection(
        collection, context_view_messages(views["send"]), logical_floors=logical_floors,
        prompt_message_ids=prompt_message_ids, limits=assembly_limits,
    )
    # Display projection is independently bounded even though it is never sent.
    assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []},
        context_view_messages(views["display"]), logical_floors=logical_floors,
        prompt_message_ids=[], limits=assembly_limits,
    )
    result = {
        "schema_version": 2 if program_result is not None else 1, "kind": "context_preparation",
        "node_input": node_input, "canonical_messages": messages,
        "send_view": views["send"], "display_view": views["display"],
        "collection": collection, "variables": variables, "config": config,
        "root_locator": {"message_id": root_message_id, "block_index": 0},
        "logical_floors": logical_floors, "protected_blocks": protected_blocks,
        "prompt_message_ids": prompt_message_ids, "assembly": assembly,
        "processing": processing, "lorebook": lorebook_results,
        "context_regex": context_traces, "s0": assembly["messages"],
        **({"program": program_result} if program_result is not None else {}),
    }
    result["evidence_digest"] = content_digest(result)
    return validate_context_preparation(result)


def _source_views(config, node_input, base, root_message_id):
    return {
        binding: make_context_view(
            _canonical(node_input, source["messages"], root_message_id),
            workflow_session_id=base["workflow_session_id"], node_binding_id=base["node_binding_id"],
            parent_turn_id=base["parent_turn_id"], projection_version=1, purpose="send",
            protected_blocks=source["protected_blocks"],
        )
        for binding, source in config.get("context_sources", {}).items()
    }


def _selected_indexes(items: list[dict], step: dict) -> list[int]:
    if step["select"]["mode"] == "all":
        return list(range(len(items)))
    requested = {
        (identity["group_instance_id"], identity["item_instance_id"])
        for identity in step["select"]["instances"]
    }
    identities = {
        (item["group_instance_id"], item["item_instance_id"]) for item in items
    }
    _require(requested <= identities, "Frozen selector references an unavailable instance")
    return [
        index for index, item in enumerate(items)
        if (item["group_instance_id"], item["item_instance_id"]) in requested
    ]


def _validate_processing(value: Any, initial: dict, config: dict, limits: PromptProcessingLimits) -> dict:
    _object(value, {"schema_version", "kind", "limits", "syntax", "stages"}, "Frozen processing evidence")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "prompt_preparation_processing"
        and _same(value["limits"], asdict(limits))
        and _same(value["syntax"], {"macro": MACRO_SYNTAX, "regex": REGEX_SYNTAX}),
        "Frozen processing profile differs from its configuration",
    )
    _require(
        type(value["stages"]) is list and len(value["stages"]) == len(config["steps"]),
        "Frozen processing stages differ from their configuration",
    )
    current = initial
    for stage, step in zip(value["stages"], config["steps"]):
        _object(stage, {"collection", "trace"}, "Frozen processing stage")
        output = validate_prompt_collection(stage["collection"])
        _check_collection_size(output, limits)
        _require(
            output["schema_version"] == current["schema_version"]
            and len(output["items"]) == len(current["items"]),
            "Processing changed collection identity or size",
        )
        indexes = _selected_indexes(current["items"], step)
        if not step["enabled"]:
            indexes = []
        elif step["kind"] == "macro":
            indexes = [index for index in indexes if current["items"][index]["interpolation"] != "literal"]
            _require(not indexes or config["variables"] is not None, "Frozen macro has no variable snapshot")
        for index, (before, after) in enumerate(zip(current["items"], output["items"])):
            _require(
                _same(
                    {key: item for key, item in before.items() if key != "text"},
                    {key: item for key, item in after.items() if key != "text"},
                ),
                "Processing changed prompt metadata",
            )
            _require(index in indexes or before["text"] == after["text"], "Processing changed an unselected item")
        expected = {
            "step": step, "input_digest": content_digest(current["items"]),
            "output_digest": content_digest(output["items"]),
            "processed_instances": [{
                "group_instance_id": output["items"][index]["group_instance_id"],
                "item_instance_id": output["items"][index]["item_instance_id"],
            } for index in indexes],
            "processed_text": False,
            "variable_snapshot_digest": content_digest(config["variables"]) if (
                step["kind"] == "macro" and indexes
            ) else None,
        }
        _require(_same(stage["trace"], expected), "Frozen processing trace correspondence is invalid")
        current = output
    return current


def _projection_targets(view: dict, step: dict) -> list[dict]:
    if not step["enabled"] or view["purpose"] not in step["purposes"]:
        return []
    protected = {(item["message_id"], item["block_index"]) for item in view["protected_blocks"]}
    targets = []
    source_roles = {"human": "user", "upstream_node": "user", "model": "assistant"}
    for message in view["messages"]:
        source = message["source"]["kind"]
        if (
            source not in step["sources"] or message["role"] != source_roles.get(source)
            or any(block["kind"] != "text" for block in message["blocks"])
        ):
            continue
        targets.extend({
            "message_id": message["message_id"], "block_index": index,
        } for index in range(len(message["blocks"])) if (message["message_id"], index) not in protected)
    return targets


def _validate_context_stages(stages: Any, base: dict, config: dict, limits: RegexLimits) -> dict:
    _require(
        type(stages) is list and len(stages) == len(config["context_regex"]),
        "Frozen context stages differ from their configuration",
    )
    current = base
    for stage, step in zip(stages, config["context_regex"]):
        _object(stage, {"schema_version", "kind", "view", "trace"}, "Frozen context regex stage")
        output = validate_context_view(stage["view"])
        _require(
            type(stage["schema_version"]) is int and stage["schema_version"] == 1
            and stage["kind"] == "context_regex_result",
            "Unknown frozen context regex stage version or kind",
        )
        _require(
            _same(
                {key: item for key, item in current.items() if key != "overrides"},
                {key: item for key, item in output.items() if key != "overrides"},
            ),
            "Context processing changed canonical messages, scope or protection",
        )
        targets = _projection_targets(current, step)
        selected = {(target["message_id"], target["block_index"]) for target in targets}
        before = {(item["message_id"], item["block_index"]): item["text"] for item in current["overrides"]}
        after = {(item["message_id"], item["block_index"]): item["text"] for item in output["overrides"]}
        _require(
            after.keys() == before.keys() | selected
            and all(after[key] == text for key, text in before.items() if key not in selected),
            "Context processing changed an unselected override",
        )
        ordered = [
            {"message_id": message["message_id"], "block_index": index,
             "text": after[(message["message_id"], index)]}
            for message in output["messages"] for index in range(len(message["blocks"]))
            if (message["message_id"], index) in after
        ]
        _require(_same(output["overrides"], ordered), "Frozen context overrides have a different order")
        _require(_same(stage["trace"], {
            "config": step, "limits": asdict(limits),
            "active": step["enabled"] and current["purpose"] in step["purposes"],
            "regex_syntax": REGEX_SYNTAX,
            "input_digest": content_digest(current), "output_digest": content_digest(output),
            "processed_blocks": targets,
        }), "Frozen context regex trace correspondence is invalid")
        current = output
    return current


def _validate_lorebook_results(results: Any, config: dict, view: dict, limits: LorebookLimits) -> None:
    _require(
        type(results) is list and len(results) == len(config["lorebooks"]),
        "Frozen Lorebook results differ from their configuration",
    )
    # Activation is deterministic, bounded literal matching, not macro/regex execution.
    work_units = 0
    for result, entry in zip(results, config["lorebooks"]):
        expected = activate_lorebook(
            entry["book"], entry["request"], view,
            _resolver(_definition_map(entry["prompts"], "item")), limits=limits,
        )
        _require(_same(result, expected), "Frozen Lorebook activation correspondence is invalid")
        work_units += expected["trace"]["work_units"]
    _require(work_units <= limits.max_work_units, "Frozen Lorebook work exceeds its shared limit")


def validate_context_preparation(
    value: Mapping[str, Any], *, node_input: dict | None = None,
    history: list[dict] | None = None, workflow_session_id: str | None = None,
    node_binding_id: str | None = None, parent_turn_id: Any = _UNSET,
    logical_floors: list[list[str]] | None = None,
    protected_blocks: list[dict] | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """Validate frozen correspondence without rerunning macro or regex nodes.

    Digests detect mutation, not authenticity. Owners should supply their trusted
    input/history/scope/protection arguments before accepting component output.
    """
    validate_json_value(value)
    _object(value, _RESULT_FIELDS | ({"program"} if value.get("schema_version") == 2 else set()),
            "Context preparation")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] in (1, 2)
        and value["kind"] == "context_preparation",
        "Unknown context preparation version or kind",
    )
    _require(
        value["evidence_digest"] == content_digest({
            key: item for key, item in value.items() if key != "evidence_digest"
        }),
        "Context preparation evidence digest differs",
    )
    trusted_config = config
    config = validate_prompt_context_config(value["config"])
    processing_limits, context_limits, lorebook_limits, assembly_limits = _limits(config["limits"])
    frozen_input = validate_record("node_input", value["node_input"])
    _object(value["root_locator"], {"message_id", "block_index"}, "Frozen root locator")
    _require(type(value["root_locator"]["block_index"]) is int and value["root_locator"]["block_index"] == 0,
             "Frozen root locator must identify its only text block")
    root = _root_message(frozen_input, value["root_locator"]["message_id"])
    _require(
        type(value["canonical_messages"]) is list and bool(value["canonical_messages"])
        and _same(value["canonical_messages"][-1], root),
        "Frozen canonical sequence has no corresponding root input",
    )
    canonical = _canonical(frozen_input, value["canonical_messages"][:-1], root["message_id"])
    for purpose in ("send", "display"):
        view = validate_context_view(value[f"{purpose}_view"])
        _require(
            view["purpose"] == purpose and view["projection_version"] == 1
            and _same(view["messages"], canonical)
            and _same(view["protected_blocks"], value["protected_blocks"]),
            "Frozen context view differs from its canonical sequence or protection",
        )
    send = value["send_view"]
    _require((value["schema_version"] == 2) == (config["schema_version"] == 3),
             "Program evidence and configuration versions differ")
    _require(
        _same(
            {key: item for key, item in send.items() if key not in ("purpose", "overrides")},
            {key: item for key, item in value["display_view"].items() if key not in ("purpose", "overrides")},
        ),
        "Send and display views belong to different scopes",
    )
    _require(_same(value["variables"], config["variables"]), "Frozen variables differ from configuration")
    if value["variables"] is not None:
        _require(
            value["variables"]["workflow_session_id"] == send["workflow_session_id"]
            and value["variables"]["node_binding_id"] == send["node_binding_id"],
            "Frozen variable scope differs from context scope",
        )
    _object(value["context_regex"], {"send", "display"}, "Frozen context traces")
    program_result = None
    for purpose in ("send", "display"):
        base = make_context_view(
            canonical, workflow_session_id=send["workflow_session_id"],
            node_binding_id=send["node_binding_id"], parent_turn_id=send["parent_turn_id"],
            projection_version=1, purpose=purpose, protected_blocks=value["protected_blocks"],
        )
        derived = _validate_context_stages(value["context_regex"][purpose], base, config, context_limits)
        if purpose == "send" and config["schema_version"] == 3:
            program_result = validate_program_result(
                value["program"], config["collection"], derived, config["preparation_state"],
                config["preparation"],
                node_input=frozen_input, cached=config["preparation_cache"],
                context_views=_source_views(config, frozen_input, base, root["message_id"]),
            )
            derived = program_result["view"]
        _require(_same(derived, value[f"{purpose}_view"]), "Frozen final context view differs from trace")
    _validate_lorebook_results(value["lorebook"], config, send, lorebook_limits)
    initial = _merge_lorebook_outputs(config, value["lorebook"])
    if program_result is not None:
        initial = program_result["collection"]
    _check_collection_size(initial, processing_limits)
    processed = _validate_processing(value["processing"], initial, config, processing_limits)
    _require(_same(processed, value["collection"]), "Frozen collection differs from processing trace")
    assembly = validate_prompt_assembly(value["assembly"], limits=assembly_limits)
    expected = assemble_prompt_collection(
        processed, context_view_messages(send), logical_floors=value["logical_floors"],
        prompt_message_ids=value["prompt_message_ids"], limits=assembly_limits,
    )
    _require(_same(assembly, expected) and _same(value["s0"], expected["messages"]),
             "Frozen S0 differs from its explicit assembly mapping")
    assemble_prompt_collection(
        {"schema_version": 1, "kind": "prompt_collection", "items": []},
        context_view_messages(value["display_view"]), logical_floors=value["logical_floors"],
        prompt_message_ids=[], limits=assembly_limits,
    )
    for actual, trusted, description in (
        (frozen_input, node_input, "node input"),
        (config, trusted_config, "resolved configuration"),
        (canonical[:-1], history, "selected canonical history"),
        (send["workflow_session_id"], workflow_session_id, "workflow session"),
        (send["node_binding_id"], node_binding_id, "node binding"),
        (
            value["logical_floors"],
            None if logical_floors is None else logical_floors + [[root["message_id"]]],
            "logical floors",
        ),
        (value["protected_blocks"], protected_blocks, "protected final locators"),
    ):
        if trusted is not None:
            _require(_same(actual, trusted), f"Context preparation differs from trusted {description}")
    if parent_turn_id is not _UNSET:
        _require(send["parent_turn_id"] == parent_turn_id, "Context preparation differs from trusted parent turn")
    return copy.deepcopy(value)
