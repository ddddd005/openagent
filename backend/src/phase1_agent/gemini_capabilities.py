"""Explicit generateContent capabilities; unknown model IDs never infer support."""

from copy import deepcopy

from .graph_contracts import require


GEMINI_PROTOCOL = "gemini-generate-content@1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Google generate-content/thinking and deprecations, checked 2026-10-09.
GEMINI_MODELS = {
    "gemini-2.5-pro": {"mode": "budget", "min": 128, "max": 32768, "disable": False,
                       "required_call_signature": False},
    "gemini-2.5-flash": {"mode": "budget", "min": 0, "max": 24576, "disable": True,
                         "required_call_signature": False},
    "gemini-2.5-flash-lite": {"mode": "budget", "min": 512, "max": 24576, "disable": True,
                              "required_call_signature": False},
    "gemini-3-flash-preview": {"mode": "level", "levels": ("minimal", "low", "medium", "high"),
                              "disable": False, "required_call_signature": True},
    "gemini-3.1-pro-preview": {"mode": "level", "levels": ("low", "medium", "high"),
                              "disable": False, "required_call_signature": True},
}


def model_capability(model):
    require(model in GEMINI_MODELS, "model_capability_unknown",
            "Gemini model ID has no installed generateContent capability record")
    return deepcopy(GEMINI_MODELS[model])


def validate_provider_parameters(protocol, parameters):
    if protocol == "chat":
        require(parameters.get("thinking", "disabled") == "disabled",
                "model_parameters_unsupported", "DeepSeek Chat requires disabled thinking")
        return
    require(protocol == "gemini", "model_protocol_unsupported", "Unknown model protocol")
    capability = model_capability(parameters["model"])
    thinking = parameters.get("thinking", "disabled")
    if thinking == "disabled":
        require(capability["disable"], "model_thinking_unsupported",
                "This Gemini model cannot disable thinking")
        return
    require(type(thinking) is dict and thinking.get("mode") == capability["mode"],
            "model_thinking_unsupported", "Thinking mode does not match the Gemini model")
    if capability["mode"] == "budget":
        budget = thinking["budget"]
        require(budget == -1 or (budget == 0 and capability["disable"])
                or capability["min"] <= budget <= capability["max"],
                "model_thinking_unsupported", "Thinking budget is outside the Gemini model range")
    else:
        require(thinking["level"] in capability["levels"], "model_thinking_unsupported",
                "Thinking level is not supported by the Gemini model")


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
