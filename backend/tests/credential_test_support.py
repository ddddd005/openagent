"""Synthetic credentials for offline tests, never a product fallback."""

from uuid import uuid4


class TestProtector:
    __test__ = False
    available = True

    def protect(self, value):
        return b"test-only:" + bytes(byte ^ 123 for byte in value)

    def unprotect(self, value):
        assert value.startswith(b"test-only:")
        return bytes(byte ^ 123 for byte in value[10:])


def mutate_test_credential(store, reference, secret):
    return store.mutate("credential.save" if secret is not None else "credential.delete", {
        "credential_ref": reference, "expected_generation": store.status(reference)["generation"],
        "idempotency_key": str(uuid4()),
    }, secret)


def seed_service_credential(service, *, reference=None, secret="private-api-secret"):
    store = service.provider_configuration.credentials
    if not store.protector.available:
        store.protector = TestProtector()
    reference = reference or "local:00000000-0000-4000-8000-000000000099"
    if store.status(reference)["state"] != "saved":
        mutate_test_credential(store, reference, secret)
    return reference
