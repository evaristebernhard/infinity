"""Host-owned tools for generic campaigns; task semantics stay outside the kernel.

Tools are enabled by a sealed host configuration, never by a skill's text. The
Python tool runs complete submitted programs in the existing isolated executor.
Its output remains candidate-authored; only the separate exact checker issues
geometric verification evidence. Neither tool reads a campaign's private data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
from typing import Any, Callable

from . import executor, math_certificates
from .math_certificates import InvalidPacking, Q2, Square, certify, json_number
from .spec import canonical, digest, file_hash, source_files, tree_hash

_MIB = 1024 * 1024
_NAMES = ("run_python", "verify_square_packing")
_EFFECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_RATIONAL = re.compile(r"[+-]?[0-9]+(?:/[0-9]+)?\Z")
_PYTHON_DEFAULTS = {
    "max_timeout_seconds": 300,
    "max_input_bytes": 262144,
    "max_result_bytes": 65536,
    "max_artifacts": 64,
}
_EXECUTOR_DEFAULTS = {
    "kind": "docker", "cpus": 1, "memory_mb": 1024, "gpus": 0,
    "pids_limit": 128, "tmpfs_mb": 128, "max_output_mb": 16,
    "max_output_files": 256, "max_log_mb": 1, "heartbeat_interval_seconds": 2,
}


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _keys(value: dict, allowed: set[str], label: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"Unknown {label} fields: {', '.join(sorted(extra))}")


def _number(value: Any, label: str, upper: float, *, integer: bool = False) -> int | float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (integer and not isinstance(value, int))
            or not 0 < value <= upper or not math.isfinite(value)):
        raise ValueError(f"{label} must be a positive {'integer' if integer else 'number'} <= {upper:g}")
    return value


def _enabled(spec: dict) -> list[str]:
    _object(spec, "spec")
    names = spec.get("capabilities", [])
    if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            or len(names) != len(set(names)) or any(name not in _NAMES for name in names)):
        raise ValueError(f"capabilities must be a distinct allowlist from {list(_NAMES)}")
    return names


def _implementation_hashes(names: list[str]) -> dict[str, str]:
    # spec.py supplies canonical encoding, source validation and file hashing.
    paths = [Path(__file__), Path(__file__).with_name("spec.py")]
    if "run_python" in names:
        paths.append(Path(executor.__file__))
    if "verify_square_packing" in names:
        paths.append(Path(math_certificates.__file__))
    return {path.name: file_hash(path) for path in paths}


def _configuration_hash(spec: dict) -> str:
    return digest({"capabilities": spec["capabilities"],
                   "capability_config": spec["capability_config"]})


def prepare_capabilities(spec: dict, *, allow_local: bool = False) -> dict:
    """Normalize and seal a host spec, inspecting but never pulling a Docker image.

    This function makes no filesystem changes. Call it during initialization,
    persist the returned spec, and keep its lock when resuming. Explicit local
    opt-in authorizes only trusted fixture code: it does not supply isolation,
    memory limits, network blocking, or protection from host filesystem access.
    """
    if not isinstance(allow_local, bool):
        raise ValueError("allow_local must be a boolean supplied by the host")
    names = sorted(_enabled(spec))
    result = json.loads(canonical(spec))
    supplied = _object(result.get("capability_config", {}), "capability_config")
    _keys(supplied, set(names), "capability_config")
    configurations = {}
    for name in names:
        config = _object(supplied.get(name, {}), f"capability_config.{name}")
        if name == "verify_square_packing":
            _keys(config, {"max_n", "max_rational_chars"}, name)
            config = {"max_n": 64, "max_rational_chars": 64, **config}
            _number(config["max_n"], "max_n", 64, integer=True)
            _number(config["max_rational_chars"], "max_rational_chars", 64, integer=True)
        else:
            _keys(config, {*_PYTHON_DEFAULTS, "executor"}, name)
            config = {**_PYTHON_DEFAULTS, **config}
            _number(config["max_timeout_seconds"], "max_timeout_seconds", 604800)
            for key, upper in (("max_input_bytes", 4 * _MIB), ("max_result_bytes", 256 * 1024),
                               ("max_artifacts", 256)):
                _number(config[key], key, upper, integer=True)
            supplied_executor = _object(config.get("executor", {}), "run_python.executor")
            _keys(supplied_executor, {*_EXECUTOR_DEFAULTS, "allow_local", "image", "image_reference"},
                  "run_python.executor")
            run = {**_EXECUTOR_DEFAULTS, **supplied_executor, "allow_local": allow_local}
            if run["kind"] not in ("docker", "local"):
                raise ValueError("run_python.executor.kind must be docker or local")
            if run["kind"] == "local" and not allow_local:
                raise ValueError("run_python local execution needs explicit --allow-local for trusted fixtures")
            if os.name != "posix":
                raise ValueError("run_python requires a POSIX host for supervised process cleanup")
            if isinstance(run["gpus"], bool) or run["gpus"] != 0:
                raise ValueError("run_python currently supports CPU execution only; configure a separate GPU tool")
            for key, upper in (("cpus", 1024), ("max_output_mb", 1024),
                               ("max_log_mb", 64), ("heartbeat_interval_seconds", 30)):
                _number(run[key], key, upper)
            for key, upper in (("memory_mb", 1048576), ("pids_limit", 4096),
                               ("tmpfs_mb", 65536), ("max_output_files", 4096)):
                _number(run[key], key, upper, integer=True)
            if run["kind"] == "docker":
                docker = shutil.which("docker")
                if docker is None:
                    raise ValueError("run_python requires Docker and a prebuilt image before initialization")
                reference = run.get("image")
                if (not isinstance(reference, str) or not reference.strip()
                        or reference.startswith("-") or len(reference) > 512):
                    raise ValueError("run_python.executor.image must name a prebuilt image")
                inspected = subprocess.run(
                    [docker, "image", "inspect", "--format", "{{.Id}}", reference],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
                    env=executor._host_environment(),
                )
                image = inspected.stdout.decode("utf-8", "replace").strip()
                if inspected.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                    detail = inspected.stderr[-1500:].decode("utf-8", "replace")
                    raise ValueError(f"Prebuilt Docker image unavailable: {reference}. {detail}")
                run.update(image=image, image_reference=reference, allow_local=False)
            elif "image" in run or "image_reference" in run:
                raise ValueError("A local fixture must not specify a Docker image")
            config["executor"] = run
        configurations[name] = config
    result["capabilities"] = names
    result["capability_config"] = configurations
    result["capability_lock"] = {
        "schema_version": 1,
        "config_sha256": _configuration_hash(result),
        "source_sha256": _implementation_hashes(names),
    }
    return result


def validate_capabilities(spec: dict) -> None:
    """Refuse changed configuration or tool implementations on campaign resume."""
    names = _enabled(spec)
    lock = _object(spec.get("capability_lock"), "capability_lock")
    _object(spec.get("capability_config"), "capability_config")
    if lock != {"schema_version": 1, "config_sha256": _configuration_hash(spec),
                "source_sha256": _implementation_hashes(names)}:
        raise ValueError("Capability configuration or implementation changed after initialization")


def capability_catalog(spec: dict) -> list[dict]:
    """Return schemas only for host-enabled tools, without granting capabilities."""
    names = _enabled(spec)
    configs = _object(spec.get("capability_config", {}), "capability_config")
    packing_config = _object(configs.get("verify_square_packing", {}), "packing configuration")
    max_n = packing_config.get("max_n", 64)
    max_chars = packing_config.get("max_rational_chars", 64)
    number = {"type": "array", "minItems": 2, "maxItems": 2,
              "items": {"type": "string", "maxLength": max_chars},
              "description": "Exact [a,b] for a+b*sqrt(2); a and b are integer or p/q strings."}
    definitions = {
        "run_python": {
            "name": "run_python",
            "description": "Run complete Python source in the configured prebuilt offline sandbox. "
                "main.py receives input JSON path and output directory as argv[1:3]; write result.json "
                "there. Returns bounded candidate-authored JSON, artifact hashes and diagnostics. "
                "Successful execution does not verify any mathematical or empirical claim.",
            "parameters": {"type": "object", "additionalProperties": False,
                "required": ["files"], "properties": {
                    "files": {"type": "object", "minProperties": 1, "maxProperties": 32,
                              "additionalProperties": {"type": "string"},
                              "description": "Complete source files including main.py; no host paths."},
                    "input": {"description": "JSON input copied into the read-only input manifest."},
                    "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 2**32 - 1},
                }},
        },
        "verify_square_packing": {
            "name": "verify_square_packing",
            "description": "Independently check unit squares inside [0,side]^2 using exact arithmetic "
                "over Q(sqrt(2)). Touching is allowed. At most 64 squares. A feasible layout supplies "
                "an upper bound; global optimality is certified only if its area equals n. "
                "This is not the Kaggle decimal CSV validator or a Lean proof.",
            "parameters": {"type": "object", "additionalProperties": False,
                "required": ["n", "side", "squares"], "properties": {
                    "n": {"type": "integer", "minimum": 1, "maximum": max_n},
                    "side": number,
                    "squares": {"type": "array", "minItems": 1, "maxItems": max_n,
                        "items": {"type": "object", "additionalProperties": False,
                            "required": ["x", "y", "cos", "sin"], "properties": {
                                "name": {"type": "string", "maxLength": 80},
                                "x": number, "y": number, "cos": number, "sin": number,
                            }}},
                }},
        },
    }
    python_config = _object(configs.get("run_python", {}), "Python configuration")
    definitions["run_python"]["parameters"]["properties"]["timeout_seconds"]["maximum"] = (
        python_config.get("max_timeout_seconds", _PYTHON_DEFAULTS["max_timeout_seconds"])
    )
    if python_config.get("executor", {}).get("kind") == "local":
        definitions["run_python"]["description"] += (
            " This campaign explicitly enables trusted local fixtures: no sandbox, network isolation, "
            "or memory enforcement is provided by local mode."
        )
    return [definitions[name] for name in names]


def _json_bytes(value: Any, limit: int, label: str) -> bytes:
    try:
        data = canonical(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"{label} must be finite JSON") from error
    if len(data) > limit:
        raise ValueError(f"{label} exceeds {limit} bytes")
    return data


def _regular_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    for entry in (*reversed(absolute.parents), absolute):
        if entry.is_symlink():
            raise ValueError(f"Capability path contains a symlink: {entry}")
    return absolute


def _new_directory(root: Path, effect_id: str) -> tuple[Path, Path]:
    if not isinstance(effect_id, str) or not _EFFECT_ID.fullmatch(effect_id):
        raise ValueError("Invalid host effect_id")
    root = _regular_path(root)
    if not root.is_dir():
        raise ValueError("Campaign root must already exist")
    effects = _regular_path(root / "effects")
    effects.mkdir(exist_ok=True, mode=0o700)
    directory = _regular_path(effects / effect_id)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ValueError("Effect directory already exists; read its recorded outcome or use a new effect") from error
    return root, directory


def _write(path: Path, data: bytes) -> None:
    """Write a new host-owned file without following links or replacing evidence."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _artifact(path: Path, root: Path, *, origin: str) -> dict:
    _regular_path(path)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Artifacts must be regular files without hard links")
    relative = path.relative_to(root).as_posix()
    if len(relative.encode("utf-8")) > 512:
        raise ValueError("Artifact path exceeds 512 UTF-8 bytes")
    return {"path": relative, "sha256": file_hash(path), "bytes": metadata.st_size,
            "origin": origin}


