"""Local editor records, separate from executable graph facts."""

from copy import deepcopy
from datetime import datetime, timezone

from .contract_errors import ContractValidationError
from .contract_json import canonical_bytes, content_digest, loads_strict, validate_json_value
from .graph_application_identity import persist_application_command_identity
from .graph_records import require, uuid_value


_ADDRESS_FIELDS = {"scope", "kind", "key", "workflow_definition_id", "workflow_session_id"}
_KINDS = {"management": {"workbench", "form", "intent"}, "consumer": {"chat-client", "chat-input"}}
_SECRET_FIELDS = {"api_key", "apikey", "access_token", "authorization", "private_key",
                  "secret_key", "password", "credential_value"}
MAX_DRAFT_BYTES = 3_900_000
MAX_DRAFT_SEQUENCE = 2**53 - 1


def initialize_draft_tables(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS local_editor_drafts ("
        "address TEXT PRIMARY KEY NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL, "
        "workflow_definition_id TEXT, workflow_session_id TEXT, "
        "sequence INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS local_editor_draft_scope "
        "ON local_editor_drafts(scope,workflow_definition_id,kind)"
    )


def validate_draft_address(address, scope):
    require(type(address) is dict and set(address) == _ADDRESS_FIELDS
            and type(scope) is str and scope in _KINDS and address["scope"] == scope
            and type(address["kind"]) is str and address["kind"] in _KINDS[scope],
            "invalid_draft_address", "Draft address or scope is invalid")
    key = address["key"]
    require(type(key) is str and 1 <= len(key) <= 512 and key.strip() == key
            and not any(ord(char) < 32 for char in key),
            "invalid_draft_address", "Draft key must be bounded text")
    workflow, session = address["workflow_definition_id"], address["workflow_session_id"]
    require((workflow is None or uuid_value(workflow)) and (session is None or uuid_value(session))
            and (session is None or workflow is not None),
            "invalid_draft_address", "Draft owner identity is invalid")
    if scope == "consumer":
        require(uuid_value(workflow) and (address["kind"] != "chat-input" or uuid_value(session)),
                "invalid_draft_address", "Consumer drafts require an exact workflow and input session")
    require(len(canonical_bytes(address)) <= 4096,
            "invalid_draft_address", "Draft address exceeds its limit")
    return deepcopy(address)


def _json(value):
    try:
        validate_json_value(value)
        size = len(canonical_bytes(value))
    except (ContractValidationError, RecursionError):
        require(False, "invalid_request", "Draft value must contain only JSON values")
    require(size <= MAX_DRAFT_BYTES, "draft_capacity_exceeded", "Draft value exceeds its limit", 413)


def _nonsecret(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            for key, child in item.items():
                require(key.casefold().replace("-", "_") not in _SECRET_FIELDS
                        or child is None or child == "",
                        "draft_secret_disallowed", "Secret credential fields cannot enter editor storage")
                pending.append(child)
        elif type(item) is list:
            pending.extend(item)


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_DRAFT_SEQUENCE


def validate_consumer_client(value, address):
    workflow, owner_session = address["workflow_definition_id"], address["workflow_session_id"]
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "workflow_definition_id", "workflow_session_id", "pending",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow.chat-client"
        and value["workflow_definition_id"] == workflow
        and (value["workflow_session_id"] is None or uuid_value(value["workflow_session_id"]))
        and (owner_session is None or owner_session == value["workflow_session_id"]),
        "invalid_consumer_draft", "Chat client draft identity is invalid")
    command = value["pending"]
    if command is None:
        return
    require(type(command) is dict and {"action", "workflow_session_id", "path", "body"} <= set(command)
            <= {"action", "workflow_session_id", "path", "body", "candidate"}
            and type(command["body"]) is dict,
            "invalid_consumer_draft", "Consumer pending command is invalid")
    action, session, path, body = (command[key] for key in ("action", "workflow_session_id", "path", "body"))
    require(type(action) is str and type(path) is str and uuid_value(body.get("idempotency_key")),
            "invalid_consumer_draft", "Consumer command identity is invalid")
    if action == "create":
        require(set(command) == {"action", "workflow_session_id", "path", "body"}
                and session is None and path == "/api/graph/consumer/sessions"
                and set(body) == {"workflow_definition_id", "definition_revision", "idempotency_key"}
                and body["workflow_definition_id"] == workflow and _integer(body["definition_revision"], 1),
                "invalid_consumer_draft", "Consumer creation request differs")
        return
    require(uuid_value(session) and session == value["workflow_session_id"]
            and _integer(body.get("expected_revision"), 1),
            "invalid_consumer_draft", "Consumer command session differs")
    suffix = "runs" if action == "start" else "events/submit" if action == "event" else (
        "candidates/fork" if action == "fork" else "control")
    require(path == f"/api/graph/sessions/{session}/consumer/{suffix}",
            "invalid_consumer_draft", "Consumer pending path differs")
    fields = {"expected_revision", "idempotency_key"}
    if action == "start":
        require(set(command) == {"action", "workflow_session_id", "path", "body"}
                and set(body) == fields | {"inputs"} and type(body["inputs"]) is dict
                and "_workflow_frozen_resources" not in body["inputs"],
                "invalid_consumer_draft", "Consumer input request differs")
    elif action == "event":
        require(set(command) == {"action", "workflow_session_id", "path", "body"}
                and set(body) == fields | {"workflow_definition_id", "definition_revision",
                                          "event_id", "event_schema_version", "payload"}
                and body["workflow_definition_id"] == workflow and _integer(body["definition_revision"], 1)
                and type(body["event_id"]) is str and 1 <= len(body["event_id"]) <= 128
                and body["event_id"].strip() == body["event_id"]
                and _integer(body["event_schema_version"], 1) and type(body["payload"]) is dict
                and "_workflow_frozen_resources" not in body["payload"],
                "invalid_consumer_draft", "Consumer event request differs")
    elif action == "fork":
        candidate = command.get("candidate")
        require(set(command) == {"action", "workflow_session_id", "path", "body", "candidate"}
                and set(body) == fields | {"candidate_id", "expected_data_revision", "expected_head_revision"}
                and uuid_value(body["candidate_id"]) and _integer(body["expected_data_revision"])
                and _integer(body["expected_head_revision"], 1)
                and type(candidate) is dict and set(candidate) == {
                    "candidate_id", "chain_run_id", "source_session_id",
                    "source_workflow_definition_id", "source_definition_revision",
                } and all(uuid_value(candidate[key]) for key in (
                    "candidate_id", "chain_run_id", "source_session_id", "source_workflow_definition_id"))
                and candidate["source_workflow_definition_id"] == workflow
                and candidate["candidate_id"] == body["candidate_id"]
                and _integer(candidate["source_definition_revision"], 1),
                "invalid_consumer_draft", "Consumer fork request differs")
    else:
        actions = {"pause", "resume", "close", "extend_budget", "retry_archive",
                   "retry_acceptance", "retry_failed_node"}
        extra = {"add_model_requests", "add_model_attempts"} if action == "extend_budget" else set()
        require(action in actions and set(command) == {"action", "workflow_session_id", "path", "body"}
                and set(body) == fields | {"action"} | extra and body["action"] == action
                and all(_integer(body[key]) for key in extra),
                "invalid_consumer_draft", "Consumer control request differs")


def validate_draft_value(address, format_version, value):
    require(_integer(format_version, 1), "invalid_request", "Draft format version must be positive")
    _json(value)
    _nonsecret(value)
    if value is not None and address["scope"] == "consumer":
        if address["kind"] == "chat-client":
            validate_consumer_client(value, address)
        else:
            require(type(value) in (str, dict), "invalid_consumer_draft",
                    "Chat input draft must be raw text or raw input fields")
    return value


def validate_draft_record(value):
    require(type(value) is dict and set(value) == {
        "schema_version", "kind", "address", "format_version", "sequence", "updated_at", "value", "deleted",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and value["kind"] == "workflow.local-draft",
        "storage_contract_violation", "Saved editor record is invalid", 500)
    address = value["address"]
    validate_draft_address(address, address.get("scope") if type(address) is dict else None)
    validate_draft_value(address, value["format_version"], value["value"])
    require(_integer(value["sequence"], 1) and type(value["updated_at"]) is str
            and type(value["deleted"]) is bool and value["deleted"] == (value["value"] is None),
            "storage_contract_violation", "Saved editor sequence is invalid", 500)
    try:
        stamp = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "storage_contract_violation",
                "Saved editor timestamp has no timezone", 500)
    except ValueError:
        require(False, "storage_contract_violation", "Saved editor timestamp is invalid", 500)
    return deepcopy(value)


def validate_draft_acceptance(record, request):
    validate_draft_record(record)
    require(record["address"] == request["address"]
            and record["sequence"] == request["expected_sequence"] + 1
            and record["format_version"] == request["format_version"]
            and canonical_bytes(record["value"]) == canonical_bytes(request["value"])
            and record["deleted"] == (request["value"] is None),
            "storage_contract_violation", "Draft acceptance differs from its request", 500)
    return record


class LocalDraftStore:
    def __init__(self, store):
        self.store = store
        self.connection = store._connection

    @staticmethod
    def _record(row):
        try:
            record = validate_draft_record(loads_strict(row["payload"]))
            address = record["address"]
            require(record["sequence"] == row["sequence"]
                    and canonical_bytes(address).decode("utf-8") == row["address"]
                    and all(address[key] == row[key] for key in (
                        "scope", "kind", "workflow_definition_id", "workflow_session_id")),
                    "storage_contract_violation", "Editor record coordinates differ", 500)
            return record
        except ContractValidationError:
            require(False, "storage_contract_violation", "Saved editor record failed validation", 500)

    def read(self, address):
        row = self.connection.execute(
            "SELECT * FROM local_editor_drafts WHERE address=?",
            (canonical_bytes(address).decode("utf-8"),),
        ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        require(record["address"] == address,
                "storage_contract_violation", "Editor record identity differs", 500)
        return record

    def list(self, scope, *, kind=None, workflow_definition_id=None):
        clauses, parameters = ["scope=?"], [scope]
        if kind is not None:
            clauses.append("kind=?")
            parameters.append(kind)
        if workflow_definition_id is not None:
            clauses.append("workflow_definition_id=?")
            parameters.append(workflow_definition_id)
        rows = self.connection.execute(
            "SELECT * FROM local_editor_drafts WHERE " + " AND ".join(clauses) + " ORDER BY address",
            parameters,
        )
        return [self._record(row) for row in rows]

    def save(self, operation, request, idempotency_key, check_owner):
        digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self.store.read_receipt_with_digest(operation, idempotency_key)
            if prior is not None:
                require(prior[0] == digest, "idempotency_conflict", "Draft request identity differs", 409)
                require(type(prior[1]) is list and len(prior[1]) == 1 and set(prior[1][0]) == {"graph_result"},
                        "storage_contract_violation", "Draft receipt is invalid", 500)
                result = validate_draft_acceptance(prior[1][0]["graph_result"], request)
                self.connection.execute("COMMIT")
                return result
            check_owner(request["address"], request["value"])
            current = self.read(request["address"])
            sequence = current["sequence"] if current else 0
            require(sequence == request["expected_sequence"], "stale_draft_sequence",
                    "Local draft has changed; keep the unsaved input", 409)
            require(sequence < MAX_DRAFT_SEQUENCE, "draft_sequence_exhausted", "Draft sequence is exhausted", 409)
            record = {
                "schema_version": 1, "kind": "workflow.local-draft", "address": deepcopy(request["address"]),
                "format_version": request["format_version"], "sequence": sequence + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "value": deepcopy(request["value"]), "deleted": request["value"] is None,
            }
            address = record["address"]
            self.connection.execute(
                "INSERT INTO local_editor_drafts VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET sequence=excluded.sequence,payload=excluded.payload",
                (canonical_bytes(address).decode("utf-8"), address["scope"], address["kind"],
                 address["workflow_definition_id"], address["workflow_session_id"], record["sequence"],
                 canonical_bytes(record).decode("utf-8")),
            )
            self.store._inject("after_draft_write")
            self.connection.execute(
                "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) "
                "VALUES(?,?,?,?,?,?)",
                (operation, idempotency_key, digest, "[]",
                 canonical_bytes([{"graph_result": record}]).decode("utf-8"), digest),
            )
            persist_application_command_identity(self.connection, table="idempotency", operation=operation,
                                                 key=idempotency_key, request_digest=digest)
            self.store._inject("before_commit")
            self.connection.execute("COMMIT")
            return deepcopy(record)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
