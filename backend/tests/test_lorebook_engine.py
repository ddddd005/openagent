"""Pure lorebook activation, bounded recursion and explicit input provenance."""

from copy import deepcopy

import pytest

import phase1_agent.lorebook_engine as engine
from phase1_agent.content_contracts import default_presentation, text_content
from phase1_agent.context_prompt_v6 import assemble_native_context_prompt
from phase1_agent.contract_errors import ContractValidationError
from phase1_agent.lorebook_engine import (
    default_lorebook_entry, evaluate_lorebook, validate_lorebook_entries, validate_lorebook_entry,
)
from phase1_agent.prompt_lifecycle import (
    lifecycle_prompt_content, make_lifecycle_prompt_item, merge_lifecycle_prompt_materials,
    validate_lifecycle_prompt_materials,
)

from test_context_update import tool_pair
from test_prompt_lifecycle import history_text, ref, uid, view


def entry(number=1, *, presentation=None, **changes):
    result = default_lorebook_entry(uid(number))
    result.update(name=f"entry-{number}", text=f"body-{number}", primary_keywords=["seed"])
    result.update(changes)
    result["presentation"].update(presentation or {})
    return result


def scan(current="seed", *, messages=None, layout=None, materials=None):
    prepared = [] if materials is None else [merge_lifecycle_prompt_materials([
        lifecycle_prompt_content(materials)])]
    return assemble_native_context_prompt(
        prepared, view(messages, layout), text_content(current),
        context_ref=ref(900), current_input_ref=ref(901))


def evaluate(entries, current="seed", *, prompt=None, node_id=None, **options):
    return evaluate_lorebook(entries, scan(current) if prompt is None else prompt,
                             node_id=uid(700) if node_id is None else node_id, **options)


def activated(result):
    return {diagnostic["entry_id"]: diagnostic["activation_round"]
            for diagnostic in result["diagnostics"] if diagnostic["status"] == "activated"}


def material(number, text, *, lorebook=False, **presentation):
    return make_lifecycle_prompt_item(
        uid(710), uid(number), text, {**default_presentation(), **presentation},
        source={"kind": "lorebook" if lorebook else "prompt_item",
                "node_id": uid(710), "member_id": uid(number)},
        lifecycle="per_request", compaction="never")


def history(number, text, role="user"):
    message = history_text(number, role)
    message["blocks"][0]["text"] = text
    return message


def history_layout(*rounds):
    return [{"kind": "history", "round_id": uid(number), "compaction": "allowed"}
            for number in rounds]


def variable(name, value, *, value_type="string", assigned=True):
    return {"registered": True, "name": name, "value_type": value_type,
            "assigned": assigned, "value": value if assigned else None}


def test_default_entry_is_valid_and_output_keeps_stable_presentation_and_lifetime():
    default = default_lorebook_entry()
    assert default["scan_depth"] == 20
    assert default["recursive"] is False
    assert default["probability_enabled"] is False
    assert validate_lorebook_entry(default) == default
    member = entry(presentation={"role": "assistant", "placement": "middle", "depth": 2, "order": -3})
    result = evaluate([member])
    output = result["materials"]
    assert validate_lifecycle_prompt_materials(output) == output
    item = output["items"][0]
    assert item["role"] == "assistant" and item["depth"] == 2 and item["order"] == -3
    assert item["lifecycle"] == "per_request" and item["compaction"] == "never"
    assert item["source"]["kind"] == "lorebook"
    assert item["source"]["node_id"] == uid(700) and item["source"]["member_id"] == member["id"]
    assert item["source"]["origin_item_id"] == item["item_instance_id"]
    assert item["origin_item_ids"] == [item["item_instance_id"]]
    assert evaluate([member])["materials"] == output
    assert evaluate([member], node_id=uid(701))["materials"]["items"][0]["item_instance_id"] != item["item_instance_id"]


@pytest.mark.parametrize("mode,has_keyword,probability_enabled,probability,expected", [
    ("constant", False, False, 0, True),
    ("constant", False, True, 0, False),
    ("constant", False, True, 100, True),
    ("keyword", False, False, 100, False),
    ("keyword", True, False, 0, True),
    ("keyword", True, True, 0, False),
    ("keyword", False, True, 100, False),
    ("keyword", True, True, 100, True),
])
def test_constant_keyword_and_probability_combinations(
        mode, has_keyword, probability_enabled, probability, expected):
    member = entry(mode=mode, probability_enabled=probability_enabled, probability=probability)
    result = evaluate([member], current="seed" if has_keyword else "unrelated")
    assert bool(result["materials"]["items"]) is expected


