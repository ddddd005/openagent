"""generateContent parameter validation, independent of upstream model names."""

from .contract_errors import require
from .frozen_model import FrozenModelParameters


GEMINI_PROTOCOL = "gemini-generate-content@1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Retain known signature requirements without making this a model admission list.
_REQUIRED_CALL_SIGNATURE_MODELS = frozenset({"gemini-3-flash-preview", "gemini-3.1-pro-preview"})


def requires_call_signature(model):
    return model in _REQUIRED_CALL_SIGNATURE_MODELS


def validate_provider_parameters(protocol, parameters):
    if protocol == "chat":
        require(parameters.get("thinking", "disabled") == "disabled",
                "model_parameters_unsupported", "DeepSeek Chat requires disabled thinking")
        return
    require(protocol == "gemini", "model_protocol_unsupported", "Unknown model protocol")
    FrozenModelParameters.from_mapping(parameters)


def thinking_wire(parameters):
    validate_provider_parameters("gemini", parameters)
    thinking = parameters.get("thinking", "disabled")
    if thinking == "disabled":
        return {"thinkingBudget": 0, "includeThoughts": False}
    return {
        "thinkingBudget" if thinking["mode"] == "budget" else "thinkingLevel":
            thinking["budget"] if thinking["mode"] == "budget" else thinking["level"],
        "includeThoughts": thinking["include_summary"],
    }


def binding_capabilities(protocol, parameters):
    validate_provider_parameters(protocol, parameters)
    return {"protocol": protocol, "tools": True, "stream": False,
            "thinking": ("disabled" if parameters["thinking"] == "disabled"
                         else parameters["thinking"]["mode"])}
