"""Pure request budgets measured against the exact prepared wire projection."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from .contract_errors import require
from .contract_json import canonical_bytes, content_digest
from .model_request_contracts import (
    MAX_SAFE_INTEGER, _validate, measurement_schema, projection_schema,
    validate_model_capacity, validate_model_runtime, validate_model_settings,
)


def calculate_budget(capacity: object, runtime: object, *, transport_output_limit: int,
                     purpose: str = "normal") -> dict:
    capacity, runtime = validate_model_capacity(capacity), validate_model_runtime(runtime)
    require(purpose in ("normal", "compaction"), "model_request_invalid", "Request purpose is unsupported")
    require(type(transport_output_limit) is int and 0 < transport_output_limit <= MAX_SAFE_INTEGER,
            "model_transport_capability_unavailable", "Transport requires an explicit positive output limit")
    window, maximum = capacity["context_window_tokens"], capacity["max_output_tokens"]
    context = window if runtime["context_length_tokens"] is None else runtime["context_length_tokens"]
    output, threshold = runtime["output_limit_tokens"], runtime["compaction_threshold_bps"]
    require(context <= window and output <= min(maximum, transport_output_limit) and output < window,
            "model_capacity_invalid", "Runtime context or output exceeds the declared model/transport capacity")
    summary = min(output, 1024)
    return {
        "context_length_tokens": context, "compaction_threshold_bps": threshold,
        "trigger_total_tokens": (context * threshold + 9999) // 10_000,
        "output_reserve_tokens": output if purpose == "normal" else summary,
        "normal_output_limit_tokens": output, "summary_output_limit_tokens": summary,
        "calculation_version": 1,
    }


def validate_measurement(value: object, settings: object | None = None, *,
                         output_reserve_tokens: int | None = None) -> dict:
    result = _validate(value, measurement_schema(), "model_request_measurement_invalid")
    require(result["total_tokens"] == result["input_tokens"] + result["output_reserve_tokens"],
            "model_request_measurement_invalid", "Measurement total differs from input and output reserve")
    require(all(text.strip() == text and not any(ord(char) < 32 or ord(char) == 127 for char in text)
                for text in (result["estimator_id"], result["estimator_version"])),
            "model_request_measurement_invalid", "Estimator identity and version must be exact")
    if result["token_count_kind"] == "utf8_bytes_estimate":
        require(result["estimator_id"] == "canonical-json-utf8-bytes" and result["estimator_version"] == "1",
                "model_request_measurement_invalid", "Byte estimates require the registered estimator version")
    if settings is not None:
        basis = validate_model_settings(settings)
        reserve = (basis["budget"]["output_reserve_tokens"] if output_reserve_tokens is None
                   else output_reserve_tokens)
        require(result["context_window_tokens"] == basis["capacity"]["context_window_tokens"]
                and result["input_limit_tokens"] == basis["capacity"]["input_limit_tokens"]
                and result["output_reserve_tokens"] == reserve,
                "model_request_measurement_invalid", "Measurement does not use the same request capacity")
    return result


def capacity_evidence(measurement: object) -> dict:
    measurement = validate_measurement(measurement)
    cold_exceeded = measurement["input_tokens"] > measurement["input_limit_tokens"]
    window_exceeded = measurement["total_tokens"] > measurement["context_window_tokens"]
    valid = not (cold_exceeded or window_exceeded)
    return {"hard_capacity_ok": valid, "capacity_diagnostic": None if valid else {
        "code": "model_request_capacity_exceeded", "input_limit_exceeded": cold_exceeded,
        "context_window_exceeded": window_exceeded,
    }}


def _measure(prepared: object, settings: dict, *, reserve: int,
             counter: Callable[[bytes], int] | None, estimator_id: str | None,
             estimator_version: str | None) -> dict:
    require(type(prepared) is dict and {"projection", "wire_request"} <= set(prepared)
            and type(prepared["wire_request"]) is dict,
            "model_request_projection_invalid", "Prepared request requires an exact projection and wire object")
    wire = canonical_bytes(prepared["wire_request"])
    projection = _validate({
        "projection_version": prepared["projection"],
        "wire_digest": content_digest(prepared["wire_request"]),
    }, projection_schema(), "model_request_projection_invalid")
    if counter is None:
        require(estimator_id is None and estimator_version is None,
                "model_request_measurement_invalid", "Default byte estimator cannot be renamed")
        count, kind = len(wire), "utf8_bytes_estimate"
        estimator_id, estimator_version = "canonical-json-utf8-bytes", "1"
    else:
        require(callable(counter) and type(estimator_id) is str and type(estimator_version) is str,
                "model_request_measurement_invalid", "Provided counter requires an exact estimator identity/version")
        # Validate the identity before invoking even an injected counter.
        validate_measurement({
            "input_tokens": 0, "output_reserve_tokens": reserve, "total_tokens": reserve,
            **{key: settings["capacity"][key] for key in ("context_window_tokens", "input_limit_tokens")},
            "token_count_kind": "provided", "estimator_id": estimator_id,
            "estimator_version": estimator_version,
        })
        count, kind = counter(wire), "provided"
    require(type(count) is int and 0 <= count <= MAX_SAFE_INTEGER,
            "model_request_measurement_invalid", "Input estimate must be an explicit nonnegative integer")
    measurement = validate_measurement({
        "input_tokens": count, "output_reserve_tokens": reserve, "total_tokens": count + reserve,
        **{key: settings["capacity"][key] for key in ("context_window_tokens", "input_limit_tokens")},
        "token_count_kind": kind, "estimator_id": estimator_id, "estimator_version": estimator_version,
    }, settings, output_reserve_tokens=reserve)
    return {"projection": projection, "measurement": measurement, **capacity_evidence(measurement)}


def measure_prepared_request(prepared: object, settings: object, *,
                             counter: Callable[[bytes], int] | None = None,
                             estimator_id: str | None = None, estimator_version: str | None = None) -> dict:
    settings = validate_model_settings(settings)
    result = _measure(prepared, settings, reserve=settings["budget"]["output_reserve_tokens"],
                      counter=counter, estimator_id=estimator_id, estimator_version=estimator_version)
    result["requires_compaction"] = (
        settings["purpose"] == "normal"
        and result["measurement"]["total_tokens"] >= settings["budget"]["trigger_total_tokens"])
    require(settings["purpose"] != "compaction" or result["hard_capacity_ok"],
            "model_request_capacity_exceeded", "Compaction request exceeds the actual model capacity",
            capacity_diagnostic=result["capacity_diagnostic"])
    return result


def measure_context(prepared: object, settings: object, *, messages: object, tools: object,
                    counter: Callable[[bytes], int] | None = None,
                    estimator_id: str | None = None, estimator_version: str | None = None) -> dict:
    """Math only. M2 must bind this call to the owned compaction preparation/projector."""
    settings = validate_model_settings(settings)
    require(settings["purpose"] == "compaction", "model_request_measurement_invalid",
            "Context measurement requires existing compaction settings")
    require(type(messages) is list and bool(messages) and type(tools) is list,
            "model_request_invalid", "Context messages and tools must be arrays")
    require(all(type(item) is dict for item in messages + tools),
            "model_request_invalid", "Context messages and tools must contain JSON objects")
    result = _measure(prepared, settings, reserve=settings["budget"]["normal_output_limit_tokens"],
                      counter=counter, estimator_id=estimator_id, estimator_version=estimator_version)
    return {"settings_id": settings["settings_id"], "messages_digest": content_digest(messages),
            "tools_digest": content_digest(tools), **result}


def validate_context_reduction(before: object, after: object, settings: object) -> dict:
    """Check same-settings compression evidence without imposing a second target."""
    from .model_request_contracts import validate_operation_payload
    before = validate_operation_payload("measure-context", before, response=True)
    after = validate_operation_payload("measure-context", after, response=True)
    settings = validate_model_settings(settings)
    require(settings["purpose"] == "compaction"
            and before["settings_id"] == after["settings_id"] == settings["settings_id"]
            and before["projection"]["projection_version"] == after["projection"]["projection_version"]
            and all(before["measurement"][key] == after["measurement"][key]
                    for key in ("token_count_kind", "estimator_id", "estimator_version"))
            and before["tools_digest"] == after["tools_digest"],
            "model_context_reduction_invalid", "Context reduction requires identical settings/projector/estimator/tools")
    for evidence in (before, after):
        validate_measurement(evidence["measurement"], settings,
                             output_reserve_tokens=settings["budget"]["normal_output_limit_tokens"])
    require(after["measurement"]["input_tokens"] < before["measurement"]["input_tokens"]
            and after["hard_capacity_ok"],
            "model_context_reduction_invalid", "Context must shrink and satisfy cold input/window capacity")
    return deepcopy(after)
