import math

import pytest

from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.contract_json import canonical_bytes, content_digest, dumps_pretty, loads_strict


def test_pretty_roundtrip_and_key_order_independent_digest():
    left = {"text": "\u4f60\u597d", "answer": {"b": 2, "a": [True, None, 1.5]}}
    right = {"answer": {"a": [True, None, 1.5], "b": 2}, "text": "\u4f60\u597d"}
    assert loads_strict(dumps_pretty(left)) == left
    assert content_digest(left) == content_digest(right)
    assert content_digest(left) == content_digest(loads_strict(dumps_pretty(left)))
    assert b" " not in canonical_bytes({"a": 1})
    assert content_digest(left).startswith("json-v1:sha256:")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {1: "x"}, (1, 2), object(), "\ud800"])
def test_python_only_or_nonfinite_values_rejected(value):
    with pytest.raises(ContractValidationError):
        dumps_pretty(value)
    with pytest.raises(ContractValidationError):
        content_digest(value)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"x":NaN}', "Infinity", "1e999", '"\\ud800"', "{"])
def test_strict_parser_rejects_invalid_or_ambiguous_json(text):
    with pytest.raises(ContractValidationError):
        loads_strict(text)


def test_cycle_rejected_but_shared_values_allowed():
    shared = [1]
    assert loads_strict(dumps_pretty([shared, shared])) == [[1], [1]]
    shared.append(shared)
    with pytest.raises(ContractValidationError):
        dumps_pretty(shared)


def test_digest_preserves_array_order_and_number_representation():
    assert content_digest([1, 2]) != content_digest([2, 1])
    assert content_digest({"n": 1}) != content_digest({"n": 1.0})
    assert content_digest({"n": -0.0}) != content_digest({"n": 0.0})
