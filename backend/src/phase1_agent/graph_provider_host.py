"""Independent provider configuration, private credentials and discovery."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import RLock

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, content_digest, loads_strict
from .credential_contract import credential_id
from .credential_store import LocalCredentialBroker, PrivateCredentialStore
from .global_resources import GlobalResourceStore
from .host_sdk import ResourceIdentity, TypeRegistry
from .model_contract import (
    CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE,
    model_type_definitions, validate_local_provider, validate_provider_record,
    validate_provider_reference,
)
from .provider_discovery import discover_provider_models
from .storage import SqliteStore


def validate_provider_key_request(request):
    require(type(request) is dict and set(request) == {
        "identity", "expected_sequence", "credential_ref", "expected_generation",
        "idempotency_key", "provider_seed",
    }, "invalid_request", "Provider Key request fields differ")
    identity = validate_provider_reference(request["identity"])
    credential_id(request["credential_ref"])
    require(type(request["expected_generation"]) is int
            and 0 <= request["expected_generation"] < 2**53 - 1
            and type(request["expected_sequence"]) is int
            and 0 <= request["expected_sequence"] < 2**53 - 1
            and uuid_value(request["idempotency_key"]),
            "invalid_request", "Provider Key request identity is invalid")
    seed = request["provider_seed"]
    if seed is not None:
        require(type(seed) is dict and set(seed) == {"name", "protocol", "base_url", "enabled"},
                "invalid_request", "Provider configuration fields differ")
        validate_local_provider({**seed, "credential_ref": request["credential_ref"]})
    else:
        require(request["expected_sequence"] > 0, "invalid_request",
                "A new provider requires its explicit configuration")
    return {**deepcopy(request), "identity": identity}


def validate_provider_binding_request(request):
    request = validate_provider_key_request(request)
    require(request["expected_generation"] > 0, "invalid_request",
            "Provider binding requires a previously saved Key generation")
    return request


class ProviderConfigurationService:
    """Management service with explicit storage authorities, no graph runtime."""

    def __init__(self, database_path, *, credential_protector=None,
                 discovery_client_factory=None, store_factory=None):
        self.database = Path(database_path).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.credentials = PrivateCredentialStore(str(self.database) + ".credentials.sqlite", credential_protector)
        self.discovery_client_factory = discovery_client_factory
        self._store_factory = store_factory or (lambda: SqliteStore(self.database))
        self._types = self._model_types()
        self._lock = RLock()
        with closing(self._store_factory()) as store:
            store._connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_key_requests ("
                "idempotency_key TEXT PRIMARY KEY,digest TEXT NOT NULL,request TEXT NOT NULL,"
                "binding_record TEXT NOT NULL,result TEXT)")

    @staticmethod
    def _model_types():
        types = TypeRegistry()
        for definition in model_type_definitions():
            if definition.scope == "global":
                types.register(definition)
        return types

    def _resources(self, store):
        return GlobalResourceStore(store, self._types)

    @staticmethod
    def _identity(identity):
        identity = ResourceIdentity.from_dict(identity).to_dict()
        require(identity["type_id"] in (CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE),
                "provider_resource_type_denied", "Resource is not managed by the provider service")
        return identity

    def new_credential_broker(self):
        return LocalCredentialBroker(self.credentials)

    def get_global_resource(self, identity):
        identity = self._identity(identity)
        with self._lock, closing(self._store_factory()) as store:
            return self._resources(store).get(identity)

    def list_global_resources(self, *, scope=None, type_id=None):
        require(type_id is None or type_id in (CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE),
                "provider_resource_type_denied", "Resource is not managed by the provider service")
        with self._lock, closing(self._store_factory()) as store:
            resources = self._resources(store)
            records = [row for current_type in ((type_id,) if type_id is not None
                       else (CHAT_PROVIDER_TYPE, MODEL_PROFILE_TYPE))
                       for row in resources.list(scope=scope, type_id=current_type)]
            return sorted(records, key=lambda row: (row["scope"], row["type_id"], row["resource_id"]))

    def save_global_resource(self, record, *, expected_sequence, idempotency_key):
        require(type(record) is dict, "global_resource_invalid", "Global resource envelope is invalid")
        self._identity({key: record.get(key) for key in (
            "envelope_version", "scope", "type_id", "resource_id")})
        with self._lock, closing(self._store_factory()) as store:
            return self._resources(store).write(record, expected_sequence=expected_sequence,
                                                idempotency_key=idempotency_key)

    def delete_global_resource(self, identity, *, expected_sequence, idempotency_key):
        identity = self._identity(identity)
        with self._lock, closing(self._store_factory()) as store:
            return self._resources(store).delete(identity, expected_sequence=expected_sequence,
                                                 idempotency_key=idempotency_key)

    def credential_status(self, credential_ref):
        return self.credentials.status(credential_ref)

    def credential_protection(self):
        return {"schema_version": 1, "kind": "workflow.credential-protection",
                "available": self.credentials.protector.available, "mode": "current-user"}

    def delete_credential(self, credential_ref, expected_generation, idempotency_key):
        return self.credentials.mutate("credential.delete", {
            "credential_ref": credential_ref, "expected_generation": expected_generation,
            "idempotency_key": idempotency_key})

    def credential_receipt(self, operation, request):
        require(type(request) is dict and set(request) == {
            "credential_ref", "expected_generation", "idempotency_key"},
            "invalid_request", "Credential receipt fields differ")
        return self.credentials.receipt(operation, request)

    @staticmethod
    def _credential_request(request):
        return {key: request[key] for key in ("credential_ref", "expected_generation", "idempotency_key")}

    @staticmethod
    def _result(outcome, reason, credential=None, provider=None):
        return {"schema_version": 1, "kind": "workflow.provider-credential-save",
                "outcome": outcome, "reason_code": reason,
                "credential": deepcopy(credential), "provider": deepcopy(provider)}

    def _original(self, store, request, operation):
        row = store._connection.execute(
            "SELECT * FROM provider_key_requests WHERE idempotency_key=?",
            (request["idempotency_key"],)).fetchone()
        if row is not None:
            identity = {"operation": operation, "parameters": request}
            require(row["digest"] == content_digest(identity)
                    and canonical_bytes(loads_strict(row["request"])) == canonical_bytes(identity),
                    "idempotency_conflict", "Provider Key request identity differs", 409)
        return row

    def _reserve(self, store, request, operation, prepare_record):
        connection = store._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self._original(store, request, operation)
            record = loads_strict(prior["binding_record"]) if prior else prepare_record()
            if prior is None:
                identity = {"operation": operation, "parameters": request}
                connection.execute("INSERT INTO provider_key_requests VALUES(?,?,?,?,NULL)", (
                    request["idempotency_key"], content_digest(identity), canonical_bytes(identity).decode(),
                    canonical_bytes(record).decode()))
            connection.execute("COMMIT")
            return prior, record
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _verify(self, store, request, record, fallback=None):
        credential = self.credentials.receipt("credential.save", self._credential_request(request))
        if credential is None:
            if fallback is not None and fallback["outcome"] != "saved" and fallback["credential"] is None:
                return deepcopy(fallback)
            return self._result("unresolved", "credential_receipt_missing" if fallback is not None
                                else "provider_credential_result_unknown")
        expected_credential = {"schema_version": 1, "kind": "workflow.credential-status",
                               "credential_ref": request["credential_ref"],
                               "generation": request["expected_generation"] + 1, "state": "saved"}
        require(canonical_bytes(credential) == canonical_bytes(expected_credential), "credential_receipt_invalid",
                "Original credential acceptance differs", 409)
        return self._verify_provider(store, request, record, credential, fallback)

    def _verify_provider(self, store, request, record, credential, fallback=None):
        resources = self._resources(store)
        provider = resources.write_receipt(record, expected_sequence=request["expected_sequence"],
                                            idempotency_key=request["idempotency_key"])
        if provider is None:
            retain_failure = fallback is not None and fallback["outcome"] != "saved"
            reason = fallback["reason_code"] if retain_failure else (
                "provider_binding_receipt_missing" if fallback is not None else "provider_binding_result_unknown")
            outcome = fallback["outcome"] if retain_failure else "unresolved"
            return self._result(outcome, reason, credential)
        expected_provider = {"reference": request["identity"],
                             "update_sequence": request["expected_sequence"] + 1, "deleted": False}
        require(canonical_bytes(provider) == canonical_bytes(expected_provider), "provider_binding_receipt_invalid",
                "Original provider acceptance differs", 409)
        if canonical_bytes(self.credentials.status(request["credential_ref"])) != canonical_bytes(credential):
            return self._result("partial", "provider_credential_changed", credential, provider)
        if resources.get(request["identity"]) != record:
            return self._result("partial", "provider_binding_changed", credential, provider)
        return self._result("saved", "provider_credential_saved", credential, provider)

    def _verify_binding(self, store, request, record, fallback=None):
        credential = self.credentials.status(request["credential_ref"])
        if credential["state"] != "saved" or credential["generation"] != request["expected_generation"]:
            return self._result("partial", "provider_credential_changed", credential)
        result = self._verify_provider(store, request, record, credential, fallback)
        if result["outcome"] == "saved":
            result["reason_code"] = "provider_credential_bound"
        return result

    def save_provider_credential(self, identity, expected_sequence, credential_ref,
                                 expected_generation, secret, idempotency_key, provider_seed):
        request = validate_provider_key_request({
            "identity": identity, "expected_sequence": expected_sequence, "credential_ref": credential_ref,
            "expected_generation": expected_generation, "idempotency_key": idempotency_key,
            "provider_seed": provider_seed})
        configuration = request["provider_seed"]
        require(configuration is not None, "invalid_request",
                "Provider saves require the complete draft configuration")
        private_request = self._credential_request(request)
        with self._lock, closing(self._store_factory()) as store:
            connection = store._connection
            def prepare_record():
                current = self._resources(store).get(request["identity"])
                if expected_sequence == 0:
                    require(current is None and self._resources(store).head(request["identity"]) is None,
                            "stale_provider_sequence", "Provider changed before Key save", 409)
                else:
                    require(current is not None and current["update_sequence"] == expected_sequence,
                            "stale_provider_sequence", "Provider changed before Key save", 409)
                    require(current["data_schema_version"] == 3, "model_provider_unavailable",
                            "Provider schema is unavailable")
                value = {**configuration, "credential_ref": credential_ref}
                validate_local_provider(value)
                return {**request["identity"], "data_schema_version": 3,
                        "update_sequence": expected_sequence + 1, "value": value}

            prior, record = self._reserve(store, request, "provider.credential.save", prepare_record)
            if prior is not None:
                # An interrupted intent never authorizes replay of either step.
                if self.credentials.receipt("credential.save", private_request) is not None:
                    self.credentials.mutate("credential.save", private_request, secret)
                return self._verify(store, request, record,
                                    loads_strict(prior["result"]) if prior["result"] else None)
            try:
                self.credentials.mutate("credential.save", private_request, secret)
            except Exception as error:
                reason = getattr(error, "reason_code", None)
                result = self._result("unresolved", reason or "provider_credential_result_unknown")
                try:
                    verified = self._verify(
                        store, request, record, self._result("partial", "provider_binding_not_applied"))
                    if verified["credential"] is not None:
                        result = verified
                except Exception:
                    pass
                connection.execute("UPDATE provider_key_requests SET result=? WHERE idempotency_key=?",
                                   (canonical_bytes(result).decode(), idempotency_key))
                return result
            try:
                self._resources(store).write(record, expected_sequence=expected_sequence,
                                             idempotency_key=idempotency_key)
                fallback = None
            except Exception as error:
                confirmed = getattr(error, "reason_code", None) in {
                    "stale_revision", "global_resource_invalid", "host_invalid_value", "host_unknown_type",
                }
                fallback = self._result(
                    "partial" if confirmed else "unresolved",
                    "provider_binding_conflict" if confirmed else "provider_binding_result_unknown")
            result = self._verify(store, request, record, fallback)
            connection.execute("UPDATE provider_key_requests SET result=? WHERE idempotency_key=?",
                               (canonical_bytes(result).decode(), idempotency_key))
            return result

    def provider_credential_receipt(self, request):
        request = validate_provider_key_request(request)
        with self._lock, closing(self._store_factory()) as store:
            prior = self._original(store, request, "provider.credential.save")
            if prior is None:
                return self._result("unresolved", "provider_credential_request_missing")
            return self._verify(store, request, loads_strict(prior["binding_record"]),
                                loads_strict(prior["result"]) if prior["result"] else None)

    def provider_binding_receipt(self, request):
        """Read pre-existing binding evidence without authorizing a new save."""
        request = validate_provider_binding_request(request)
        with self._lock, closing(self._store_factory()) as store:
            prior = self._original(store, request, "provider.credential.bind")
            if prior is None:
                return self._result("unresolved", "provider_credential_request_missing")
            return self._verify_binding(store, request, loads_strict(prior["binding_record"]),
                                        loads_strict(prior["result"]) if prior["result"] else None)

    def list_provider_models(self, identity, expected_sequence):
        require(type(expected_sequence) is int and 0 < expected_sequence <= 2**53 - 1,
                "invalid_request", "Provider sequence is invalid")
        current = self.get_global_resource(identity)
        require(current is not None and current["update_sequence"] == expected_sequence,
                "stale_provider_sequence", "Provider changed before discovery", 409)
        provider = validate_provider_record(current, identity)
        broker = self.new_credential_broker()
        lease = broker.freeze(provider["credential_ref"])
        rows = discover_provider_models(provider, broker.authorize(lease), self.discovery_client_factory)
        require(self.get_global_resource(identity) == current,
                "stale_provider_sequence", "Provider changed during discovery", 409)
        broker.authorize(lease)
        return {"schema_version": 1, "kind": "workflow.provider-models", "reference": identity,
                "update_sequence": expected_sequence, "protocol": provider["protocol"], "models": rows}
