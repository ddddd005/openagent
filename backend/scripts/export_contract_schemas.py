"""Export the v2 record registry for inspection and offline consumers."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_agent.contract_json import dumps_pretty
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas" / "contracts-v2.schema.json",
    )
    args = parser.parse_args()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:phase1-agent:contracts:v2:1",
        "title": "Versioned workflow contract records",
        "$comment": (
            "Use a named $defs schema. uuid4 and utc_millis require format checkers. "
            "Python validators additionally enforce strict JSON integer representation, "
            "raw tool argument agreement, local-only embedded schemas and "
            "typed v2 event payload semantics. Custom event payloads additionally "
            "require their frozen behavior declarations. "
            "Reference/lifecycle and transactional checks are separate."
        ),
        "$defs": CONTRACT_SCHEMAS,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps_pretty(schema) + "\n", encoding="utf-8")
    print(f"Exported {len(CONTRACT_SCHEMAS)} record schemas to {args.output}")


if __name__ == "__main__":
    main()
