"""Trusted configuration partitions and transactional workflow editing."""

from copy import deepcopy
from uuid import uuid4

from .contract_errors import require, uuid4_string
from .contract_json import canonical_bytes


PROMPT_CONFIGURATION_FIELDS = ("text", "presentation", "metadata", "lifecycle", "compaction")
PROMPT_PRESENTATION_FIELDS = frozenset(("role", "placement", "depth", "order", "enabled"))
MAX_EDIT_SEQUENCE = 2**53 - 1


def validate_adjustable_configuration(component_id, component_version, declaration):
    """Only the reviewed exact prompt contracts may declare adjustable fields."""
    if declaration is None:
        return None
    require((component_id, component_version) in (("prompts.item", "3"), ("prompts.group", "3")),
            "graph_invalid_adjustable_declaration", "This exact node contract cannot declare adjustable configuration")
    group = component_id == "prompts.group"
    expected_keys = {"schema_version", "policy", "fields"}
    if group:
        expected_keys |= {"member_identity_field", "member_fields"}
    require(type(declaration) is dict and set(declaration) == expected_keys
            and type(declaration["schema_version"]) is int and declaration["schema_version"] == 1
            and declaration["policy"] == "prompt",
            "graph_invalid_adjustable_declaration", "Adjustable configuration declaration fields differ")
    fields = declaration["fields"]
    expected_fields = ("members",) if group else PROMPT_CONFIGURATION_FIELDS
    require(type(fields) is list and len(fields) == len(expected_fields)
            and all(type(field) is str for field in fields) and set(fields) == set(expected_fields),
            "graph_invalid_adjustable_declaration", "Adjustable fields differ from the reviewed whitelist")
    if group:
        members = declaration["member_fields"]
        require(declaration["member_identity_field"] == "id" and type(members) is list
                and len(members) == len(PROMPT_CONFIGURATION_FIELDS)
                and all(type(field) is str for field in members)
                and set(members) == set(PROMPT_CONFIGURATION_FIELDS),
                "graph_invalid_adjustable_declaration", "Prompt member declaration differs from the reviewed whitelist")
    return deepcopy(declaration)


