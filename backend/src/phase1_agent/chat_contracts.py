"""Independent logical chat values, without execution or artifact ownership."""

from __future__ import annotations

from copy import deepcopy

from .contract_errors import require, uuid_value
from .contract_json import canonical_bytes, validate_json_value
from .host_sdk import DataTypeDefinition


CHAT_CONTEXT_TYPE = "workflow.chat-context"
CHAT_CONTEXT_SCHEMA_VERSION = 1
CHAT_CONTEXT_MAX_BYTES = 4_000_000
EMPTY_CHAT_CONTEXT = {"rounds": []}


def empty_chat_context() -> dict:
    return deepcopy(EMPTY_CHAT_CONTEXT)


def _fields(value, fields):
    require(type(value) is dict and set(value) == set(fields),
            "chat_invalid_state", "Logical chat fields are invalid")


def validate_chat_context(value) -> dict:
    """Validate source-free messages, independent candidates and exact selection."""
    validate_json_value(value)
    require(len(canonical_bytes(value)) <= CHAT_CONTEXT_MAX_BYTES,
            "host_value_too_large", "Data value exceeds the registered limit")
    _fields(value, ("rounds",))
    require(type(value["rounds"]) is list, "chat_invalid_state", "Chat rounds must be a list")
    round_ids, candidate_ids, message_ids = set(), set(), set()
    for round_value in value["rounds"]:
        _fields(round_value, (
            "round_id", "base_checkpoint_id", "submitted_input", "candidates", "selected_candidate_id",
        ))
        identity = round_value["round_id"]
        require(uuid_value(identity) and identity not in round_ids,
                "chat_invalid_state", "Chat rounds require unique exact identities")
        round_ids.add(identity)
        require(round_value["base_checkpoint_id"] is None
                or uuid_value(round_value["base_checkpoint_id"]),
                "chat_invalid_state", "A chat round requires an exact checkpoint basis")
        require(round_value["submitted_input"] is None
                or type(round_value["submitted_input"]) is str,
                "chat_invalid_state", "Submitted chat input must be text or absent")
        candidates = round_value["candidates"]
        require(type(candidates) is list, "chat_invalid_state", "Chat candidates must be a list")
        own_candidates = set()
        for candidate in candidates:
            _fields(candidate, ("candidate_id", "messages", "result_checkpoint_id"))
            candidate_id = candidate["candidate_id"]
            require(uuid_value(candidate_id) and candidate_id not in candidate_ids,
                    "chat_invalid_state", "Chat candidates require unique exact identities")
            candidate_ids.add(candidate_id)
            own_candidates.add(candidate_id)
            require(candidate["result_checkpoint_id"] is None
                    or uuid_value(candidate["result_checkpoint_id"]),
                    "chat_invalid_state", "Candidate results require an exact success checkpoint")
            messages = candidate["messages"]
            require(type(messages) is list and bool(messages),
                    "chat_invalid_state", "A chat candidate requires its actual messages")
            for message in messages:
                _fields(message, ("message_id", "role", "text"))
                message_id = message["message_id"]
                require(uuid_value(message_id) and message_id not in message_ids,
                        "chat_invalid_state", "Chat messages require unique exact identities")
                message_ids.add(message_id)
                require(message["role"] in ("user", "assistant", "system")
                        and type(message["text"]) is str,
                        "chat_invalid_state", "Chat messages require an explicit role and text")
        selected = round_value["selected_candidate_id"]
        require(selected is None or type(selected) is str and selected in own_candidates,
                "chat_invalid_state", "The selected candidate must belong to its exact round")
    return deepcopy(value)


def validate_chat_context_write(value, context):
    """The unadapted type permits only empty ordinary initialization."""
    require(context["operation"] == "initialize" and value == EMPTY_CHAT_CONTEXT,
            "chat_write_evidence_required",
            "Chat updates require an accepted operation or exact node write evidence")


def chat_context_data_type(*, write_validator=None) -> DataTypeDefinition:
    return DataTypeDefinition(
        CHAT_CONTEXT_TYPE, CHAT_CONTEXT_SCHEMA_VERSION,
        {"type": "object", "properties": {"rounds": {"type": "array"}},
         "required": ["rounds"], "additionalProperties": False},
        default_value=empty_chat_context(), validator=validate_chat_context,
        write_validator=(validate_chat_context_write if write_validator is None else write_validator),
        max_bytes=CHAT_CONTEXT_MAX_BYTES, checkpoint_admission=True,
    )


def append_chat_candidate(
        value, *, round_id, base_checkpoint_id, submitted_input,
        candidate_id, messages, select=True) -> dict:
    """Build one pending candidate without fetching data or assigning execution facts."""
    require(type(select) is bool, "chat_invalid_state", "Candidate selection must be explicit")
    result = validate_chat_context(value)
    target = next((item for item in result["rounds"] if item["round_id"] == round_id), None)
    if target is None:
        target = {
            "round_id": round_id, "base_checkpoint_id": base_checkpoint_id,
            "submitted_input": submitted_input, "candidates": [], "selected_candidate_id": None,
        }
        result["rounds"].append(target)
    else:
        require(target is result["rounds"][-1], "chat_round_locked",
                "A chat round with later rounds cannot accept another candidate", 409)
        require(target["base_checkpoint_id"] == base_checkpoint_id
                and target["submitted_input"] == submitted_input,
                "chat_round_basis_mismatch", "A round retains its original checkpoint and input")
    target["candidates"].append({
        "candidate_id": candidate_id, "messages": deepcopy(messages), "result_checkpoint_id": None,
    })
    if select:
        target["selected_candidate_id"] = candidate_id
    return validate_chat_context(result)


def bind_chat_candidate_checkpoint(value, *, round_id, candidate_id, checkpoint_id) -> dict:
    """Attach an exact success point; the caller owns proof and atomic settlement."""
    require(uuid_value(checkpoint_id), "chat_invalid_state", "A success checkpoint is required")
    result = validate_chat_context(value)
    target = next((item for item in result["rounds"] if item["round_id"] == round_id), None)
    candidate = None if target is None else next((
        item for item in target["candidates"] if item["candidate_id"] == candidate_id), None)
    require(candidate is not None, "chat_candidate_not_found",
            "The candidate must belong to the exact chat round", 409)
    require(candidate["result_checkpoint_id"] in (None, checkpoint_id),
            "chat_candidate_checkpoint_conflict", "A candidate keeps its accepted success point", 409)
    candidate["result_checkpoint_id"] = checkpoint_id
    return validate_chat_context(result)


def remap_chat_context_checkpoints(value, checkpoint_id_map) -> dict:
    """Rewrite only checkpoint associations when constructing independent target copies."""
    require(type(checkpoint_id_map) is dict and all(
        uuid_value(source) and uuid_value(target)
        for source, target in checkpoint_id_map.items()),
        "chat_checkpoint_mapping_invalid", "Chat checkpoint mapping requires exact identities")
    result = validate_chat_context(value)
    for round_value in result["rounds"]:
        references = [(round_value, "base_checkpoint_id")]
        references.extend((candidate, "result_checkpoint_id") for candidate in round_value["candidates"])
        for record, field in references:
            identity = record[field]
            if identity is None:
                continue
            require(identity in checkpoint_id_map, "chat_checkpoint_mapping_missing",
                    "A retained candidate checkpoint is outside the copied history", 409,
                    checkpoint_id=identity)
            record[field] = checkpoint_id_map[identity]
    return validate_chat_context(result)
