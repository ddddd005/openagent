"""Trusted configuration rules and projections for schema 3 workflow editing.

The configuration functional interface accepts and dispatches commands; the
configuration data interface performs limited reads and writes in the caller's
transaction.  This module only decides policy validity, edit categories,
confirmation and derivation, and composes the current configuration view.  It
does not open transactions, hold repositories or touch SQLite directly.

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

from .contract_errors import require, uuid4_string, uuid_value
from .contract_json import canonical_bytes
from .host_sdk import bounded_name
from .configuration_mutations import (
    configuration_mutation_rule, validate_editable_input_configuration,
)


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
    default_mode: str = "fixed"


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
    "input": ConfigurationPolicyKind(
        schema_version=1,
        contracts=(("tools.editable-input", "1"),),
        fields=("text",),
        validate_entry=validate_editable_input_configuration,
        default_mode="adjustable",
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


def default_configuration_policy(component_id, component_version, declaration):
    declaration = validate_adjustable_configuration(component_id, component_version, declaration)
    mode = configuration_policy_kind(declaration).default_mode if declaration is not None else "fixed"
    return {"schema_version": 1, "prompt_mode": mode}


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


class WorkflowConfiguration:
    def _configuration_descriptor(self, node, *, definitions=None):
        from .workflow_definitions import WorkflowDefinitionCatalog

        if definitions is None:
            definitions = getattr(self, "definition_query", None)
        if definitions is None:
            definitions = WorkflowDefinitionCatalog(
                self.registry, getattr(self, "_package_manifests", ()))
        view = definitions.get("node", {
            "component_id": node["component_id"],
            "component_version": node["component_version"],
        })
        require(view is not None, "graph_missing_node_type",
                "Current trusted node definition is unavailable", 409,
                node_id=node["node_binding_id"])
        return deepcopy(view["declaration"])

    def _compose_configuration(self, current, fixed_document, *, registry=None, definitions=None):
        if definitions is not None:
            definitions_registry = getattr(definitions, "registry", None)
            if definitions_registry is not None:
                if registry is None:
                    registry = definitions_registry
                else:
                    require(definitions_registry is registry, "graph_definition_basis_mismatch",
                            "Configuration composition requires one definition basis")
        registry = self.registry if registry is None else registry
        if definitions is None:
            definitions = getattr(self, "definition_query", None) if registry is self.registry else None
        if definitions is None:
            from .workflow_definitions import WorkflowDefinitionCatalog
            definitions = WorkflowDefinitionCatalog(
                registry, getattr(self, "_package_manifests", ()))
        document = deepcopy(fixed_document)
        document["name"] = current["display_name"]
        document["edit_sequence"] = current["edit_sequence"]
        for node in document["nodes"]:
            identity = node["node_binding_id"]
            presentation = current["node_presentation"][identity]
            node.update(deepcopy(presentation))
            descriptor = self._configuration_descriptor(node, definitions=definitions)
            declaration = validate_node_configuration(node, descriptor)
            if declaration is not None and node["configuration_policy"]["prompt_mode"] == "adjustable":
                for field in declaration["fields"]:
                    node["config"].pop(field, None)
                node["config"].update(deepcopy(current["adjustable_config"][identity]))
                validate_node_configuration(node, descriptor)
        from .graph_contracts import validate_session_item_registrations
        validate_session_item_registrations(document, registry)
        return document

    def _configuration_view(self, data, identity, *, registry=None, definitions=None):
        current, fixed = data.configuration_view(identity)
        require(current is not None, "not_found", "Current workflow configuration not found", 404)
        return self._compose_configuration(current, fixed, registry=registry, definitions=definitions)

    def _write_configuration(self, data, document, edit_sequence):
        adjustable = {}
        presentation = {}
        for node in document["nodes"]:
            identity = node["node_binding_id"]
            declaration = validate_node_configuration(node, self._configuration_descriptor(node))
            adjustable[identity] = adjustable_configuration_projection(
                node["config"], declaration, node["configuration_policy"])
            presentation[identity] = {field: deepcopy(node[field]) for field in ("title", "position")}
        data.save_current_configuration({
            "workflow_definition_id": document["workflow_definition_id"], "edit_sequence": edit_sequence,
            "fixed_definition_revision": document["revision"], "display_name": document["name"],
            "node_presentation": presentation, "adjustable_config": adjustable,
        })

    def _patch_node_configuration(self, data, *, workflow_definition_id, node_binding_id, component_id,
                                  component_version, changes, expected_edit_sequence, confirm_derivation=None):
        uuid4_string(workflow_definition_id)
        uuid4_string(node_binding_id)
        self._validate_configuration_edit_basis(expected_edit_sequence, confirm_derivation)
        rule = configuration_mutation_rule(component_id, component_version)
        rule.validate_changes(changes, node_id=node_binding_id)
        self._require_definition_active_in(data, workflow_definition_id)
        current = data.current_configuration(workflow_definition_id)
        require(current is not None, "graph_configuration_missing",
                "Configuration patch requires a current schema 3 configuration", 409)
        require(current["edit_sequence"] == expected_edit_sequence,
                "stale_edit_sequence", "Workflow configuration changed", 409)
        proposed = self._configuration_view(data, workflow_definition_id)
        node = next((entry for entry in proposed["nodes"] if entry["node_binding_id"] == node_binding_id), None)
        require(node is not None, "graph_missing_node", "Configuration patch target node not found", 404,
                node_id=node_binding_id)
        require((node["component_id"], node["component_version"]) == (component_id, component_version),
                "graph_configuration_target_mismatch", "Configuration patch target contract differs", 409,
                node_id=node_binding_id)
        node["config"].update(deepcopy(changes))
        rule.validate_configuration(node["config"], node_id=node_binding_id)
        return self._save_configuration_update(
            data, proposed, workflow_definition_id=workflow_definition_id,
            expected_edit_sequence=expected_edit_sequence, confirm_derivation=confirm_derivation)

    @staticmethod
    def _validate_configuration_edit_basis(expected_edit_sequence, confirm_derivation):
        require(type(expected_edit_sequence) is int and 0 <= expected_edit_sequence <= MAX_EDIT_SEQUENCE,
                "invalid_request", "Expected configuration edit sequence is invalid")
        require(confirm_derivation is None or type(confirm_derivation) is bool, "invalid_request",
                "Derivation confirmation must be boolean")

    @staticmethod
    def _require_no_unfinished_run(data, workflow_definition_id):
        """G21: user configuration edits stay locked while a run is unfinished.

        Occupancy is read from the caller's configuration-data transaction: a
        persisted ``workflow_session`` still holding ``active_chain_run_id``
        belongs to a run that has neither succeeded nor been explicitly closed.
        The rule is scoped to the workflow definition identity and needs the
        persisted sessions only, so a process restart does not unlock a run.
        """
        unfinished = any(session.get("active_chain_run_id") is not None
                         for session in data.definition_sessions(workflow_definition_id))
        require(not unfinished, "configuration_run_locked",
                "Configuration cannot change while a workflow run is unfinished", 409,
                workflow_definition_id=workflow_definition_id)

    def _save_configuration_update(self, data, document, *, workflow_definition_id, expected_edit_sequence,
                                   confirm_derivation):
        from .graph_contracts import validate_graph_document, validate_session_item_registrations
        uuid4_string(workflow_definition_id)
        self._validate_configuration_edit_basis(expected_edit_sequence, confirm_derivation)
        proposed = validate_graph_document(document)
        require(proposed["schema_version"] == 3, "graph_invalid_document",
                "Configuration updates require graph document schema 3")
        validate_session_item_registrations(proposed, self.registry)
        require(proposed["workflow_definition_id"] == workflow_definition_id, "invalid_request",
                "Submitted document identity must equal the source workflow")
        require("edit_sequence" not in proposed or proposed["edit_sequence"] == expected_edit_sequence,
                "invalid_request", "Document edit sequence differs from its editing basis")
        proposed.pop("edit_sequence", None)
        self._require_definition_active_in(data, workflow_definition_id)
        self._require_no_unfinished_run(data, workflow_definition_id)
        current = data.current_configuration(workflow_definition_id)
        require((current["edit_sequence"] if current else 0) == expected_edit_sequence,
                "stale_edit_sequence", "Workflow configuration changed", 409)
        existing = data.definition_revisions(workflow_definition_id)
        require(current is not None or not existing, "graph_configuration_missing",
                "An existing definition has no current schema 3 configuration", 409)
        effective = any(not data.archived("session", session["workflow_session_id"])
                        for session in data.definition_sessions(workflow_definition_id))
        for node in proposed["nodes"]:
            validate_node_configuration(node, self._configuration_descriptor(node))
        if current is None:
            require(proposed["revision"] == 1, "invalid_request", "A new workflow starts at fixed revision 1")
            categories = ["fixed"]
            outcome, sequence = "saved", 1
            data.save_fixed_revision(proposed, 0)
            self._write_configuration(data, proposed, sequence)
        else:
            require(proposed["revision"] == current["fixed_definition_revision"], "stale_revision",
                    "Document fixed revision differs from its editing basis", 409)
            previous = self._configuration_view(data, workflow_definition_id)
            categories = classify_configuration_changes(
                previous, proposed, self._configuration_descriptor)
            if effective and "fixed" in categories and not confirm_derivation:
                # Ask before writing any definition, configuration or session.
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
                data.save_fixed_revision(proposed, 0)
                self._write_configuration(data, proposed, sequence)
            elif categories:
                require(current["edit_sequence"] < MAX_EDIT_SEQUENCE, "graph_edit_sequence_exhausted",
                        "Workflow configuration edit sequence is exhausted", 409)
                sequence = current["edit_sequence"] + 1
                if "fixed" in categories:
                    proposed["revision"] += 1
                    data.save_fixed_revision(proposed, current["fixed_definition_revision"])
                self._write_configuration(data, proposed, sequence)
            else:
                sequence = current["edit_sequence"]
        actual = self._configuration_view(data, proposed["workflow_definition_id"])
        return {
            "schema_version": 1, "kind": "workflow.configuration-result", "outcome": outcome,
            "source_workflow_definition_id": workflow_definition_id,
            "workflow_definition_id": actual["workflow_definition_id"],
            "definition_revision": actual["revision"], "edit_sequence": sequence,
            "source_has_effective_session": effective, "changed_categories": categories,
            "node_id_map": {node["node_binding_id"]: node["node_binding_id"] for node in actual["nodes"]},
            "session_id": None, "document": actual,
        }

    def copy_configuration(self, data, *, source_workflow_definition_id,
                           expected_source_definition_revision,
                           expected_source_edit_sequence,
                           target_workflow_definition_id, target_name):
        """Copy one formal current configuration into a brand-new workflow identity.

        This runs inside the caller's existing command transaction (the shared
        atomic configuration command envelope already replayed/checked the native
        receipt).  It reads the source current configuration and its exact fixed
        revision in that same transaction, verifies the source versions and that
        the target identity has no formal record, then writes a deep-copied
        document with the target identity, name, ``revision=1`` and
        ``edit_sequence=1``.  It never updates, locks or pauses the source, never
        requires an effective session and never copies sessions, runs,
        checkpoints or variable values.

        G-A is decided (2026-10-21): an archived formal source is allowed.  The
        source is only read, so its archive state is not a gate; the source must
        still have a current configuration.  The result shape carries no extra
        archive field.
        """
        from .graph_contracts import validate_graph_document, validate_session_item_registrations

        require(uuid_value(source_workflow_definition_id)
                and uuid_value(target_workflow_definition_id)
                and source_workflow_definition_id != target_workflow_definition_id,
                "invalid_request", "Copy source and target must be distinct workflow identities")
        require(type(expected_source_definition_revision) is int
                and 1 <= expected_source_definition_revision <= MAX_EDIT_SEQUENCE,
                "invalid_request", "Copy source definition revision is invalid")
        require(type(expected_source_edit_sequence) is int
                and 1 <= expected_source_edit_sequence <= MAX_EDIT_SEQUENCE,
                "invalid_request", "Copy source edit sequence is invalid")
        require(bounded_name(target_name), "invalid_request",
                "Copy target name must be a bounded name")

        current, fixed = data.configuration_view(source_workflow_definition_id)
        if current is None:
            # A physically deleted source has no formal fixed revision either.
            # A legacy fixed-only item is not a formal copy source: never read a
            # historical fixed document to fabricate a current configuration.
            require(data.has_definition_revision(source_workflow_definition_id),
                    "not_found", "Source workflow definition not found", 404)
            require(False, "graph_configuration_missing",
                    "Copy requires the source current configuration", 409)
        require(current["fixed_definition_revision"] == expected_source_definition_revision,
                "stale_revision", "Source workflow definition changed", 409)
        require(current["edit_sequence"] == expected_source_edit_sequence,
                "stale_edit_sequence", "Source workflow configuration changed", 409)

        # The target must not exist in any formal form, so the copy can never
        # silently overwrite or take over an existing definition.
        require(not data.definition_revisions(target_workflow_definition_id),
                "graph_definition_conflict", "Target workflow definition already exists", 409)
        require(data.current_configuration(target_workflow_definition_id) is None,
                "graph_definition_conflict", "Target workflow definition already exists", 409)
        require(data.management_record("definition", target_workflow_definition_id) is None,
                "graph_definition_conflict", "Target workflow definition already has a management record", 409)

        proposed = self._compose_configuration(current, fixed)
        proposed["workflow_definition_id"] = target_workflow_definition_id
        proposed["revision"] = 1
        proposed["name"] = target_name
        proposed.pop("edit_sequence", None)
        proposed = validate_graph_document(proposed)
        require(proposed["schema_version"] == 3, "graph_invalid_document",
                "Configuration copies require graph document schema 3")
        validate_session_item_registrations(proposed, self.registry)
        for node in proposed["nodes"]:
            validate_node_configuration(node, self._configuration_descriptor(node))
        data.save_fixed_revision(proposed, 0)
        self._write_configuration(data, proposed, 1)
        actual = self._configuration_view(data, target_workflow_definition_id)
        return {
            "schema_version": 1, "kind": "workflow.configuration-copy-result", "outcome": "copied",
            "source_workflow_definition_id": source_workflow_definition_id,
            "source_definition_revision": expected_source_definition_revision,
            "source_edit_sequence": expected_source_edit_sequence,
            "workflow_definition_id": target_workflow_definition_id,
            "definition_revision": 1, "edit_sequence": 1, "session_id": None,
            "node_id_map": {node["node_binding_id"]: node["node_binding_id"]
                            for node in actual["nodes"]},
            "document": actual,
        }
