"""The API contract loads without FastAPI (BUG-7).

`scripts/export_contract.py --mcp --check` runs in the MCP environment, which
has pydantic but not FastAPI. Since 4.4.0 api_contract imported
EmbeddingRequest from embeddings_api, which imports FastAPI, so the MCP
contract step failed in CI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_without_fastapi(code: str) -> subprocess.CompletedProcess:
    # sys.modules[name] = None makes any later `import name` fail.
    guard = "import sys; sys.modules['fastapi'] = None; "
    return subprocess.run(
        [sys.executable, "-c", guard + code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_api_contract_imports_without_fastapi():
    run = _run_without_fastapi(
        "import plugboard.api_contract as c; print(c.CONTRACT_VERSION)"
    )
    assert run.returncode == 0, run.stderr


def test_mcp_contract_check_runs_without_fastapi():
    run = _run_without_fastapi(
        "import runpy; sys.argv = ['export_contract.py', '--mcp', '--check']; "
        "runpy.run_path('scripts/export_contract.py', run_name='__main__')"
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_embeddings_api_still_exposes_the_request_model():
    from plugboard import embeddings_api, embeddings_schema

    assert embeddings_api.EmbeddingRequest is embeddings_schema.EmbeddingRequest
