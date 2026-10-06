import httpx
import pytest

from scripts.online_probe import CappedTransport


def test_probe_transport_permits_only_configured_https_and_caps_attempts():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="ok")

    transport = CappedTransport(max_attempts=1, delegate=httpx.MockTransport(handler))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RuntimeError, match="DeepSeek HTTPS"):
            client.get("https://example.com/")
        with pytest.raises(RuntimeError, match="DeepSeek HTTPS"):
            client.get("http://api.deepseek.com/chat/completions")
        assert transport.attempts == 0
        assert client.get("https://api.deepseek.com/chat/completions").status_code == 200
        with pytest.raises(RuntimeError, match="budget exhausted"):
            client.get("https://api.deepseek.com/chat/completions")
    assert len(requests) == 1
    assert transport.attempts == 1
