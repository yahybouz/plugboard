"""No Caddy example of the repository may set a negative `flush_interval`.

Plugboard does not need it: each of its streams is `text/event-stream` or has
no `Content-Length`, and Caddy sends such a response to the client at once
without it. From Caddy 2.6 to 2.8, `flush_interval -1` also kept Caddy from
cancelling the request to the backend when the client hung up (Caddy pull
request 4952, reverted in Caddy 2.9.0). Plugboard then did not see the client
leave during a request that is not streamed, or during a stream before its
first byte, so it could neither stop the provider call nor free the chat slot.

Checked: every Caddyfile, and every fenced code block of a Markdown file that
holds a `reverse_proxy` directive, in README.md, docs/ and deploy/.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NEGATIVE_FLUSH = re.compile(r"^\s*flush_interval\s+-", re.MULTILINE)
_FENCE = re.compile(r"^\s*(```|~~~)")


def _repo_files():
    yield ROOT / "README.md"
    for folder in ("docs", "deploy"):
        yield from sorted(p for p in (ROOT / folder).rglob("*") if p.is_file())


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


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
            yield rel, _read(path)
        elif path.suffix.lower() == ".md":
            text = _read(path)
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
        "Plugboard does not need a negative flush_interval, and with -1 "
        "Caddy 2.6 to 2.8 ignore a client that hangs up: "
        f"remove it from {offenders}"
    )


def test_code_blocks_reads_fenced_blocks_only():
    text = "prose flush_interval -1\n```\nreverse_proxy x {\n    flush_interval -1\n}\n```\nafter\n"
    blocks = list(_code_blocks(text))
    assert blocks == ["reverse_proxy x {\n    flush_interval -1\n}"]
    assert _NEGATIVE_FLUSH.search(blocks[0])
