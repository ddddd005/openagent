"""Real worker deadlines, bounded replacements and deterministic regex syntax."""

from dataclasses import replace
import json
import re
import subprocess
import sys
import time

import pytest

from phase1_agent.prompt_errors import PromptProcessingError
from phase1_agent import prompt_regex
from phase1_agent.prompt_regex import (
    MAX_BATCH_TEXTS, REGEX_SYNTAX, RegexLimits, RegexRule,
    regex_replace, regex_replace_many, regex_rule_from_dict, regex_rule_to_dict,
    validate_regex_limits,
)


LIMITS = RegexLimits(timeout_seconds=2)


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(PromptProcessingError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def recording_processes(monkeypatch):
    created = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", record)
    return created


def test_rule_structure_roundtrip_does_not_compile_regex(monkeypatch):
    def no_compile(*args, **kwargs):
        raise AssertionError("Regex compilation escaped into the host process")

    monkeypatch.setattr(re, "compile", no_compile)
    value = {"pattern": "(", "replacement": r"\g<missing>", "flags": "im", "mode": "all"}
    rule = regex_rule_from_dict(value)
    assert regex_rule_to_dict(rule) == value
    assert REGEX_SYNTAX == "python-re-v1"


@pytest.mark.parametrize("value", [
    None, [], {}, {"pattern": "a"},
    {"pattern": "a", "replacement": "", "flags": "", "mode": "all", "extra": 1},
    {"pattern": 1, "replacement": "", "flags": "", "mode": "all"},
    {"pattern": "a", "replacement": 1, "flags": "", "mode": "all"},
    {"pattern": "a", "replacement": "", "flags": None, "mode": "all"},
    {"pattern": "a", "replacement": "", "flags": "", "mode": "all\ud800"},
    {"pattern": "a\ud800", "replacement": "", "flags": "", "mode": "all"},
    {"pattern": "a", "replacement": "", "flags": "", "mode": "ALL"},
])
def test_bad_rule_structure_is_rejected(value):
    assert_error("regex_invalid_rule", regex_rule_from_dict, value)


@pytest.mark.parametrize("flags", ["ii", "g", "u", "I", " i", "xmx"])
def test_flags_use_an_exact_nonduplicated_allowlist(flags):
    assert_error(
        "regex_invalid_flags", regex_rule_from_dict,
        {"pattern": "a", "replacement": "", "flags": flags, "mode": "all"},
    )


def test_rules_and_limits_are_explicit_types():
    assert_error(
        "regex_invalid_rule", regex_replace, "a", {}, limits=LIMITS,
    )
    assert_error(
        "regex_invalid_limits", regex_replace, "a", RegexRule("a", "b"), limits={},
    )
    with pytest.raises(TypeError):
        regex_replace("a", RegexRule("a", "b"))


@pytest.mark.parametrize("changes", [
    {"max_input_chars": -1}, {"max_input_chars": True},
    {"max_input_chars": 8_000_001}, {"max_output_chars": 8_000_001},
    {"max_matches": 100_001}, {"max_matches": 1.0},
    {"max_rule_chars": 65_537}, {"timeout_seconds": 0},
    {"timeout_seconds": -1}, {"timeout_seconds": 10.01},
    {"timeout_seconds": True}, {"timeout_seconds": float("inf")},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": "1"},
    {"timeout_seconds": 10 ** 1000},
])
def test_invalid_resource_profiles_do_not_spawn_a_worker(monkeypatch, changes):
    def no_spawn(*args, **kwargs):
        raise AssertionError("Invalid input spawned a worker")

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", no_spawn)
    assert_error(
        "regex_invalid_limits", validate_regex_limits, replace(LIMITS, **changes),
    )
    assert_error(
        "regex_invalid_limits", regex_replace, "a", RegexRule("a", "b"),
        limits=replace(LIMITS, **changes),
    )


@pytest.mark.parametrize("text,pattern,replacement,flags,mode,expected", [
    ("a a", "a", "aa", "", "all", "aa aa"),
    ("a a", "a", "aa", "", "first", "aa a"),
    ("{{name}}", "name", "other", "", "all", "{{other}}"),
    ("hi", "hi", "{{name}}", "", "all", "{{name}}"),
    ("A\na", "^a$", "X", "im", "all", "X\nX"),
    ("a\nb", ".", "X", "s", "all", "XXX"),
    ("ab", "a  # comment\n b", "X", "x", "all", "X"),
    ("\u00e9a", r"\w", "X", "a", "all", "\u00e9X"),
    ("ab", "", "-", "", "all", "-a-b-"),
    ("", "", "yes", "", "all", "yes"),
    ("abc", "z", "unused", "", "all", "abc"),
    ("/a/i a", "/a/i", "X", "", "all", "X a"),
    ("a", "(a)", "$1", "", "all", "$1"),
    ("b", "(a)?b", r"<\1>", "", "all", "<>"),
])
def test_python_re_semantics_and_single_pass(text, pattern, replacement, flags, mode, expected):
    assert regex_replace(
        text, RegexRule(pattern, replacement, flags, mode), limits=LIMITS,
    ) == expected


def test_serial_nodes_expand_only_when_explicitly_called_again():
    rule = RegexRule("a", "aa")
    first = regex_replace("a", rule, limits=LIMITS)
    assert first == "aa"
    assert regex_replace(first, rule, limits=LIMITS) == "aaaa"


@pytest.mark.parametrize("template", [
    r"\1-\g<name>-\g<0>", r"\\\1", r"\01\001\0", r"\111",
    r"\n\t\a\b\f\r\v", r"\&-\1", r"\g<01>", "literal",
])
def test_capture_and_escape_templates_match_standard_engine(template):
    pattern = r"(?P<name>a)(b)?"
    text = "a ab"
    expected = re.sub(pattern, template, text)
    assert regex_replace(text, RegexRule(pattern, template), limits=LIMITS) == expected


@pytest.mark.parametrize("text", ["", "no match here"])
@pytest.mark.parametrize("replacement", [r"\2", r"\g<missing>", r"\g<", "\\", r"\q", r"\400"])
def test_invalid_replacement_is_rejected_even_without_a_match(text, replacement):
    assert_error(
        "regex_invalid_replacement", regex_replace,
        text, RegexRule("(x)", replacement), limits=LIMITS,
    )


def test_empty_batch_still_validates_regex_and_replacement():
    assert regex_replace_many([], RegexRule("x", "y"), limits=LIMITS) == []
    assert_error(
        "regex_invalid_replacement", regex_replace_many,
        [], RegexRule("(x)", r"\2"), limits=LIMITS,
    )
    assert_error(
        "regex_invalid_pattern", regex_replace_many,
        [], RegexRule("(", ""), limits=LIMITS,
    )


@pytest.mark.parametrize("pattern", ["(", "[", "*", r"(?P<x>a)(?P<x>b)", r"\g<1>"])
def test_invalid_pattern_is_a_locatable_redacted_error(pattern):
    error = assert_error(
        "regex_invalid_pattern", regex_replace,
        "secret body", RegexRule(pattern, ""), limits=LIMITS, node_id="regex-node",
    )
    assert error.node_id == "regex-node"
    assert "secret" not in str(error)
    assert pattern not in str(error)


def test_match_budget_and_output_budget_are_cumulative_across_batch():
    texts = ["aa", "aa"]
    assert_error(
        "regex_match_limit", regex_replace_many, texts, RegexRule("a", "b"),
        limits=replace(LIMITS, max_matches=3),
    )
    assert_error(
        "regex_output_limit", regex_replace_many, texts, RegexRule("a", "bbb"),
        limits=replace(LIMITS, max_output_chars=11),
    )
    assert texts == ["aa", "aa"]
    assert regex_replace_many(
        texts, RegexRule("a", "bbb"), limits=replace(LIMITS, max_output_chars=12),
    ) == ["bbbbbb", "bbbbbb"]


def test_first_means_first_per_original_text_not_first_across_batch():
    assert regex_replace_many(
        ["aa", "aa"], RegexRule("a", "b", mode="first"),
        limits=replace(LIMITS, max_matches=2),
    ) == ["ba", "ba"]


def test_output_limit_includes_unmatched_prefix_and_tail():
    assert_error(
        "regex_output_limit", regex_replace, "aaaa", RegexRule("z", ""),
        limits=replace(LIMITS, max_output_chars=3),
    )
    assert_error(
        "regex_output_limit", regex_replace, "aaaax", RegexRule("x", "yy"),
        limits=replace(LIMITS, max_output_chars=5),
    )
    assert_error(
        "regex_output_limit", regex_replace, "xaaaa", RegexRule("x", "yy"),
        limits=replace(LIMITS, max_output_chars=5),
    )


def test_zero_resource_budgets_are_meaningful():
    limits = replace(LIMITS, max_input_chars=0, max_output_chars=0, max_matches=0)
    assert regex_replace("", RegexRule("x", ""), limits=limits) == ""
    assert_error(
        "regex_match_limit", regex_replace, "", RegexRule("", ""), limits=limits,
    )
    assert_error(
        "regex_output_limit", regex_replace, "", RegexRule("", "a"),
        limits=replace(limits, max_matches=1),
    )


def test_zero_width_matches_use_the_standard_original_input_iteration():
    assert regex_replace(
        "a", RegexRule("x*", "y"), limits=replace(LIMITS, max_matches=2),
    ) == "yay"
    assert_error(
        "regex_match_limit", regex_replace, "a", RegexRule("x*", "y"),
        limits=replace(LIMITS, max_matches=1),
    )


def test_huge_capture_expansion_is_rejected_before_allocating_it():
    assert_error(
        "regex_output_limit", regex_replace, "a" * 100_000,
        RegexRule("(.*)", r"\g<1>" * 2000),
        limits=replace(LIMITS, max_output_chars=100_000),
    )


@pytest.mark.parametrize("texts,code", [
    (("a",), "regex_input_limit"),
    ([True], "regex_invalid_input"),
    (["\ud800"], "regex_invalid_input"),
    ([""] * (MAX_BATCH_TEXTS + 1), "regex_input_limit"),
])
def test_invalid_text_inputs_are_rejected_before_spawn(monkeypatch, texts, code):
    def no_spawn(*args, **kwargs):
        raise AssertionError("Invalid input spawned a worker")

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", no_spawn)
    assert_error(
        code, regex_replace_many, texts, RegexRule("x", ""), limits=LIMITS,
    )


def test_input_and_rule_limits_are_checked_before_spawn(monkeypatch):
    def no_spawn(*args, **kwargs):
        raise AssertionError("Invalid input spawned a worker")

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", no_spawn)
    assert_error(
        "regex_input_limit", regex_replace_many, ["aa", "aa"], RegexRule("x", ""),
        limits=replace(LIMITS, max_input_chars=3),
    )
    assert_error(
        "regex_rule_limit", regex_replace, "", RegexRule("aa", "bb"),
        limits=replace(LIMITS, max_rule_chars=3),
    )
    assert_error(
        "regex_rule_limit", regex_rule_to_dict, RegexRule("a" * 65_537, ""),
    )


def test_one_process_per_batch_and_no_host_regex_compilation(monkeypatch):
    processes = recording_processes(monkeypatch)

    def no_compile(*args, **kwargs):
        raise AssertionError("Untrusted regex was compiled in the parent")

    monkeypatch.setattr(re, "compile", no_compile)
    assert regex_replace_many(
        ["a", "aa"], RegexRule("a", "b"), limits=LIMITS,
    ) == ["b", "bb"]
    assert len(processes) == 1
    assert processes[0].poll() == 0


def test_real_catastrophic_backtracking_is_killed_and_waited(monkeypatch):
    processes = recording_processes(monkeypatch)
    started = time.monotonic()
    error = assert_error(
        "regex_timeout", regex_replace, "a" * 30_000 + "!",
        RegexRule("(a+)+$", "x"),
        limits=replace(LIMITS, timeout_seconds=0.25), node_id="deadline-node",
    )
    assert error.node_id == "deadline-node"
    assert time.monotonic() - started < 3
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed
    assert processes[0].stdout.closed
    assert processes[0].stderr.closed


def test_one_deadline_covers_the_whole_batch(monkeypatch):
    processes = recording_processes(monkeypatch)
    assert_error(
        "regex_timeout", regex_replace_many, ["ok", "a" * 30_000 + "!"],
        RegexRule("(a+)+$", "x"),
        limits=replace(LIMITS, timeout_seconds=0.25),
    )
    assert len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"\xff", b"[]", b'{"texts":["a"],"extra":1}',
    b'{"texts":[]}', b'{"texts":[1]}', b'{"error":[]}',
    b'{"error":"not-a-known-error"}', b'{"texts":["a"],"texts":["b"]}',
    b'{"texts":["a\\ud800"]}',
])
def test_bad_worker_responses_are_explicit_failures(monkeypatch, raw):
    monkeypatch.setattr(prompt_regex, "_run_worker", lambda *args, **kwargs: raw)
    assert_error(
        "regex_worker_failed", regex_replace, "a", RegexRule("a", "b"), limits=LIMITS,
    )


