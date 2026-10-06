"""Bounded Python-re-v1 replacements isolated from the service process."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import loads_strict
from .prompt_errors import PromptProcessingError


REGEX_SYNTAX = "python-re-v1"
MAX_BATCH_TEXTS = 16_384
MAX_INPUT_CHARS = 8_000_000
MAX_OUTPUT_CHARS = 8_000_000
MAX_MATCHES = 100_000
MAX_RULE_CHARS = 65_536
MAX_TIMEOUT_SECONDS = 10.0
MAX_WIRE_BYTES = 40_000_000

_RULE_FIELDS = {"pattern", "replacement", "flags", "mode"}
_LIMIT_FIELDS = {
    "max_input_chars", "max_output_chars", "max_matches",
    "max_rule_chars", "timeout_seconds",
}
_WORKER_ERROR_CODES = {
    "regex_invalid_pattern", "regex_invalid_replacement", "regex_input_limit",
    "regex_output_limit", "regex_match_limit", "regex_rule_limit",
    "regex_worker_failed",
}


@dataclass(frozen=True)
class RegexRule:
    pattern: str
    replacement: str
    flags: str = ""
    mode: str = "all"


@dataclass(frozen=True)
class RegexLimits:
    """Explicit execution profile, with further hard maxima enforced below."""

    max_input_chars: int = 1_000_000
    max_output_chars: int = 1_000_000
    max_matches: int = 10_000
    max_rule_chars: int = 16_384
    timeout_seconds: float = 1.0


def _fail(
    code: str, message: str, *, node_id: str | None = None,
) -> PromptProcessingError:
    return PromptProcessingError(code, message, node_id=node_id)


def _text(value: Any, label: str) -> str:
    if type(value) is not str:
        raise _fail("regex_invalid_rule", f"{label} must be text")
    if len(value) > MAX_RULE_CHARS:
        raise _fail("regex_rule_limit", "Regex rule exceeds the hard length limit")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise _fail("regex_invalid_rule", f"{label} must be valid UTF-8") from exc
    return value


def regex_rule_from_dict(value: Any) -> RegexRule:
    """Validate structure only; untrusted regular expressions are not compiled."""
    if type(value) is not dict or set(value) != _RULE_FIELDS:
        raise _fail("regex_invalid_rule", "Regex rule fields must be exact")
    pattern = _text(value["pattern"], "Regex pattern")
    replacement = _text(value["replacement"], "Regex replacement")
    flags = _text(value["flags"], "Regex flags")
    mode = _text(value["mode"], "Regex mode")
    if any(flag not in "imsxa" for flag in flags) or len(set(flags)) != len(flags):
        raise _fail(
            "regex_invalid_flags", "Regex flags must be unique members of i/m/s/x/a",
        )
    if mode not in {"first", "all"}:
        raise _fail("regex_invalid_rule", "Regex mode must be first or all")
    if len(pattern) + len(replacement) > MAX_RULE_CHARS:
        raise _fail("regex_rule_limit", "Regex rule exceeds the hard length limit")
    return RegexRule(pattern, replacement, flags, mode)


def regex_rule_to_dict(rule: RegexRule) -> dict[str, str]:
    if type(rule) is not RegexRule:
        raise _fail("regex_invalid_rule", "Regex rule must be a RegexRule")
    value = asdict(rule)
    regex_rule_from_dict(value)
    return value


def validate_regex_limits(limits: RegexLimits) -> None:
    """Validate the profile even when a configured processing step is disabled."""
    if type(limits) is not RegexLimits:
        raise _fail("regex_invalid_limits", "Regex limits must be explicit")
    maxima = {
        "max_input_chars": MAX_INPUT_CHARS,
        "max_output_chars": MAX_OUTPUT_CHARS,
        "max_matches": MAX_MATCHES,
        "max_rule_chars": MAX_RULE_CHARS,
    }
    for name, maximum in maxima.items():
        value = getattr(limits, name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise _fail("regex_invalid_limits", "Regex resource limit is invalid")
    timeout = limits.timeout_seconds
    if (
        type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        or not math.isfinite(timeout)
    ):
        raise _fail("regex_invalid_limits", "Regex execution deadline is invalid")


def _limits_from_dict(value: Any) -> RegexLimits:
    if type(value) is not dict or set(value) != _LIMIT_FIELDS:
        raise _fail("regex_invalid_limits", "Regex limit fields must be exact")
    limits = RegexLimits(**value)
    validate_regex_limits(limits)
    return limits


def _validate_texts(texts: Any, limits: RegexLimits) -> None:
    if type(texts) is not list or len(texts) > MAX_BATCH_TEXTS:
        raise _fail("regex_input_limit", "Regex input must be a bounded text batch")
    total = 0
    for text in texts:
        if type(text) is not str:
            raise _fail("regex_invalid_input", "Regex input elements must be text")
        total += len(text)
        if total > limits.max_input_chars:
            raise _fail("regex_input_limit", "Regex input exceeds the total limit")
        try:
            text.encode("utf-8")
        except UnicodeError as exc:
            raise _fail("regex_invalid_input", "Regex input must be valid UTF-8") from exc


def _run_worker(payload: bytes, *, deadline: float, output_cap: int) -> bytes:
    """Drain bounded pipes concurrently so writing stdin also obeys the deadline."""
    package_root = str(Path(__file__).resolve().parent.parent)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = package_root + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "phase1_agent.prompt_regex_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=package_root, env=environment, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise _fail("regex_worker_failed", "Regex worker could not start") from exc
    output = bytearray()
    errors: list[str] = []
    interrupted = threading.Event()
    threads: list[threading.Thread] = []

    def read_pipe(pipe: Any, cap: int, destination: bytearray | None) -> None:
        total = 0
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                total += len(chunk)
                if total > cap:
                    errors.append("output")
                    interrupted.set()
                    return
                if destination is not None:
                    destination.extend(chunk)
        except (OSError, ValueError):
            errors.append("pipe")
            interrupted.set()

    def write_input() -> None:
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, ValueError):
            errors.append("pipe")
            interrupted.set()
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                errors.append("pipe")
                interrupted.set()

    try:
        for target, arguments in (
            (read_pipe, (process.stdout, output_cap, output)),
            (read_pipe, (process.stderr, 4096, None)),
            (write_input, ()),
        ):
            thread = threading.Thread(
                target=target, args=arguments, daemon=True,
                name="prompt-regex-pipe",
            )
            threads.append(thread)
            thread.start()
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if interrupted.is_set():
                raise _fail("regex_worker_failed", "Regex worker protocol failed")
            if remaining <= 0:
                raise _fail("regex_timeout", "Regex execution exceeded its deadline")
            interrupted.wait(min(0.02, remaining))
        for thread in threads:
            thread.join()
        if errors or process.returncode != 0:
            raise _fail("regex_worker_failed", "Regex worker did not complete cleanly")
        if time.monotonic() >= deadline:
            raise _fail("regex_timeout", "Regex execution exceeded its deadline")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for thread in threads:
            if thread.ident is not None:
                thread.join()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def regex_replace_many(
    texts: list[str], rule: RegexRule, *, limits: RegexLimits,
    node_id: str | None = None,
) -> list[str]:
    """Apply one rule once to each original input under one batch-wide budget."""
    try:
        validate_regex_limits(limits)
        rule_value = regex_rule_to_dict(rule)
        if len(rule.pattern) + len(rule.replacement) > limits.max_rule_chars:
            raise _fail("regex_rule_limit", "Regex rule exceeds the configured limit")
        _validate_texts(texts, limits)
        deadline = time.monotonic() + limits.timeout_seconds
        payload = json.dumps(
            {
                "schema_version": 1, "syntax": REGEX_SYNTAX, "texts": texts,
                "rule": rule_value, "limits": asdict(limits),
            },
            ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_WIRE_BYTES:
            raise _fail("regex_input_limit", "Regex worker input exceeds its byte limit")
        output_cap = min(
            MAX_WIRE_BYTES, 6 * limits.max_output_chars + 8 * len(texts) + 4096,
        )
        raw = _run_worker(payload, deadline=deadline, output_cap=output_cap)
        try:
            response = loads_strict(raw.decode("utf-8"))
        except (UnicodeError, ContractValidationError) as exc:
            raise _fail("regex_worker_failed", "Regex worker returned invalid JSON") from exc
        if type(response) is not dict:
            raise _fail("regex_worker_failed", "Regex worker returned an invalid response")
        if (
            set(response) == {"error"} and type(response["error"]) is str
            and response["error"] in _WORKER_ERROR_CODES
        ):
            raise _fail(response["error"], "Regex preparation was rejected")
        if set(response) != {"texts"}:
            raise _fail("regex_worker_failed", "Regex worker returned unexpected fields")
        result = response["texts"]
        if (
            type(result) is not list or len(result) != len(texts)
            or any(type(text) is not str for text in result)
            or sum(len(text) for text in result) > limits.max_output_chars
        ):
            raise _fail("regex_worker_failed", "Regex worker returned invalid text output")
        return result
    except PromptProcessingError as exc:
        if exc.node_id is None:
            exc.node_id = node_id
        raise


def regex_replace(
    text: str, rule: RegexRule, *, limits: RegexLimits,
    node_id: str | None = None,
) -> str:
    return regex_replace_many([text], rule, limits=limits, node_id=node_id)[0]