def validate_configuration_policy(value):
    require(type(value) is dict and set(value) == {"schema_version", "prompt_mode"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["prompt_mode"] in ("fixed", "adjustable"),
            "graph_invalid_configuration_policy", "Node configuration policy is invalid")
    return deepcopy(value)


def _validate_prompt_fields(value, *, node_id):
    if "id" in value:
        uuid4_string(value["id"])
    if "text" in value:
        require(type(value["text"]) is str and len(value["text"]) <= 1_000_000,
                "graph_invalid_config", "Prompt text must be bounded text", node_id=node_id)
    if "presentation" in value:
        presentation = value["presentation"]
        require(type(presentation) is dict, "graph_invalid_config",
                "Prompt presentation must be an object", node_id=node_id)
        if "role" in presentation:
            require(presentation["role"] in ("system", "user", "assistant"),
                    "graph_invalid_config", "Prompt role is invalid", node_id=node_id)
        if "placement" in presentation:
            require(presentation["placement"] in ("before", "middle", "after"),
                    "graph_invalid_config", "Prompt placement is invalid", node_id=node_id)
        for field in ("depth", "order"):
            if field in presentation:
                require((field == "depth" and presentation[field] is None)
                        or type(presentation[field]) is int
                        and (-MAX_EDIT_SEQUENCE if field == "order" else 0) <= presentation[field] <= MAX_EDIT_SEQUENCE,
                        "graph_invalid_config", "Prompt numeric presentation is invalid", node_id=node_id)
        if "enabled" in presentation:
            require(type(presentation["enabled"]) is bool, "graph_invalid_config",
                    "Prompt enabled flag must be boolean", node_id=node_id)
    if "metadata" in value:
        require(type(value["metadata"]) is dict, "graph_invalid_config",
                "Prompt metadata must be an object", node_id=node_id)
    for field, allowed in (("lifecycle", ("per_request", "context_once")), ("compaction", ("never", "allowed"))):
        if field in value:
            require(type(value[field]) is str and value[field] in allowed, "graph_invalid_config",
                    "Prompt lifecycle or compaction format is invalid", node_id=node_id)


def validate_node_configuration(node, descriptor):
    policy = validate_configuration_policy(node["configuration_policy"])
    declaration = (validate_adjustable_configuration(node["component_id"], node["component_version"],
                                                     descriptor.get("adjustable_configuration"))
                   if descriptor is not None else None)
    require(policy["prompt_mode"] == "fixed" or declaration is not None,
            "graph_configuration_not_adjustable", "Node has no trusted adjustable configuration declaration",
            node_id=node["node_binding_id"])
    if declaration is None:
        return None
    config = node["config"]
    if node["component_id"] == "prompts.item":
        _validate_prompt_fields(config, node_id=node["node_binding_id"])
    elif "members" in config:
        members = config["members"]
        require(type(members) is list and len(members) <= 1024, "graph_invalid_config",
                "Prompt members must be a bounded array", node_id=node["node_binding_id"])
        identities = set()
        for member in members:
            require(type(member) is dict and "id" in member, "graph_invalid_config",
                    "Prompt members require explicit identities", node_id=node["node_binding_id"])
            _validate_prompt_fields(member, node_id=node["node_binding_id"])
            require(member["id"] not in identities, "graph_invalid_config",
                    "Prompt member identity is duplicated", node_id=node["node_binding_id"])
            identities.add(member["id"])
    return declaration


def _fixed_member_fields(member):
    fixed = {field: deepcopy(value) for field, value in member.items()
             if field not in (*PROMPT_CONFIGURATION_FIELDS, "id")}
    presentation = member.get("presentation")
    if type(presentation) is dict:
        extra = {field: deepcopy(value) for field, value in presentation.items()
                 if field not in PROMPT_PRESENTATION_FIELDS}
        if extra:
            fixed["presentation"] = extra
    return fixed


def fixed_configuration_projection(config, declaration, policy):
    """Project immutable fields without comparing consumed values to today's values."""
    if declaration is None or policy is None or policy["prompt_mode"] != "adjustable":
        return deepcopy(config)
    fixed = {field: deepcopy(value) for field, value in config.items()
             if field not in declaration["fields"]}
    if "member_identity_field" in declaration:
        members = config.get("members", [])
        extra = {member["id"]: fields for member in members
                 if (fields := _fixed_member_fields(member))}
        if extra:
            fixed["members"] = extra
    else:
        presentation = config.get("presentation")
        if type(presentation) is dict:
            extra = {field: deepcopy(value) for field, value in presentation.items()
                     if field not in PROMPT_PRESENTATION_FIELDS}
            if extra:
                fixed["presentation"] = extra
    return fixed


def adjustable_configuration_projection(config, declaration, policy):
    if declaration is None or policy["prompt_mode"] != "adjustable":
        return {}
    return {field: deepcopy(config[field]) for field in declaration["fields"] if field in config}


def classify_configuration_changes(previous, proposed, descriptor_lookup):
    def partitions(document):
        fixed = deepcopy(document)
        fixed.pop("name")
        fixed.pop("revision")
        fixed.pop("edit_sequence", None)
        display = {"name": document["name"], "nodes": {}}
        adjustable = {}
        for node in fixed["nodes"]:
            identity = node["node_binding_id"]
            display["nodes"][identity] = {"title": node.pop("title"), "position": node.pop("position")}
            declaration = validate_node_configuration(node, descriptor_lookup(node))
            if declaration is not None and node["configuration_policy"]["prompt_mode"] == "adjustable":
                adjustable[identity] = adjustable_configuration_projection(
                    node["config"], declaration, node["configuration_policy"])
            node["config"] = fixed_configuration_projection(node["config"], declaration,
                                                            node["configuration_policy"])
        return display, adjustable, fixed

    left, right = partitions(previous), partitions(proposed)
    return [category for category, before, after in zip(("display", "adjustable", "fixed"), left, right)
            if canonical_bytes(before) != canonical_bytes(after)]


def initialize_configuration_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_current_configuration ("
        "workflow_definition_id TEXT PRIMARY KEY NOT NULL, edit_sequence INTEGER NOT NULL, "
        "fixed_definition_revision INTEGER NOT NULL, display_name TEXT NOT NULL, "
        "node_presentation_json TEXT NOT NULL, adjustable_config_json TEXT NOT NULL)")


class WorkflowConfiguration:
    def _configuration_descriptor(self, repo, node):
        entry = self.registry.get(node["component_id"], node["component_version"])
        saved = repo.maybe("node_definition", component_id=node["component_id"],
                           component_version=node["component_version"])
        if saved is not None:
            if entry is not None:
                registered = self._runtime_descriptor(entry)
                persisted = deepcopy(saved["descriptor"])
                registered.pop("executable", None)
                persisted.pop("executable", None)
                require(repo.equal(persisted, registered),
                        "node_definition_changed", "Registered declaration differs from its saved version", 409,
                        node_id=node["node_binding_id"])
            return saved["descriptor"]
        return self._runtime_descriptor(entry) if entry is not None else None

    def _configuration_document(self, repo, identity):
        current = repo.current_configuration(identity)
        require(current is not None, "not_found", "Current workflow configuration not found", 404)
        document = deepcopy(self._document(repo, identity, current["fixed_definition_revision"]))
        document["name"] = current["display_name"]
        document["edit_sequence"] = current["edit_sequence"]
        for node in document["nodes"]:
            identity = node["node_binding_id"]
            presentation = current["node_presentation"][identity]
            node.update(deepcopy(presentation))
            descriptor = self._configuration_descriptor(repo, node)
            declaration = validate_node_configuration(node, descriptor)
            if declaration is not None and node["configuration_policy"]["prompt_mode"] == "adjustable":
                for field in declaration["fields"]:
                    node["config"].pop(field, None)
                node["config"].update(deepcopy(current["adjustable_config"][identity]))
                validate_node_configuration(node, descriptor)
        return document

    def _write_configuration(self, repo, document, edit_sequence):
        adjustable = {}
        presentation = {}
        for node in document["nodes"]:
            identity = node["node_binding_id"]
            declaration = validate_node_configuration(node, self._configuration_descriptor(repo, node))
            adjustable[identity] = adjustable_configuration_projection(
                node["config"], declaration, node["configuration_policy"])
            presentation[identity] = {field: deepcopy(node[field]) for field in ("title", "position")}
        repo.put_current_configuration({
            "workflow_definition_id": document["workflow_definition_id"], "edit_sequence": edit_sequence,
            "fixed_definition_revision": document["revision"], "display_name": document["name"],
            "node_presentation": presentation, "adjustable_config": adjustable,
        })

    def update_definition(self, document, *, workflow_definition_id, expected_edit_sequence, idempotency_key):
        request = {"workflow_definition_id": workflow_definition_id,
                   "expected_edit_sequence": expected_edit_sequence, "document": document}

        def change(repo):
            from .graph_contracts import validate_graph_document
            uuid4_string(workflow_definition_id)
            require(type(expected_edit_sequence) is int and 0 <= expected_edit_sequence <= MAX_EDIT_SEQUENCE,
                    "invalid_request", "Expected configuration edit sequence is invalid")
            proposed = validate_graph_document(document)
            require(proposed["schema_version"] == 3, "graph_invalid_document",
                    "Configuration updates require graph document schema 3")
            require(proposed["workflow_definition_id"] == workflow_definition_id, "invalid_request",
                    "Submitted document identity must equal the source workflow")
            require("edit_sequence" not in proposed or proposed["edit_sequence"] == expected_edit_sequence,
                    "invalid_request", "Document edit sequence differs from its editing basis")
            proposed.pop("edit_sequence", None)
            self._require_definition_active(repo, workflow_definition_id)
            current = repo.current_configuration(workflow_definition_id)
            require((current["edit_sequence"] if current else 0) == expected_edit_sequence,
                    "stale_edit_sequence", "Workflow configuration changed", 409)
            existing = repo.rows("workflow_definition_revision", workflow_definition_id=workflow_definition_id)
            require(current is not None or not existing, "graph_configuration_missing",
                    "An existing definition has no current schema 3 configuration", 409)
            effective = any(not self._archived(repo, "session", session["workflow_session_id"])
                            for session in repo.rows("workflow_session", workflow_definition_id=workflow_definition_id))
            for node in proposed["nodes"]:
                validate_node_configuration(node, self._configuration_descriptor(repo, node))
            if current is None:
                require(proposed["revision"] == 1, "invalid_request", "A new workflow starts at fixed revision 1")
                categories = ["fixed"]
                outcome, sequence = "saved", 1
                self._save_definition(repo, proposed, 0)
                self._write_configuration(repo, proposed, sequence)
            else:
                previous = self._configuration_document(repo, workflow_definition_id)
                require(proposed["revision"] == current["fixed_definition_revision"], "stale_revision",
                        "Document fixed revision differs from its editing basis", 409)
                categories = classify_configuration_changes(
                    previous, proposed, lambda node: self._configuration_descriptor(repo, node))
                outcome = "derived" if effective and "fixed" in categories else "saved" if categories else "unchanged"
                if outcome == "derived":
                    proposed["workflow_definition_id"] = str(uuid4())
                    proposed["revision"] = 1
                    sequence = 1
                    self._save_definition(repo, proposed, 0)
                    self._write_configuration(repo, proposed, sequence)
                elif categories:
                    require(current["edit_sequence"] < MAX_EDIT_SEQUENCE, "graph_edit_sequence_exhausted",
                            "Workflow configuration edit sequence is exhausted", 409)
                    sequence = current["edit_sequence"] + 1
                    if "fixed" in categories:
                        proposed["revision"] += 1
                        self._save_definition(repo, proposed, current["fixed_definition_revision"])
                    self._write_configuration(repo, proposed, sequence)
                else:
                    sequence = current["edit_sequence"]
            actual = self._configuration_document(repo, proposed["workflow_definition_id"])
            return {
                "schema_version": 1, "kind": "workflow.configuration-result", "outcome": outcome,
                "source_workflow_definition_id": workflow_definition_id,
                "workflow_definition_id": actual["workflow_definition_id"],
                "definition_revision": actual["revision"], "edit_sequence": sequence,
                "source_has_effective_session": effective, "changed_categories": categories,
                "node_id_map": {node["node_binding_id"]: node["node_binding_id"] for node in actual["nodes"]},
                "session_id": None, "document": actual,
            }

        return self._change("graph.definition.update", idempotency_key, request, change)
