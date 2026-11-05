"""Small real regression task for offline architecture tests, never a Kaggle score."""

import json
import math
from pathlib import Path
import random


BASELINE = '''import json, os, sys
from pathlib import Path
manifest_path, output = Path(sys.argv[1]), Path(sys.argv[2])
data = json.loads(manifest_path.read_text())
train = data["train"]
mean = sum(row["y"] for row in train) / len(train)
predictions = [{"id": row["id"], "value": mean} for row in data["predict"]]
output.mkdir(parents=True, exist_ok=True)
(output / "predictions.json").write_text(json.dumps({"predictions": predictions}))
'''

LINEAR = '''import json, sys
from pathlib import Path
manifest_path, output = Path(sys.argv[1]), Path(sys.argv[2])
data = json.loads(manifest_path.read_text())
train = data["train"]
xm = sum(r["x"] for r in train) / len(train)
ym = sum(r["y"] for r in train) / len(train)
slope = sum((r["x"]-xm)*(r["y"]-ym) for r in train) / sum((r["x"]-xm)**2 for r in train)
intercept = ym - slope*xm
predictions = [{"id": r["id"], "value": slope*r["x"]+intercept} for r in data["predict"]]
output.mkdir(parents=True, exist_ok=True)
(output / "predictions.json").write_text(json.dumps({"predictions": predictions}))
'''


def prepare(output_root: Path):
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError("Smoke data directory must be empty")
    rng = random.Random(17)
    points = []
    for i in range(36):
        x = rng.uniform(-4, 4)
        points.append({"id": str(i), "x": x, "y": 1.7 * x - 0.3 + rng.gauss(0, 0.03)})
    train, validation, holdout = points[:20], points[20:28], points[28:]
    for folder, split, rows in (("public", "validation", validation), ("public_final", "holdout", holdout)):
        (output_root / folder).mkdir(parents=True)
        (output_root / folder / "manifest.json").write_text(json.dumps({
            "train": train, "predict": [{"id": r["id"], "x": r["x"]} for r in rows],
            "split": split}))
        (output_root / "private").mkdir(exist_ok=True)
        (output_root / "private" / f"{split}_truth.json").write_text(json.dumps(rows))


def evaluate(prepared_root: Path, output_dir: Path, *, split="validation") -> dict:
    if split not in ("validation", "holdout"):
        raise ValueError("Unknown evaluation split")
    truth = json.loads((prepared_root / "private" / f"{split}_truth.json").read_text())
    raw = json.loads((output_dir / "predictions.json").read_text())
    rows = raw["predictions"]
    if not isinstance(rows, list) or len(rows) != len(truth):
        raise ValueError("Prediction coverage differs from the frozen evaluation set")
    values = {}
    for row in rows:
        key, value = row.get("id"), row.get("value")
        if key in values or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Duplicate image ID or invalid prediction")
        values[key] = value
    if set(values) != {r["id"] for r in truth}:
        raise ValueError("Prediction IDs differ from the frozen evaluation set")
    mse = sum((values[r["id"]] - r["y"])**2 for r in truth) / len(truth)
    return {"metric": "mse", "score": mse, "details": {"examples": len(truth), "fixture": True}}


def task(data_root: Path) -> dict:
    return {"schema_version": 1, "task_id": "offline-regression-smoke", "domain": "smoke",
            "description": "Fit y from x using the supplied training examples. This is an offline architecture fixture.",
            "objective": {"name": "mse", "direction": "minimize"},
            "data": {"prepared_root": str(data_root.resolve())}, "baseline_files": {"main.py": BASELINE},
            "executor": {"kind": "local", "allow_local": True, "cpus": 1, "memory_mb": 512,
                         "gpus": 0, "max_output_mb": 8},
            "budget": {"max_workers": 4, "max_model_calls": 24, "max_output_tokens": 4096,
                       "max_experiments": 4, "max_parallel_experiments": 2, "max_trial_seconds": 10,
                       "max_total_trial_seconds": 60, "max_wall_seconds": 600, "final_trial_seconds": 10},
            "search": {"families": ["baseline", "linear", "robust"], "stagnation_trials": 3, "min_delta": 1e-6}}
