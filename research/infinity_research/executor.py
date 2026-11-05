"""Run one candidate with bounded resources and a parent-death watchdog.

Production candidates run in a prebuilt Docker image.  The ``local`` backend
exists for explicitly trusted tests only; it does not isolate a candidate from
the host, its files, or its network.  This module does not assign research
budgets: the caller must reserve a slot before invoking it and release the
reservation after recording the result.
"""

from __future__ import annotations

import json
import errno
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

_MIB = 1024 * 1024
_POLL_SECONDS = 0.05


class ExecutionError(RuntimeError):
    """A candidate failed to execute or violated its execution contract."""

    def __init__(
        self,
        message: str,
        *,
        elapsed_seconds: float = 0.0,
        exit_code: int | None = None,
        log_path: str | None = None,
        kind: str = "execution_failed",
    ) -> None:
        super().__init__(message)
        self.elapsed_seconds = elapsed_seconds
        self.exit_code = exit_code
        self.log_path = log_path
        self.kind = kind


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _checked_path(path: Path, name: str) -> Path:
    """Reject symlink components instead of resolving them out of sight."""
    absolute = Path(os.path.abspath(path))
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise ValueError(f"{name} contains a symlink: {component}")
    return absolute


def _scan_output(root: Path, max_bytes: int, max_entries: int) -> int:
    """Bound the complete artifact tree, including files in subdirectories."""
    total = 0
    count = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def walk(directory_fd: int, depth: int) -> None:
        nonlocal total, count
        if depth > 64:
            raise ValueError("output exceeds 64 directory levels")
        # Directory FDs and O_NOFOLLOW prevent a candidate from swapping a
        # scanned directory for a symlink between checking it and descending.
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                count += 1
                if count > max_entries:
                    raise ValueError(f"output exceeds {max_entries} filesystem entries")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A running candidate atomically renamed a file.
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(f"output contains a symlink: {entry.name}")
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        nested_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        if error.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise ValueError(f"output directory became a symlink: {entry.name}") from error
                        raise
                    try:
                        walk(nested_fd, depth + 1)
                    finally:
                        os.close(nested_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    if metadata.st_nlink > 1:
                        raise ValueError(f"output contains a hard-linked file: {entry.name}")
                    total += metadata.st_size
                    if total > max_bytes:
                        raise ValueError(f"output exceeds the {max_bytes}-byte artifact limit")
                else:
                    raise ValueError(f"output contains a non-regular file: {entry.name}")
    root_fd = os.open(root, flags)
    try:
        walk(root_fd, 0)
    finally:
        os.close(root_fd)
    return total


def _check_readonly_tree(root: Path) -> None:
    """Prevent a symlink inside an input mount from reaching a different tree."""
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError(f"read-only input contains a symlink: {entry.path}")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"read-only input contains a non-regular file: {entry.path}")


def _host_environment() -> dict[str, str]:
    """The supervisor and trusted local tests receive no provider credentials."""
    permitted = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LC_ALL",
        "TZ",
    )
    return {name: os.environ[name] for name in permitted if name in os.environ}


def _gpu_device(config: dict[str, Any]) -> str | None:
    gpus = config.get("gpus", 0)
    if isinstance(gpus, bool) or gpus not in (0, 1):
        raise ValueError("gpus must be 0 or 1; the campaign assigns one device per slot")
    if not gpus:
        return None
    device = config.get("gpu_device")
    if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", device):
        raise ValueError("a GPU trial requires an explicit gpu_device assigned by the campaign")
    return device


