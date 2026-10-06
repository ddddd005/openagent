"""Shared resource fixtures keep their identity and mutable records isolated."""

from uuid import UUID

from resource_fixtures import resource


def test_resource_preserves_default_and_custom_text_contract():
    record = resource()
    assert record["schema_version"] == 1
    assert record["kind"] == "global_prompt"
    assert record["revision"] == 1
    assert record["name"] == "Global" and record["enabled"]
    assert UUID(record["resource_id"]).version == 4
    member = record["members"][0]
    assert UUID(member["id"]).version == 4
    assert {key: value for key, value in member.items() if key != "id"} == {
        "name": "Global member", "text": "global v1", "role": "system",
        "placement": "before", "depth": None, "order": 1, "enabled": True,
    }
    assert resource("custom text")["members"][0]["text"] == "custom text"


def test_resource_calls_do_not_share_identity_or_mutable_members():
    first, second = resource(), resource()
    assert first["resource_id"] != second["resource_id"]
    assert first["members"][0]["id"] != second["members"][0]["id"]
    first["members"][0]["text"] = "changed"
    assert second["members"][0]["text"] == "global v1"