def _log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    _regular_path(path)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Execution log must be a regular file without hard links")
    with path.open("rb") as stream:
        stream.seek(max(0, metadata.st_size - 8192))
        return stream.read(8192).decode("utf-8", "replace")


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key[:80]}")
        result[key] = value
    return result


def _run_python(args: dict, *, root: Path, directory: Path, spec: dict,
                envelope: dict, heartbeat: Callable[[], None]) -> dict:
    config = spec["capability_config"]["run_python"]
    files = source_files(args.get("files"))
    input_bytes = _json_bytes(args.get("input", {}), config["max_input_bytes"], "input")
    timeout = _number(args.get("timeout_seconds", config["max_timeout_seconds"]),
                      "timeout_seconds", config["max_timeout_seconds"])
    seed = args.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer in [0, 2**32 - 1]")
    source = directory / "source"
    public = directory / "inputs" / "public"
    output = directory / "output"
    source.mkdir()
    public.mkdir(parents=True)
    for name, contents in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(target, contents.encode("utf-8"))
    _write(public / "manifest.json", input_bytes)
    source_hash = tree_hash(source)
    input_hash = hashlib.sha256(input_bytes).hexdigest()
    envelope["evidence"] = {
        "kind": "execution", "judgement": "unverified", "semantic_claim": "not_checked",
        "source_sha256": source_hash, "input_sha256": input_hash, "seed": seed,
        "timeout_seconds": timeout, "configuration_sha256": spec["capability_lock"]["config_sha256"],
        "implementation_sha256": spec["capability_lock"]["source_sha256"],
        "execution_mode": config["executor"]["kind"],
        "isolation": "docker" if config["executor"]["kind"] == "docker" else "trusted_local_no_isolation",
    }
    envelope.update(result_json=None, result_origin="candidate", artifacts=[])
    heartbeat_error: list[Exception] = []

    def tick():
        try:
            heartbeat()
        except Exception as error:
            heartbeat_error.append(error)
            raise

    log = output.with_name("output.execution.log")
    started = time.monotonic()
    try:
        execution = executor.execute_candidate(
            source, directory / "inputs", output, config["executor"], timeout,
            seed=seed, heartbeat=tick,
        )
        if (tree_hash(source) != source_hash
                or file_hash(public / "manifest.json") != input_hash):
            raise ValueError("Candidate modified its sealed source or input")
        image_id = execution.get("image_id")
        if config["executor"]["kind"] == "docker" and image_id != config["executor"]["image"]:
            raise ValueError("Executor did not use the pinned Docker image")
        artifacts = []
        for path in sorted(output.rglob("*")):
            if path.is_symlink():
                raise ValueError("Output contains a symlink")
            if path.is_dir():
                continue
            artifacts.append(_artifact(path, root, origin="candidate"))
            if len(artifacts) > config["max_artifacts"]:
                raise ValueError("Output exceeds max_artifacts")
        result = _regular_path(output / "result.json")
        if not result.is_file() or result.stat().st_size > config["max_result_bytes"]:
            raise ValueError("Program must write result.json within max_result_bytes")
        parsed = json.loads(result.read_text(encoding="utf-8"), parse_constant=_reject_constant,
                            object_pairs_hook=_unique_object)
        # json.loads accepts 1e999 as infinity; canonical encoding also checks floats.
        _json_bytes(parsed, config["max_result_bytes"], "result.json")
        envelope.update(status="completed", result_json=parsed, artifacts=artifacts,
                        execution=execution)
        envelope["evidence"].update(image_id=image_id, exit_code=execution["exit_code"])
        # Do not expose an absolute filesystem path as a model-controlled argument.
        envelope["execution"]["log_path"] = log.relative_to(root).as_posix()
    except (executor.ExecutionError, OSError, ValueError, RecursionError) as error:
        if heartbeat_error:
            raise heartbeat_error[0]
        envelope.update(status="failed", error=str(error)[:2000],
                        error_kind=getattr(error, "kind", "invalid_output"))
        # A failed cleanup can leave compute alive. Do not silently admit more
        # work into that environment. This flag is host-owned, never copied
        # from the candidate's result_json.
        envelope["requires_campaign_stop"] = envelope["error_kind"] == "cleanup_failed"
        envelope["evidence"]["exit_code"] = getattr(error, "exit_code", None)
    envelope["elapsed_seconds"] = time.monotonic() - started
    envelope["log_tail"] = _log_tail(log)
    envelope["log_origin"] = "candidate"
    envelope["artifact_refs"] = [artifact["path"] for artifact in envelope["artifacts"]]
    heartbeat()
    return envelope


