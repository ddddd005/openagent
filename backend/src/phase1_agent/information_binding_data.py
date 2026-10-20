"""Durable invocation-source binding evidence in its own storage adapter.

Only exact invocation coordinates and accepted source declarations are kept
here.  Provider bodies remain in their package or registered storage reader,
including after restart.
"""

from .contract_json import canonical_bytes, loads_strict
from .graph_records import require
from .host_sdk import InformationSourceReference
from .runtime_hosting import InvocationOwner


def initialize_information_binding_tables(connection) -> None:
    """Create the existing binding-evidence table; no schema change."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS workflow_information_bindings ("
        "node_run_id TEXT NOT NULL, source_id TEXT NOT NULL, exact_version TEXT NOT NULL, "
        "generation INTEGER NOT NULL, owner TEXT NOT NULL, declaration TEXT NOT NULL, "
        "PRIMARY KEY(node_run_id,source_id,exact_version,generation))"
    )


class InformationBindingStore:
    """Durable evidence of which source was actually bound to an invocation."""

    def __init__(self, store):
        self.connection = store._connection
        # Construction initializes the existing table before any explicit write
        # transaction opens; this preserves the original initialization time.
        initialize_information_binding_tables(self.connection)

    def accept(self, owner, generation, definitions):
        InvocationOwner.from_dict(owner)
        require(type(generation) is int and 1 <= generation <= 2**53 - 1,
                "information_invalid_generation", "Generation must be a positive integer")
        require(self.connection.in_transaction, "storage_contract_violation",
                "Information binding acceptance requires a transaction", 500)
        for definition in definitions:
            reference = InformationSourceReference.from_dict(definition["source_ref"])
            coordinates = (owner["node_run_id"], reference.source_id, reference.exact_version, generation)
            encoded_owner = canonical_bytes(owner).decode("utf-8")
            encoded_definition = canonical_bytes(definition).decode("utf-8")
            prior = self.connection.execute(
                "SELECT owner,declaration FROM workflow_information_bindings "
                "WHERE node_run_id=? AND source_id=? AND exact_version=? AND generation=?", coordinates,
            ).fetchone()
            require(prior is None or (prior["owner"], prior["declaration"])
                    == (encoded_owner, encoded_definition),
                    "information_binding_conflict", "Invocation source binding was redefined", 409)
            if prior is None:
                self.connection.execute(
                    "INSERT INTO workflow_information_bindings VALUES(?,?,?,?,?,?)",
                    (*coordinates, encoded_owner, encoded_definition),
                )

    def get(self, reference, owner, generation):
        row = self.connection.execute(
            "SELECT owner,declaration FROM workflow_information_bindings "
            "WHERE node_run_id=? AND source_id=? AND exact_version=? AND generation=?",
            (owner["node_run_id"], reference.source_id, reference.exact_version, generation),
        ).fetchone()
        require(row is not None and loads_strict(row["owner"]) == owner,
                "information_unbound", "Source was not bound to this exact invocation generation", 404)
        return loads_strict(row["declaration"])

    def list(self, run_ids):
        # Query the requested runs; discovery does not scan provider bodies.
        result = []
        for run_id in run_ids:
            for row in self.connection.execute(
                "SELECT owner,generation,declaration FROM workflow_information_bindings "
                "WHERE node_run_id=? ORDER BY source_id,exact_version,generation", (run_id,),
            ):
                result.append({"owner": loads_strict(row["owner"]), "generation": row["generation"],
                               "declaration": loads_strict(row["declaration"])})
        return result