def _docker_command(
    executable: str,
    image: str,
    container_name: str,
    candidate: Path,
    public_data: Path,
    output: Path,
    config: dict[str, Any],
    seed: int,
) -> list[str]:
    cpus = _positive_number(config.get("cpus", 2), "cpus")
    memory = _positive_int(config.get("memory_mb", 4096), "memory_mb")
    pids = _positive_int(config.get("pids_limit", 256), "pids_limit")
    temporary = _positive_int(config.get("tmpfs_mb", 512), "tmpfs_mb")
    gpu_device = _gpu_device(config)
    for path in (candidate, public_data, output):
        if "," in str(path) or "\n" in str(path):
            raise ValueError("Docker bind-mount paths cannot contain commas or newlines")
    command = [
        executable,
        "run",
        "--pull=never",
        "--name",
        container_name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        str(pids),
        "--cpus",
        str(cpus),
        "--memory",
        f"{memory}m",
        "--memory-swap",
        f"{memory}m",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={temporary}m,mode=1777",
        "--mount",
        f"type=bind,src={candidate},dst=/candidate,readonly",
        "--mount",
        f"type=bind,src={public_data},dst=/input,readonly",
        "--mount",
        f"type=bind,src={output},dst=/output",
        "--workdir",
        "/candidate",
        "--env",
        f"RESEARCH_SEED={seed}",
        "--env",
        "PYTHONUNBUFFERED=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "HOME=/tmp",
    ]
    if os.name == "posix":
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    if gpu_device is not None:
        command.extend(["--gpus", f"device={gpu_device}"])
    else:
        command.extend(["--env", "CUDA_VISIBLE_DEVICES="])
    command.extend([image, "python", "/candidate/main.py", "/input/manifest.json", "/output"])
    return command


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    # Kill descendants even when the group's original leader already exited.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The process group has already exited.
    elif process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _cleanup_container(command: list[str] | None) -> str | None:
    if command is None:
        return None
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"container cleanup failed: {error}"
    if result.returncode and b"No such container" not in result.stdout:
        return "container cleanup failed: " + result.stdout[:2048].decode("utf-8", "replace")
    return None


class _SupervisorStopped(Exception):
    pass


