"""Host CLI: prepare a task, initialize a campaign, inspect evidence and finalize."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

from .spec import canonical, file_hash, load_task, tree_hash
from .store import ResearchStore


def smoke(output: Path) -> dict:
    from .core import experiment, finalize
    from .domains import smoke as domain
    if output.exists() and any(output.iterdir()):
        raise ValueError("Smoke output directory must be new or empty")
    data = output / "data"
    domain.prepare(data)
    spec = domain.task(data)
    spec["data_sha256"] = tree_hash(data)
    spec["evaluator_sha256"] = file_hash(Path(domain.__file__))
    store = ResearchStore.create(output / "campaign", spec)
    attempts = [
        {"idempotency_key": "smoke-baseline", "hypothesis": "A constant mean is the initial baseline",
         "family": "baseline", "files": {"main.py": domain.BASELINE}, "seed": 42, "timeout_seconds": 10},
        {"idempotency_key": "smoke-linear", "hypothesis": "A fitted linear model should improve MSE",
         "family": "linear", "files": {"main.py": domain.LINEAR}, "seed": 42, "timeout_seconds": 10},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: experiment(store, args), attempts))
    if any(row["state"] != "succeeded" for row in results):
        raise RuntimeError(f"Offline subprocess smoke failed: {canonical(results)}")
    duplicate = experiment(store, {**attempts[1], "idempotency_key": "same-code-new-call"})
    store.finding({"idempotency_key": "smoke-note", "kind": "method",
                   "text": "Fitting a line improved held-out validation MSE over the constant baseline.",
                   "evidence_ids": [r["id"] for r in results]})
    final = finalize(store)
    report = store.report()
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return {"fixture": True, "used_real_llm": False, "campaign": str(store.root),
            "validation_scores": {r["family"]: r["score"] for r in results},
            "duplicate_reused": duplicate["duplicate"], "experiments_recorded": report["budget"]["experiments_used"],
            "final_mse": final["score"], "report": str(output / "report.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-filament", help="Split official training annotations by observation date")
    prepare.add_argument("--images", required=True, type=Path)
    prepare.add_argument("--annotations", required=True, type=Path)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--test-images", type=Path)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--validation-fraction", type=float, default=0.2)
    prepare.add_argument("--holdout-fraction", type=float, default=0.1)
    init = commands.add_parser("init")
    init.add_argument("--task", required=True, type=Path)
    init.add_argument("--campaign", required=True, type=Path)
    init.add_argument("--data", type=Path)
    init.add_argument("--allow-local", action="store_true", help="Explicitly allow unsandboxed trusted fixture execution")
    init.add_argument("--skills-root", action="append", type=Path, default=[],
                      help="Reviewed local skill root to snapshot for a schema-2 campaign; repeatable")
    for name in ("status", "recover", "stop", "finalize", "report"):
        command = commands.add_parser(name)
        command.add_argument("--campaign", required=True, type=Path)
        if name == "report":
            command.add_argument("--output", type=Path)
    demo = commands.add_parser("smoke", help="Run actual CPU subprocess experiments, without a model or Kaggle data")
    demo.add_argument("--output", required=True, type=Path)
    predict = commands.add_parser("predict-test", help="Run the frozen final program on unlabelled test inputs")
    predict.add_argument("--campaign", required=True, type=Path)
    predict.add_argument("--output", required=True, type=Path)
    predict.add_argument("--timeout-seconds", type=float, default=3600)
    export = commands.add_parser("export-filament-submission", help="Validate previously produced test predictions and write CSV")
    export.add_argument("--data", required=True, type=Path)
    export.add_argument("--predictions-dir", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare-filament":
            from .domains.filament import prepare_dataset
            result = prepare_dataset(args.images, args.annotations, args.output, seed=args.seed,
                                     validation_fraction=args.validation_fraction,
                                     holdout_fraction=args.holdout_fraction, test_images=args.test_images)
        elif args.command == "init":
            version = json.loads(args.task.read_text()).get("schema_version")
            if version == 2:
                if args.data is not None:
                    raise ValueError("Generic campaigns receive inputs through capabilities, not --data")
                from .generic_api import initialize
                store = initialize(args.task, args.campaign, skill_roots=args.skills_root,
                                   allow_local=args.allow_local)
            else:
                if args.skills_root:
                    raise ValueError("--skills-root requires a schema-2 generic campaign")
                spec = load_task(args.task, data_override=args.data, allow_local=args.allow_local)
                store = ResearchStore.create(args.campaign, spec)
            result = {"campaign": str(store.root), "task_id": store.spec["task_id"], "ready": True}
        elif args.command == "smoke":
            result = smoke(args.output.resolve())
        elif args.command == "export-filament-submission":
            from .domains.filament import export_submission
            result = export_submission(args.data, args.predictions_dir, args.output)
        else:
            from .generic_api import campaign_version
            if campaign_version(args.campaign) == 2:
                from .generic_api import context
                from .kernel import GenericStore
                store = GenericStore(args.campaign)
                if args.command == "status":
                    result = context(store)
                elif args.command == "recover":
                    result = store.recover()
                elif args.command == "stop":
                    store.stop()
                    result = {"stopped": True, "note": "New work stops; admitted tools drain"}
                elif args.command == "report":
                    result = store.report()
                    if args.output:
                        args.output.write_text(json.dumps(result, indent=2) + "\n")
                else:
                    raise ValueError("Generic campaigns use capability evidence and work results; this command is for schema-1 competitions")
                print(json.dumps(result, indent=2, allow_nan=False))
                return 0
            store = ResearchStore(args.campaign)
            if args.command == "status":
                result = store.context()
                result.pop("baseline_files", None)
            elif args.command == "recover":
                from .core import recover
                result = recover(store)
            elif args.command == "stop":
                store.stop()
                result = {"stopped": True, "note": "Running trials drain normally; queued trials are cancelled"}
            elif args.command == "finalize":
                from .core import finalize
                result = finalize(store)
            elif args.command == "predict-test":
                from .core import predict_test
                result = predict_test(store, args.output, timeout_seconds=args.timeout_seconds)
            else:
                result = store.report()
                if args.output:
                    args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2, allow_nan=False))
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
