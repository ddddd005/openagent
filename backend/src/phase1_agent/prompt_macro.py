"""Bounded, original-input-only variable interpolation without side effects."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .contract_errors import ContractValidationError
from .contract_json import validate_json_value
from .prompt_errors import PromptProcessingError
from .prompt_variables import VARIABLE_NAME_PATTERN, validate_variable_snapshot


MACRO_SYNTAX = "simple-variables-v1"
_NAME = re.compile(VARIABLE_NAME_PATTERN)


@dataclass(frozen=True)
class MacroLimits:
    max_input_chars: int
    max_output_chars: int
    max_substitutions: int

    def __post_init__(self) -> None:
        for value in (self.max_input_chars, self.max_output_chars, self.max_substitutions):
            if type(value) is not int or value < 0:
                raise PromptProcessingError(
                    "invalid_macro_limits", "Macro limits must be explicit nonnegative integers",
                )


def macro_replace(
    text: str, snapshot: dict[str, Any], *, node_id: str, limits: MacroLimits,
) -> str:
    """Replace original {{name}} references once, reading a detached snapshot.

    Only original-input \\{, \\} and \\\\ escapes are consumed. Replacement
    values are literal text; a later explicit macro node may interpret them.
    """
    def fail(code: str, message: str, offset: int | None = None) -> None:
        raise PromptProcessingError(
            code, message, node_id=node_id if type(node_id) is str else None, offset=offset,
        )

    if type(node_id) is not str or not node_id.strip():
        fail("invalid_macro_node", "Macro node identity must be nonempty text")
    if type(limits) is not MacroLimits:
        fail("invalid_macro_limits", "Macro processing requires explicit limits")
    if type(text) is not str:
        fail("invalid_macro_input", "Macro input must be text")
    try:
        validate_json_value(text)
    except ContractValidationError:
        fail("invalid_macro_input", "Macro input must be valid UTF-8 text")
    if len(text) > limits.max_input_chars:
        fail("macro_input_limit", "Macro input length exceeds its limit", 0)
    try:
        values = validate_variable_snapshot(snapshot)["values"]
    except PromptProcessingError as exc:
        fail(exc.code, str(exc))
    output: list[str] = []
    output_chars = 0
    substitutions = 0

    def append(value: str, offset: int) -> None:
        nonlocal output_chars
        output_chars += len(value)
        if output_chars > limits.max_output_chars:
            fail("macro_output_limit", "Macro output length exceeds its limit", offset)
        output.append(value)

    position = 0
    while position < len(text):
        char = text[position]
        if char == "\\" and position + 1 < len(text) and text[position + 1] in "{}\\":
            append(text[position + 1], position)
            position += 2
            continue
        if text.startswith("}}", position):
            fail("invalid_macro_reference", "Macro has an unmatched closing delimiter", position)
        if not text.startswith("{{", position):
            append(char, position)
            position += 1
            continue
        closing = text.find("}}", position + 2)
        if closing == -1:
            fail("invalid_macro_reference", "Macro has an unclosed reference", position)
        name = text[position + 2:closing]
        if _NAME.fullmatch(name) is None:
            fail("invalid_macro_reference", "Macro reference name is malformed", position)
        if name not in values or "value" not in values[name]:
            fail("missing_macro_variable", "Macro variable is undefined or unassigned", position)
        substitutions += 1
        if substitutions > limits.max_substitutions:
            fail("macro_substitution_limit", "Macro substitution count exceeds its limit", position)
        variable = values[name]
        if variable["type"] == "string":
            replacement = variable["value"]
        else:
            try:
                replacement = json.dumps(variable["value"], allow_nan=False, separators=(",", ":"))
            except (ValueError, OverflowError):
                fail("invalid_macro_variable", "Macro variable cannot be rendered", position)
        append(replacement, position)
        position = closing + 2
    return "".join(output)