def _supervise(spec: dict[str, Any]) -> dict[str, Any]:
    """Own the candidate process so a dead RPC parent cannot orphan a trial."""
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    result: dict[str, Any] = {"ok": False, "kind": "execution_failed"}
    log = None
    stopped = False

    def stop_signal(_number: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    deadline = spec["deadline"]
    written = 0
    truncated = False
    next_scan = started
    try:
        if os.getppid() != spec["parent_pid"]:
            raise _SupervisorStopped("research parent exited before the trial started")
        log_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        log = os.fdopen(os.open(spec["log_path"], log_flags, 0o600), "wb", buffering=0)
        process = subprocess.Popen(
            spec["command"],
            cwd=spec["cwd"],
            env=spec["environment"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is None:
            raise RuntimeError("candidate stdout was not connected")
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if stopped or os.getppid() != spec["parent_pid"]:
                raise _SupervisorStopped("research parent exited or cancelled the trial")
            if now >= deadline:
                result["kind"] = "timeout"
                raise TimeoutError(f"candidate exceeded {spec['timeout_seconds']:g} seconds")
            if now >= next_scan:
                try:
                    _scan_output(
                        Path(spec["output_dir"]), spec["max_output_bytes"], spec["max_output_entries"]
                    )
                except ValueError:
                    result["kind"] = "invalid_output"
                    raise
                next_scan = now + 0.2
            for key, _mask in selector.select(min(_POLL_SECONDS, max(0.0, deadline - now))):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = spec["max_log_bytes"] - written
                if remaining > 0:
                    kept = chunk[:remaining]
                    log.write(kept)
                    written += len(kept)
                if len(chunk) > remaining:
                    truncated = True
            code = process.poll()
            if code is not None:
                # Descendants may retain stdout forever.  Clean them up first,
                # then drain buffered output without waiting on an inherited FD.
                _kill_process_group(process)
                while True:
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    remaining = spec["max_log_bytes"] - written
                    if remaining > 0:
                        kept = chunk[:remaining]
                        log.write(kept)
                        written += len(kept)
                    if len(chunk) > remaining:
                        truncated = True
                result["exit_code"] = code
                if code:
                    result["kind"] = "nonzero_exit"
                    raise RuntimeError(f"candidate exited with status {code}")
                result.update(ok=True, kind="completed")
                break
    except _SupervisorStopped as error:
        result.update(ok=False, kind="cancelled", error=str(error))
    except Exception as error:
        result.update(ok=False, error=str(error))
    finally:
        selector.close()
        if process is not None:
            try:
                _kill_process_group(process)
            except (OSError, subprocess.TimeoutExpired) as error:
                result.update(ok=False, kind="cleanup_failed", error=f"process cleanup failed: {error}")
            if process.stdout is not None:
                process.stdout.close()
            result["exit_code"] = process.returncode
        cleanup_error = _cleanup_container(spec.get("cleanup_command"))
        if cleanup_error is not None:
            previous = result.get("error")
            result.update(
                ok=False,
                kind="cleanup_failed",
                error=f"{previous}; {cleanup_error}" if previous else cleanup_error,
            )
        if log is not None:
            log.close()
    if result["ok"]:
        try:
            result["output_bytes"] = _scan_output(
                Path(spec["output_dir"]), spec["max_output_bytes"], spec["max_output_entries"]
            )
        except (OSError, ValueError) as error:
            result.update(ok=False, kind="invalid_output", error=str(error))
    result.update(
        elapsed_seconds=time.monotonic() - started,
        log_path=spec["log_path"],
        log_bytes=written,
        log_truncated=truncated,
    )
    return result


def execute_candidate(
    candidate_dir: Path,
    prepared_root: Path,
    output_dir: Path,
    config: dict[str, Any],
    timeout_seconds: float,
    *,
    seed: int,
    heartbeat: Callable[[], None] | None = None,
    split: str = "validation",
) -> dict[str, Any]:
    """Execute ``main.py manifest.json output_dir`` and return execution evidence.

    Only ``prepared_root/public`` is visible during search.  A sealed final run
    uses ``public_final``; test inference uses ``public_test`` with unlabelled
    test inputs.  None of these mounts includes private evaluation labels.  The
    caller evaluates validation/holdout predictions independently after success.

    The local mode deliberately has no security boundary and is restricted to
    trusted fixtures by ``allow_local=True``.  Linux/WSL is required for its
    process-group cleanup.  Docker must be available locally with the configured
    image already built.  GPU trials require an explicit campaign-assigned
    ``gpu_device``; this layer never silently grants all GPUs.
    """
    started = time.monotonic()
    log_path: Path | None = None
    supervisor: subprocess.Popen[bytes] | None = None
    pending_error: ExecutionError | None = None
    try:
        if os.name != "posix":
            raise ValueError("candidate execution requires Linux/WSL or another POSIX host")
        timeout = _positive_number(timeout_seconds, "timeout_seconds")
        interval = _positive_number(config.get("heartbeat_interval_seconds", 2), "heartbeat interval")
        max_log = max(1, int(_positive_number(config.get("max_log_mb", 2), "max_log_mb") * _MIB))
        max_output = max(
            1, int(_positive_number(config.get("max_output_mb", 512), "max_output_mb") * _MIB)
        )
        max_entries = _positive_int(config.get("max_output_files", 10000), "max_output_files")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be an integer in [0, 2**32 - 1]")
        input_directories = {"validation": "public", "holdout": "public_final", "test": "public_test"}
        if split not in input_directories:
            raise ValueError("split must be validation, holdout, or test")
        candidate = _checked_path(Path(candidate_dir), "candidate_dir")
        prepared = _checked_path(Path(prepared_root), "prepared_root")
        public = _checked_path(
            prepared / input_directories[split], "public input"
        )
        output = _checked_path(Path(output_dir), "output_dir")
        if not candidate.is_dir() or not (candidate / "main.py").is_file():
            raise ValueError("candidate_dir must contain main.py")
        if not public.is_dir() or not (public / "manifest.json").is_file():
            raise ValueError(f"prepared input is missing {public / 'manifest.json'}")
        if (
            output.is_relative_to(candidate)
            or candidate.is_relative_to(output)
            or output.is_relative_to(prepared)
            or prepared.is_relative_to(output)
        ):
            raise ValueError("output_dir must be separate from candidate and prepared data")
        _check_readonly_tree(candidate)
        _check_readonly_tree(public)
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError("output_dir must be empty; use a fresh directory for every attempt")
        log_path = _checked_path(output.with_name(output.name + ".execution.log"), "execution log")
        mode = config.get("kind", "docker")
        environment = _host_environment()
        environment.update(
            PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", RESEARCH_SEED=str(seed)
        )
        cleanup_command = None
        image_id = None
        gpu_device = _gpu_device(config)
        if mode == "local":
            if config.get("allow_local") is not True:
                raise ValueError("local execution requires allow_local=true and trusted candidates")
            if os.name != "posix":
                raise ValueError("trusted local execution requires Linux/WSL for process-group cleanup")
            environment["CUDA_VISIBLE_DEVICES"] = gpu_device or ""
            command = [sys.executable, str(candidate / "main.py"), str(public / "manifest.json"), str(output)]
        elif mode == "docker":
            executable = shutil.which("docker")
            if executable is None:
                raise ValueError("Docker is required; install Docker and build the configured image")
            image = config.get("image")
            if not isinstance(image, str) or not image.strip() or image.startswith("-"):
                raise ValueError("executor.image must name a prebuilt Docker image")
            # Resolve a local tag once, so a changing tag cannot change the image
            # between inspection and launch.  No registry pull occurs here.
            inspected = subprocess.run(
                [executable, "image", "inspect", "--format", "{{.Id}}", image],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(timeout, 10),
                check=False,
                env=environment,
            )
            image_id = inspected.stdout.decode("utf-8", "replace").strip()
            if inspected.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                detail = inspected.stderr[:2048].decode("utf-8", "replace")
                raise ValueError(f"prebuilt Docker image unavailable: {image}. {detail}")
            container = "infinity-research-" + uuid.uuid4().hex
            command = _docker_command(executable, image_id, container, candidate, public, output, config, seed)
            cleanup_command = [executable, "rm", "--force", container]
        else:
            raise ValueError("executor.kind must be docker or local")
        if heartbeat is not None:
            heartbeat()
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ExecutionError("trial preparation exhausted its timeout", kind="timeout")
        spec = {
            "command": command,
            "cwd": str(candidate),
            "environment": environment,
            "cleanup_command": cleanup_command,
            "timeout_seconds": remaining,
            "deadline": started + timeout,
            "max_log_bytes": max_log,
            "max_output_bytes": max_output,
            "max_output_entries": max_entries,
            "output_dir": str(output),
            "log_path": str(log_path),
            "parent_pid": os.getpid(),
        }
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--supervise"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_host_environment(),
        )
        if supervisor.stdin is None:
            raise RuntimeError("supervisor stdin was not connected")
        supervisor.stdin.write(json.dumps(spec).encode("utf-8"))
        supervisor.stdin.close()
        supervisor.stdin = None
        next_heartbeat = time.monotonic() + interval
        while supervisor.poll() is None:
            now = time.monotonic()
            if heartbeat is not None and now >= next_heartbeat:
                try:
                    heartbeat()
                except Exception as error:
                    raise ExecutionError(f"trial heartbeat failed: {error}", kind="heartbeat_failed") from error
                next_heartbeat = now + interval
            # The supervisor enforces the trial deadline.  This second deadline
            # also bounds a failed supervisor or unresponsive Docker cleanup.
            if now - started > timeout + 15:
                raise ExecutionError("trial supervisor did not finish cleanup", kind="cleanup_failed")
            time.sleep(_POLL_SECONDS)
        stdout, stderr = supervisor.communicate(timeout=1)
        if supervisor.returncode:
            detail = stderr[:4096].decode("utf-8", "replace")
            raise ExecutionError(f"trial supervisor failed: {detail}", kind="supervisor_failed")
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError) as error:
            raise ExecutionError("trial supervisor returned invalid evidence", kind="supervisor_failed") from error
        elapsed = time.monotonic() - started
        if not result.get("ok"):
            raise ExecutionError(
                result.get("error", "candidate execution failed"),
                elapsed_seconds=elapsed,
                exit_code=result.get("exit_code"),
                log_path=str(log_path),
                kind=result.get("kind", "execution_failed"),
            )
        result.pop("ok", None)
        result.update(elapsed_seconds=elapsed, execution_mode=mode, image_id=image_id)
        return result
    except ExecutionError as error:
        pending_error = error
        if not error.elapsed_seconds:
            error.elapsed_seconds = time.monotonic() - started
        if error.log_path is None and log_path is not None:
            error.log_path = str(log_path)
        raise
    except Exception as error:
        pending_error = ExecutionError(
            str(error),
            elapsed_seconds=time.monotonic() - started,
            log_path=str(log_path) if log_path is not None else None,
            kind="setup_failed",
        )
        raise pending_error from error
    finally:
        if supervisor is not None:
            if supervisor.poll() is None:
                supervisor.terminate()
                try:
                    supervisor.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    _kill_process_group(supervisor)
            if supervisor.stdin is not None:
                supervisor.stdin.close()
            if supervisor.stdout is not None:
                supervisor.stdout.close()
            if supervisor.stderr is not None:
                supervisor.stderr.close()
        if pending_error is not None:
            pending_error.elapsed_seconds = time.monotonic() - started


def _main() -> int:
    if sys.argv[1:] != ["--supervise"]:
        print("This module is an internal candidate supervisor.", file=sys.stderr)
        return 2
    try:
        spec = json.load(sys.stdin)
        result = _supervise(spec)
    except Exception as error:
        result = {"ok": False, "kind": "supervisor_failed", "error": str(error)}
    try:
        print(json.dumps(result), flush=True)
    except BrokenPipeError:
        # The original RPC process is gone.  _supervise already cleaned up.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