def _q2(value: Any, *, label: str, max_chars: int) -> Q2:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{label} must be [a,b] for a+b*sqrt(2)")
    from fractions import Fraction
    coefficients = []
    for item in value:
        if not isinstance(item, str) or not 0 < len(item) <= max_chars or not _RATIONAL.fullmatch(item):
            raise ValueError(f"{label} coefficients must be bounded integer or p/q strings")
        try:
            coefficient = Fraction(item)
        except (ValueError, ZeroDivisionError) as error:
            raise ValueError(f"{label} has an invalid rational coefficient") from error
        if coefficient.numerator.bit_length() > 256 or coefficient.denominator.bit_length() > 256:
            raise ValueError(f"{label} coefficient exceeds the exact arithmetic limit")
        coefficients.append(coefficient)
    return Q2(*coefficients)


def _verify_square_packing(args: dict, *, root: Path, directory: Path, spec: dict,
                           envelope: dict, heartbeat: Callable[[], None]) -> dict:
    config = spec["capability_config"]["verify_square_packing"]
    raw = _json_bytes(args, 256 * 1024, "square packing")
    n = _number(args.get("n"), "n", config["max_n"], integer=True)
    if not isinstance(args.get("squares"), list) or len(args["squares"]) != n:
        raise ValueError("squares must contain exactly n entries")
    side = _q2(args.get("side"), label="side", max_chars=config["max_rational_chars"])
    squares = []
    for index, item in enumerate(args["squares"]):
        _object(item, "square")
        _keys(item, {"name", "x", "y", "cos", "sin"}, "square")
        name = item.get("name", str(index))
        if not isinstance(name, str) or not 0 < len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Square name must contain 1..80 printable characters")
        values = [_q2(item.get(key), label=f"square {index}.{key}", max_chars=config["max_rational_chars"])
                  for key in ("x", "y", "cos", "sin")]
        squares.append(Square(name, *values))
    if len({square.name for square in squares}) != n:
        raise ValueError("Square names must be distinct")
    _write(directory / "candidate.json", raw)
    started = time.monotonic()
    evidence = {
        "kind": "verification", "claim": "layout_feasible", "scope": {"n": n},
        "method": "exact_Q_sqrt2_separating_axes",
        "subject_sha256": hashlib.sha256(raw).hexdigest(),
        "verifier_sha256": spec["capability_lock"]["source_sha256"],
        "configuration_sha256": spec["capability_lock"]["config_sha256"],
        "trust_basis": "Python Fraction arithmetic, separating-axis criterion, and the area lower bound",
        "formal_proof_assistant_checked": False,
        "kaggle_decimal_submission_validated": False,
        "optimality": "not_proven",
    }
    try:
        certificate = certify(side, squares, heartbeat=heartbeat)
        evidence.update(judgement="verified", optimality=certificate["global_optimality"])
    except InvalidPacking as error:
        certificate = {"n": n, "side": side, "feasible": False, "reason": str(error)}
        evidence.update(judgement="refuted", reason=str(error))
    certificate_bytes = json.dumps(certificate, default=json_number, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(certificate_bytes) > 16 * _MIB:
        raise ValueError("Exact certificate exceeds the 16 MiB host limit")
    path = directory / "certificate.json"
    _write(path, certificate_bytes)
    artifact = _artifact(path, root, origin="host_verifier")
    evidence["certificate_sha256"] = artifact["sha256"]
    envelope.update(status="completed", evidence=evidence, artifacts=[artifact],
                    artifact_refs=[artifact["path"]],
                    result_json={"feasible": certificate["feasible"], "n": n,
                                 "optimality": evidence["optimality"],
                                 "certificate": artifact["path"]},
                    result_origin="host_verifier", elapsed_seconds=time.monotonic() - started)
    heartbeat()
    return envelope


def execute_capability(name: str, args: dict, *, root: Path, spec: dict,
                       effect_id: str, heartbeat: Callable[[], None] | None = None) -> dict:
    """Execute one already-reserved host effect and return a bounded evidence envelope.

    The caller owns durable admission and fencing. An effect id is a new physical
    attempt; an existing directory is never re-executed or overwritten. A caller
    must serve idempotent replays from its ledger and inspect interrupted attempts
    before creating a replacement. Lost-lease heartbeat exceptions propagate.
    """
    validate_capabilities(spec)
    if name not in spec["capabilities"]:
        raise ValueError(f"Capability is not enabled: {name}")
    _object(args, "arguments")
    _keys(args, {"files", "input", "timeout_seconds", "seed"} if name == "run_python"
          else {"n", "side", "squares"}, "arguments")
    tick = heartbeat if heartbeat is not None else lambda: None
    tick()
    root, directory = _new_directory(root, effect_id)
    envelope = {"capability": name, "effect_id": effect_id,
                "request_sha256": digest({"capability": name, "arguments": args,
                                          "lock": spec["capability_lock"]})}
    handler = _run_python if name == "run_python" else _verify_square_packing
    result = handler(args, root=root, directory=directory, spec=spec,
                     envelope=envelope, heartbeat=tick)
    # Stored outside the candidate mount, after the candidate process tree ends.
    _write(directory / "outcome.json", _json_bytes(result, 512 * 1024, "tool outcome"))
    return result


def read_artifact(root: Path, effect: dict, path: str) -> dict:
    """Read only indexed artifact bytes, checking their hash after bounded reading.

    ``effect`` must come from the host ledger after its access checks. The model
    supplies only the effect identifier and one exact indexed path. File content
    is data, and never expands the caller's capabilities or changes a verdict.
    """
    _object(effect, "effect")
    result = _object(effect.get("result"), "effect.result")
    effect_id = result.get("effect_id")
    if (not isinstance(effect_id, str) or not _EFFECT_ID.fullmatch(effect_id)
            or not isinstance(path, str) or not path.startswith(f"effects/{effect_id}/")
            or path not in result.get("artifact_refs", [])):
        raise ValueError("Path is not an artifact of this effect")
    relative = PurePosixPath(path)
    if (relative.is_absolute() or "\\" in path or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise ValueError("Invalid artifact path")
    matches = [item for item in result.get("artifacts", [])
               if isinstance(item, dict) and item.get("path") == path]
    if len(matches) != 1:
        raise ValueError("Artifact index is missing or ambiguous")
    artifact = matches[0]
    root = _regular_path(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, flags)
    try:
        # Descend by file descriptor: a path swapped for a link during a read
        # cannot redirect the lookup outside the permitted artifact tree.
        for part in relative.parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Artifact must be a regular file without hard links")
            if metadata.st_size > 256 * 1024:
                raise ValueError("Artifact exceeds the 256 KiB inline-read limit")
            data = stream.read(256 * 1024 + 1)
    except OSError as error:
        raise ValueError("Artifact path is missing, inaccessible, or contains a symlink") from error
    finally:
        os.close(directory_fd)
    if len(data) > 256 * 1024:
        raise ValueError("Artifact exceeds the 256 KiB inline-read limit")
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != artifact.get("sha256") or len(data) != artifact.get("bytes"):
        raise ValueError("Artifact bytes changed after the tool recorded them")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Artifact is binary; inline reads require UTF-8 text") from error
    return {"path": path, "sha256": checksum, "bytes": len(data),
            "origin": artifact.get("origin"), "content": content, "content_is_data": True}
