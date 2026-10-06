"""Redacted, locatable failures from the pure prompt preparation stages."""

from __future__ import annotations

from typing import Any

from .contract_errors import ContractValidationError


class PromptProcessingError(ContractValidationError):
    def __init__(
        self, code: str, message: str, *, node_id: str | None = None,
        item_instance_id: str | None = None, group_instance_id: str | None = None,
        offset: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id
        self.item_instance_id = item_instance_id
        self.group_instance_id = group_instance_id
        self.offset = offset

    def diagnostic(self) -> dict[str, Any]:
        return {
            "code": self.code, "node_id": self.node_id,
            "item_instance_id": self.item_instance_id,
            "group_instance_id": self.group_instance_id, "offset": self.offset,
        }
