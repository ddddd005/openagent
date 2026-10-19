"""Independent global prompt and lorebook management over shared storage."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import RLock

from .contract_errors import require
from .contract_json import canonical_bytes, content_digest, validate_json_value
from .graph_application_identity import persist_application_command_identity
from .global_resources import GlobalResourceStore
from .host_sdk import ResourceIdentity, TypeRegistry
from .model_contract import CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE
from .prompt_lifecycle_nodes import PROMPT_RESOURCE_TYPE, lifecycle_prompt_resource_definition
from .resource_contracts import resource_id, resource_revision
from .storage import SqliteStore
from .tavern.lorebook_resources import LOREBOOK_RESOURCE_TYPE, lorebook_resource_definition


CONTENT_RESOURCE_TYPES = (PROMPT_RESOURCE_TYPE, LOREBOOK_RESOURCE_TYPE)
PROVIDER_RESOURCE_TYPES = (CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE)
CONTENT_METADATA_OPERATION = "graph.content.metadata.update"


def initialize_content_metadata(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS content_resource_metadata ("
        "scope TEXT NOT NULL,type_id TEXT NOT NULL,resource_id TEXT NOT NULL,"
        "display_name TEXT NOT NULL,metadata_sequence INTEGER NOT NULL CHECK(metadata_sequence>=1),"
        "PRIMARY KEY(scope,type_id,resource_id))"
    )


def validate_content_metadata(value):
    require(type(value) is dict and set(value) == {"reference", "display_name", "metadata_sequence"},
            "content_metadata_invalid", "Content metadata envelope is invalid")
    validate_json_value(value)
    reference = ResourceIdentity.from_dict(value["reference"]).to_dict()
    require(reference["type_id"] == PROMPT_RESOURCE_TYPE,
            "content_metadata_type_denied", "Display metadata belongs to prompt resources")
    require(type(value["display_name"]) is str and len(value["display_name"]) <= 128
            and value["display_name"].strip() == value["display_name"],
            "content_metadata_invalid", "Display name must be bounded text without surrounding whitespace")
    resource_revision(value["metadata_sequence"], zero=True)
    return deepcopy(value)


def global_resource_service(owner, type_id):
    require(type(type_id) is str, "invalid_request", "Resource type is invalid")
    return (owner.provider_configuration if type_id in PROVIDER_RESOURCE_TYPES
            else owner.content_configuration)


def list_managed_global_resources(owner, *, scope=None, type_id=None):
    if type_id is not None:
        return global_resource_service(owner, type_id).list_global_resources(scope=scope, type_id=type_id)
    records = [*owner.provider_configuration.list_global_resources(scope=scope),
               *owner.content_configuration.list_global_resources(scope=scope)]
    return sorted(records, key=lambda row: (row["scope"], row["type_id"], row["resource_id"]))


class ContentConfigurationService:
    """Own content CRUD, without a provider, credential vault or graph runtime."""

    def __init__(self, database_path, *, store_factory=None, type_registry_factory=None):
        self.database = Path(database_path).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._store_factory = store_factory or (lambda: SqliteStore(self.database))
        self._lock = RLock()
        self._types = TypeRegistry()
        for definition in (lifecycle_prompt_resource_definition(), lorebook_resource_definition()):
            self._types.register(definition)
        self._type_registry_factory = type_registry_factory or (lambda: self._types)

    @staticmethod
    def _managed_types(types):
        return tuple(sorted({definition["type_id"] for definition in types.catalog(scope="global")
                             if definition["type_id"] not in PROVIDER_RESOURCE_TYPES}))

    def _identity(self, identity, types):
        identity = ResourceIdentity.from_dict(identity).to_dict()
        require(identity["type_id"] in self._managed_types(types),
                "content_resource_type_denied", "Resource is not managed by the content service")
        return identity

    def _resources(self, store, *, types=None):
        return GlobalResourceStore(store, self._type_registry_factory() if types is None else types)

    def get_global_resource(self, identity, *, types=None):
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store, types=types)
            identity = self._identity(identity, resources.types)
            return resources.get(identity)

    def list_global_resources(self, *, scope=None, type_id=None):
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            managed_types = self._managed_types(resources.types)
            require(type_id is None or type_id in managed_types,
                    "content_resource_type_denied", "Resource is not managed by the content service")
            records = [row for current_type in ((type_id,) if type_id is not None else managed_types)
                       for row in resources.list(scope=scope, type_id=current_type)]
            return sorted(records, key=lambda row: (row["scope"], row["type_id"], row["resource_id"]))

    def save_global_resource(self, record, *, expected_sequence, idempotency_key):
        require(type(record) is dict, "global_resource_invalid", "Global resource envelope is invalid")
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            self._identity({key: record.get(key) for key in (
                "envelope_version", "scope", "type_id", "resource_id")}, resources.types)
            return resources.write(record, expected_sequence=expected_sequence,
                                   idempotency_key=idempotency_key)

    def delete_global_resource(self, identity, *, expected_sequence, idempotency_key):
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            identity = self._identity(identity, resources.types)
            return resources.delete(identity, expected_sequence=expected_sequence,
                                    idempotency_key=idempotency_key)

    def _metadata_reference(self, reference, resources):
        reference = self._identity(reference, resources.types)
        require(reference["type_id"] == PROMPT_RESOURCE_TYPE,
                "content_metadata_type_denied", "Display metadata belongs to prompt resources")
        return reference

    @staticmethod
    def _metadata_current(connection, reference):
        row = connection.execute(
            "SELECT display_name,metadata_sequence FROM content_resource_metadata "
            "WHERE scope=? AND type_id=? AND resource_id=?",
            tuple(reference[key] for key in ("scope", "type_id", "resource_id")),
        ).fetchone()
        return validate_content_metadata({
            "reference": reference, "display_name": row["display_name"] if row else "",
            "metadata_sequence": row["metadata_sequence"] if row else 0,
        })

    def read_content_metadata(self, reference):
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            reference = self._metadata_reference(reference, resources)
            initialize_content_metadata(store._connection)
            require(resources.get(reference) is not None,
                    "global_resource_missing", "Prompt resource does not exist", 404)
            return self._metadata_current(store._connection, reference)

    def _metadata_request(self, reference, display_name, expected_metadata_sequence, resources):
        reference = self._metadata_reference(reference, resources)
        validate_json_value(display_name)
        require(type(display_name) is str and len(display_name) <= 128,
                "content_metadata_invalid", "Display name must be bounded text")
        resource_revision(expected_metadata_sequence, zero=True)
        return {"reference": reference, "display_name": display_name,
                "expected_metadata_sequence": expected_metadata_sequence}

    @staticmethod
    def _metadata_receipt(store, request, idempotency_key):
        digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
        prior = store.read_receipt_with_digest(CONTENT_METADATA_OPERATION, idempotency_key)
        if prior is None:
            return None
        require(prior[0] == digest, "idempotency_conflict", "Content metadata request differs", 409)
        require(type(prior[1]) is list and len(prior[1]) == 1
                and type(prior[1][0]) is dict and set(prior[1][0]) == {"graph_result"},
                "storage_contract_violation", "Content metadata receipt is invalid", 500)
        result = validate_content_metadata(prior[1][0]["graph_result"])
        require(result["reference"] == request["reference"]
                and result["display_name"] == request["display_name"].strip()
                and result["metadata_sequence"] == request["expected_metadata_sequence"] + 1,
                "storage_contract_violation", "Content metadata receipt differs from its request", 500)
        return result

    def read_content_metadata_receipt(self, reference, display_name, expected_metadata_sequence, idempotency_key):
        resource_id(idempotency_key)
        with self._lock, closing(self._store_factory()) as store:
            request = self._metadata_request(reference, display_name, expected_metadata_sequence, self._resources(store))
            return self._metadata_receipt(store, request, idempotency_key)

    def update_content_metadata(self, reference, display_name, expected_metadata_sequence, idempotency_key):
        resource_id(idempotency_key)
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            request = self._metadata_request(reference, display_name, expected_metadata_sequence, resources)
            initialize_content_metadata(store._connection)
            connection = store._connection
            require(not connection.in_transaction, "transaction_in_progress",
                    "Content metadata requires its own short transaction", 409)
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = self._metadata_receipt(store, request, idempotency_key)
                if prior is not None:
                    connection.execute("COMMIT")
                    return prior
                require(resources.get(request["reference"]) is not None,
                        "global_resource_missing", "Prompt resource does not exist", 404)
                current = self._metadata_current(connection, request["reference"])
                require(current["metadata_sequence"] == expected_metadata_sequence,
                        "stale_metadata_sequence", "Content display name has changed", 409)
                require(expected_metadata_sequence < 2**53 - 1,
                        "metadata_sequence_exhausted", "Content metadata sequence is exhausted", 409)
                result = validate_content_metadata({
                    "reference": request["reference"], "display_name": display_name.strip(),
                    "metadata_sequence": expected_metadata_sequence + 1,
                })
                connection.execute(
                    "INSERT INTO content_resource_metadata VALUES(?,?,?,?,?) "
                    "ON CONFLICT(scope,type_id,resource_id) DO UPDATE SET "
                    "display_name=excluded.display_name,metadata_sequence=excluded.metadata_sequence",
                    (*tuple(request["reference"][key] for key in ("scope", "type_id", "resource_id")),
                     result["display_name"], result["metadata_sequence"]),
                )
                store._inject("after_content_metadata_write")
                digest = "workflow-op-v1:" + content_digest(request).rsplit(":", 1)[1]
                connection.execute(
                    "INSERT INTO idempotency (operation,key,digest,result_refs,result_payload,request_digest) "
                    "VALUES(?,?,?,?,?,?)",
                    (CONTENT_METADATA_OPERATION, idempotency_key, digest, "[]",
                     canonical_bytes([{"graph_result": result}]).decode("utf-8"), digest),
                )
                persist_application_command_identity(connection, table="idempotency",
                                                     operation=CONTENT_METADATA_OPERATION,
                                                     key=idempotency_key, request_digest=digest)
                store._inject("before_commit")
                connection.execute("COMMIT")
                return deepcopy(result)
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
