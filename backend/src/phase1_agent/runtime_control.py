"""Public suspension signal; packages keep their own continuation payloads."""


class NodeExecutionSuspended(RuntimeError):
    def __init__(self, status="paused", reason_code="runtime_safe_point", message="Execution reached a retained safe point"):
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code
