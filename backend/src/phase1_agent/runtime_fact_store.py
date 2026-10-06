"""Immutable runtime facts, separate from mutable session business objects."""

from copy import deepcopy
import base64

from .contract_json import canonical_bytes, loads_strict, validate_json_value
from .contract_errors import ContractValidationError
from .graph_records import graph_error, require


class RuntimeFactStore:
    """Persist package facts without interpreting a package's private state."""

    def __init__(self, store):
        self.store = store
        self.connection = store._connection
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_runtime_facts ("
            "fact_id TEXT NOT NULL, session_id TEXT NOT NULL, chain_id TEXT NOT NULL, "
            "node_run_id TEXT NOT NULL, stream_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
            "payload TEXT NOT NULL, PRIMARY KEY(node_run_id,stream_id,fact_id), "
            "UNIQUE(node_run_id,stream_id,sequence))"
        )

    def accept(self, *, owner, stream_id, fact):
        require(self.connection.in_transaction, "storage_contract_violation",
                "Runtime fact acceptance requires a transaction", 500)
        validate_json_value(fact)
        require(type(fact) is dict and type(fact.get("fact_id")) is str
                and 0 < len(fact["fact_id"]) <= 128
                and type(fact.get("sequence")) is int and 0 <= fact["sequence"] <= 2**53 - 1
                and type(stream_id) is str and 0 < len(stream_id) <= 512,
                "runtime_fact_invalid", "Runtime fact identity or sequence is invalid")
        encoded = canonical_bytes(fact).decode("utf-8")
        require(len(encoded.encode("utf-8")) <= 4_000_000, "runtime_fact_too_large",
                "Runtime fact exceeds the persistence limit")
        coordinates = (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"])
        prior = self.connection.execute(
            "SELECT * FROM workflow_runtime_facts WHERE node_run_id=? AND stream_id=? AND fact_id=?",
            (owner["node_run_id"], stream_id, fact["fact_id"]),
        ).fetchone()
        if prior is not None:
            require((prior["session_id"], prior["chain_id"], prior["node_run_id"]) == coordinates
                    and prior["stream_id"] == stream_id and prior["payload"] == encoded,
                    "runtime_fact_conflict", "Runtime fact identity was reused with different content", 409)
            return {"fact_id": fact["fact_id"]}
        previous = self.connection.execute(
            "SELECT MAX(sequence) AS last_sequence FROM workflow_runtime_facts "
            "WHERE node_run_id=? AND stream_id=?", (owner["node_run_id"], stream_id)
        ).fetchone()["last_sequence"]
        require(fact["sequence"] in (0, 1) if previous is None else fact["sequence"] == previous + 1,
                "runtime_fact_sequence", "Runtime facts must be accepted in sequence", 409)
        self.connection.execute(
            "INSERT INTO workflow_runtime_facts VALUES(?,?,?,?,?,?,?)",
            (fact["fact_id"], *coordinates, stream_id, fact["sequence"], encoded),
        )
        self.store._inject("after_runtime_fact_write")
        return {"fact_id": fact["fact_id"]}

    def list(self, chain_id, *, node_run_id=None):
        clause = " AND node_run_id=?" if node_run_id is not None else ""
        coordinates = (chain_id, node_run_id) if node_run_id is not None else (chain_id,)
        return [deepcopy(loads_strict(row["payload"])) for row in self.connection.execute(
            "SELECT payload FROM workflow_runtime_facts WHERE chain_id=?" + clause +
            " ORDER BY rowid", coordinates,
        )]

    def executor_facts(self, owner, reference, fact_ids):
        require(type(fact_ids) is list and 0 < len(fact_ids) <= 4096
                and all(type(identity) is str and 0 < len(identity) <= 128 for identity in fact_ids)
                and len(set(fact_ids)) == len(fact_ids),
                "runtime_fact_reference_invalid", "Fact references must be unique bounded identities")
        stream_id = "executor:" + reference["executor_id"] + "@" + reference["exact_version"]
        rows = self.connection.execute(
            "SELECT fact_id,payload FROM workflow_runtime_facts "
            "WHERE session_id=? AND chain_id=? AND node_run_id=? AND stream_id=?",
            (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"], stream_id),
        )
        values = {row["fact_id"]: loads_strict(row["payload"]) for row in rows}
        require(set(fact_ids) <= set(values), "runtime_fact_reference_denied",
                "Fact references are outside this accepted executor invocation", 403)
        return [deepcopy(values[identity]) for identity in fact_ids]

    def read_executor_page(self, owner, reference, generation, *, limit=100, cursor=None):
        """Read original immutable facts without creating an observation copy."""
        from .runtime_hosting import InvocationOwner
        from .runtime_executor_contracts import ExecutorReference
        InvocationOwner.from_dict(owner)
        executor = ExecutorReference.from_dict(reference)
        require(type(generation) is int and 1 <= generation <= 2**53 - 1
                and type(limit) is int and 1 <= limit <= 100,
                "information_invalid_limit", "Fact page requires bounded generation and page size")
        stream = "executor:" + executor.executor_id + "@" + executor.exact_version
        target = {"owner": owner, "generation": generation, "stream": stream}
        after = -1
        if cursor is not None:
            require(type(cursor) is str and 0 < len(cursor) <= 4096,
                    "information_invalid_cursor", "Fact cursor must be bounded opaque text")
            try:
                position = loads_strict(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
            except (ValueError, UnicodeError, TypeError, ContractValidationError) as error:
                raise graph_error("information_invalid_cursor", "Invalid fact reader cursor") from error
            require(type(position) is dict and set(position) == {"target", "after"}
                    and position["target"] == target
                    and type(position["after"]) is int and 0 <= position["after"] <= 2**53 - 1,
                    "information_invalid_cursor", "Fact cursor belongs to a different invocation or source")
            after = position["after"]
        coordinates = (owner["workflow_session_id"], owner["chain_run_id"], owner["node_run_id"],
                       stream, generation)
        if after >= 0 and self.connection.execute(
            "SELECT 1 FROM workflow_runtime_facts WHERE session_id=? AND chain_id=? "
            "AND node_run_id=? AND stream_id=? AND json_extract(payload,'$.generation')=? AND sequence=?",
            (*coordinates, after),
        ).fetchone() is None:
            return {"items": [], "next_cursor": None, "status": "gap"}
        rows = self.connection.execute(
            "SELECT sequence,payload FROM workflow_runtime_facts WHERE session_id=? AND chain_id=? "
            "AND node_run_id=? AND stream_id=? AND json_extract(payload,'$.generation')=? "
            "AND sequence>? ORDER BY sequence LIMIT ?", (*coordinates, after, limit + 1),
        )
        items, size, more, last_sequence = [], 0, False, after
        try:
            for row in rows:
                payload_size = len(row["payload"].encode("utf-8"))
                if len(items) == limit or size + payload_size > 3_900_000:
                    require(bool(items), "information_response_too_large",
                            "A single fact exceeds this reader's page byte bound", 409)
                    more = True
                    break
                fact = loads_strict(row["payload"])
                require(fact["owner"] == owner and fact["generation"] == generation,
                        "storage_contract_violation", "Fact page differs from its exact invocation", 500)
                items.append(fact)
                size += payload_size
                last_sequence = row["sequence"]
        finally:
            rows.close()
        next_cursor = None
        if more:
            next_cursor = base64.urlsafe_b64encode(canonical_bytes({
                "target": target, "after": last_sequence,
            })).decode("ascii")
        return {"items": items, "next_cursor": next_cursor, "status": "ok"}
