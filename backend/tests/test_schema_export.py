import subprocess
import sys
from pathlib import Path

from phase1_agent.contract_json import loads_strict
from phase1_agent.contracts_v2 import CONTRACT_SCHEMAS


def test_schema_export_matches_registered_schemas(tmp_path):
    package = Path(__file__).resolve().parents[1]
    output = tmp_path / "contracts.json"
    subprocess.run(
        [sys.executable, str(package / "scripts" / "export_contract_schemas.py"), "--output", str(output)],
        cwd=package, check=True, capture_output=True, text=True,
    )
    exported = loads_strict(output.read_text(encoding="utf-8"))
    assert exported["$defs"] == CONTRACT_SCHEMAS
