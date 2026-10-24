"""Real subprocess boundary tests with synthetic ports; no ML inference claimed."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest


def runtime_module():
    import plugboard.decision_runtime as runtime

    return runtime


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    mod = runtime_module()
    monkeypatch.setenv("PLUGBOARD_DECISION_ASSETS", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_DECISION_PYTHON", sys.executable)
    monkeypatch.setattr(
        mod.decision_assets, "verify_assets", lambda *a, **kw: (True, "ready")
    )
    return mod


def command(monkeypatch, runtime, code):
    monkeypatch.setattr(
        runtime, "_command", lambda model, root, python: [python, "-c", code]
    )


def test_unconfigured_runtime_unavailable(monkeypatch):
    runtime = runtime_module()
    monkeypatch.delenv("PLUGBOARD_DECISION_PYTHON", raising=False)
    assert runtime.available("laya") == (False, "runtime_not_configured")
    assert runtime.available("../unsafe")[0] is False


@pytest.mark.asyncio
async def test_real_subprocess_receives_request_without_gateway_secrets(
    runtime, monkeypatch
):
    monkeypatch.setenv("API_KEY", "secret-client")
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "secret-upstream")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-cloud")
    command(
        monkeypatch,
        runtime,
        "import os,json,sys; r=json.load(sys.stdin); print(json.dumps({'answers':{},'offline':os.environ.get('HF_HUB_OFFLINE'),'keys':[k for k in os.environ if 'KEY' in k],'state':r['state']}))",
    )
    result = await runtime.infer("laya", {"state": "synthetic"})
    assert result == {"answers": {}, "offline": "1", "keys": [], "state": "synthetic"}


@pytest.mark.asyncio
async def test_runtime_timeout_kills_owned_process_group(
    runtime, monkeypatch, tmp_path
):
    pidfile = tmp_path / "owned.pid"
    command(
        monkeypatch,
        runtime,
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(10)",
    )
    monkeypatch.setattr(runtime, "TIMEOUT_SECONDS", 0.15)
    with pytest.raises(runtime.RuntimeFailure, match="timeout"):
        await runtime.infer("laya", {"state": "synthetic"})
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_busy_worker_rejects_second_model(runtime, monkeypatch):
    command(
        monkeypatch, runtime, "import time; time.sleep(.15); print('{\"answers\":{}}')"
    )
    first = asyncio.create_task(runtime.infer("laya", {"state": "synthetic"}))
    await asyncio.sleep(0.03)
    with pytest.raises(runtime.RuntimeFailure, match="busy"):
        await runtime.infer("laya-multilingual", {"state": "synthetic"})
    assert await first == {"answers": {}}


@pytest.mark.asyncio
async def test_runtime_errors_are_bounded_and_redacted(runtime, monkeypatch):
    command(
        monkeypatch,
        runtime,
        "import sys; print('private-state secret-token',file=sys.stderr); sys.exit(2)",
    )
    with pytest.raises(runtime.RuntimeFailure) as exc:
        await runtime.infer("laya", {"state": "private-state"})
    assert str(exc.value) == "worker_failed"


@pytest.mark.asyncio
async def test_worker_output_budget_is_enforced(runtime, monkeypatch):
    command(monkeypatch, runtime, "print('x'*200000)")
    with pytest.raises(runtime.RuntimeFailure, match="output_limit"):
        await runtime.infer("laya", {"state": "synthetic"})


@pytest.mark.asyncio
async def test_invalid_worker_json_is_normalized(runtime, monkeypatch):
    command(monkeypatch, runtime, "print('private-invalid-response')")
    with pytest.raises(runtime.RuntimeFailure, match="invalid_output"):
        await runtime.infer("laya", {"state": "synthetic"})


@pytest.mark.asyncio
async def test_cancellation_cleans_process_and_releases_admission(
    runtime, monkeypatch, tmp_path
):
    pidfile = tmp_path / "cancel.pid"
    command(
        monkeypatch,
        runtime,
        f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(10)",
    )
    task = asyncio.create_task(runtime.infer("laya", {"state": "synthetic"}))
    for _ in range(40):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    command(monkeypatch, runtime, "print('{\"answers\":{}}')")
    assert await runtime.infer("laya", {"state": "synthetic"}) == {"answers": {}}


def load_worker():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "decision_worker_test",
        Path(__file__).resolve().parents[1] / "scripts" / "decision_worker.py",
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_laya_budget_rejects_state_or_question_truncation():
    worker = load_worker()

    class Tokenizer:
        mask_token = "[MASK]"

        def __call__(self, text, **kwargs):
            return {"input_ids": text.split()}

    class Common:
        @staticmethod
        def render_options(q):
            return list(q["criteria"].values())

        @staticmethod
        def build_head(tok, q, head_max_len):
            return [0] * 12, [0, 1], {}

    class Agent:
        @staticmethod
        def _to_internal(q):
            return {**q, "ins": q["instructions"], "t": q["type"]}

    cfg = {"max_len": 20, "head_max_len": 192}
    req = {
        "state": "small state",
        "questions": {
            "q": {
                "type": "choice",
                "instructions": "pick",
                "criteria": {"a": "yes", "b": "no"},
            }
        },
    }
    worker.check_laya_budget(Tokenizer(), Common, Agent, req, cfg)
    req["state"] = "word " * 20
    with pytest.raises(ValueError, match="context_limit"):
        worker.check_laya_budget(Tokenizer(), Common, Agent, req, cfg)
    req["state"] = "small"
    req["questions"]["q"]["criteria"]["a"] = "word " * 49
    with pytest.raises(ValueError, match="context_limit"):
        worker.check_laya_budget(Tokenizer(), Common, Agent, req, cfg)


def test_worker_projects_only_canonical_fields():
    worker = load_worker()
    result = worker.canonical_answers(
        {
            "q": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.8,
                "probabilities": {"a": 0.9, "b": 0.1},
                "action": {"act_probability": 0.99},
                "answer_confidence": 0.9,
            }
        },
        {"q": {"type": "choice"}},
    )
    assert result == {
        "answers": {
            "q": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.8,
                "probabilities": {"a": 0.9, "b": 0.1},
            }
        }
    }


@pytest.mark.parametrize("count", [3, 7])
def test_worker_normalizes_only_small_official_rounding_residue(count):
    worker = load_worker()
    dist = {str(i): round(1 / count, 4) for i in range(count)}
    answers = {
        "q": {
            "type": "choice",
            "choice": "0",
            "confidence": 0.1234,
            "probabilities": dist,
        }
    }
    result = worker.canonical_answers(answers, {"q": {"type": "choice"}})["answers"][
        "q"
    ]
    assert abs(sum(result["probabilities"].values()) - 1) < 1e-12
    assert result["confidence"] == 0.1234


def test_worker_recomputes_score_after_rounding_normalization():
    worker = load_worker()
    raw = {
        "type": "score",
        "score": 1.0,
        "confidence": 0.4,
        "legend": {"0": "low", "1": "mid", "2": "high"},
        "probabilities": {"0": 0.3333, "1": 0.3333, "2": 0.3333},
    }
    result = worker.canonical_answers({"q": raw}, {"q": {"type": "score"}})["answers"][
        "q"
    ]
    assert result["score"] == sum(
        int(k) * v for k, v in result["probabilities"].items()
    )
    assert result["confidence"] == 0.4


def test_worker_does_not_repair_corrupt_upstream_distribution():
    worker = load_worker()
    raw = {
        "type": "choice",
        "choice": "a",
        "confidence": 0.4,
        "probabilities": {"a": 0.1, "b": 0.1},
    }
    result = worker.canonical_answers({"q": raw}, {"q": {"type": "choice"}})["answers"][
        "q"
    ]
    assert result["probabilities"] == {"a": 0.1, "b": 0.1}


def official_ports(monkeypatch):
    from types import ModuleType, SimpleNamespace

    worker = load_worker()
    monkeypatch.setattr(sys, "path", list(sys.path))
    tok = object()
    tokenizer_calls = []
    transformers = ModuleType("transformers")

    def load_tokenizer(*args, **kwargs):
        tokenizer_calls.append((args, kwargs))
        return tok

    transformers.AutoTokenizer = SimpleNamespace(from_pretrained=load_tokenizer)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return worker, tok, tokenizer_calls


@pytest.mark.parametrize("model_id", ["laya", "laya-multilingual"])
def test_official_laya_wiring_uses_local_agent_and_preserves_confidence(
    monkeypatch, tmp_path, model_id
):
    from types import ModuleType

    worker, tok, tokenizer_calls = official_ports(monkeypatch)
    spec = {
        "runtime": "laya",
        "source_revision": "a" * 40,
        "repo": "fixture/laya",
        "revision": "b" * 40,
    }
    checkpoint = worker.snapshot(tmp_path, spec["repo"], spec["revision"])
    checkpoint.mkdir(parents=True)
    (checkpoint / "rl_agent_config.json").write_text(
        '{"max_len":512,"head_max_len":192}'
    )
    for name in [
        "model.safetensors",
        "encoder/config.json",
        "tokenizer/tokenizer.json",
        "tokenizer/tokenizer_config.json",
    ]:
        target = checkpoint / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    calls = []

    class Agent:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))

        def predict(self, state, questions):
            assert state == "synthetic"
            return {
                "answers": {
                    "q": {"type": "noul", "noul": 0.9, "confidence": 0.9, "action": {}}
                }
            }

    laya = ModuleType("laya")
    laya.common = object()
    agent = ModuleType("laya.agent")
    agent.Agent = Agent
    fixed_paths = []
    agent._fix_tokenizer_config = lambda path: fixed_paths.append(path)
    monkeypatch.setitem(sys.modules, "laya", laya)
    monkeypatch.setitem(sys.modules, "laya.agent", agent)
    budget_calls = []
    monkeypatch.setattr(
        worker, "check_laya_budget", lambda *args: budget_calls.append(args)
    )
    req = {
        "state": "synthetic",
        "questions": {"q": {"type": "noul", "instructions": "true?"}},
    }
    assert worker.run_official(model_id, tmp_path, req, spec) == {
        "answers": {"q": {"type": "noul", "noul": 0.9}}
    }
    assert len(calls) == 1
    staged = Path(calls[0][0])
    assert staged != checkpoint
    assert calls[0][1] == {"device": "cpu", "compile": False}
    assert fixed_paths == [str(staged)]
    assert budget_calls[0][0] is tok
    assert tokenizer_calls == [
        (
            (str(staged / "tokenizer"),),
            {"local_files_only": True, "trust_remote_code": False},
        )
    ]


def test_worker_accepts_quantization_bound_for_sixteen_score_levels():
    worker = load_worker()
    raw = {
        "type": "score",
        "score": 7.5031,
        "confidence": 0.1,
        "legend": {str(i): str(i) for i in range(16)},
        "probabilities": {str(i): 0.0625 for i in range(16)},
    }
    result = worker.canonical_answers({"q": raw}, {"q": {"type": "score"}})["answers"][
        "q"
    ]
    assert result["score"] == 7.5


def test_worker_does_not_repair_inconsistent_score():
    worker = load_worker()
    raw = {
        "type": "score",
        "score": 2.0,
        "confidence": 0.1,
        "legend": {"0": "a", "1": "b", "2": "c"},
        "probabilities": {"0": 0.3333, "1": 0.3333, "2": 0.3333},
    }
    result = worker.canonical_answers({"q": raw}, {"q": {"type": "score"}})["answers"][
        "q"
    ]
    assert result["score"] == 2.0


@pytest.mark.asyncio
async def test_asset_directory_lock_bounds_multiple_gateway_processes(
    runtime, monkeypatch, tmp_path
):
    import fcntl

    lock = tmp_path / ".inference.lock"
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command(monkeypatch, runtime, "print('{\"answers\":{}}')")
        with pytest.raises(runtime.RuntimeFailure, match="worker_busy"):
            await runtime.infer("laya", {"state": "synthetic"})


def test_laya_staging_keeps_verified_checkpoint_immutable(tmp_path):
    worker = load_worker()
    checkpoint = tmp_path / "checkpoint"
    names = [
        "model.safetensors",
        "rl_agent_config.json",
        "encoder/config.json",
        "tokenizer/tokenizer.json",
        "tokenizer/tokenizer_config.json",
    ]
    for name in names:
        target = checkpoint / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original")
    with worker.staged_laya_checkpoint(checkpoint, tmp_path) as staged:
        (staged / "tokenizer/tokenizer_config.json").write_bytes(
            b"official-compatible-config"
        )
        assert (
            checkpoint / "tokenizer/tokenizer_config.json"
        ).read_bytes() == b"original"
        assert (staged / "model.safetensors").stat().st_ino == (
            checkpoint / "model.safetensors"
        ).stat().st_ino
    assert not staged.exists()


@pytest.mark.asyncio
async def test_timeout_removes_owned_temporary_staging(runtime, monkeypatch, tmp_path):
    command(
        monkeypatch,
        runtime,
        "import os,tempfile,time; tempfile.mkdtemp(prefix='staged-checkpoint-',dir=os.environ['TMPDIR']); time.sleep(10)",
    )
    monkeypatch.setattr(runtime, "TIMEOUT_SECONDS", 0.15)
    with pytest.raises(runtime.RuntimeFailure, match="worker_timeout"):
        await runtime.infer("laya", {"state": "synthetic"})
    assert not [path for path in tmp_path.iterdir() if path.is_dir()]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["kev-0.8b", "jev"])
async def test_removed_or_remote_model_rejected_before_local_runtime(runtime, model_id):
    assert runtime.available(model_id) == (False, "unknown_model")
    with pytest.raises(runtime.RuntimeFailure, match="unknown_model"):
        await runtime.infer(model_id, {"state": "synthetic"})


@pytest.mark.parametrize("model_id", ["kev-0.8b", "jev"])
def test_official_worker_rejects_removed_or_remote_model_before_imports(
    monkeypatch, tmp_path, model_id
):
    worker = load_worker()
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ValueError, match="unknown_model"):
        worker.run_official(model_id, tmp_path, {}, {"runtime": "kev"})


@pytest.mark.parametrize(
    "script", ["decision_worker", "decision_download", "decision_smoke"]
)
def test_local_cli_rejects_removed_kev_before_using_assets(
    monkeypatch, tmp_path, capsys, script
):
    import runpy
    import plugboard.decision_assets as assets

    monkeypatch.setattr(
        assets, "verify_assets", lambda *args, **kwargs: (False, "unused")
    )
    monkeypatch.setattr(runtime_module(), "available", lambda *args: (False, "unused"))
    args = ["kev-0.8b"]
    if script == "decision_worker":
        args = ["--model", "kev-0.8b"]
    if script != "decision_smoke":
        args += (
            ["--assets", str(tmp_path), "--check"]
            if script == "decision_download"
            else ["--assets", str(tmp_path)]
        )
    monkeypatch.setattr(sys, "argv", [script, *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts" / f"{script}.py"),
            run_name="__main__",
        )
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
