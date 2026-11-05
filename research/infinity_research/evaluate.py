"""Resource-bounded, host-owned evaluator subprocess; never imports candidate code."""

import argparse
import json
import math
from pathlib import Path
import signal
import sys


def validate_evaluation_host(domain: str) -> None:
    if sys.platform == "darwin" and domain != "smoke":
        raise ValueError(
            "Production evaluation requires Linux/WSL; macOS supports only the trusted smoke fixture"
        )


def _apply_resource_limits(domain: str, memory_mb: int, timeout: float) -> dict:
    validate_evaluation_host(domain)
    import resource

    memory = max(memory_mb, 256) * 1024 * 1024
    cpu = max(1, math.ceil(timeout))
    limits = {"memory_enforced": True, "address_space_limit_bytes": memory,
              "cpu_soft_limit_seconds": cpu, "cpu_hard_limit_seconds": cpu + 1}
    if sys.platform == "darwin":
        # XNU rejects RLIMIT_AS below the process's existing virtual mappings,
        # which can already exceed this cap on arm64. RLIMIT_RSS is its alias.
        # Only the explicitly trusted offline fixture may omit the memory cap;
        # production evaluation fails above instead of weakening its contract.
        limits.update(
            memory_enforced=False, address_space_limit_bytes=None,
            reason="Trusted Darwin smoke fixture; address-space limit is not enforced",
        )
    else:
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    return limits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("validation", "holdout"), required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    if hasattr(signal, "SIGALRM"):
        signal.alarm(max(1, math.ceil(args.timeout)))
    limits = _apply_resource_limits(args.domain, args.memory_mb, args.timeout)
    from .domains import get_domain
    result = get_domain(args.domain).evaluate(args.data, args.output, split=args.split)
    result["resource_limits"] = limits
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
