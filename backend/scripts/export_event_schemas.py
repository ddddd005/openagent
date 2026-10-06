"""Export typed temporary event payloads and authorized source-linked views."""

from __future__ import annotations

import argparse
from pathlib import Path

from phase1_agent.contract_json import dumps_pretty
from phase1_agent.event_contracts import EVENT_STREAM_SCHEMAS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "schemas" / "run-events-v1.schema.json",
    )
    args = parser.parse_args()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:phase1-agent:run-events:v1:1",
        "title": "Temporary typed run event payloads and projections",
        "$comment": (
            "RunEvent v2 envelopes are exported with the public contract records. "
            "A projected payload references the source schema but does not claim "
            "to validate against it. Behavior payloads require exact frozen declarations. "
            "Python validators additionally enforce strict JSON integers, stage "
            "correspondence and local-only schema references."
        ),
        "$defs": EVENT_STREAM_SCHEMAS,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dumps_pretty(schema) + "\n", encoding="utf-8")
    print(f"Exported {len(EVENT_STREAM_SCHEMAS)} event schemas to {args.output}")


if __name__ == "__main__":
    main()
