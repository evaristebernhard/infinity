"""Single-request JSON subprocess protocol used by the native Infinity launcher."""

import argparse
import json
from pathlib import Path
import sys

from .spec import canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("operation")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Research request exceeds 1 MiB")
        body = json.loads(raw or b"{}")
        from .core import dispatch
        from .store import ResearchStore
        result = dispatch(ResearchStore(args.campaign), args.operation, body)
        response = {"ok": True, "result": result}
    except Exception as error:
        response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    print(canonical(response), flush=True)


if __name__ == "__main__":
    main()
