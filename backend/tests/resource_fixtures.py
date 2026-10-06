"""Resource records shared by graph and legacy tests without service imports."""

from uuid import uuid4


def resource(text="global v1"):
    return {
        "schema_version": 1, "kind": "global_prompt", "resource_id": str(uuid4()),
        "revision": 1, "name": "Global", "enabled": True,
        "members": [{"id": str(uuid4()), "name": "Global member", "text": text,
                     "role": "system", "placement": "before", "depth": None, "order": 1, "enabled": True}],
    }
