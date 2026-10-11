"""Provider validation and credentials do not initialize graph/model hosts."""

import ast
from pathlib import Path
import subprocess
import sys

import pytest

from phase1_agent.contract_errors import ContractValidationError, uuid_value
from phase1_agent.credential_contract import credential_id
from phase1_agent.credential_store import LocalCredentialBroker, PrivateCredentialStore
from credential_test_support import TestProtector, mutate_test_credential


SOURCE = Path(__file__).resolve().parents[1] / "src" / "phase1_agent"


@pytest.mark.parametrize("module", ["credential_contract", "credential_store",
                                    "gemini_capabilities", "gemini_adapter", "model_contract",
                                    "provider_discovery"])
def test_lower_provider_modules_have_no_direct_graph_host_dependency(module):
    tree = ast.parse((SOURCE / (module + ".py")).read_text(encoding="utf-8"))
    dependencies = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level}
    assert {"graph_contracts", "graph_records", "model_service"}.isdisjoint(dependencies)


@pytest.mark.parametrize("module", ["credential_store", "gemini_adapter", "model_contract",
                                    "provider_discovery"])
def test_loading_provider_boundary_does_not_load_graph_or_model_hosts(module):
    result = subprocess.run([sys.executable, "-c", (
        f"import phase1_agent.{module}; import sys; "
        "assert 'phase1_agent.graph_contracts' not in sys.modules; "
        "assert 'phase1_agent.graph_records' not in sys.modules; "
        "assert 'phase1_agent.model_service' not in sys.modules"
    )], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("identity", [None, "", "00000000-0000-1000-8000-000000000001",
                                     "00000000-0000-4000-8000-00000000000A"])
def test_credential_contract_rejects_noncanonical_uuid4(identity):
    assert not uuid_value(identity)
    with pytest.raises(ContractValidationError) as rejected:
        credential_id("local:" + str(identity))
    assert rejected.value.reason_code == "credential_reference_invalid"


def test_private_lease_detects_rotation_without_revealing_secret(tmp_path):
    store = PrivateCredentialStore(tmp_path / "credentials.sqlite", TestProtector())
    reference = "local:00000000-0000-4000-8000-000000000099"
    mutate_test_credential(store, reference, "old-private-secret")
    broker = LocalCredentialBroker(store)
    lease = broker.freeze(reference)
    assert "old-private-secret" not in repr(lease)
    mutate_test_credential(store, reference, "new-private-secret")
    with pytest.raises(ContractValidationError) as rejected:
        broker.authorize(lease)
    assert rejected.value.reason_code == "model_credential_changed"
    assert "private-secret" not in str(rejected.value)
