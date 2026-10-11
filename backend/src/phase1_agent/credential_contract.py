"""Credential references and run leases, independent of storage and models."""

from dataclasses import dataclass, field

from .contract_errors import require, uuid_value


def credential_id(reference):
    require(type(reference) is str and reference.startswith("local:")
            and uuid_value(reference[6:]), "credential_reference_invalid",
            "A local credential reference is required")
    return reference[6:]


@dataclass(frozen=True, repr=False)
class CredentialLease:
    reference: str
    evidence: bytes = field(repr=False)
