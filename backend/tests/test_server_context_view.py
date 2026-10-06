"""Same-origin context reads and previews are bounded, typed, and non-executing."""

from contextlib import closing
from copy import deepcopy

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.workflow import A_BINDING, B_BINDING, WorkflowService
from test_server import FakeService
from test_server_prompt_configs import request, running_server
from test_workflow_context_view import database, draft, fence, persistent_data
from test_workflow_prepared_context import factory, run_turn, uid


class ContextService(FakeService):
    def read_node_context(self, sid, binding, **payload):
        self.calls.append(("read_context", sid, binding, deepcopy(payload)))
        return {"schema_version": 1, "kind": "workflow_context_read"}

    def preview_node_context(self, sid, binding, **payload):
        self.calls.append(("preview_context", sid, binding, deepcopy(payload)))
        return {"schema_version": 1, "kind": "workflow_context_preview"}


def revisions():
    return {
        "expected_session_revision": 1, "expected_ref_revision": 1,
        "expected_head_commit_id": uid(9999),
    }


def path(operation="read", *, sid=uid(9998), binding=A_BINDING):
    return f"/api/sessions/{sid}/nodes/{binding}/context/{operation}"


def test_http_strict_context_and_inline_preview_dispatch_only_read_methods():
    service = ContextService()
    with running_server(service) as port:
        assert request(port, "POST", path(), revisions())[0] == 200
        payload = {**revisions(), "prompt_config": draft(), "text": "next"}
        assert request(port, "POST", path("preview"), payload)[0] == 200
    assert service.calls == [
        ("read_context", uid(9998), A_BINDING, revisions()),
        ("preview_context", uid(9998), A_BINDING, payload),
    ]


@pytest.mark.parametrize("change", [
    lambda value: value.update(expected_session_revision=True),
    lambda value: value.update(expected_ref_revision=0),
    lambda value: value.update(expected_head_commit_id="PRIVATE-ID"),
    lambda value: value.update(parent_turn_id="PRIVATE-PARENT"),
    lambda value: value.update(idempotency_key="PRIVATE"),
    lambda value: value.pop("expected_head_commit_id"),
])
def test_http_invalid_revision_fences_are_redacted_400_without_service_call(change):
    service = ContextService()
    payload = revisions()
    change(payload)
    with running_server(service) as port:
        status, body = request(port, "POST", path(), payload)
    assert status == 400 and body["error"]["reason_code"] == "invalid_request"
    assert "PRIVATE" not in str(body) and not service.calls


@pytest.mark.parametrize("change", [
    lambda value: value.update(text={"PRIVATE": True}),
    lambda value: value["prompt_config"].update(steps=[{"PRIVATE": True}]),
    lambda value: value["prompt_config"]["items"][0].update(role="tool"),
    lambda value: value["prompt_config"]["groups"][0]["members"][0].update(revision=999),
    lambda value: value["prompt_config"]["items"].append(deepcopy(value["prompt_config"]["items"][0])),
])
def test_http_preview_rejects_unbounded_private_or_broken_exact_drafts(change):
    service = ContextService()
    payload = {**revisions(), "prompt_config": draft(), "text": "PRIVATE-TEXT"}
    change(payload)
    with running_server(service) as port:
        status, body = request(port, "POST", path("preview"), payload)
    assert status == 400 and body["error"]["reason_code"] == "invalid_request"
    assert "PRIVATE" not in str(body) and not service.calls


def test_http_context_routes_require_post_origin_and_exact_path():
    service = ContextService()
    with running_server(service) as port:
        assert request(port, "GET", path())[0] == 404
        assert request(port, "POST", path(), revisions(), origin=False)[0] == 403
        assert request(port, "POST", path() + "?head=latest", revisions())[0] == 404
        assert request(port, "POST", path(), raw=b'{"expected_session_revision":1,"expected_session_revision":2}')[0] == 400
    assert not service.calls


@pytest.mark.parametrize("status,reason", [
    (409, "stale_revision"), (404, "not_found"),
    (500, "storage_contract_violation"), (400, "context_limit"),
    (400, "unsupported"), (409, "ownership_mismatch"),
])
def test_http_context_preserves_safe_typed_failures_and_redacts_internal_details(status, reason):
    class Rejected(ContextService):
        def read_node_context(self, *_args, **_kwargs):
            error = ContractValidationError("PRIVATE-STORAGE-BODY")
            error.status_code, error.reason_code = status, reason
            raise error
    with running_server(Rejected()) as port:
        actual, body = request(port, "POST", path(), revisions())
    assert actual == status and body["error"]["reason_code"] == reason
    assert "PRIVATE" not in str(body)


def test_real_http_archived_context_and_preview_do_not_write_or_dispatch(database):
    calls = []
    with closing(WorkflowService(database, model_factory=factory(calls))) as service:
        sid = service.create_session()["workflow_session_id"]
        run_turn(service, sid, "archived input", "first")
        before, call_count = persistent_data(database), len(calls)
        with running_server(service) as port:
            status, archived = request(port, "POST", path(sid=sid), fence(service, sid))
            assert status == 200 and len(archived["turns"]) == 1
            status, prepared = request(port, "POST", path("preview", sid=sid), {
                **fence(service, sid), "prompt_config": draft(), "text": "next input",
            })
            assert status == 200 and prepared["status"] == "ready"
            status, pending = request(port, "POST", path("preview", sid=sid, binding=B_BINDING), {
                **fence(service, sid), "prompt_config": draft(), "text": None,
            })
            assert status == 200 and pending["status"] == "pending"
        assert persistent_data(database) == before and len(calls) == call_count
