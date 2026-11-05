# Solar Filament Segmentation 2026 research task

This is a concrete instance-segmentation task for the autonomous research
controller. It includes data preparation, an independently computed local PQ
score, a small trainable candidate, a final holdout view and a CSV exporter.
It has no measured competition score yet. The baseline is deliberately simple:
a small U-Net trained from scratch, followed by connected components.

## 1. Install and prepare the official competition data

Run these commands from the Infinity repository root:

```bash
python -m pip install -e 'research[filament]'

infinity-research-task prepare-filament \
  --images /absolute/path/MAGFiLO_1.0_Kaggle_2026/train/train_images \
  --annotations /absolute/path/MAGFiLO_1.0_Kaggle_2026/train/MAGFiLO_1.0_Annotations_kaggle2026_train.json \
  --test-images /absolute/path/MAGFiLO_1.0_Kaggle_2026/test/test_images \
  --output /absolute/path/filament-prepared \
  --seed 42 --validation-fraction 0.2 --holdout-fraction 0.1
```

Supply the actual paths if the archive layout differs. The command does not
download data or accept competition rules. Use only the training annotations
distributed with the competition. The separately published full MAGFiLO release
can contain labels for the competition test observations and is not an input to
this task.

Preparation groups all annotator passes of a physical observation together.
Entire observation dates stay together; byte-identical images on different
dates also join the same group. Fractions apply to these groups, so image counts
may differ slightly from the requested ratios. At least three independent
groups are required. A missing or invalid date in an observation filename is an
error, not a reason to fall back to a random per-row split.

Prepared views are:

| Directory | Contents | Candidate access |
| --- | --- | --- |
| `public` | Training images/annotations, validation images, optional unlabelled test images | Search experiments only |
| `public_final` | The same training data, with holdout images as the prediction target | Final frozen candidate only |
| `public_test` | The same training data, with test images as the prediction target | Export inference only |
| `private` | Validation/holdout truth, split metadata and content hashes | Host evaluator only |

The final run uses exactly the same training partition. It reruns the frozen
candidate with its fixed seed; it does not claim to reuse a training checkpoint.
The campaign store enforces the one-time final evaluation. Calling the domain's
Python evaluator by itself is not a substitute for that campaign policy.

Every input image is checked against its recorded dimensions. Real competition
images are 2048 x 2048; smaller images are supported for local synthetic tests,
but the Kaggle CSV exporter rejects non-2048 output.

## 2. Build the candidate environment and initialize

Use a Linux/WSL host with Docker. The default GPU configuration also needs an
NVIDIA driver and the NVIDIA Container Toolkit on the host. The image installs
PyTorch, NumPy, Pillow, SciPy and pycocotools; no pretrained weights or packages
are downloaded during candidate execution.

```bash
docker build -t infinity-research-filament:local \
  research/examples/filament-segmentation-2026

infinity-research-task init \
  --task research/examples/filament-segmentation-2026/task.json \
  --data /absolute/path/filament-prepared \
  --campaign /absolute/path/filament-campaign
```

The example reserves one GPU and permits one training experiment at a time,
while up to eight research workers can propose and inspect work. For CPU-only
execution, set `executor.gpus` to `0` and `executor.gpu_devices` to `[]` before
initializing. Keep Docker execution for generated candidates. The local executor
is intended for explicitly trusted fixtures. Initialization resolves the image
tag to a local image ID and saves that ID with the campaign.

The task budget allows 24 experiments, up to one hour each, with a total
24-hour experiment budget and a two-day campaign wall-time limit. One final
hour is reserved inside the total budget. These are initial configuration
values, not a claim that the baseline needs or benefits from that much compute.

See the [research launcher instructions](../../README.md) to start Infinity's
agent workers against the initialized campaign.

## 3. Candidate interface

The executor invokes the supplied source as:

```bash
python /candidate/main.py /input/manifest.json /output
```

It mounts one public input view read-only, the candidate source read-only, and
an output directory. `private` is never mounted. The candidate's random seed is
provided as `RESEARCH_SEED`.

Manifest paths are relative to the manifest's directory:

```json
{
  "schema_version": 1,
  "domain": "filament",
  "train": {
    "annotations": "train/annotations.json",
    "images": [
      {"image_id": "20260101120000Mh", "path": "train/images/20260101120000Mh.jpeg", "height": 2048, "width": 2048}
    ]
  },
  "predict": {
    "split": "validation",
    "images": [
      {"image_id": "20260102120000Mh", "path": "validation/images/20260102120000Mh.jpeg", "height": 2048, "width": 2048}
    ]
  }
}
```

The candidate must write `output/predictions.json`:

```json
{
  "schema_version": 1,
  "processed_images": ["20260102120000Mh"],
  "predictions": [
    {"image_id": "20260102120000Mh", "instances": []}
  ]
}
```

Every requested image has exactly one prediction record. `instances` may be
empty. Nonempty entries contain COCO RLE objects:

```json
{"size": [2048, 2048], "counts": [0, 1, 4194303]}
```

Compressed COCO count strings are also accepted. RLE uses column-major pixel
order. The host checks exact image coverage, dimensions, run lengths and JSON
validity, then computes the score from its own labels. A score included in the
candidate's output has no authority.

