"""Host-owned research operations called by native Infinity tools."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from .domains import get_domain
from .executor import ExecutionError, execute_candidate
from .spec import canonical, digest, file_hash, tree_hash
from .store import ResearchStore


TERMINAL = {"succeeded", "failed", "interrupted", "cancelled"}


class ContractError(ValueError):
    """Host inputs no longer match the campaign; changing candidate code cannot repair this."""


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_inputs(store: ResearchStore):
    root = Path(store.spec["data"]["prepared_root"])
    if tree_hash(root) != store.spec["data_sha256"]:
        raise ContractError("Prepared dataset changed after campaign initialization")
    domain = get_domain(store.spec["domain"])
    if file_hash(Path(domain.__file__)) != store.spec["evaluator_sha256"]:
        raise ContractError("Evaluator implementation changed after campaign initialization")


def _materialize(store: ResearchStore, candidate: dict) -> tuple[Path, Path]:
    parent = store.root / "candidates" / candidate["id"]
    source = parent / "source"
    output = parent / "output"
    if parent.exists():
        raise ValueError("A candidate execution directory already exists; refusing to mix attempts")
    source.mkdir(parents=True)
    output.mkdir()
    for name, content in candidate["files"].items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _atomic_json(parent / "candidate.json", {k: v for k, v in candidate.items() if k != "files"})
    return source, output


def _evidence(store: ResearchStore, candidate: dict, output: Path, execution: dict,
              *, split="validation", timeout: float) -> dict:
    # The generated program cannot supply its own score or choose its evaluation subset.
    if timeout <= 0:
        raise TimeoutError("Whole-trial budget expired before independent evaluation")
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parent.parent),
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    # Evaluation also has a hard deadline. Large/malformed predictions must not stall every worker.
    scored = subprocess.run(
        [sys.executable, "-m", "infinity_research.evaluate", "--domain", store.spec["domain"],
         "--data", store.spec["data"]["prepared_root"], "--output", str(output), "--split", split,
         "--memory-mb", str(store.spec["executor"]["memory_mb"]), "--timeout", str(timeout)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=timeout + 1,
        check=False,
    )
    if scored.returncode:
        raise ValueError(f"Independent evaluator failed: {scored.stderr.decode(errors='replace')[-8000:]}")
    result = json.loads(scored.stdout)
    score = result.get("score")
    if (result.get("metric") != store.spec["objective"]["name"] or isinstance(score, bool)
            or not isinstance(score, (int, float)) or not math.isfinite(score)):
        raise ValueError("Evaluator returned an invalid metric or score")
    result = {**result, "evidence": {
        "candidate_id": candidate["id"], "candidate_sha256": digest(candidate["files"]),
        "task_sha256": digest(store.spec), "data_sha256": store.spec["data_sha256"],
        "evaluator_sha256": store.spec["evaluator_sha256"],
        "output_sha256": tree_hash(output), "seed": candidate["seed"], "split": split,
        "execution": execution,
    }}
    _atomic_json(output.parent / "evaluation.json", result)
    return result


def _parent_guard(parent_pid: int):
    if os.name == "posix" and parent_pid > 1 and os.getppid() != parent_pid:
        raise RuntimeError("Infinity launcher exited; cancelling its experiment subprocess")


def run_candidate(store: ResearchStore, candidate_id: str) -> dict:
    owner = uuid.uuid4().hex
    parent_pid = os.getppid()
    while True:
        _parent_guard(parent_pid)
        item = store.candidate(candidate_id, include_files=True)
        if item["state"] in TERMINAL:
            return store.candidate(candidate_id)
        # Any live bridge may execute the oldest queued job. This also recovers a
        # proposal whose original caller died before it acquired a resource slot.
        claimed = store.claim(None, owner)
        if claimed is None:
            time.sleep(0.2)
            continue
        work_id = claimed["id"]
        item = store.candidate(work_id, include_files=True)
        started = time.monotonic()
        try:
            verify_inputs(store)
            source, output = _materialize(store, item)
            config = dict(store.spec["executor"])
            if config.get("gpus", 0):
                config["gpu_device"] = config["gpu_devices"][claimed["slot"]]

            def heartbeat():
                _parent_guard(parent_pid)
                store.heartbeat(work_id, owner)

            # Input verification/setup consume the same hard timeout as computation.
            remaining = claimed["effective_timeout"] - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Experiment budget expired while validating sealed inputs")
            evaluation_reserve = min(120.0, max(0.2, remaining * 0.2))
            if remaining <= evaluation_reserve:
                raise TimeoutError("Insufficient trial time for execution and evaluation")
            execution = execute_candidate(source, Path(store.spec["data"]["prepared_root"]),
                                          output, config, remaining - evaluation_reserve,
                                          seed=item["seed"], heartbeat=heartbeat)
            heartbeat()
            result = _evidence(store, item, output, execution,
                               timeout=claimed["effective_timeout"] - (time.monotonic() - started))
            completed = store.finish(work_id, owner, result=result, elapsed=time.monotonic() - started)
        except Exception as error:
            # All execution and evaluator failures become evidence. They never receive a score.
            if isinstance(error, ContractError) or (isinstance(error, ExecutionError)
                    and (error.kind in ("cleanup_failed", "setup_failed", "supervisor_failed")
                         or error.exit_code == 125)):
                store.stop(f"infrastructure_failure: {type(error).__name__}: {error}")
            try:
                completed = store.finish(work_id, owner, result=None,
                                         elapsed=time.monotonic() - started,
                                         error=f"{type(error).__name__}: {error}")
            except RuntimeError:
                # The durable deadline won the race. Its interrupted record remains authoritative.
                completed = store.candidate(work_id)
                if completed["state"] not in TERMINAL:
                    raise
        if work_id == candidate_id:
            return completed


def experiment(store: ResearchStore, args: dict) -> dict:
    proposal = store.propose(args)
    result = run_candidate(store, proposal["candidate_id"])
    result["duplicate"] = proposal["duplicate"]
    result["diagnostics"] = diagnostics(store, proposal["candidate_id"])
    result["research_context"] = {k: v for k, v in store.context().items()
                                  if k in ("stop", "stop_reason", "best_candidate_id", "suggested_family",
                                           "replan_recommended", "budget")}
    return result


def recover(store: ResearchStore) -> dict:
    ids = store.unfinished()
    # This is host execution recovery, not another LLM invocation or re-evaluation of completed work.
    with ThreadPoolExecutor(max_workers=store.spec["budget"]["max_workers"]) as pool:
        results = list(pool.map(lambda cid: run_candidate(store, cid), ids))
    return {"recovered": len(results), "states": {r["id"]: r["state"] for r in results}}


def finalize(store: ResearchStore) -> dict:
    verify_inputs(store)
    final = store.begin_final()
    candidate = store.candidate(final["candidate_id"], include_files=True)
    source = store.root / "candidates" / candidate["id"] / "source"
    output = store.root / "final" / "output"
    started = time.monotonic()
    try:
        if output.parent.exists():
            raise ValueError("Final output already exists")
        output.mkdir(parents=True)
        files = {p.relative_to(source).as_posix(): p.read_text() for p in source.rglob("*") if p.is_file()}
        if files != candidate["files"]:
            raise ValueError("Promoted candidate source changed after evaluation")
        config = dict(store.spec["executor"])
        if config.get("gpus", 0):
            config["gpu_device"] = config["gpu_devices"][0]
        parent_pid = os.getppid()
        execution_timeout = final["timeout"] - min(120.0, max(0.2, final["timeout"] * 0.2))
        execution = execute_candidate(source, Path(store.spec["data"]["prepared_root"]), output,
                                      config, execution_timeout, seed=candidate["seed"],
                                      heartbeat=lambda: _parent_guard(parent_pid), split="holdout")
        result = _evidence(store, candidate, output, execution, split="holdout",
                           timeout=final["timeout"] - (time.monotonic() - started))
        result["elapsed_seconds"] = time.monotonic() - started
        store.finish_final(final["owner"], result)
        return result
    except Exception as error:
        store.finish_final(final["owner"], None, f"{type(error).__name__}: {error}")
        raise


def predict_test(store: ResearchStore, output: Path, *, timeout_seconds: float = 3600) -> dict:
    """Explicit post-research execution of the frozen program, without labels or a score.

    This command has its own timeout and does not reopen research admission or
    consume the closed search budget. Submission format validation is a separate
    domain-specific export operation.
    """
    import fcntl

    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("Test prediction timeout must be finite and positive")
    # Two manual exports of one campaign must not silently share its first GPU.
    with (store.root / "test-prediction.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Test prediction is already running for this campaign") from error
        started = time.monotonic()
        report = store.report()
        final = report.get("final_evaluation")
        if report["final_status"] != "completed" or not final:
            raise ValueError("A successful final evaluation is required before test prediction")
        prepared = Path(store.spec["data"]["prepared_root"])
        if not (prepared / "public_test" / "manifest.json").is_file():
            raise ValueError("No public_test view; prepare the dataset with test images before initialization")
        verify_inputs(store)
        evidence = final["evidence"]
        candidate = store.candidate(evidence["candidate_id"], include_files=True)
        if (digest(candidate["files"]) != evidence["candidate_sha256"]
                or candidate["seed"] != evidence["seed"]):
            raise ValueError("Candidate no longer matches the frozen final evaluation")
        output = output.resolve()
        source = output.with_name(output.name + ".source")
        evidence_path = output.with_name(output.name + ".test_execution.json")
        for target in (output, source, evidence_path):
            if any(target == root or target.is_relative_to(root) for root in (prepared, store.root)):
                raise ValueError("Test prediction output must be outside the sealed dataset and campaign")
            if target.exists() or target.is_symlink():
                raise ValueError("Test prediction output, source and evidence paths must be new")
        source.mkdir(parents=True)
        output.mkdir(parents=True)
        for name, content in candidate["files"].items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        result = {
            "candidate_id": candidate["id"], "candidate_sha256": digest(candidate["files"]),
            "task_sha256": digest(store.spec), "data_sha256": store.spec["data_sha256"],
            "seed": candidate["seed"], "split": "test", "output": str(output),
            "evidence_path": str(evidence_path),
            "charged_to_research_budget": False, "timeout_seconds": timeout_seconds,
            "score": None, "status": "running",
        }
        # The candidate may change its output directory's permissions or create
        # any filename there. Host evidence stays outside that writable mount.
        _atomic_json(evidence_path, result)
        try:
            config = dict(store.spec["executor"])
            if config.get("gpus", 0):
                config["gpu_device"] = config["gpu_devices"][0]
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Test prediction timeout expired while verifying frozen inputs")
            parent_pid = os.getppid()
            result["execution"] = execute_candidate(
                source, prepared, output, config, remaining, seed=candidate["seed"],
                heartbeat=lambda: _parent_guard(parent_pid), split="test",
            )
            if not (output / "predictions.json").is_file():
                raise ValueError("Test candidate did not produce predictions.json")
            result["predictions_sha256"] = file_hash(output / "predictions.json")
            result["status"] = "completed_unscored"
            return result
        except Exception as error:
            result.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            result["elapsed_seconds"] = time.monotonic() - started
            _atomic_json(evidence_path, result)


def diagnostics(store: ResearchStore, candidate_id: str) -> dict:
    # Resolve the ID through the ledger, never accept an arbitrary file path from an agent.
    store.candidate(candidate_id)
    log = store.root / "candidates" / candidate_id / "output.execution.log"
    tail = ""
    if log.is_file() and not log.is_symlink():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 12000))
            tail = stream.read(12000).decode(errors="replace")
    return {"log_tail": tail, "status": "candidate_output_not_independently_verified"}


def dispatch(store: ResearchStore, operation: str, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ValueError("Operation arguments must be an object")
    if operation == "context":
        return store.context()
    if operation == "experiment":
        return experiment(store, args)
    if operation == "candidate":
        item = store.candidate(args["candidate_id"], include_files=True)
        item["diagnostics"] = diagnostics(store, args["candidate_id"])
        return item
    if operation == "finding":
        return store.finding(args)
    if operation == "reserve_model_call":
        return store.reserve_model_call(args["request_id"])
    if operation == "record_usage":
        return store.record_usage(args["request_id"], args["input_tokens"], args["output_tokens"])
    if operation == "recover":
        return recover(store)
    if operation == "finalize":
        return finalize(store)
    raise ValueError(f"Unknown research operation: {operation}")
