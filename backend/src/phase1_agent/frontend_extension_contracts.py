"""Versioned bindings for trusted, locally implemented frontend extensions."""

from copy import deepcopy
import re

from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import HOST_PROTOCOL_VERSION, bounded_name, ensure


def validate_frontend_binding(kind, entrypoint, binding, host_protocol_version,
                              component_id, component_version):
    validate_json_value(binding)
    ensure(type(host_protocol_version) is int and host_protocol_version == HOST_PROTOCOL_VERSION,
           "package_extension_protocol_mismatch", "Frontend host protocol is not supported")
    ensure(type(entrypoint) is str and len(entrypoint) <= 512
           and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", entrypoint),
           "package_invalid_extension", "Frontend entrypoint must be a trusted local symbol")
    ensure(type(binding) is dict and set(binding) == {"surface", "slot", "target"},
           "package_invalid_extension", "Frontend binding fields are invalid")
    surface, slot, target = binding["surface"], binding["slot"], binding["target"]
    expected = {
        ("workbench", "panel"): "workbench-panel",
        ("workbench", "session-object"): "renderer",
        ("workbench", "node-fields"): "field-editor",
        ("consumer", "public-output"): "consumer",
    }
    ensure(type(surface) is str and type(slot) is str and expected.get((surface, slot)) == kind,
           "package_invalid_extension", "Frontend surface, slot and kind must agree")
    if slot == "panel":
        valid = type(target) is dict and not target and component_id is None and component_version is None
    elif slot == "node-fields":
        valid = (type(target) is dict and set(target) == {"component_id", "component_version"}
                 and bounded_name(target["component_id"]) and bounded_name(target["component_version"])
                 and (component_id, component_version) == (target["component_id"], target["component_version"]))
    else:
        valid = (type(target) is dict and set(target) == {"scope", "type_id", "schema_version"}
                 and target["scope"] == ("session" if slot == "session-object" else "content")
                 and bounded_name(target["type_id"])
                 and type(target["schema_version"]) is int and target["schema_version"] > 0
                 and component_id is None and component_version is None)
    ensure(valid, "package_invalid_extension", "Frontend target identity is invalid")
    return deepcopy(binding)


def validate_frontend_targets(registry, extensions):
    """Validate after all dependencies and registrations have been staged."""
    bindings = set()
    for extension in extensions:
        if extension.get("schema_version") != 2:
            continue
        binding = extension["binding"]
        if binding["slot"] == "panel":
            continue
        identity = canonical_bytes(binding)
        ensure(identity not in bindings, "package_duplicate_frontend_binding",
               "Frontend surface, slot and target are already bound")
        bindings.add(identity)
        target = binding["target"]
        if binding["slot"] == "node-fields":
            available = registry.get(target["component_id"], target["component_version"]) is not None
        else:
            available = registry.data_types.get(
                target["type_id"], target["schema_version"], scope=target["scope"]) is not None
        ensure(available, "package_frontend_target_missing",
               "Frontend binding targets an unavailable exact registered contract")
