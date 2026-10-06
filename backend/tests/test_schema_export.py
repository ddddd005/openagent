import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

from phase1_agent.contract_json import loads_strict
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS


def test_schema_export_matches_runtime_registry():
    package = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(dir=package / "tests", prefix="contract-export-") as directory:
        output = Path(directory) / "contracts.json"
        subprocess.run(
            [sys.executable, str(package / "scripts" / "export_contract_schemas.py"), "--output", str(output)],
            cwd=package, check=True, capture_output=True, text=True,
        )
        exported = loads_strict(output.read_text(encoding="utf-8"))
        assert exported["$defs"] == CONTRACT_SCHEMAS
        committed = loads_strict((package / "schemas" / "contracts-v2.schema.json").read_text(encoding="utf-8"))
        assert committed == exported
