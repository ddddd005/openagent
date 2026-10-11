"""Private, user-protected credentials and secret-free mutation receipts."""

from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3

from .contract_json import canonical_bytes, loads_strict
from .contract_errors import require, uuid_value
from .credential_contract import CredentialLease, credential_id


class WindowsCredentialProtector:
    """DPAPI current-user protection, never machine scope or plaintext."""

    def __init__(self, path):
        self.available = os.name == "nt"
        self.entropy = hashlib.sha256(("openagent-credentials:" + str(Path(path).resolve())).encode()).digest()

    def _transform(self, value, decrypt):
        require(self.available, "credential_protection_unavailable",
                "Private credentials require supported current-user system protection", 503)

        class Blob(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(data):
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            return Blob(len(data), buffer), buffer

        source, source_buffer = blob(value)
        entropy, entropy_buffer = blob(self.entropy)
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        callback.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        callback.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        require(bool(callback(ctypes.byref(source), None, ctypes.byref(entropy),
                              None, None, 1, ctypes.byref(output))),
                "credential_protection_failed", "Current-user credential protection failed", 503)
        try:
            return ctypes.string_at(output.data, output.length)
        finally:
            kernel.LocalFree(output.data)

    def protect(self, value):
        return self._transform(value, False)

    def unprotect(self, value):
        return self._transform(value, True)


class PrivateCredentialStore:
    def __init__(self, path, protector=None):
        self.path = Path(path).resolve()
        self.protector = protector or WindowsCredentialProtector(self.path)

    def _connection(self):
        require(self.protector.available, "credential_protection_unavailable",
                "Private credentials require supported current-user system protection", 503)
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS vault_meta (key TEXT PRIMARY KEY,value BLOB NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS credentials "
                           "(identity TEXT PRIMARY KEY,generation INTEGER NOT NULL,secret BLOB)")
        connection.execute("CREATE TABLE IF NOT EXISTS receipts "
                           "(key TEXT PRIMARY KEY,digest TEXT NOT NULL,request TEXT NOT NULL,result TEXT NOT NULL)")
        return connection

    @staticmethod
    def _status(reference, row):
        return {"schema_version": 1, "kind": "workflow.credential-status",
                "credential_ref": reference, "generation": row["generation"] if row else 0,
                "state": "saved" if row and row["secret"] is not None else "missing"}

    def status(self, reference):
        identity = credential_id(reference)
        if not self.path.exists():
            return self._status(reference, None)
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT generation,secret FROM credentials WHERE identity=?", (identity,)).fetchone()
            if row is not None and row["secret"] is not None:
                self._open_secret(row["secret"])
            return self._status(reference, row)

    def _open_secret(self, ciphertext):
        try:
            return self.protector.unprotect(ciphertext).decode("utf-8")
        except Exception as failure:
            if getattr(failure, "reason_code", None):
                raise
            require(False, "credential_protection_failed", "Local credential could not be opened", 503)

    def read(self, reference):
        identity = credential_id(reference)
        require(self.path.exists(), "model_credential_unavailable", "Local credential is unavailable")
        try:
            with closing(self._connection()) as connection:
                row = connection.execute("SELECT generation,secret FROM credentials WHERE identity=?", (identity,)).fetchone()
                require(row is not None and row["secret"] is not None,
                        "model_credential_unavailable", "Local credential is unavailable")
                return self._open_secret(row["secret"]), row["generation"]
        except Exception as failure:
            if getattr(failure, "reason_code", None):
                raise
            require(False, "credential_protection_failed", "Local credential could not be opened", 503)

    def mutate(self, operation, request, secret=None):
        reference = request["credential_ref"]
        identity = credential_id(reference)
        expected = request["expected_generation"]
        require(type(expected) is int and 0 <= expected < 2**53 - 1,
                "invalid_request", "Credential generation is invalid")
        require(uuid_value(request["idempotency_key"]) and operation in ("credential.save", "credential.delete"),
                "invalid_request", "Credential request identity is invalid")
        if operation == "credential.save":
            require(type(secret) is str and 1 <= len(secret) <= 8192 and secret == secret.strip()
                    and not any(ord(char) < 32 or ord(char) == 127 for char in secret),
                    "credential_secret_invalid", "Credential input is invalid")
        public_request = {"operation": operation, **request}
        with closing(self._connection()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                master = connection.execute("SELECT value FROM vault_meta WHERE key='receipt-key'").fetchone()
                if master is None:
                    evidence_key = os.urandom(32)
                    connection.execute("INSERT INTO vault_meta VALUES('receipt-key',?)",
                                       (self.protector.protect(evidence_key),))
                else:
                    evidence_key = self.protector.unprotect(master["value"])
                digest = hmac.new(evidence_key, canonical_bytes([public_request, secret]), hashlib.sha256).hexdigest()
                prior = connection.execute("SELECT * FROM receipts WHERE key=?", (request["idempotency_key"],)).fetchone()
                if prior:
                    require(hmac.compare_digest(prior["digest"], digest), "idempotency_conflict",
                            "Credential request identity differs", 409)
                    connection.execute("COMMIT")
                    return loads_strict(prior["result"])
                row = connection.execute("SELECT * FROM credentials WHERE identity=?", (identity,)).fetchone()
                require((row["generation"] if row else 0) == expected, "stale_credential_generation",
                        "Credential changed; the input was not saved", 409)
                ciphertext = self.protector.protect(secret.encode("utf-8")) if operation == "credential.save" else None
                connection.execute("INSERT INTO credentials VALUES(?,?,?) "
                                   "ON CONFLICT(identity) DO UPDATE SET generation=excluded.generation,secret=excluded.secret",
                                   (identity, expected + 1, ciphertext))
                result = self._status(reference, {"generation": expected + 1, "secret": ciphertext})
                connection.execute("INSERT INTO receipts VALUES(?,?,?,?)",
                                   (request["idempotency_key"], digest,
                                    canonical_bytes(public_request).decode(), canonical_bytes(result).decode()))
                connection.execute("COMMIT")
                return result
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def receipt(self, operation, request):
        credential_id(request["credential_ref"])
        require(operation in ("credential.save", "credential.delete") and uuid_value(request["idempotency_key"])
                and type(request["expected_generation"]) is int
                and 0 <= request["expected_generation"] < 2**53 - 1,
                "invalid_request", "Credential receipt identity is invalid")
        if not self.path.exists():
            return None
        with closing(self._connection()) as connection:
            row = connection.execute("SELECT request,result FROM receipts WHERE key=?", (request["idempotency_key"],)).fetchone()
            if row is None:
                return None
            require(canonical_bytes(loads_strict(row["request"]))
                    == canonical_bytes({"operation": operation, **request}),
                    "idempotency_conflict", "Credential receipt identity differs", 409)
            return loads_strict(row["result"])


class LocalCredentialBroker:
    def __init__(self, store):
        self.store = store
        self._evidence_key = os.urandom(32)

    def _evidence(self, secret, generation):
        return hmac.new(self._evidence_key, canonical_bytes([secret, generation]), hashlib.sha256).digest()

    def freeze(self, reference):
        credential_id(reference)
        secret, generation = self.store.read(reference)
        return CredentialLease(reference, self._evidence(secret, generation))

    def authorize(self, lease):
        credential_id(lease.reference)
        secret, generation = self.store.read(lease.reference)
        require(hmac.compare_digest(lease.evidence, self._evidence(secret, generation)),
                "model_credential_changed", "Credential changed after run preparation")
        return secret
