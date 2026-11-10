"""No Caddy example of the repository may set a negative `flush_interval`.

With `flush_interval -1`, Caddy sends the request upstream without the
client's cancellation and finishes it even when the client has gone
(Caddy docs, `reverse_proxy`: "does not cancel the request to the backend even
if the client disconnects early"). Behind such a proxy Plugboard never sees a
non-streamed client hang up, so it can neither stop the provider call nor free
the chat slot. Streams do not need the setting: Caddy flushes a
`text/event-stream` response at once.

Checked: every Caddyfile, and every fenced code block of a Markdown file that
holds a `reverse_proxy` directive.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "data",
    "graphify-out",
    "__pycache__",
}
_NEGATIVE_FLUSH = re.compile(r"^\s*flush_interval\s+-", re.MULTILINE)
_FENCE = re.compile(r"^\s*(```|~~~)")


def _repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _is_caddyfile(path):
    name = path.name.lower()
    return name.startswith("caddyfile") or path.suffix.lower() in {
        ".caddy",
        ".caddyfile",
    }


def _code_blocks(text):
    block, fence = None, None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if block is None:
            if match:
                block, fence = [], match.group(1)
        elif match and match.group(1) == fence:
            yield "\n".join(block)
            block, fence = None, None
        else:
            block.append(line)


def _caddy_examples():
    for path in _repo_files():
        rel = path.relative_to(ROOT).as_posix()
        if _is_caddyfile(path):
            yield rel, path.read_text(encoding="utf-8")
        elif path.suffix.lower() == ".md":
            text = path.read_text(encoding="utf-8")
            for block in _code_blocks(text):
                if "reverse_proxy" in block:
                    yield rel, block


def test_deployment_guide_has_a_caddy_example():
    """The check below must look at the guide's example, not at nothing."""
    files = {rel for rel, _ in _caddy_examples()}
    assert "docs/guide/deployment.md" in files


def test_no_caddy_example_sets_negative_flush_interval():
    offenders = [
        rel for rel, block in _caddy_examples() if _NEGATIVE_FLUSH.search(block)
    ]
    assert offenders == [], (
        "flush_interval -1 makes Caddy ignore a client that hangs up: "
        f"remove it from {offenders}"
    )


def test_code_blocks_reads_fenced_blocks_only():
    text = "prose flush_interval -1\n```\nreverse_proxy x {\n    flush_interval -1\n}\n```\nafter\n"
    blocks = list(_code_blocks(text))
    assert blocks == ["reverse_proxy x {\n    flush_interval -1\n}"]
    assert _NEGATIVE_FLUSH.search(blocks[0])
