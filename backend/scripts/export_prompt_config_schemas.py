"""Export the separate version-1 prompt configuration record schemas."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_agent.contract_json import dumps_pretty
from phase1_agent.prompt_config import PROMPT_CONFIG_SCHEMAS, PROMPT_CONFIG_V2_SCHEMA


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--output", type=Path,
        default=None,
    )
    args = parser.parse_args()
    if args.output is None:
        args.output = (Path(__file__).resolve().parents[1] / "schemas"
                       / f"prompt-config-v{args.version}.schema.json")
    definitions = dict(PROMPT_CONFIG_SCHEMAS)
    if args.version == 2:
        definitions["config_v2"] = PROMPT_CONFIG_V2_SCHEMA
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:phase1-agent:prompt-config:v{args.version}",
        "title": f"Prompt configuration records (schema version {args.version})",
        "$comment": (
            "Use a named $defs schema. Entity and instance IDs are canonical UUID4. "
            "Python additionally requires integer values, rejects cyclic/non-JSON values "
            "and checks same-scope instance/input-name/override-target uniqueness. "
            "Exact-reference existence, effective override validity and catalog CAS "
            "are checked separately. Disabled references are still validated."
        ),
        "$defs": definitions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps_pretty(schema) + "\n", encoding="utf-8")
    print(f"Exported {len(definitions)} prompt record schemas to {args.output}")


if __name__ == "__main__":
    main()
