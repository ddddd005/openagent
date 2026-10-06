"""Current execution selection without rewriting the historical project row."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .contract_json import canonical_bytes, loads_strict
from .graph_records import require


CURRENT_EXECUTION_CONFIGURATION = "current-execution"
HISTORICAL_PROJECT_CONFIGURATION = "project"


@dataclass(frozen=True)
class GraphPackageSelection:
    configuration_id: str | None
    enabled_packages: Any
    payload: str | None


class GraphPackageSelectionStore:
    def __init__(self, store):
        self.connection = store._connection

    def _payload(self, configuration_id):
        row = self.connection.execute(
            "SELECT payload FROM graph_project_packages WHERE configuration_id=?",
            (configuration_id,),
        ).fetchone()
        return row["payload"] if row is not None else None

    def current_payload(self) -> str | None:
        return self._payload(CURRENT_EXECUTION_CONFIGURATION)

    def historical_payload(self) -> str | None:
        return self._payload(HISTORICAL_PROJECT_CONFIGURATION)

    def read_effective(self, default_selection) -> GraphPackageSelection:
        for configuration_id in (
            CURRENT_EXECUTION_CONFIGURATION, HISTORICAL_PROJECT_CONFIGURATION,
        ):
            payload = self._payload(configuration_id)
            if payload is not None:
                return GraphPackageSelection(configuration_id, loads_strict(payload), payload)
        return GraphPackageSelection(None, deepcopy(default_selection), None)

    def write_current(self, enabled_packages) -> None:
        require(self.connection.in_transaction, "storage_contract_violation",
                "Package selection changes require the owning transaction", 500)
        self.connection.execute(
            "INSERT INTO graph_project_packages VALUES(?,?) "
            "ON CONFLICT(configuration_id) DO UPDATE SET payload=excluded.payload",
            (CURRENT_EXECUTION_CONFIGURATION, canonical_bytes(enabled_packages).decode("utf-8")),
        )