@pytest.mark.parametrize("mode", ["constant", "keyword"])
def test_disabled_entry_is_never_scanned_sampled_or_added_to_recursive_materials(mode):
    disabled = entry(mode=mode, probability_enabled=True, probability=50,
                     text="next", presentation={"enabled": False})
    follower = entry(2, recursive=True, primary_keywords=["next"])
    result = evaluate([disabled, follower], random_value=lambda: pytest.fail("Disabled probability was sampled"))
    assert result["materials"]["items"] == []
    assert result["diagnostics"][0]["status"] == "disabled"
    assert result["diagnostics"][0]["probability_passed"] is None


@pytest.mark.parametrize("rule", engine.KEYWORD_RULES)
@pytest.mark.parametrize("primary,secondary", [(False, False), (True, False), (False, True), (True, True)])
def test_keyword_group_truth_table_and_any_match_within_each_group(rule, primary, secondary):
    current = " ".join(word for word, yes in (("main", primary), ("second", secondary)) if yes)
    member = entry(primary_keywords=["absent-main", "main"], secondary_keywords=["absent-secondary", "second"],
                   keyword_rule=rule)
    result = evaluate([member], current=current)
    expected = (primary or secondary if rule == "primary_or_secondary" else primary and secondary
                if rule == "primary_and_secondary" else primary and not secondary)
    assert bool(result["materials"]["items"]) is expected
    assert result["diagnostics"][0]["primary_matched"] is primary
    assert result["diagnostics"][0]["secondary_matched"] is secondary


@pytest.mark.parametrize("primary,secondary,rule,expected", [
    ([], [], "primary_or_secondary", False),
    (["seed"], [], "primary_or_secondary", True),
    ([], ["seed"], "primary_or_secondary", True),
    (["seed"], [], "primary_and_secondary", False),
    ([], ["seed"], "primary_and_secondary", False),
    (["seed"], [], "primary_and_not_secondary", True),
    ([], ["seed"], "primary_and_not_secondary", False),
    ([""], [""], "primary_or_secondary", False),
])
def test_empty_keyword_groups_are_false(primary, secondary, rule, expected):
    result = evaluate([entry(primary_keywords=primary, secondary_keywords=secondary, keyword_rule=rule)])
    assert bool(result["materials"]["items"]) is expected


@pytest.mark.parametrize("keyword,current,case_sensitive,expected", [
    ("DRAGON", "a dragon arrives", False, True),
    ("DRAGON", "a dragon arrives", True, False),
    ("cat", "concatenate", False, True),
    ("[a-z]+", "letters", False, False),
    ("[a-z]+", "literal [a-z]+ here", False, True),
    ("\u9f99\u65cf", "\u8fd9\u91cc\u6709\u9f99\u65cf", False, True),
    ("STRASSE", "Stra\u00dfe", False, True),
])
def test_casefold_literal_substring_and_chinese_matching(keyword, current, case_sensitive, expected):
    result = evaluate([entry(primary_keywords=[keyword], case_sensitive=case_sensitive)], current=current)
    assert bool(result["materials"]["items"]) is expected


def test_keywords_do_not_bridge_message_or_text_block_boundaries():
    initial = scan("def", messages=[history(10, "abc", "assistant")], layout=history_layout(50))
    assert not evaluate([entry(primary_keywords=["abcdef"])], prompt=initial)["materials"]["items"]
    separate = history(11, "abc", "assistant")
    separate["blocks"].append({"kind": "text", "text": "def"})
    initial = scan("", messages=[separate], layout=history_layout(50))
    assert not evaluate([entry(primary_keywords=["abcdef"])], prompt=initial)["materials"]["items"]
    assert evaluate([entry(primary_keywords=["abc"], secondary_keywords=["def"],
                           keyword_rule="primary_and_secondary")], prompt=initial)["materials"]["items"]


@pytest.mark.parametrize("value,value_type,keyword", [
    ("dragon", "string", "dragon"),
    (0, "integer", "0"),
    (1.25, "number", "1.25"),
    (False, "boolean", "false"),
])
def test_readonly_variable_keywords_use_current_scalar_values(value, value_type, keyword):
    values = [variable("target", value, value_type=value_type)]
    member = entry(primary_keywords=["{{target}}"])
    before = deepcopy((member, values))
    result = evaluate([member], current=f"prefix {keyword} suffix", variables=values)
    assert result["materials"]["items"]
    assert (member, values) == before
    assert member["primary_keywords"] == ["{{target}}"]


