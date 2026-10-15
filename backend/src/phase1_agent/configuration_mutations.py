"""Trusted narrow configuration-edit permissions, independent of policy modes."""

from dataclasses import dataclass
from typing import Callable

from .contract_errors import require


def validate_editable_input_configuration(value, *, node_id):
    require(type(value) is dict and set(value) == {"text"}
            and type(value["text"]) is str and len(value["text"]) <= 1_000_000,
            "graph_invalid_config", "Editable input requires bounded text only", node_id=node_id)


@dataclass(frozen=True)
class ConfigurationMutationRule:
    fields: tuple[str, ...]
    validate_configuration: Callable[..., None]

    def validate_changes(self, changes, *, node_id):
        require(type(changes) is dict and bool(changes) and set(changes) <= set(self.fields),
                "graph_invalid_configuration_patch", "Configuration patch fields are not permitted",
                node_id=node_id)


CONFIGURATION_MUTATION_RULES = {
    ("tools.editable-input", "1"): ConfigurationMutationRule(
        fields=("text",), validate_configuration=validate_editable_input_configuration),
}


def configuration_mutation_rule(component_id, component_version):
    require(type(component_id) is str and type(component_version) is str,
            "invalid_request", "Configuration patch requires an exact component identity")
    rule = CONFIGURATION_MUTATION_RULES.get((component_id, component_version))
    require(rule is not None, "graph_configuration_mutation_not_allowed",
            "This exact node contract has no configuration mutation permission")
    return rule
