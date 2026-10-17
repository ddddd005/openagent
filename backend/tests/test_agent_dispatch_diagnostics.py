"""Sanitized pre-dispatch causes survive Agent execution without retries."""

import pytest

from phase1_agent.contract_errors import ModelRequestError, PublicDiagnostic
from phase1_agent.runtime import RunFailed
from test_runtime import ScriptedAdapter, execute


def test_kernel_keeps_explicit_public_diagnostic_and_does_not_retry():
    safe = PublicDiagnostic({"code": "model_not_dispatched", "cause_code": "model_credential_changed",
                             "step": "dispatch_authorization", "recovery": "save_provider_key"})
    adapter = ScriptedAdapter(ModelRequestError("model_not_dispatched", public_diagnostic=safe))
    with pytest.raises(RunFailed) as caught:
        execute(adapter)
    assert caught.value.code == "model_not_dispatched"
    assert caught.value.public_diagnostic.to_dict() == safe.to_dict()
    assert len(adapter.inputs) == 1