@pytest.mark.parametrize("values", [None, [], [variable("target", "")], [variable("target", None, assigned=False)]])
def test_missing_unassigned_or_empty_variable_does_not_match_literal_remainder(values):
    member = entry(primary_keywords=["{{target}}suffix"])
    assert not evaluate([member], current="suffix", variables=values)["materials"]["items"]
    member["primary_keywords"].append("suffix")
    assert evaluate([member], current="suffix", variables=values)["materials"]["items"]


def test_variable_expansion_is_single_pass_and_does_not_interpret_assignments():
    values = [variable("target", "{{other}}"), variable("other", "dragon")]
    assert not evaluate([entry(primary_keywords=["{{target}}"])], current="dragon", variables=values)["materials"]["items"]
    assert evaluate([entry(primary_keywords=["{{target}}"])], current="{{other}}", variables=values)["materials"]["items"]
    assert not evaluate([entry(primary_keywords=["{{target=dragon}}"])], current="dragon", variables=values)["materials"]["items"]
    assert values[0]["value"] == "{{other}}"


@pytest.mark.parametrize("values", [
    [variable("target", "a"), variable("target", "b")],
    [variable("target", 1)],
    [{"registered": False, "name": "target", "assigned": False, "value_type": None, "value": None}],
    {"target": "dragon"},
])
def test_illegal_variable_objects_or_duplicate_names_are_not_silently_ignored(values):
    with pytest.raises(ContractValidationError):
        evaluate([entry()], variables=values)


@pytest.mark.parametrize("depth,target,expected", [
    (0, "current", False), (1, "current", True), (1, "reply", False),
    (2, "reply", True), (2, "root", False), (3, "root", True), (20, "root", True),
])
def test_scan_depth_counts_latest_root_then_complete_delta_not_rounds(depth, target, expected):
    initial = scan("current", messages=[history(10, "root"), history(11, "reply", "assistant")],
                   layout=history_layout(50, 50))
    result = evaluate([entry(scan_depth=depth, primary_keywords=[target])], prompt=initial)
    assert bool(result["materials"]["items"]) is expected


