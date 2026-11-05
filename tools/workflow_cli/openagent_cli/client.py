import math
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import jsonio


class ApiError(Exception):
    def __init__(self, status, payload):
        super().__init__("HTTP request rejected")
        self.status = status
        self.payload = payload


class TransportError(Exception):
    def __init__(self, message, *, write_unknown=False):
        super().__init__(message)
        self.write_unknown = write_unknown


class GraphClient:
    def __init__(self, base_url, timeout=30):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("base URL must be an HTTP origin without credentials or path")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.origin = parsed.scheme + "://" + parsed.netloc
        self.timeout = timeout

    def _request(self, path, body=None, *, write=False):
        data = None if body is None else jsonio.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": self.origin}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as error:
            try:
                raw = error.read()
            except (OSError, HTTPException):
                raise TransportError("HTTP error response truncated", write_unknown=write) from None
            try:
                payload = jsonio.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                payload = {"error": {"code": "invalid_response", "message": "Non-JSON HTTP error"}}
            raise ApiError(error.code, payload) from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise TransportError("HTTP transport failed", write_unknown=write) from None
        try:
            return jsonio.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise TransportError("Invalid JSON response", write_unknown=write) from None

    def describe(self):
        return self._request("/api/graph/application")

    def query(self, operation, parameters=None):
        return self._request("/api/graph/queries", {
            "operation": operation, "parameters": {} if parameters is None else parameters})

    def command(self, request):
        if not isinstance(request, dict) or set(request) != {"operation", "parameters"} or not isinstance(request["parameters"], dict):
            raise ValueError("request must contain only operation and parameters")
        return self._request("/api/graph/commands", request, write=True)
