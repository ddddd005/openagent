"""Private, disposable worker for the Python-re-v1 prompt regex protocol."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import loads_strict
from .prompt_errors import PromptProcessingError
from .prompt_regex import (
    MAX_WIRE_BYTES, REGEX_SYNTAX, RegexLimits, RegexRule, _fail,
    _limits_from_dict, _validate_texts, regex_rule_from_dict,
)


_FLAGS = {
    "i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL,
    "x": re.VERBOSE, "a": re.ASCII,
}
_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
}


def _replacement_plan(replacement: str) -> list[tuple[str, Any]]:
    """Measure re-validated templates before any potentially large expansion.

    Matching and template validation still belong to the standard engine. This
    small scanner only separates validated group references from literal chunks.
    """
    result: list[tuple[str, Any]] = []
    position = 0
    literal_start = 0
    while position < len(replacement):
        if replacement[position] != "\\":
            position += 1
            continue
        if position > literal_start:
            result.append(("literal", replacement[literal_start:position]))
        position += 1
        escape = replacement[position]
        position += 1
        if escape == "g":
            closing = replacement.index(">", position + 1)
            name = replacement[position + 1:closing]
            result.append(("group", int(name) if name.isdecimal() else name))
            position = closing + 1
        elif escape == "0":
            digits = escape
            while len(digits) < 3 and position < len(replacement):
                if replacement[position] not in "01234567":
                    break
                digits += replacement[position]
                position += 1
            result.append(("literal", chr(int(digits, 8))))
        elif escape in "123456789":
            digits = escape
            if position < len(replacement) and replacement[position] in "0123456789":
                digits += replacement[position]
                position += 1
            if (
                len(digits) == 2 and all(char in "01234567" for char in digits)
                and position < len(replacement) and replacement[position] in "01234567"
            ):
                digits += replacement[position]
                position += 1
                result.append(("literal", chr(int(digits, 8))))
            else:
                result.append(("group", int(digits)))
        else:
            result.append(("literal", _ESCAPES.get(escape, "\\" + escape)))
        literal_start = position
    if literal_start < len(replacement):
        result.append(("literal", replacement[literal_start:]))
    return result


def _expanded_size(plan: list[tuple[str, Any]], match: re.Match[str]) -> int:
    return sum(
        len(value) if kind == "literal" else match.end(value) - match.start(value)
        for kind, value in plan
    )


def _replace_many(texts: list[str], rule: RegexRule, limits: RegexLimits) -> list[str]:
    if len(rule.pattern) + len(rule.replacement) > limits.max_rule_chars:
        raise _fail("regex_rule_limit", "Regex rule exceeds the configured limit")
    _validate_texts(texts, limits)
    flags = 0
    for flag in rule.flags:
        flags |= _FLAGS[flag]
    try:
        compiled = re.compile(rule.pattern, flags)
    except (re.error, OverflowError, RecursionError, ValueError) as exc:
        raise _fail("regex_invalid_pattern", "Regex pattern is invalid") from exc
    try:
        compiled.sub(rule.replacement, "")
        plan = _replacement_plan(rule.replacement)
    except (re.error, IndexError, OverflowError, RecursionError, ValueError) as exc:
        raise _fail("regex_invalid_replacement", "Regex replacement is invalid") from exc

    result: list[str] = []
    total_output = 0
    total_matches = 0
    for text in texts:
        parts: list[str] = []
        position = 0
        for match in compiled.finditer(text):
            total_matches += 1
            if total_matches > limits.max_matches:
                raise _fail("regex_match_limit", "Regex matches exceed the total limit")
            unmatched_size = match.start() - position
            remaining = limits.max_output_chars - total_output - unmatched_size
            if remaining < 0 or _expanded_size(plan, match) > remaining:
                raise _fail("regex_output_limit", "Regex output exceeds the total limit")
            if unmatched_size:
                parts.append(text[position:match.start()])
                total_output += unmatched_size
            expanded = match.expand(rule.replacement)
            parts.append(expanded)
            total_output += len(expanded)
            position = match.end()
            if rule.mode == "first":
                break
        tail_size = len(text) - position
        if total_output + tail_size > limits.max_output_chars:
            raise _fail("regex_output_limit", "Regex output exceeds the total limit")
        if tail_size:
            parts.append(text[position:])
            total_output += tail_size
        result.append("".join(parts))
    return result


def _request(value: Any) -> list[str]:
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "syntax", "texts", "rule", "limits"}
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["syntax"] != REGEX_SYNTAX
    ):
        raise _fail("regex_worker_failed", "Regex worker request is invalid")
    rule = regex_rule_from_dict(value["rule"])
    limits = _limits_from_dict(value["limits"])
    return _replace_many(value["texts"], rule, limits)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
        if len(raw) > MAX_WIRE_BYTES:
            raise _fail("regex_worker_failed", "Regex worker request exceeds its byte cap")
        request = loads_strict(raw.decode("utf-8"))
        response = {"texts": _request(request)}
    except PromptProcessingError as exc:
        response = {"error": exc.code}
    except (ContractValidationError, UnicodeError, MemoryError):
        response = {"error": "regex_worker_failed"}
    raw_output = json.dumps(
        response, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(raw_output) > MAX_WIRE_BYTES:
        raw_output = b'{"error":"regex_worker_failed"}'
    sys.stdout.buffer.write(raw_output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
