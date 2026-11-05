"""Exercise the real work graph and tools with a deterministic, trusted policy.

This is an integration fixture, not a model run or an autonomous discovery.
"""

from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import sys
import uuid

# Keep the documented direct invocation usable from an uninstalled checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from infinity_research.generic_api import initialize, invoke  # noqa: E402


GRID_PROGRAM = """import json, pathlib, sys
request = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert request['n'] == 4
q = lambda value: [str(value), '0']
squares = [{'name': f'{x}-{y}', 'x': q(x), 'y': q(y),
            'cos': q(1), 'sin': q(0)}
           for x in ('1/2', '3/2') for y in ('1/2', '3/2')]
pathlib.Path(sys.argv[2], 'result.json').write_text(json.dumps(
    {'n': 4, 'side': q(2), 'squares': squares}))
"""


def main(output: Path):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Demo output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(__file__).with_name("task.json").read_text())
    spec["task_id"] = "trusted-generic-integration-fixture"
    spec["capabilities"].append("run_python")
    spec["capability_config"]["run_python"] = {
        "executor": {"kind": "local", "cpus": 1, "memory_mb": 256, "max_output_mb": 2},
        "max_timeout_seconds": 10,
    }
    spec["limits"].update(max_workers=2, max_parallel_tools=2)
    path = output / "fixture-task.json"
    path.write_text(json.dumps(spec, indent=2) + "\n")
    store = initialize(path, output / "campaign", allow_local=True)

    def call(owner, tool, arguments=None):
        return invoke(store, {"owner": owner, "tool": tool, "arguments": arguments or {},
                              "request_key": "fixture:" + uuid.uuid4().hex})

    def lease_arguments(claim):
        return {"work_id": claim["work_id"], "lease_token": claim["lease_token"]}

    planner = call("planner", "claim_work", {"work_id": "root"})
    call("planner", "read_skill", {"name": "research-decomposition"})
    children = [call("planner", "create_work", {
        "goal": f"Produce and exactly check the supplied n={n} construction",
        "parent_id": "root", "skill_names": ["geometric-packing"],
    })["work_id"] for n in (4, 5)]
    call("planner", "wait_work", {**lease_arguments(planner), "depends_on": children})

    def solve(item):
        n, work_id = item
        owner = f"worker-{n}"
        claim = call(owner, "claim_work", {"work_id": work_id})
        lease = lease_arguments(claim)
        call(owner, "read_skill", {"name": "geometric-packing"})
        effects = []
        if n == 4:
            execution = call(owner, "run_python", {**lease, "arguments": {
                "files": {"main.py": GRID_PROGRAM}, "input": {"n": 4},
                "timeout_seconds": 10, "seed": 0,
            }})
            if execution["state"] != "succeeded" or execution["result"]["status"] != "completed":
                raise RuntimeError(f"Python fixture failed: {execution}")
            effects.append(execution["id"])
            layout = execution["result"]["result_json"]
        else:
            corners = [("SW", ["1/2", "0"], ["1/2", "0"]),
                       ("SE", ["3/2", "1/2"], ["1/2", "0"]),
                       ("NW", ["1/2", "0"], ["3/2", "1/2"]),
                       ("NE", ["3/2", "1/2"], ["3/2", "1/2"])]
            squares = [{"name": name, "x": x, "y": y, "cos": ["1", "0"], "sin": ["0", "0"]}
                       for name, x, y in corners]
            squares.append({"name": "center", "x": ["1", "1/4"], "y": ["1", "1/4"],
                            "cos": ["0", "1/2"], "sin": ["0", "1/2"]})
            layout = {"n": 5, "side": ["2", "1/2"], "squares": squares}
        checked = call(owner, "verify_square_packing", {**lease, "arguments": layout})
        if checked["state"] != "succeeded" or checked["result"]["evidence"]["judgement"] != "verified":
            raise RuntimeError(f"Exact verification failed: {checked}")
        effects.append(checked["id"])
        call(owner, "complete_work", {**lease, "effect_ids": effects, "result": {
            "n": n, "evidence": checked["result"]["evidence"],
            "interpretation": "feasible construction; optimality only if the area bound is closed",
        }})
        return {"n": n, "effect_id": checked["id"], "evidence": checked["result"]["evidence"]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(solve, zip((4, 5), children)))
    joined = call("joiner", "claim_work", {"work_id": "root"})
    for work_id in children:
        call("joiner", "read_work", {"work_id": work_id})
    call("joiner", "complete_work", {**lease_arguments(joined),
        "effect_ids": [row["effect_id"] for row in results],
        "result": {"checked_constructions": results, "used_real_model": False}})
    report = store.report()
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    if report["stop_reason"] != "work_graph_completed":
        raise RuntimeError("Fixture graph failed to complete")
    return {"fixture": True, "used_real_model": False, "campaign": str(store.root),
            "work_counts": report["work_counts"], "effects": len(report["effects"]),
            "checks": results, "report": str(report_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(main(parser.parse_args().output), indent=2))
