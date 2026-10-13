"""Trusted configuration partitions and transactional workflow editing.

A node configuration is split by the policy declared on its exact contract.
Adjustable fields are the current-value family: their values are not part of
the fixed workflow check, are not saved in checkpoints and are read at their
latest saved value.  A declaration names one reviewed kind in
CONFIGURATION_POLICY_KINDS, so another member of that family (the later
global-reference content, once unified with it) is a table entry plus its own
entry validator instead of a new branch in the classification path.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from .contract_errors import require, uuid4_string
from .contract_json import canonical_bytes


PROMPT_CONFIGURATION_FIELDS = ("text", "presentation", "metadata", "lifecycle", "compaction")
PROMPT_PRESENTATION_FIELDS = frozenset(("role", "placement", "depth", "order", "enabled"))
MAX_EDIT_SEQUENCE = 2**53 - 1


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


@dataclass(frozen=True)
class ConfigurationPolicyKind:
    """One reviewed current-value family available to exact node contracts.

    `contracts` is the reviewed whitelist of exact component identities that may
    declare this kind; `fields` names the values that stay current instead of
    entering the fixed definition.  A contract listed in `member_contracts`
    instead keeps an identified member array current: `members` plus
    `member_fields` addressed by `member_identity_field`.  `nested_fields`
    keeps a reviewed whitelist inside a declared field without opening the
    whole container.  `validate_entry` is the kind's own storage-format check.
    """

    schema_version: int
    contracts: tuple[tuple[str, str], ...]
    fields: tuple[str, ...]
    member_contracts: tuple[tuple[str, str], ...] = ()
    member_fields: tuple[str, ...] = ()
    member_identity_field: str | None = None
    nested_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    validate_entry: Callable[..., None] | None = None


CONFIGURATION_POLICY_KINDS: dict[str, ConfigurationPolicyKind] = {
    "prompt": ConfigurationPolicyKind(
        schema_version=1,
        contracts=(("prompts.item", "3"), ("prompts.group", "3")),
        fields=PROMPT_CONFIGURATION_FIELDS,
        member_contracts=(("prompts.group", "3"),),
        member_fields=PROMPT_CONFIGURATION_FIELDS,
        member_identity_field="id",
        nested_fields=(("presentation", tuple(PROMPT_PRESENTATION_FIELDS)),),
        validate_entry=_validate_prompt_fields,
    ),
}


def validate_adjustable_configuration(component_id, component_version, declaration):
    """Only a registered kind's reviewed exact contracts may declare current values."""
    if declaration is None:
        return None
    require(type(declaration) is dict and type(declaration.get("policy")) is str,
            "graph_invalid_adjustable_declaration", "Adjustable configuration requires a registered policy kind")
    kind = CONFIGURATION_POLICY_KINDS.get(declaration["policy"])
    identity = (component_id, component_version)
    require(kind is not None and identity in kind.contracts,
            "graph_invalid_adjustable_declaration", "This exact node contract cannot declare adjustable configuration")
    member_contract = identity in kind.member_contracts
    expected_keys = {"schema_version", "policy", "fields"}
    if member_contract:
        expected_keys |= {"member_identity_field", "member_fields"}
    require(set(declaration) == expected_keys
            and type(declaration["schema_version"]) is int and declaration["schema_version"] == kind.schema_version,
            "graph_invalid_adjustable_declaration", "Adjustable configuration declaration fields differ")
    fields = declaration["fields"]
    expected_fields = ("members",) if member_contract else kind.fields
    require(type(fields) is list and len(fields) == len(expected_fields)
            and all(type(field) is str for field in fields) and set(fields) == set(expected_fields),
            "graph_invalid_adjustable_declaration", "Adjustable fields differ from the reviewed whitelist")
    if member_contract:
        members = declaration["member_fields"]
        require(declaration["member_identity_field"] == kind.member_identity_field and type(members) is list
                and len(members) == len(kind.member_fields)
                and all(type(field) is str for field in members)
                and set(members) == set(kind.member_fields),
                "graph_invalid_adjustable_declaration", "Prompt member declaration differs from the reviewed whitelist")
    return deepcopy(declaration)


def validate_configuration_policy(value):
    require(type(value) is dict and set(value) == {"schema_version", "prompt_mode"}
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["prompt_mode"] in ("fixed", "adjustable"),
            "graph_invalid_configuration_policy", "Node configuration policy is invalid")
    return deepcopy(value)


