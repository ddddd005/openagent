"""Lightweight request gate for ordinary and explicitly prepared snapshots."""

from __future__ import annotations

from typing import Any

from .contract_errors import ContractValidationError
from .contracts_v2 import validate_record


PREPARATION_CAPABILITY = "prompt_preparation_v1"


def check_prepared_request_capacity(
    snapshot: dict[str, Any], *, messages: list[dict[str, Any]] | None = None,
) -> None:
    frozen = validate_record("input_snapshot", snapshot)
    payload = frozen["config"]["payload"]
    resolved = payload.get("resolved", {})
    if type(resolved) is not dict:
        raise ContractValidationError("Resolved components must be a JSON object")
    descriptor = resolved.get("context", {})
    if type(descriptor) is not dict:
        raise ContractValidationError("Resolved Context descriptor must be a JSON object")
    capabilities = descriptor.get("capabilities", [])
    if (type(capabilities) is not list
            or any(type(capability) is not str for capability in capabilities)):
        raise ContractValidationError("Resolved Context capabilities must be a JSON array of strings")
    if PREPARATION_CAPABILITY not in capabilities:
        if payload.get("context_preparation") is not None:
            raise ContractValidationError("Legacy Context cannot carry prepared projection evidence")
        return

    from .prepared_context import _check_validated_prepared_request_capacity

    _check_validated_prepared_request_capacity(frozen, messages=messages)
