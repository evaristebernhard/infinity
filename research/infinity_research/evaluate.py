"""Resource-bounded, host-owned evaluator subprocess; never imports candidate code."""

import argparse
import json
import math
from pathlib import Path
import signal


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
    try:
        import resource
    except ImportError:
        resource = None
    if resource is not None:
        memory = max(args.memory_mb, 256) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        cpu = max(1, math.ceil(args.timeout))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    from .domains import get_domain
    result = get_domain(args.domain).evaluate(args.data, args.output, split=args.split)
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