The baseline's settings are ordinary constants near the top of `baseline.py`.
Research workers can change the model, loss, sampling, augmentation and
instance reconstruction. Its training loss is only a diagnostic; candidate
selection uses host-computed full-image PQ. The initial 200-step scratch model
is a runnable starting point, not a tuned segmentation solution.

## 4. Metric and evidence

The reference is the organizer's [Self Evaluation Notebook](https://www.kaggle.com/code/azimahmadzadeh/self-evaluation-notebook),
version 6. The UTF-8 notebook source inspected during development had SHA-256:

```text
ae7c9445b4eee2b77b216df5c9772fdf92fda15611e98abd7ef5f02a4e380801
```

The local `solar_pq_v6` implementation follows its hit-matrix scoring:

* A pair is a hit only when IoU is strictly greater than 0.5.
* All hit pairs contribute TP and IoU; this is not Hungarian matching.
* A prediction with no hit is FP; a GT instance with no hit is FN.
* Counts are pooled across independent annotator-image passes.
* PQ is the sum of hit IoUs divided by `TP + 0.5 FP + 0.5 FN`.
* A zero denominator returns 0.0.

Each COCO image record is retained as an annotator pass. The overall score
uses only passes containing GT instances, matching the notebook's grouping of
GT CSV rows. Empty passes remain available for training and diagnostics;
`details.unscored_empty_passes` records their image IDs, view IDs and prediction
counts without adding them to PQ. Synthetic parity tests do not establish that
all private Kaggle submission checks are identical.

The evaluator computes mask intersections from RLE intervals without loading
PyTorch or building full image tensors. Its result includes counts per annotator
pass, the prediction hash, dataset/split fingerprints and the reference source
hash. No public MAGFiLO test labels or leaderboard feedback participate in this
local research score.

The regression tests include a pinned reference result for 100 synthetic
5 x 7 images: the official notebook's grouping and scoring functions and the
local evaluator both produced `0.3992170194922966`, with 189 scored annotator
passes and 10 empty passes excluded (absolute error `0.0`). Intersections and
areas are integer counts; each IoU is rounded to float32 before the Python
sum, matching the notebook's float32 tensors followed by `.tolist()`.
This number is only a metric fixture,
not a segmentation-model or competition result. COCO compressed RLE encoding
is also checked against pycocotools on asymmetric masks.

Run this task's tests after installing the optional filament dependencies:

```bash
PYTHONPATH=research python -m unittest discover -s research/tests -p test_filament.py -v
```

Development verification also ran the actual baseline on CPU with six synthetic
64 x 64 images, two optimizer steps, 32-pixel training crops and a reduced
8-channel model. Checkpoint writing, prediction generation and independent PQ
evaluation completed. This checks the executable path; full-resolution training,
the CUDA Docker environment and leaderboard performance still require a real
competition run.

## 5. Final holdout and submission export

The native launcher normally runs the one-time holdout evaluation when research
finishes. If it was started with `--no-finalize`, wait for active experiments to
finish, then run it explicitly:

```bash
infinity-research-task finalize --campaign /absolute/path/filament-campaign
```

Do not repeat this command after a final evaluation has already started, even
if that evaluation failed. Inspect the recorded result with:

```bash
infinity-research-task report --campaign /absolute/path/filament-campaign
```

After a successful final evaluation, generate test predictions from its frozen
candidate. Preparation must have included `--test-images` so that `public_test`
exists. Choose a new output directory:

```bash
infinity-research-task predict-test \
  --campaign /absolute/path/filament-campaign \
  --output /absolute/path/test-output \
  --timeout-seconds 3600
```

The command uses the candidate ID recorded in the successful final evaluation,
its frozen source, original seed and pinned Docker image. Only the `public_test`
input view is mounted. It reruns the complete candidate program, including
training, before inference; it does not reuse a saved checkpoint. This is an
explicit extra run after research, with a separate timeout (3600 seconds by
default), outside the closed campaign's research budget.

The new directory contains `predictions.json`. Host execution evidence is saved
beside it as `/absolute/path/test-output.test_execution.json`, outside the
candidate's writable mount. The frozen source is materialized in
`/absolute/path/test-output.source`. All three paths must be new and outside
the sealed dataset and campaign. Keep these files together when recording the
submission's origin. Then validate coverage and export the CSV:

```bash
infinity-research-task export-filament-submission \
  --data /absolute/path/filament-prepared \
  --predictions-dir /absolute/path/test-output \
  --output /absolute/path/submission.csv
```

The exporter requires exact test-image coverage and 2048 x 2048 masks, writes
`filament_id,segmentation_rle`, and refuses to overwrite an existing file. A
processed image with no detections contributes no CSV rows. Empty masks must
not be inserted as placeholder filaments. Nothing is uploaded automatically.

## Sources

* [Competition overview and PQ update](https://www.kaggle.com/competitions/filament-segmentation-2026/overview)
* [Official self-evaluation notebook](https://www.kaggle.com/code/azimahmadzadeh/self-evaluation-notebook)
* [Host clarification on external MAGFiLO labels](https://www.kaggle.com/competitions/filament-segmentation-2026/discussion/729396)
* [Competition rules](https://www.kaggle.com/competitions/filament-segmentation-2026/rules)
