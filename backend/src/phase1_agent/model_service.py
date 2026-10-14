"""Built-in protocol transport construction; request ownership lives in M2."""

from .adapter import DeepSeekAdapter
from .frozen_model import FrozenConfiguredAdapter, FrozenModelParameters
from .gemini_adapter import GeminiAdapter
from .gemini_capabilities import validate_provider_parameters


def create_chat_transport(*, provider: dict, parameters: dict, api_key: str, http_client=None):
    """Create the exact supported non-streaming transport with SDK retries disabled."""
    frozen = FrozenModelParameters.from_mapping(parameters)
    declared = dict(frozen.as_mapping())
    protocol = provider["protocol"]
    validate_provider_parameters(protocol, declared)
    if protocol == "gemini":
        adapter = GeminiAdapter(
            api_key=api_key, model=frozen.model, base_url=provider["base_url"],
            max_tokens=frozen.max_tokens, temperature=frozen.temperature,
            thinking=declared["thinking"], http_client=http_client,
        )
    else:
        adapter = DeepSeekAdapter(
            api_key=api_key, model=frozen.model, base_url=provider["base_url"],
            max_tokens=frozen.max_tokens, temperature=frozen.temperature,
            max_retries=0, http_client=http_client,
        )
    return FrozenConfiguredAdapter(adapter, frozen, provider_address=provider["base_url"])
