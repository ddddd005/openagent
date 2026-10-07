"""Bounded, read-only lorebook activation over one explicit native prompt."""

from __future__ import annotations

from copy import deepcopy
import math
import random
import re

from .content_contracts import default_presentation, object_schema, presentation_schema, validate_presentation
from .context_prompt_v6 import native_history_floor_starts, validate_native_context_prompt
from .contract_json import canonical_bytes, validate_json_value
from .graph_contracts import require, uuid4_string
from .prompt_contract import _source_origins, check_prompt_schema
from .prompt_lifecycle import (
    lifecycle_prompt_content, make_lifecycle_prompt_item, merge_lifecycle_prompt_materials,
)
from .tool_package import _scalar_text, _validate_variable


MAX_ENTRIES = 1024
MAX_KEYWORDS_PER_GROUP = 128
MAX_KEYWORD_CHARS = 4096
MAX_TEXT_CHARS = 1_000_000
MAX_SCAN_DEPTH = 4096
MAX_WIRE_BYTES = 4_000_000
MAX_RESOLVED_KEYWORD_CHARS = 1_000_000
MAX_SCAN_CHAR_WORK = 64_000_000
MAX_RECURSIVE_ROUNDS = 3
DEFAULT_ENTRY_ID = "877a4804-ac85-4ce0-b9ea-7855d77e9701"
KEYWORD_RULES = (
    "primary_or_secondary", "primary_and_secondary", "primary_and_not_secondary",
)
_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")
_UUID = {"type": "string", "pattern":
         "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"}


def default_lorebook_entry(member_id=None):
    return {
        "id": DEFAULT_ENTRY_ID if member_id is None else member_id,
        "name": "", "text": "", "presentation": default_presentation(), "metadata": {},
        "mode": "keyword", "primary_keywords": [], "secondary_keywords": [],
        "keyword_rule": "primary_or_secondary", "case_sensitive": False, "recursive": False,
        "scan_depth": 20, "probability_enabled": False, "probability": 100,
    }


def lorebook_entry_schema():
    keywords = {"type": "array", "maxItems": MAX_KEYWORDS_PER_GROUP,
                "items": {"type": "string", "maxLength": MAX_KEYWORD_CHARS}}
    return object_schema({
        "id": _UUID, "name": {"type": "string", "maxLength": 128},
        "text": {"type": "string", "maxLength": MAX_TEXT_CHARS},
        "presentation": presentation_schema(), "metadata": {"type": "object"},
        "mode": {"enum": ["constant", "keyword"]},
        "primary_keywords": keywords, "secondary_keywords": deepcopy(keywords),
        "keyword_rule": {"enum": list(KEYWORD_RULES)}, "case_sensitive": {"type": "boolean"},
        "recursive": {"type": "boolean"},
        "scan_depth": {"type": "integer", "minimum": 0, "maximum": MAX_SCAN_DEPTH},
        "probability_enabled": {"type": "boolean"},
        "probability": {"type": "number", "minimum": 0, "maximum": 100},
    })


def validate_lorebook_entry(value):
    validate_json_value(value)
    check_prompt_schema(value, lorebook_entry_schema(), "lorebook_invalid_entry",
                        "Lorebook entry configuration is invalid")
    uuid4_string(value["id"])
    validate_presentation(value["presentation"])
    require(len(canonical_bytes(value)) <= MAX_WIRE_BYTES, "lorebook_budget_exceeded",
            "Lorebook entry exceeds its configuration budget")
    return deepcopy(value)


def validate_lorebook_entries(values):
    require(type(values) is list and len(values) <= MAX_ENTRIES, "lorebook_invalid_entries",
            "Lorebook entries require a bounded array")
    result = [validate_lorebook_entry(value) for value in values]
    identities = [value["id"] for value in result]
    require(len(identities) == len(set(identities)), "lorebook_duplicate_entry",
            "Lorebook entries cannot repeat a stable member identity")
    require(len(canonical_bytes(result)) <= MAX_WIRE_BYTES, "lorebook_budget_exceeded",
            "Lorebook entries exceed their configuration budget")
    return result


def _variables(values):
    require(values is None or type(values) is list and len(values) <= MAX_ENTRIES,
            "lorebook_invalid_variables", "Lorebook variables require explicit authorized object values")
    values = [] if values is None else values
    require(len(canonical_bytes(values)) <= MAX_WIRE_BYTES, "lorebook_budget_exceeded",
            "Authorized variable values exceed their read budget")
    result = {}
    for value in values:
        _validate_variable(value)
        if not value["registered"]:
            continue
        name = value["name"]
        require(name not in result, "variable_name_conflict",
                "Authorized variable objects declare the same name")
        result[name] = _scalar_text(value["value_type"], value["value"]) if value["assigned"] else None
    return result


class _Budget:
    def __init__(self):
        self.scan = MAX_SCAN_CHAR_WORK
        self.keywords = MAX_RESOLVED_KEYWORD_CHARS

    def consume_scan(self, amount):
        self.scan -= amount
        require(self.scan >= 0, "lorebook_budget_exceeded",
                "Lorebook matching exceeded its bounded character-work budget")

    def consume_keyword(self, amount):
        self.keywords -= amount
        require(self.keywords >= 0, "lorebook_budget_exceeded",
                "Resolved lorebook keywords exceed their expansion budget")


def _resolve_keyword(keyword, variables, budget):
    matches = list(_TOKEN.finditer(keyword))
    if any(variables.get(match.group(1)) in (None, "") for match in matches):
        return None
    pieces, cursor, total = [], 0, 0
    for match in matches:
        replacement = variables[match.group(1)]
        literal = keyword[cursor:match.start()]
        size = len(literal) + len(replacement)
        budget.consume_keyword(size)
        total += size
        pieces.extend((literal, replacement))
        cursor = match.end()
    tail = keyword[cursor:]
    budget.consume_keyword(len(tail))
    total += len(tail)
    if total == 0:
        return None
    pieces.append(tail)
    return "".join(pieces)


