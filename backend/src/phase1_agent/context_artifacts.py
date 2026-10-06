"""Small context-package helpers independent of an Agent executor or kernel."""

from hashlib import sha256
from uuid import UUID

from .context_package import _exact_input
from .contract_json import canonical_bytes
from .graph_contracts import require


def projection_id(basis, part):
    return str(UUID(bytes=sha256((basis + ":" + part).encode("utf-8")).digest()[:16], version=4))


def resolve_input(context, port, expected):
    record = context.host_call("artifacts:read", "resolve-input", {"port": port})
    require(type(record) is dict and set(record) == {"value", "producer", "output_id"}
            and record["output_id"] == _exact_input(context, port)["output_id"]
            and canonical_bytes(record["value"]) == canonical_bytes(expected),
            "context_input_mismatch", "Context input differs from its exact accepted immutable artifact")
    return record
