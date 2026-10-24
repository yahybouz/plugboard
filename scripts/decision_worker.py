#!/usr/bin/env python3
"""One offline official decision inference, JSON stdin/stdout; no HTTP listener."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import json
import math
import os
from pathlib import Path
import socket
import shutil
import tempfile
import sys


def check_laya_budget(tok, common, agent_class, request: dict, cfg: dict) -> None:
    """Reject every upstream truncation point, including per-option 48-token caps."""

    def tokenize(text):
        return tok(text, add_special_tokens=False)["input_ids"]

    state_tokens = tokenize(request["state"].replace(tok.mask_token, " "))
    for question in request["questions"].values():
        q = agent_class._to_internal(question)
        options = common.render_options(q)
        lengths = [
            len(tokenize(" " + opt.replace(tok.mask_token, " "))) for opt in options
        ]
        remaining = cfg["head_max_len"] - sum(1 + n for n in lengths)
        instruction = tokenize(
            f"{q['t']} question: {q['ins'].replace(tok.mask_token, ' ')}"
        )
        if (
            any(n > 48 for n in lengths)
            or remaining < 16
            or len(instruction) > max(8, remaining)
        ):
            raise ValueError("context_limit")
        head, _, _ = common.build_head(tok, q, cfg["head_max_len"])
        if len(state_tokens) + len(head) + 1 > cfg["max_len"]:
            raise ValueError("context_limit")


def canonical_answers(answers: dict, questions: dict) -> dict:
    fields = {
        "choice": ("type", "choice", "confidence", "probabilities"),
        "noul": ("type", "noul"),
        "score": ("type", "score", "confidence", "probabilities", "legend"),
    }
    result = {
        qid: {key: answers[qid][key] for key in fields[q["type"]]}
        for qid, q in questions.items()
    }
    for answer in result.values():
        if answer["type"] == "noul":
            continue
        dist = answer["probabilities"]
        values = list(dist.values())
        if not all(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and 0 <= v <= 1
            for v in values
        ):
            continue
        total = math.fsum(values)
        # The official runtime serializes to four decimals. Repair only
        # the bounded serialization residue; gateway rejects corrupt distributions.
        if total > 0 and abs(total - 1) <= 0.001:
            answer["probabilities"] = {
                key: value / total for key, value in dist.items()
            }
            if answer["type"] == "score":
                expectation = math.fsum(
                    int(key) * value for key, value in answer["probabilities"].items()
                )
                # Each rounded probability and the rounded source score can
                # differ by <=5e-5. Include the exact renormalization shift.
                rounding_bound = 0.00005 * (1 + sum(int(key) for key in dist))
                rounding_bound += abs(total - 1) * abs(expectation)
                if abs(answer["score"] - expectation) <= rounding_bound + 1e-12:
                    answer["score"] = expectation
    return {"answers": result}


def snapshot(root: Path, repo: str, revision: str) -> Path:
    return (
        root / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
    )


@contextmanager
def staged_laya_checkpoint(checkpoint: Path, root: Path):
    # Official Laya fixes tokenizer configuration in place. Keep the verified
    # snapshot immutable; only its small configurations are copied for the call.
    with tempfile.TemporaryDirectory(prefix=".worker-laya-", dir=root) as directory:
        staged = Path(directory)
        for name in [
            "model.safetensors",
            "rl_agent_config.json",
            "encoder/config.json",
            "tokenizer/tokenizer.json",
            "tokenizer/tokenizer_config.json",
        ]:
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in {"model.safetensors", "tokenizer/tokenizer.json"}:
                os.link(checkpoint / name, target)
            else:
                shutil.copyfile(checkpoint / name, target)
        yield staged


def run_official(model_id: str, root: Path, request: dict, spec: dict) -> dict:
    if model_id not in {"laya", "laya-multilingual"} or spec.get("runtime") != "laya":
        raise ValueError("unknown_model")
    sys.path.insert(
        0, str(root / "runtime" / spec["runtime"] / spec["source_revision"])
    )
    from transformers import AutoTokenizer

    checkpoint = snapshot(root, spec["repo"], spec["revision"])
    from laya.agent import Agent, _fix_tokenizer_config
    from laya import common

    with staged_laya_checkpoint(
        checkpoint, Path(os.environ.get("TMPDIR", str(root)))
    ) as staged:
        _fix_tokenizer_config(str(staged))
        cfg = json.loads((staged / "rl_agent_config.json").read_text())
        tok = AutoTokenizer.from_pretrained(
            str(staged / "tokenizer"),
            local_files_only=True,
            trust_remote_code=False,
        )
        check_laya_budget(tok, common, Agent, request, cfg)
        agent = Agent(str(staged), device="cpu", compile=False)
        return canonical_answers(
            agent.predict(request["state"], request["questions"])["answers"],
            request["questions"],
        )


def _deny_network(*args, **kwargs):
    raise OSError("offline_worker")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=["laya", "laya-multilingual"])
    parser.add_argument("--assets", required=True, type=Path)
    args = parser.parse_args()
    # -I ignores user site/PYTHONPATH; only this reviewed repository is added.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from plugboard.decision_assets import read_manifest, verify_assets

    ready, _ = verify_assets(args.model, args.assets, deep=True)
    if not ready:
        return 1
    # Refuse smaller machines instead of pretending suitability for the 4 GiB VPS.
    if hasattr(os, "sysconf"):
        try:
            if os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") < 8 * 1024**3:
                return 1
        except (ValueError, OSError):
            pass
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    output = sys.stdout
    try:
        body = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(body) > 64 * 1024:
            return 1
        request = json.loads(body)
        # Upstream libraries may print paths/warnings: never forward them through stdout.
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            result = run_official(
                args.model, args.assets, request, read_manifest()["models"][args.model]
            )
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded.encode()) > 128 * 1024:
            return 1
        output.write(encoded)
        return 0
    except ValueError as exc:
        return 42 if str(exc) == "context_limit" else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