def test_worker_output_is_revalidated_against_limits(monkeypatch):
    monkeypatch.setattr(
        prompt_regex, "_run_worker", lambda *args, **kwargs: b'{"texts":["long"]}',
    )
    assert_error(
        "regex_worker_failed", regex_replace, "a", RegexRule("a", "b"),
        limits=replace(LIMITS, max_output_chars=3),
    )


@pytest.mark.parametrize("program", [
    "import sys;sys.stdout.write('invalid json')",
    "import sys;sys.stderr.write('private source');sys.exit(3)",
    "import sys;sys.stdout.write('X'*100000);sys.stdout.flush()",
    "import sys;sys.stderr.write('X'*100000);sys.stderr.flush()",
])
def test_faulty_real_worker_is_reaped_and_error_is_redacted(monkeypatch, program):
    created = []
    original = subprocess.Popen

    def substituted_worker(command, **kwargs):
        process = original([sys.executable, "-c", program], **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", substituted_worker)
    error = assert_error(
        "regex_worker_failed", regex_replace,
        "private input", RegexRule("a", "b"), limits=LIMITS,
    )
    assert "private" not in str(error)
    assert created[0].poll() is not None
    assert created[0].stdin.closed
    assert created[0].stdout.closed
    assert created[0].stderr.closed


def test_deadline_includes_a_worker_that_never_reads_stdin(monkeypatch):
    original = subprocess.Popen
    created = []

    def blocked_worker(command, **kwargs):
        process = original(
            [sys.executable, "-c", "import time;time.sleep(30)"], **kwargs,
        )
        created.append(process)
        return process

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", blocked_worker)
    assert_error(
        "regex_timeout", regex_replace, "a" * 100_000, RegexRule("a", "b"),
        limits=replace(LIMITS, timeout_seconds=0.25),
    )
    assert created[0].poll() is not None


def test_host_interruption_kills_and_waits_worker(monkeypatch):
    original = subprocess.Popen
    created = []

    def interrupted_worker(*args, **kwargs):
        process = original(*args, **kwargs)
        original_poll = process.poll
        polls = 0

        def interrupt_once():
            nonlocal polls
            polls += 1
            if polls == 1:
                raise KeyboardInterrupt
            return original_poll()

        process.poll = interrupt_once
        created.append(process)
        return process

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", interrupted_worker)
    with pytest.raises(KeyboardInterrupt):
        regex_replace("a", RegexRule("a", "b"), limits=LIMITS)
    assert created[0].poll() is not None
    assert created[0].stdin.closed
    assert created[0].stdout.closed
    assert created[0].stderr.closed


def test_worker_launch_failure_is_explicit(monkeypatch):
    def failed_launch(*args, **kwargs):
        raise OSError("private path")

    monkeypatch.setattr(prompt_regex.subprocess, "Popen", failed_launch)
    error = assert_error(
        "regex_worker_failed", regex_replace, "a", RegexRule("a", "b"), limits=LIMITS,
    )
    assert "private" not in str(error)


def test_worker_stdout_has_only_strict_json(monkeypatch):
    original = prompt_regex._run_worker
    captured = []

    def capture(*args, **kwargs):
        raw = original(*args, **kwargs)
        captured.append(raw)
        return raw

    monkeypatch.setattr(prompt_regex, "_run_worker", capture)
    assert regex_replace("\u4e2d\u6587", RegexRule(".", "x"), limits=LIMITS) == "xx"
    assert json.loads(captured[0]) == {"texts": ["xx"]}


def test_json_control_character_escaping_has_a_sixfold_wire_budget():
    text = "\0" * 5000
    assert regex_replace(
        text, RegexRule("x", ""), limits=replace(LIMITS, max_output_chars=5000),
    ) == text
