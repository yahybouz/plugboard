"""BUG-143: redaction runs in bounded time on hostile input.

``RedactingFilter`` runs ``redact()`` on every rendered log line, in the
thread that logs (the event loop), before any truncation. The first
``secret_header`` pattern held two ``\\s*`` around optional characters: an
``authorization:`` followed by a long run of blanks took quadratic time
(about 5 s for 8,000 spaces, over 90 s for 100,000). Every pattern now has to
stay near linear on 100,000-character inputs built to make a regex backtrack.

Each input runs in a child process with a timeout, so a regression fails
here instead of hanging the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
N = 100_000
# Generous for a loaded machine: a linear pass over 100 k characters takes
# a few milliseconds; the quadratic pattern took minutes.
LIMIT_S = 0.5
CHILD_TIMEOUT_S = 20

# Each value is a Python expression of ``n`` building the input.
HOSTILE = {
    "spaces": "' ' * n",
    "tabs": "'\\t' * n",
    "colons": "':' * n",
    "equals": "'=' * n",
    "double quotes": "'\"' * n",
    "single quotes": '"\'" * n',
    "backslashes": "'\\\\' * n",
    "authorization repeated": "'authorization' * (n // 13)",
    "authorization: repeated": "'authorization:' * (n // 14)",
    "authorization: then spaces": "'authorization:' + ' ' * n",
    "authorization= then spaces": "'authorization=' + ' ' * n",
    "authorization then spaces": "'authorization' + ' ' * n",
    "authorization: quote spaces": "'authorization: \"' + ' ' * n",
    "authorization escaped quote": "'authorization\\\\\"' + ' ' * n",
    "x-api-key: then tabs": "'x-api-key:' + '\\t' * n",
    "x-api-key then nbsp": "'X-Api-Key:' + '\\u00a0' * n",
    "bearer then spaces": "'Authorization: bearer' + ' ' * n",
    "blank colon pairs": "'authorization' + ' :' * (n // 2)",
    "tuple then spaces": "'(\"x-api-key\",' + ' ' * n",
    "token= then word": "'token=' + 'a' * n",
    "token= repeated": "'token=' * (n // 6)",
    "token= quote spaces": "'token=\"' + ' ' * n",
    "token=quote unclosed": "'token=\"' + 'a' * n",
    "key=quote repeated": "'key=\"' * (n // 5)",
    "env name repeated": "'API_KEY_' * (n // 8)",
    "env underscores": "'X' + '_' * n + 'KEY='",
    "env token word": "'GITHUB_TOKEN=' + 'a' * n",
    "one long word": "'a' * n",
    "underscores": "'_' * n",
    "dashes": "'-' * n",
    "sk-ant repeated": "'sk-ant-' * (n // 7)",
    "AIza repeated": "'AIza' * (n // 4)",
    "json token unclosed": "'\"token\": \"' + 'a' * n",
    "json token repeated": '\'"token":"\' * (n // 9)',
}

CHILD = """
import json, sys, time
from plugboard.log_redact import redact
n = int(sys.argv[1])
s = eval(sys.argv[2], {"n": n})
start = time.perf_counter()
redact(s)
print(json.dumps({"len": len(s), "s": time.perf_counter() - start}))
"""


@pytest.mark.parametrize("expr", list(HOSTILE.values()), ids=list(HOSTILE))
def test_redact_takes_bounded_time(expr):
    env = dict(os.environ, PLUGBOARD_SKIP_DOTENV="1")
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, str(N), expr],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"redact() still running after {CHILD_TIMEOUT_S} s")
    assert done.returncode == 0, done.stderr[-800:]
    result = json.loads(done.stdout)
    assert result["len"] >= N - 16
    assert result["s"] < LIMIT_S, f"{result['s']:.2f} s for {result['len']} chars"
