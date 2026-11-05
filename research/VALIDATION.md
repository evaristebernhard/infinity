# Development validation

Verified on 2026-11-05 with Linux, Python 3.12 and Rust 1.99.1. These results
validate the implementation paths described below; they are not a measured
Kaggle result or a long-duration production campaign.

## Checks completed

| Check | Result |
| --- | --- |
| Python backend, ledger, executor and domain tests | 56 passed, including the optional pycocotools interoperability tests and evaluator resource-limit checks |
| Native `infinity-research` tests | 14 passed |
| `cargo clippy -p infinity-research --all-targets --locked -- -D warnings` | Passed |
| `cargo fmt --all --check` | Passed |
| Native launcher `--help` | Passed |
| Standalone exact square-packing example | Six self-checks passed; exact certificates generated for n=4 and n=5 |
| `git diff --check` | Passed |
| Repository `./check.bash` | Blocked at the prerequisite check because `jj` is unavailable; the complete upstream workspace suite did not run |

Reproduce the scoped checks from the repository root after installing a current
Rust toolchain and the Python dependencies:

```bash
python -m pip install -e './research[filament]'
python -m unittest discover -s research/tests -v
cargo test -p infinity-research --locked
cargo clippy -p infinity-research --all-targets --locked -- -D warnings
cargo fmt --all --check
```

## What the tests exercise

The native concurrency test launches **100 actual Infinity worker threads** with
a mock model provider, four simulated experiment slots and a global cap of 250
model calls. All 100 admitted experiments drain, the peak experiment concurrency
is four, and provider invocations stay within the call cap. This is a scheduling
and admission test, not a test of 100 paid models or 100 GPU jobs.

Separate native integration tests use the real Python SQLite backend, actual
candidate subprocesses and independent scoring. They cover continued research,
durable budget stops, one-time final evaluation, restart without resetting the
budget, and recovery of a final queued job whose original caller disappeared.

The provider tests inspect serialized Chat Completions HTTP requests to ensure
the admitted output-token cap is present. They also exercise the total model
deadline while a stream remains active and its reduction to the remaining
search wall time.

The first full upstream CI run passed Ubuntu tests but found a missing generated
license notice and a macOS Python integration failure. The follow-up adds the
missing `infinity-research` license entry, richer integration-test diagnostics,
and an explicit Darwin policy. Only the known trusted `smoke` domain may run
there without an address-space cap, which is disclosed in evaluation evidence;
production domains fail during initialization. Linux retains its address-space
and CPU limits, including a real subprocess test that rejects an oversized
allocation. A dedicated macOS smoke job checks the deployment-specific behavior.
Do not infer macOS production support from that fixture job.

Python tests cover atomic admission, resource reservation, semantic candidate
deduplication, stale-worker fencing, failed trials, orphaned queue recovery,
private-label separation, data-contract mutation, child-process cleanup after
timeout and parent death, bounded logs and outputs, one-time holdout selection,
and frozen-program test prediction without reopening the research budget.

## Offline end-to-end example

```bash
python -m infinity_research.cli smoke --output /tmp/infinity-research-smoke
```

A clean run executes two real regression candidates, independently scores them,
reuses a repeated candidate, records the finding and evaluates the selected
program once on the sealed holdout. The observed fixture values were:

| Fixture measurement | Value |
| --- | --- |
| Constant baseline validation MSE | 17.952755646459018 |
| Fitted linear candidate validation MSE | 0.000967031584943735 |
| Selected program holdout MSE | 0.0002192978325845944 |
| Unique experiments recorded | 2 |
| Duplicate candidate reused | Yes |

No model API or competition data is involved in this example.

## Filament-specific checks

The organizer's version 6 notebook grouping and scoring functions were compared
with the local evaluator on 100 synthetic 5 x 7 images, with 189 scored annotator
passes and 10 empty passes excluded. Both produced `0.3992170194922966`, an
absolute difference of zero. The reference source hash and metric assumptions
are recorded in the [competition adapter guide](examples/filament-segmentation-2026/README.md).
The regression test preserves this reference result. COCO compressed RLE and
polygon handling were checked with pycocotools 2.0.10.

The supplied segmentation baseline also completed two actual CPU optimizer
steps and inference on six synthetic 64 x 64 images, using 32-pixel training
crops and a reduced eight-channel model. Checkpoint writing, prediction output
and independent PQ evaluation completed. This is an executable-path check;
it does not establish full-resolution model quality.

The separate [square-packing example](examples/squares-in-squares/README.md)
uses exact arithmetic in Q(sqrt(2)) to check a five-square construction of side
`2 + sqrt(2)/2` and the four-square 2-by-2 grid. Every pair has a recorded
separating-axis witness. The five-square result establishes feasibility only;
the four-square grid also attains the area lower bound. Invalid overlapping,
out-of-bounds and non-unit examples are rejected. This is not a Lean proof,
an autonomous search result or a tested Kaggle decimal submission, and the
mathematical task adapter described in the design document is not registered.

## Validation still requiring the deployment environment

Docker is unavailable in this development environment. Container arguments,
read-only mounts, GPU assignment and resource limits are covered by tests, but
the actual Docker image build and GPU execution have not been run here. The
production task requires that deployment check before a paid research run.

No real provider API request, full-resolution competition training, multi-day
campaign, Kaggle submission or leaderboard score was performed. Model-provider
wire tests use a local HTTP fixture; native campaign tests use a mock provider.
The implementation is a single-host research system, not a multi-machine
distributed scheduler.
