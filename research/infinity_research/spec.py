"""Versioned task contracts. Only the host can load or change a contract."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hash(root: Path) -> str:
    """Hash paths and bytes, never follow candidate-created symlinks."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Expected a regular directory: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not accepted in sealed inputs: {path}")
        if path.is_file():
            entries.append((path.relative_to(root).as_posix(), file_hash(path)))
        elif not path.is_dir():
            raise ValueError(f"Special file is not accepted: {path}")
    return digest(entries)


def source_files(files: Any) -> dict[str, str]:
    if not isinstance(files, dict) or not files or len(files) > 32:
        raise ValueError("files must contain 1..32 UTF-8 source files")
    total = 0
    result = {}
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("Source paths and contents must be strings")
        path = PurePosixPath(name)
        if (not name or len(name) > 200 or path.is_absolute() or "\\" in name
                or any(part in (".", "..", "") or part.startswith(".")
                       for part in name.split("/"))
                or path.suffix not in (".py", ".json", ".txt", ".md")):
            raise ValueError(f"Invalid source path: {name!r}")
        total += len(content.encode("utf-8"))
        result[path.as_posix()] = content
    if total > 512 * 1024:
        raise ValueError("Candidate source exceeds 512 KiB")
    if "main.py" not in result:
        raise ValueError("Candidate must provide main.py")
    return result


def _positive(obj: dict, name: str, *, integer: bool = False) -> float:
    value = obj.get(name)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0
            or (integer and not isinstance(value, int))):
        raise ValueError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return value


def validate(spec: dict) -> None:
    if spec.get("schema_version") != 1:
        raise ValueError("Unsupported task schema_version (expected 1)")
    if not isinstance(spec.get("task_id"), str) or not spec["task_id"].strip():
        raise ValueError("task_id is required")
    if spec.get("domain") not in ("filament", "smoke"):
        raise ValueError("Unknown domain; register a host-owned domain adapter first")
    objective = spec.get("objective", {})
    if objective.get("direction") not in ("maximize", "minimize"):
        raise ValueError("objective.direction must be maximize or minimize")
    expected_metric = {"filament": "solar_pq_v6", "smoke": "mse"}[spec["domain"]]
    if objective.get("name") != expected_metric:
        raise ValueError(f"Domain {spec['domain']} requires metric {expected_metric}")
    budget = spec.get("budget", {})
    for key in ("max_workers", "max_model_calls", "max_output_tokens", "max_experiments",
                "max_parallel_experiments"):
        _positive(budget, key, integer=True)
    for key in ("max_trial_seconds", "max_total_trial_seconds", "max_wall_seconds",
                "final_trial_seconds"):
        _positive(budget, key)
    if budget["final_trial_seconds"] >= budget["max_total_trial_seconds"]:
        raise ValueError("Total trial budget must leave room for both search and final evaluation")
    if budget["max_wall_seconds"] <= budget["final_trial_seconds"] + 30:
        raise ValueError("Wall budget must reserve final_trial_seconds plus 30 seconds for cleanup/transfer")
    if budget["max_parallel_experiments"] > budget["max_workers"]:
        raise ValueError("Experiment concurrency cannot exceed max_workers")
    if budget["max_workers"] > 1000:
        raise ValueError("max_workers exceeds the supported local launcher bound (1000)")
    executor = spec.get("executor", {})
    if executor.get("kind") not in ("docker", "local"):
        raise ValueError("executor.kind must be docker or local")
    if executor["kind"] == "local" and not executor.get("allow_local", False):
        raise ValueError("Local execution requires explicit --allow-local during initialization")
    if executor["kind"] == "docker" and not executor.get("image"):
        raise ValueError("A prebuilt Docker image is required")
    for key in ("cpus", "max_output_mb"):
        _positive(executor, key)
    _positive(executor, "memory_mb", integer=True)
    if isinstance(executor.get("gpus", 0), bool) or executor.get("gpus", 0) not in (0, 1):
        raise ValueError("This executor supports zero or one GPU per experiment")
    if executor.get("gpus", 0):
        devices = executor.get("gpu_devices", [])
        if (len(devices) < budget["max_parallel_experiments"]
                or len(set(devices)) != len(devices)
                or any(not isinstance(x, str) or not x.isdigit() for x in devices)):
            raise ValueError("Provide distinct numeric gpu_devices for every experiment slot")
    search = spec.get("search", {})
    families = search.get("families", [])
    if (not families or len(families) > 100 or len(set(families)) != len(families)
            or any(not isinstance(x, str) or not x.strip() for x in families)):
        raise ValueError("search.families must contain distinct nonempty names")
    _positive(search, "stagnation_trials", integer=True)
    delta = search.get("min_delta", 0)
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) or delta < 0:
        raise ValueError("search.min_delta must be finite and nonnegative")
    source_files(spec["baseline_files"])


def load_task(path: Path, *, data_override: Path | None = None,
              allow_local: bool = False) -> dict:
    path = path.resolve()
    spec = json.loads(path.read_text())
    from .evaluate import validate_evaluation_host
    validate_evaluation_host(spec.get("domain"))
    baseline = spec.pop("baseline", None)
    if baseline is not None:
        spec["baseline_files"] = {"main.py": (path.parent / baseline).read_text()}
    root = data_override or (path.parent / spec.get("data", {}).get("prepared_root", ""))
    root = root.resolve()
    if not (root / "public" / "manifest.json").is_file():
        raise ValueError(f"Prepared dataset missing: {root / 'public' / 'manifest.json'}; run prepare first")
    if not (root / "public_final" / "manifest.json").is_file():
        raise ValueError("Prepared dataset must include a separate public_final holdout manifest")
    if not (root / "private" / "validation_truth.json").is_file():
        raise ValueError("Host validation truth is missing")
    if not (root / "private" / "holdout_truth.json").is_file():
        raise ValueError("Host holdout truth is missing")
    spec["data"] = {**spec.get("data", {}), "prepared_root": str(root)}
    spec.setdefault("executor", {})["allow_local"] = bool(allow_local)
    from .domains import get_domain
    domain = get_domain(spec["domain"])
    spec["data_sha256"] = tree_hash(root)
    spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
    validate(spec)
    if spec["executor"]["kind"] == "docker":
        docker = shutil.which("docker")
        if docker is None:
            raise ValueError("Docker is required before initializing a production campaign; build its image first")
        reference = spec["executor"]["image"]
        if not isinstance(reference, str) or reference.startswith("-"):
            raise ValueError("Invalid Docker image reference")
        inspected = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", reference],
                                   capture_output=True, text=True, timeout=15, check=False)
        pinned = inspected.stdout.strip()
        if inspected.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", pinned):
            raise ValueError(f"Prebuilt Docker image unavailable: {reference}; {inspected.stderr[-1500:]}")
        spec["executor"]["image_reference"] = reference
        spec["executor"]["image"] = pinned
    return spec
