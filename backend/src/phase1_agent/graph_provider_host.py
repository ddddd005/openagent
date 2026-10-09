"""Management-only credentials, readiness and provider discovery."""

import os
from .credential_store import LocalCredentialBroker
from .graph_records import require
from .model_contract import validate_provider_record
from .provider_discovery import discover_provider_models


class GraphProviderHost:
    def _new_credential_broker(self):
        return LocalCredentialBroker(self._credentials)

    def credential_status(self, credential_ref):
        with self._lock:
            self._check_open()
            if credential_ref in ("env:DEEPSEEK_API_KEY", "env:GEMINI_API_KEY"):
                return {"schema_version": 1, "kind": "workflow.credential-status",
                        "credential_ref": credential_ref, "generation": 0,
                        "state": "saved" if os.environ.get(credential_ref[4:], "").strip() else "missing"}
            return self._credentials.status(credential_ref)

    def credential_protection(self):
        with self._lock:
            self._check_open()
            return {"schema_version": 1, "kind": "workflow.credential-protection",
                    "available": self._credentials.protector.available, "mode": "current-user"}

    def save_credential(self, credential_ref, secret, expected_generation, idempotency_key):
        request = {"credential_ref": credential_ref, "expected_generation": expected_generation,
                   "idempotency_key": idempotency_key}
        with self._lock:
            self._check_open()
            return self._credentials.mutate("credential.save", request, secret)

    def delete_credential(self, credential_ref, expected_generation, idempotency_key):
        request = {"credential_ref": credential_ref, "expected_generation": expected_generation,
                   "idempotency_key": idempotency_key}
        with self._lock:
            self._check_open()
            return self._credentials.mutate("credential.delete", request)

    def credential_receipt(self, operation, request):
        require(type(request) is dict and set(request) == {
            "credential_ref", "expected_generation", "idempotency_key"},
            "invalid_request", "Credential receipt fields differ")
        with self._lock:
            self._check_open()
            return self._credentials.receipt(operation, request)

    def list_provider_models(self, identity, expected_sequence):
        require(type(expected_sequence) is int and 0 < expected_sequence <= 2**53 - 1,
                "invalid_request", "Provider sequence is invalid")
        current = self.get_global_resource(identity)
        require(current is not None and current["update_sequence"] == expected_sequence,
                "stale_provider_sequence", "Provider changed before discovery", 409)
        provider = validate_provider_record(current, identity)
        broker = self._new_credential_broker()
        lease = broker.freeze(provider["credential_ref"])
        rows = discover_provider_models(provider, broker.authorize(lease), self._discovery_client_factory)
        require(self.get_global_resource(identity) == current,
                "stale_provider_sequence", "Provider changed during discovery", 409)
        broker.authorize(lease)
        return {"schema_version": 1, "kind": "workflow.provider-models", "reference": identity,
                "update_sequence": expected_sequence, "protocol": provider["protocol"], "models": rows}
