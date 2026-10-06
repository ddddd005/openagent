"""Strict JSON and a versioned, local deterministic digest representation."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .contract_errors import ContractValidationError


def validate_json_value(value: Any) -> None:
    """Reject Python-only values, nonfinite numbers, cycles and invalid UTF-8."""
    active: set[int] = set()

    def visit(item: Any) -> None:
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ContractValidationError("JSON numbers must be finite")
            return
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise ContractValidationError("JSON strings must be valid UTF-8") from exc
            return
        if type(item) not in (list, dict):
            raise ContractValidationError("Only JSON values are allowed")
        identity = id(item)
        if identity in active:
            raise ContractValidationError("Cyclic JSON values are not allowed")
        active.add(identity)
        try:
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ContractValidationError("JSON object keys must be strings")
                    visit(key)
                    visit(child)
            else:
                for child in item:
                    visit(child)
        finally:
            active.remove(identity)

    try:
        visit(value)
    except RecursionError as exc:
        raise ContractValidationError("JSON nesting exceeds the supported depth") from exc


def loads_strict(text: str) -> Any:
    """Parse JSON without silently accepting duplicate keys or nonfinite values."""
    if type(text) is not str:
        raise ContractValidationError("JSON input must be text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ContractValidationError("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ContractValidationError("Nonfinite JSON constants are not allowed")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        validate_json_value(value)
        return value
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("Invalid JSON input") from exc


def dumps_pretty(value: Any) -> str:
    """Serialize the presentation form, not an identity or an execution object."""
    validate_json_value(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    except (ValueError, RecursionError) as exc:
        raise ContractValidationError("JSON value cannot be serialized") from exc


def canonical_bytes(value: Any) -> bytes:
    """Encode json-v1 (Python JSON number spelling, sorted keys, no whitespace).

    This profile is deliberately not advertised as RFC 8785. Other languages
    must not produce idempotency digests until their encoding is verified.
    """
    validate_json_value(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise ContractValidationError("JSON value cannot be serialized") from exc


def content_digest(value: Any) -> str:
    return "json-v1:sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()