def configuration_policy_kind(declaration):
    require(type(declaration) is dict and type(declaration.get("policy")) is str
            and declaration["policy"] in CONFIGURATION_POLICY_KINDS,
            "graph_invalid_adjustable_declaration", "Adjustable configuration requires a registered policy kind")
    return CONFIGURATION_POLICY_KINDS[declaration["policy"]]


def _member_container(declaration):
    """A declaration that keeps identified members current rather than one plain entry."""
    return "member_identity_field" in declaration


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
    kind = configuration_policy_kind(declaration)
    config = node["config"]
    if not _member_container(declaration):
        if kind.validate_entry is not None:
            kind.validate_entry(config, node_id=node["node_binding_id"])
        return declaration
    if "members" in config:
        members = config["members"]
        identity_field = declaration["member_identity_field"]
        require(type(members) is list and len(members) <= 1024, "graph_invalid_config",
                "Prompt members must be a bounded array", node_id=node["node_binding_id"])
        identities = set()
        for member in members:
            require(type(member) is dict and identity_field in member, "graph_invalid_config",
                    "Prompt members require explicit identities", node_id=node["node_binding_id"])
            kind.validate_entry(member, node_id=node["node_binding_id"])
            require(member[identity_field] not in identities, "graph_invalid_config",
                    "Prompt member identity is duplicated", node_id=node["node_binding_id"])
            identities.add(member[identity_field])
    return declaration


def _fixed_projection(value, excluded, nested_fields):
    fixed = {field: deepcopy(item) for field, item in value.items() if field not in excluded}
    for field, allowed in nested_fields:
        nested = value.get(field)
        if type(nested) is dict:
            extra = {key: deepcopy(item) for key, item in nested.items() if key not in allowed}
            if extra:
                fixed[field] = extra
    return fixed


def fixed_configuration_projection(config, declaration, policy):
    """Project immutable fields without comparing consumed values to today's values.

    The declaration's kind decides which values belong to the current-value
    family; nothing here is specific to one component identity.
    """
    if declaration is None or policy is None or policy["prompt_mode"] != "adjustable":
        return deepcopy(config)
    kind = configuration_policy_kind(declaration)
    if "member_identity_field" not in declaration:
        return _fixed_projection(config, declaration["fields"], kind.nested_fields)
    fixed = _fixed_projection(config, declaration["fields"], kind.nested_fields)
    identity_field = declaration["member_identity_field"]
    excluded = (*declaration["member_fields"], identity_field)
    extra = {member[identity_field]: fields for member in config.get("members", [])
             if (fields := _fixed_projection(member, excluded, kind.nested_fields))}
    if extra:
        fixed["members"] = extra
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

    def update_definition(self, document, *, workflow_definition_id, expected_edit_sequence, idempotency_key,
                          confirm_derivation=None):
        # The confirmed second submission carries an explicit flag and its own
        # idempotency key; a request without it can only ever ask, never derive.
        request = {"workflow_definition_id": workflow_definition_id,
                   "expected_edit_sequence": expected_edit_sequence, "document": document}
        if confirm_derivation is not None:
            request["confirm_derivation"] = confirm_derivation

        def change(repo):
            from .graph_contracts import validate_graph_document
            uuid4_string(workflow_definition_id)
            require(type(expected_edit_sequence) is int and 0 <= expected_edit_sequence <= MAX_EDIT_SEQUENCE,
                    "invalid_request", "Expected configuration edit sequence is invalid")
            require(confirm_derivation is None or type(confirm_derivation) is bool, "invalid_request",
                    "Derivation confirmation must be boolean")
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
                require(proposed["revision"] == current["fixed_definition_revision"], "stale_revision",
                        "Document fixed revision differs from its editing basis", 409)
                previous = self._configuration_document(repo, workflow_definition_id)
                categories = classify_configuration_changes(
                    previous, proposed, lambda node: self._configuration_descriptor(repo, node))
                if effective and "fixed" in categories and not confirm_derivation:
                    # Ask before anything is written: the source definition,
                    # current configuration and sessions all stay untouched.
                    proposed["edit_sequence"] = expected_edit_sequence
                    return {
                        "schema_version": 1, "kind": "workflow.configuration-result",
                        "outcome": "confirmation_required",
                        "source_workflow_definition_id": workflow_definition_id,
                        "workflow_definition_id": workflow_definition_id,
                        "definition_revision": proposed["revision"], "edit_sequence": expected_edit_sequence,
                        "source_has_effective_session": True, "changed_categories": categories,
                        "node_id_map": {node["node_binding_id"]: node["node_binding_id"]
                                        for node in proposed["nodes"]},
                        "session_id": None, "document": proposed,
                    }
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
