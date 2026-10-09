"""Bounded, single-attempt provider model discovery, outside model execution."""

from urllib.parse import urlsplit
import httpx

from .contract_json import loads_strict
from .gemini_capabilities import GEMINI_MODELS
from .graph_records import require

MAX_PAGES = 10
MAX_BYTES = 1_000_000
MAX_MODELS = 1000


def _positive(value):
    return value if type(value) is int and 0 < value <= 2**53 - 1 else None


def discover_provider_models(provider, secret, client_factory=None):
    gemini = provider["protocol"] == "gemini"
    endpoint = provider["base_url"].rstrip("/") + "/models"
    origin = urlsplit(endpoint)
    factory = client_factory or httpx.Client
    rows, seen_pages, token = [], set(), None
    total_bytes = 0
    try:
        with factory(timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False) as client:
            for _ in range(MAX_PAGES):
                headers = {"x-goog-api-key": secret} if gemini else {"Authorization": "Bearer " + secret}
                params = {"pageSize": 100, **({"pageToken": token} if token else {})} if gemini else {}
                with client.stream("GET", endpoint, headers=headers, params=params) as response:
                    require(response.url.scheme == origin.scheme and response.url.host == origin.hostname
                            and response.url.port == origin.port,
                            "provider_discovery_redirect_denied", "Model discovery changed its origin", 502)
                    require(200 <= response.status_code < 300, "provider_discovery_failed",
                            "Provider model discovery failed", 502)
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        total_bytes += len(chunk)
                        require(total_bytes <= MAX_BYTES, "provider_discovery_capacity",
                                "Model discovery response exceeded its limit", 502)
                        content.extend(chunk)
                payload = loads_strict(bytes(content).decode("utf-8"))
                require(type(payload) is dict and type(payload.get("models" if gemini else "data")) is list,
                        "provider_discovery_invalid", "Provider model list is invalid", 502)
                for item in payload["models" if gemini else "data"]:
                    require(type(item) is dict, "provider_discovery_invalid", "Provider model entry is invalid", 502)
                    name = item.get("name" if gemini else "id")
                    require(type(name) is str and 1 <= len(name) <= 256
                            and not any(ord(char) < 32 or ord(char) == 127 for char in name),
                            "provider_discovery_invalid", "Provider model identity is invalid", 502)
                    if gemini:
                        methods = item.get("supportedGenerationMethods", [])
                        require(type(methods) is list, "provider_discovery_invalid", "Generation methods are invalid", 502)
                        if "generateContent" not in methods:
                            continue
                        require(name.startswith("models/"), "provider_discovery_invalid", "Gemini model name is invalid", 502)
                        name = name.removeprefix("models/")
                    rows.append({"model": name, "adapted": not gemini or name in GEMINI_MODELS,
                                 "input_token_limit": _positive(item.get("inputTokenLimit")) if gemini else None,
                                 "output_token_limit": _positive(item.get("outputTokenLimit" if gemini else "max_output_tokens")),
                                 "context_window_tokens": None if gemini else _positive(item.get("context_window"))})
                    require(len(rows) <= MAX_MODELS, "provider_discovery_capacity", "Too many discovered models", 502)
                token = payload.get("nextPageToken") if gemini else None
                if not token:
                    break
                require(type(token) is str and len(token) <= 2048 and token not in seen_pages,
                        "provider_discovery_invalid", "Model discovery pagination is invalid", 502)
                seen_pages.add(token)
            else:
                require(False, "provider_discovery_capacity", "Model discovery page limit exceeded", 502)
    except Exception as failure:
        if getattr(failure, "reason_code", None):
            raise
        require(False, "provider_discovery_unavailable", "Model discovery could not complete", 502)
    require(len({row["model"] for row in rows}) == len(rows),
            "provider_discovery_invalid", "Provider returned repeated model identities", 502)
    return rows
