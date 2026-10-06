"""Regex projections onto explicit source/purpose scopes, never saved history."""

from __future__ import annotations

import copy
import re
from dataclasses import asdict
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import content_digest, validate_json_value
from .prompt_regex import (
    REGEX_SYNTAX, RegexLimits, regex_replace_many, regex_rule_from_dict, validate_regex_limits,
)
from .prompt_values import context_view_messages, validate_context_view


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SOURCE_ROLES = {"human": "user", "upstream_node": "user", "model": "assistant"}


def validate_context_regex(config: dict[str, Any]) -> dict[str, Any]:
    validate_json_value(config)
    if (
        type(config) is not dict or set(config) != {
            "schema_version", "kind", "node_id", "enabled", "purposes", "sources", "rule",
        }
        or type(config["schema_version"]) is not int or config["schema_version"] != 1
        or config["kind"] != "context_regex" or type(config["enabled"]) is not bool
        or type(config["node_id"]) is not str or _UUID.fullmatch(config["node_id"]) is None
    ):
        raise ContractValidationError("Context regex configuration is invalid")
    for field, allowed in (
        ("purposes", ("display", "send")), ("sources", tuple(_SOURCE_ROLES)),
    ):
        selected = config[field]
        if (
            type(selected) is not list or not selected
            or any(type(item) is not str or item not in allowed for item in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ContractValidationError("Context regex scopes must be nonempty unique arrays")
    regex_rule_from_dict(config["rule"])
    return copy.deepcopy(config)


def apply_context_regex(
    view: dict[str, Any], config: dict[str, Any], *, limits: RegexLimits = RegexLimits(),
) -> dict[str, Any]:
    """Return a detached view and deterministic evidence of ordinary text edits.

    Final text protection is an explicit locator supplied by the projection
    owner. Canonical messages remain identical, including tool protocol blocks.
    """
    view = validate_context_view(view)
    config = validate_context_regex(config)
    validate_regex_limits(limits)
    original_digest = content_digest(view)
    protected = {
        (target["message_id"], target["block_index"]) for target in view["protected_blocks"]
    }
    targets = []
    texts = []
    active = config["enabled"] and view["purpose"] in config["purposes"]
    if active:
        for message in context_view_messages(view):
            source = message["source"]["kind"]
            if source not in config["sources"] or message["role"] != _SOURCE_ROLES.get(source):
                continue
            # Mixed assistant text/tool-call messages are protocol groups too.
            if any(block["kind"] != "text" for block in message["blocks"]):
                continue
            for index, block in enumerate(message["blocks"]):
                target = message["message_id"], index
                if target not in protected:
                    targets.append(target)
                    texts.append(block["text"])
        replacements = regex_replace_many(
            texts, regex_rule_from_dict(config["rule"]), limits=limits, node_id=config["node_id"],
        )
        overrides = {
            (target["message_id"], target["block_index"]): target["text"]
            for target in view["overrides"]
        }
        overrides.update(zip(targets, replacements))
        view["overrides"] = [
            {"message_id": message["message_id"], "block_index": index,
             "text": overrides[(message["message_id"], index)]}
            for message in view["messages"] for index in range(len(message["blocks"]))
            if (message["message_id"], index) in overrides
        ]
    view = validate_context_view(view)
    return {
        "schema_version": 1, "kind": "context_regex_result", "view": view,
        "trace": {
            "config": config, "limits": asdict(limits), "active": active, "regex_syntax": REGEX_SYNTAX,
            "input_digest": original_digest, "output_digest": content_digest(view),
            "processed_blocks": [
                {"message_id": identity, "block_index": index} for identity, index in targets
            ],
        },
    }
