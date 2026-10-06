"""Exact model dependencies and secret-free credential evidence for one chain."""

from __future__ import annotations

import copy
import hashlib
import hmac
import os
import re
from typing import Any

from .frozen_model import FrozenModelParameters
from .model_configuration import (
    MODEL_BINDINGS, configuration_error, exact, identifier, require, revision,
    validate_provider,
)


def validate_model_selection(value: Any) -> dict[str, Any]:
    exact(value, {"schema_version", "kind", "config_id", "revision"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow_model_selection")
    identifier(value["config_id"])
    revision(value["revision"])
    return copy.deepcopy(value)


def _credential(provider: dict[str, Any]) -> str:
    if provider["credential_ref"] != "env:DEEPSEEK_API_KEY":
        raise configuration_error("credential_reference_missing", 409)
    secret = os.environ.get("DEEPSEEK_API_KEY")
    if not secret or not secret.strip():
        raise configuration_error("credential_unavailable", 409)
    return secret


def _fingerprint(secret: str) -> str:
    # A credential value never enters the plan, snapshot, URL or public projection.
    return hashlib.sha256(b"workflow-chat-credential-v1\0" + secret.encode("utf-8")).hexdigest()


def validate_model_binding(value: Any) -> dict[str, Any]:
    exact(value, {"node_id", "provider", "credential_evidence", "parameters"})
    identifier(value["node_id"])
    provider = validate_provider(value["provider"])
    require(provider["enabled"] and provider["credential_ref"] is not None)
    require(type(value["credential_evidence"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["credential_evidence"]) is not None)
    parameters = FrozenModelParameters.from_mapping(value["parameters"])
    require(dict(parameters.as_mapping()) == value["parameters"])
    return copy.deepcopy(value)


def validate_model_plan(value: Any) -> dict[str, Any]:
    exact(value, {"schema_version", "kind", "selection", "nodes"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["kind"] == "workflow_model_plan")
    validate_model_selection(value["selection"])
    require(type(value["nodes"]) is dict and set(value["nodes"]) in (set(), {"A", "B"}))
    for binding in value["nodes"].values():
        validate_model_binding(binding)
    return copy.deepcopy(value)


def resolve_model_selection(selection, catalog) -> dict[str, Any]:
    selection = validate_model_selection(selection)
    record = catalog.get_revision("model", selection["config_id"], selection["revision"])
    if record is None:
        raise configuration_error("not_found", 404)
    problems = catalog.diagnostics(record)
    if problems:
        raise configuration_error(problems[0]["code"], 409)
    nodes = {}
    by_id = {node["id"]: node for node in record["nodes"]}
    stages = dict(zip(sorted(MODEL_BINDINGS), ("A", "B")))
    for edge in record["edges"]:
        node = by_id[edge["source"]]
        ref = node["provider_ref"]
        provider = catalog.get_revision("provider", ref["provider_id"], ref["revision"])
        nodes[stages[edge["target_binding_id"]]] = {
            "node_id": node["id"], "provider": provider,
            "credential_evidence": _fingerprint(_credential(provider)),
            "parameters": dict(FrozenModelParameters.from_mapping(node["parameters"]).as_mapping()),
        }
    return validate_model_plan({
        "schema_version": 1, "kind": "workflow_model_plan", "selection": selection, "nodes": nodes,
    })

def validate_saved_model_plan(value, catalog) -> dict[str, Any]:
    plan = validate_model_plan(value)
    selection = plan["selection"]
    record = catalog.get_revision("model", selection["config_id"], selection["revision"])
    if record is None:
        raise configuration_error("storage_contract_violation", 500)
    stages = dict(zip(sorted(MODEL_BINDINGS), ("A", "B")))
    by_id = {node["id"]: node for node in record["nodes"]}
    expected = {}
    for edge in record["edges"]:
        node = by_id[edge["source"]]
        ref = node["provider_ref"]
        if ref is None:
            raise configuration_error("storage_contract_violation", 500)
        expected[stages[edge["target_binding_id"]]] = {
            "node_id": node["id"],
            "provider": catalog.get_revision("provider", ref["provider_id"], ref["revision"]),
            "parameters": dict(FrozenModelParameters.from_mapping(node["parameters"]).as_mapping()),
        }
    actual = {
        stage: {key: binding[key] for key in ("node_id", "provider", "parameters")}
        for stage, binding in plan["nodes"].items()
    }
    if actual != expected or record["nodes"] and set(expected) != {"A", "B"}:
        raise configuration_error("storage_contract_violation", 500)
    return plan


def verify_model_binding(value, catalog) -> str:
    binding = validate_model_binding(value)
    provider = binding["provider"]
    saved = catalog.get_revision("provider", provider["provider_id"], provider["revision"])
    current = catalog.get_current("provider", provider["provider_id"])
    if saved != provider:
        raise configuration_error("storage_contract_violation", 500)
    if current is None or not current["enabled"]:
        raise configuration_error("provider_unavailable", 409)
    if current["credential_ref"] != provider["credential_ref"]:
        raise configuration_error("credential_reference_missing", 409)
    credential = _credential(provider)
    if not hmac.compare_digest(_fingerprint(credential), binding["credential_evidence"]):
        raise configuration_error("credential_changed", 409)
    return credential