def _message_texts(message):
    return [block["text"] for block in message["blocks"] if block["kind"] == "text"]


def _scan_inputs(prompt):
    view, floors = prompt["context"], []
    starts = native_history_floor_starts(view)
    for start, end in zip(starts, starts[1:]):
        texts = []
        for message, layout in zip(view["messages"][start:end], view["layout"][start:end]):
            if layout["kind"] == "history" and message["role"] in ("user", "assistant"):
                texts.extend(_message_texts(message))
        floors.append(texts)
    floors.append(_message_texts(prompt["current_input"]))
    lorebook = []
    for message, provenance in zip(prompt["messages"], prompt["provenance"]):
        if provenance["kind"] == "material" and any(
                origin.get("kind") == "lorebook" for origin in _source_origins(provenance["source"])):
            lorebook.extend(_message_texts(message))
    return floors, lorebook


def _fold(text, case_sensitive, cache, budget):
    if case_sensitive:
        return text
    if text not in cache:
        budget.consume_scan(len(text))
        cache[text] = text.casefold()
    return cache[text]


def _group_matches(keywords, texts, *, case_sensitive, cache, budget):
    for keyword in keywords:
        if keyword is None:
            continue
        needle = _fold(keyword, case_sensitive, cache, budget)
        for text in texts:
            haystack = _fold(text, case_sensitive, cache, budget)
            if len(needle) > len(haystack):
                continue
            budget.consume_scan(len(haystack) + len(needle))
            if needle in haystack:
                return True
    return False


def _keyword_passed(rule, primary, secondary):
    if rule == "primary_or_secondary":
        return primary or secondary
    if rule == "primary_and_secondary":
        return primary and secondary
    return primary and not secondary


def _probability(entry, random_value):
    if not entry["probability_enabled"] or entry["probability"] == 100:
        return True
    if entry["probability"] == 0:
        return False
    value = random_value()
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1,
            "lorebook_random_invalid", "Lorebook probability source must return a value in [0, 1)")
    return value < entry["probability"] / 100


def evaluate_lorebook(entries, scan_prompt, *, node_id, variables=None, random_value=None):
    """Return only activated materials; each call owns its three extra rounds."""
    uuid4_string(node_id)
    entries = validate_lorebook_entries(entries)
    prompt = validate_native_context_prompt(scan_prompt)
    values = _variables(variables)
    budget, cache = _Budget(), {}
    floors, upstream_texts = _scan_inputs(prompt)
    resolved, diagnostics, probability = [], [], []
    random_value = random.random if random_value is None else random_value
    for entry in entries:
        enabled = entry["presentation"]["enabled"]
        passed = _probability(entry, random_value) if enabled else None
        keywords = {
            group: [_resolve_keyword(keyword, values, budget) for keyword in entry[group]]
            if enabled and passed and entry["mode"] == "keyword" else []
            for group in ("primary_keywords", "secondary_keywords")
        }
        resolved.append(keywords)
        probability.append(passed)
        diagnostics.append({
            "entry_id": entry["id"], "name": entry["name"],
            "status": "disabled" if not enabled else "probability_rejected" if not passed else "keyword_miss",
            "activation_round": None, "probability_passed": passed,
            "primary_matched": None, "secondary_matched": None,
        })
    active, recursive_texts, rounds = set(), list(upstream_texts), 0
    for round_number in range(MAX_RECURSIVE_ROUNDS + 1):
        if round_number:
            candidates = [index for index, entry in enumerate(entries)
                          if index not in active and probability[index]
                          and entry["mode"] == "keyword" and entry["recursive"]]
            if not candidates:
                break
            rounds = round_number
        else:
            candidates = range(len(entries))
        newly_active = []
        for index in candidates:
            entry, diagnostic = entries[index], diagnostics[index]
            if not probability[index]:
                continue
            if entry["mode"] == "constant":
                passed = True
            else:
                depth = entry["scan_depth"]
                texts = [text for floor in floors[-depth:] for text in floor] if depth else []
                if entry["recursive"]:
                    texts.extend(recursive_texts)
                primary = _group_matches(
                    resolved[index]["primary_keywords"], texts, case_sensitive=entry["case_sensitive"],
                    cache=cache, budget=budget)
                secondary = _group_matches(
                    resolved[index]["secondary_keywords"], texts, case_sensitive=entry["case_sensitive"],
                    cache=cache, budget=budget)
                diagnostic.update(primary_matched=primary, secondary_matched=secondary)
                passed = _keyword_passed(entry["keyword_rule"], primary, secondary)
            if passed:
                newly_active.append(index)
                diagnostic.update(status="activated", activation_round=round_number)
        if not newly_active:
            break
        active.update(newly_active)
        new_texts = [entries[index]["text"] for index in newly_active if entries[index]["text"]]
        if not new_texts:
            break
        recursive_texts.extend(new_texts)
    items = [make_lifecycle_prompt_item(
        node_id, entry["id"], entry["text"], entry["presentation"],
        source={"kind": "lorebook", "node_id": node_id, "member_id": entry["id"]},
        metadata=entry["metadata"], lifecycle="per_request", compaction="never",
    ) for index, entry in enumerate(entries) if index in active]
    materials = merge_lifecycle_prompt_materials([lifecycle_prompt_content(items)])
    require(len(canonical_bytes(materials)) <= MAX_WIRE_BYTES, "lorebook_budget_exceeded",
            "Activated lorebook materials exceed their output budget")
    return {"materials": materials, "diagnostics": diagnostics, "recursive_rounds": rounds}