def test_default_twenty_floors_excludes_the_twenty_first_not_the_tenth_round():
    messages = [history(100 + index, f"marker-{index:02d}", "user" if index % 2 == 0 else "assistant")
                for index in range(20)]
    initial = scan("latest", messages=messages, layout=history_layout(*[50 + index // 2 for index in range(20)]))
    result = evaluate([entry(1, primary_keywords=["marker-00"]), entry(2, primary_keywords=["marker-01"])],
                      prompt=initial)
    assert activated(result) == {uid(2): 0}


def test_tool_protocol_does_not_add_floors_or_keyword_text_and_whole_delta_is_scanned():
    call, result = tool_pair()
    call["blocks"][0]["text"] = "model-first"
    call["blocks"][1].update(raw_arguments='{"keyword":"hidden-argument"}',
                             parsed_arguments={"keyword": "hidden-argument"})
    result["blocks"][0].update(content={"keyword": "hidden-result"}, model_visible_text="hidden-result")
    reply = history(55, "model-last", "assistant")
    messages = [history(10, "old-root"), call, result, reply]
    layout = history_layout(60, 60, 60, 60)
    layout[1]["compaction"] = layout[2]["compaction"] = "never"
    initial = scan("latest", messages=messages, layout=layout)
    members = [entry(index, primary_keywords=[text], scan_depth=2)
               for index, text in enumerate(("model-first", "model-last", "old-root", "hidden-argument", "hidden-result"), 1)]
    assert activated(evaluate(members, prompt=initial)) == {uid(1): 0, uid(2): 0}


def test_presets_checkpoint_and_once_text_never_impersonate_real_conversation():
    checkpoint = {"schema_version": 4, "message_id": uid(5), "role": "user",
                  "source": {"kind": "context_checkpoint", "compaction_id": uid(6)},
                  "blocks": [{"kind": "text", "text": "checkpoint-word"}]}
    initial = scan("actual", messages=[checkpoint, history(7, "once-word")], layout=[
        {"kind": "summary", "round_id": None, "compaction": "allowed"},
        {"kind": "once", "round_id": uid(50), "compaction": "allowed"},
    ], materials=[material(1, "preset-user", role="user"), material(2, "preset-assistant", role="assistant")])
    members = [entry(index, recursive=True, primary_keywords=[text])
               for index, text in enumerate(("checkpoint-word", "once-word", "preset-user", "preset-assistant"), 1)]
    assert not activated(evaluate(members, prompt=initial))


def test_name_metadata_source_and_role_labels_are_not_scanned():
    first = entry(1, mode="constant", name="hidden-name", text="ordinary",
                  metadata={"secret": "hidden-metadata"}, presentation={"role": "assistant"})
    members = [first, *[entry(index, recursive=True, primary_keywords=[text])
                       for index, text in enumerate(("hidden-name", "hidden-metadata", "lorebook", "assistant"), 2)]]
    assert activated(evaluate(members)) == {uid(1): 0}


@pytest.mark.parametrize("recursive,depth,expected", [(False, 20, False), (True, 0, True), (True, 1, True)])
def test_upstream_lorebook_is_explicit_recursive_material_independent_of_insertion_depth(recursive, depth, expected):
    initial = scan("unrelated", materials=[material(3, "upstream-key", lorebook=True,
                                                  placement="middle", depth=999, role="user")])
    result = evaluate([entry(recursive=recursive, scan_depth=depth, primary_keywords=["upstream-key"])], prompt=initial)
    assert bool(result["materials"]["items"]) is expected
    if expected:
        assert result["diagnostics"][0]["activation_round"] == 0
        assert result["materials"]["items"][0]["text"] == "body-1"


def test_disabled_upstream_lorebook_and_ordinary_same_role_materials_do_not_participate():
    initial = scan("", materials=[material(1, "upstream-key", lorebook=True, enabled=False),
                                 material(2, "ordinary-key", role="assistant")])
    members = [entry(1, recursive=True, primary_keywords=["upstream-key"]),
               entry(2, recursive=True, primary_keywords=["ordinary-key"])]
    assert not activated(evaluate(members, prompt=initial))


@pytest.mark.parametrize("reverse", [False, True])
def test_initial_match_then_exactly_three_synchronous_additional_rounds(reverse):
    members = [entry(index, text=f"token-{index}", primary_keywords=["seed" if index == 1 else f"token-{index - 1}"],
                     recursive=True) for index in range(1, 6)]
    result = evaluate(list(reversed(members)) if reverse else members)
    assert activated(result) == {uid(1): 0, uid(2): 1, uid(3): 2, uid(4): 3}
    assert result["recursive_rounds"] == 3
    assert uid(5) not in activated(result)


def test_nonrecursive_follower_never_uses_local_activated_text_and_cycles_do_not_duplicate():
    members = [entry(1, text="second", primary_keywords=["seed", "first"], recursive=True),
               entry(2, text="first", primary_keywords=["second"], recursive=True),
               entry(3, text="not-active", primary_keywords=["second"], recursive=False)]
    result = evaluate(members)
    assert activated(result) == {uid(1): 0, uid(2): 1}
    assert len(result["materials"]["items"]) == 2
    assert result["recursive_rounds"] < 3


def test_probability_is_sampled_once_even_when_keyword_is_found_in_a_later_recursive_round():
    samples = []

    def draw():
        samples.append(1)
        return 0.25

    members = [entry(1, text="next"), entry(2, text="last", recursive=True, primary_keywords=["next"],
                                         probability_enabled=True, probability=50),
               entry(3, recursive=True, primary_keywords=["last"], probability_enabled=True, probability=50)]
    result = evaluate(members, random_value=draw)
    assert activated(result) == {uid(1): 0, uid(2): 1, uid(3): 2}
    assert len(samples) == 2


def test_probability_rejected_body_cannot_activate_a_follower_and_threshold_is_strict():
    result = evaluate([entry(1, mode="constant", text="hidden", probability_enabled=True, probability=50),
                       entry(2, recursive=True, primary_keywords=["hidden"])], random_value=lambda: 0.5)
    assert not activated(result)
    assert result["diagnostics"][0]["status"] == "probability_rejected"


@pytest.mark.parametrize("value", [-0.1, 1, True, float("nan"), "0.2"])
def test_invalid_random_value_is_rejected(value):
    with pytest.raises(ContractValidationError) as caught:
        evaluate([entry(probability_enabled=True, probability=50)], random_value=lambda: value)
    assert caught.value.reason_code == "lorebook_random_invalid"


def test_each_node_has_fresh_recursive_rounds_and_only_explicit_upstream_input():
    first = evaluate([entry(1, text="a1"), entry(2, text="a2", recursive=True, primary_keywords=["a1"]),
                      entry(3, text="a3", recursive=True, primary_keywords=["a2"]),
                      entry(4, text="a4", recursive=True, primary_keywords=["a3"])])
    initial = scan("", materials=first["materials"]["items"])
    members = [entry(index, text=f"b{index}", primary_keywords=["a4" if index == 1 else f"b{index - 1}"],
                     recursive=True, scan_depth=0) for index in range(1, 6)]
    result = evaluate(members, prompt=initial, node_id=uid(701))
    assert activated(result) == {uid(1): 0, uid(2): 1, uid(3): 2, uid(4): 3}
    assert result["recursive_rounds"] == 3
    assert all(item["source"]["node_id"] == uid(701) for item in result["materials"]["items"])
    assert not activated(evaluate(members, current="", node_id=uid(702)))


def test_merged_upstream_origins_preserve_lorebook_provenance_without_repeating_output():
    first = material(1, "upstream-key", lorebook=True)
    second = material(2, "upstream-key", lorebook=True)
    initial = scan("", materials=[first, second, first])
    result = evaluate([entry(recursive=True, primary_keywords=["upstream-key"])], prompt=initial)
    assert len(result["materials"]["items"]) == 1
    assert result["materials"]["items"][0]["text"] == "body-1"
    assert len(initial["items"]) == 1 and len(initial["items"][0]["origin_item_ids"]) == 2


def test_all_input_configuration_prompt_and_variables_remain_unchanged():
    members = [entry(1, primary_keywords=["{{target}}"], text="next"),
               entry(2, recursive=True, primary_keywords=["next"])]
    variables = [variable("target", "seed")]
    prompt = scan(materials=[material(3, "plain material")])
    originals = deepcopy((members, prompt, variables))
    evaluate(members, prompt=prompt, variables=variables)
    assert (members, prompt, variables) == originals


@pytest.mark.parametrize("changes", [
    {"mode": "other"}, {"scan_depth": -1}, {"scan_depth": True}, {"scan_depth": 4097},
    {"probability": -1}, {"probability": 101}, {"probability": True},
    {"primary_keywords": [0]}, {"primary_keywords": ["x"] * 129},
    {"primary_keywords": ["x" * 4097]}, {"text": "x" * 1_000_001},
    {"name": "x" * 129}, {"extra": True}, {"keyword_rule": "and"},
])
def test_entry_configuration_limits_are_strict(changes):
    with pytest.raises(ContractValidationError):
        validate_lorebook_entry(entry(**changes))


def test_duplicate_entry_identity_and_fake_tool_presentation_are_rejected():
    with pytest.raises(ContractValidationError) as caught:
        validate_lorebook_entries([entry(), entry()])
    assert caught.value.reason_code == "lorebook_duplicate_entry"
    with pytest.raises(ContractValidationError):
        validate_lorebook_entry(entry(presentation={"role": "tool"}))
    with pytest.raises(ContractValidationError):
        validate_lorebook_entry(entry(presentation={"placement": "before", "depth": 1}))


def test_scan_requires_exact_native_projection_and_does_not_reconstruct_missing_history():
    with pytest.raises(ContractValidationError):
        evaluate([], prompt={"schema_version": 2, "kind": "workflow.prompt"})
    prompt = scan()
    prompt["messages"][-1]["blocks"][0]["text"] = "modified"
    with pytest.raises(ContractValidationError):
        evaluate([entry()], prompt=prompt)


def test_character_work_expansion_and_output_budgets_fail_explicitly(monkeypatch):
    monkeypatch.setattr(engine, "MAX_SCAN_CHAR_WORK", 5)
    with pytest.raises(ContractValidationError) as caught:
        evaluate([entry()], current="a lengthy seed")
    assert caught.value.reason_code == "lorebook_budget_exceeded"
    monkeypatch.setattr(engine, "MAX_SCAN_CHAR_WORK", 64_000_000)
    monkeypatch.setattr(engine, "MAX_RESOLVED_KEYWORD_CHARS", 3)
    with pytest.raises(ContractValidationError) as caught:
        evaluate([entry(primary_keywords=["{{target}}"])], variables=[variable("target", "dragon")])
    assert caught.value.reason_code == "lorebook_budget_exceeded"


def test_empty_group_and_no_match_are_canonical_empty_materials():
    expected = {"schema_version": 1, "kind": "workflow.prompt-materials", "items": []}
    empty = evaluate([])
    assert empty == {"materials": expected, "diagnostics": [], "recursive_rounds": 0}
    assert evaluate([entry()], current="other")["materials"] == expected
