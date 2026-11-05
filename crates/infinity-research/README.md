# infinity-research

Run a bounded group of autonomous research workers on **Infinity's native agent
runtime**. Schema-2 campaigns use a generic work graph, a host-supplied tool catalog
and pinned skills. Schema-1 campaigns retain the Kaggle `filament-segmentation-2026`
experiment loop. The Rust runtime does not contain domain logic.

Workers propose hypotheses and complete candidate programs, run experiments,
inspect failures, and share evidence through the persistent Python backend in
[`research/`](../../research). The trusted backend owns the frozen task,
evaluation, candidate lineage, deduplication, compute queue, and SQLite budgets.

## Run an initialized campaign

Use a current Rust toolchain and Python 3.11 or later. This crate uses the standard
file-lock API introduced in Rust 1.89; the full workspace's dependencies may
require a newer compiler. Linux or WSL is the intended environment for the
Docker executor and Unix-socket model providers.

Install the domain's host dependencies once, prepare the competition data, and
build the candidate Docker image using the instructions in `research/`. Then,
from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './research[filament]'

.venv/bin/python -m infinity_research.cli init \
  --task research/examples/filament-segmentation-2026/task.json \
  --data "$PWD/data/filament-prepared" \
  --campaign "$PWD/runs/filament-01"
```

Set your provider key in the shell environment. This example reads
`DEEPSEEK_API_KEY`; keys are not part of the task, campaign files, or generated
candidate environment.

```bash
cargo run -p infinity-research -- \
  --campaign "$PWD/runs/filament-01" \
  --research-root "$PWD/research" \
  --python "$PWD/.venv/bin/python" \
  --provider openai-compatible \
  --api-base https://api.deepseek.com \
  --api-key-env DEEPSEEK_API_KEY \
  --model deepseek-flash \
  --disable-thinking \
  --workers 8 \
  --max-output-tokens 8192
```

The endpoint uses **Chat Completions**, selected explicitly through rig's
`completions_api()`. `--disable-thinking` sends
`{"thinking":{"type":"disabled"}}`. This is required for DeepSeek through the
generic rig 0.31 OpenAI adapter: that adapter does not preserve DeepSeek's
`reasoning_content` across tool turns. This path therefore supports DeepSeek's
non-thinking tool mode; it does not claim working thinking-mode replay. Current
model names and this requirement are documented in DeepSeek's
[API quick start](https://api-docs.deepseek.com/),
[thinking-mode guide](https://api-docs.deepseek.com/guides/thinking_mode/), and
[tool-call guide](https://api-docs.deepseek.com/guides/tool_calls/).

For another OpenAI-compatible endpoint, set its base URL, model ID, and key
environment variable, and omit `--disable-thinking` unless that endpoint supports
the same setting. The wire output limit defaults to `max_tokens`; endpoints that
require `max_completion_tokens` use `--output-token-field max-completion-tokens`.
The selected limit is written into the serialized HTTP request, including with
rig versions whose generic Chat Completions conversion omits `max_tokens`.

`--context-window` controls Infinity's automatic conversation compaction.
Set it to the documented capacity of your model; the default is a conservative
65,536 tokens, and zero disables compaction. A compacting child still goes
through the same durable model budget and inference semaphore.

`--research-root` is the directory containing `infinity_research/rpc.py`, not the
campaign directory. It defaults to `research` relative to the launch directory.
An explicit Python path is resolved before the subprocess changes directories;
virtual-environment symlinks are preserved.

## Use an existing Infinity model provider

The launcher also accepts Infinity's existing `RemoteModelProvider` protocol.
This mode works with any compatible provider executable; it does not require
AWS or Bedrock.

```bash
cargo run -p infinity-research -- \
  --campaign "$PWD/runs/filament-01" \
  --research-root "$PWD/research" \
  --python "$PWD/.venv/bin/python" \
  --provider remote \
  --provider-socket /absolute/path/to/provider.sock \
  --model YOUR_PROVIDER_MODEL_ID \
  --workers 8
```

Alternatively, `--provider-command-json '["/absolute/path/to/provider","arg"]'`
starts a provider. The first stdout line must be its Unix socket path, as in the
existing Infinity provider contract. Arguments are passed directly without a
shell, and stderr remains diagnostic output. A launcher-owned provider process
is terminated when the launcher exits. A pre-existing socket provider remains
owned by whoever started it. Remote providers must honor the protocol's
`CompletionRequest.max_tokens`; the launcher cannot enforce an external
provider's behavior after the request leaves the process.

## What runs autonomously

The launcher builds `AgentSystemBuilder::new_local` and calls
`without_builtin_tools()`. For schema 2, it registers the backend's bounded tool
catalog, injects native owner/request identity around `invoke_tool`, and wakes
idle workers only for ready work or their own active leases. Flat campaigns
dynamically create and wait for work inside a bounded native thread pool.
Opt-in hierarchical campaigns use one conversation per logical work item, with
a shared execution-slot limit and at most two delegation levels. The host binds
each thread to its work; agents cannot claim an ancestor's goal. A successful
`wait_work` parks the conversation and releases its lease without polling the
model. The host reclaims that same work and returns `resume_claim` when its
dependencies settle. `all_settled` permits failed research branches; the default
`all_success` still requires every dependency to succeed.

`limits.max_parallel_model_calls` can reduce the single shared provider semaphore
below `--workers`; no subtree gets an independent quota. Logical threads may
remain parked while other work uses those slots. See the
[hierarchical examples](../../research/examples/hierarchical-research/README.md)
and [design](../../research/docs/hierarchical-agents.md).

A new domain
capability does not require another Rust tool enum variant. Work completion is
not a proof verdict, and generic campaigns never invoke the holdout finalizer.
See the [generic examples](../../research/examples/generic-research/README.md).

Schema 1 keeps its four existing tools and eager experiment continuation:

| Native tool | Backend operation | Purpose |
| --- | --- | --- |
| `research_context` | `context` | Read task, budgets, leaderboard, failures, findings, and baseline |
| `run_experiment` | `experiment` | Admit, queue, execute, and independently score a complete candidate |
| `read_candidate` | `candidate` | Inspect source, metadata, evaluation, and bounded failure diagnostics |
| `record_finding` | `finding` | Persist an agent-authored observation or hypothesis with evidence IDs |

Tool execution returns asynchronously through Infinity's native message queue.
An experiment waiting for a GPU does not hold an inference permit. Tool-result
delivery wakes the originating thread. The Rust adapter generates idempotency
keys from the native group and tool-call IDs; a model cannot choose those keys.

The backend subprocess contract is deliberately small:

```text
python -m infinity_research.rpc --campaign /absolute/campaign OPERATION
```

It receives one JSON request on stdin and returns one JSON response on stdout:
`{"ok":true,"result":...}` or `{"ok":false,"error":"..."}`. Diagnostics go to
stderr. The launcher also uses host-only `recover`, `reserve_model_call`,
`record_usage`, and `finalize` operations. None are exposed as model tools.

## Limits, stopping, and restart

Schema 2 uses work-item/lease limits and a separate tool-call concurrency limit.
The host supervisor renews leases belonging to live native threads, including
while a model is reasoning. Tool processes also renew their own lease. A lost
or expired token cannot be revived. Work completion ends the workflow but does
not assert that the scientific goal is proved. Interrupted effects remain unknown
and are not automatically replayed. The schema-1 experiment details below retain
their previous meaning.

The task contract supplies the authoritative maximum workers, model calls,
experiment count, experiment concurrency, trial time, and campaign wall time.
The launcher refuses a worker count above that contract. It reserves every
model invocation in SQLite before sending it, including runtime retries and
compaction calls. An inference semaphore limits active model streams to the
requested number of workers. The smaller of the CLI cap and backend cap is sent
as the output-token limit.

Reported input/output usage is persisted separately. Missing provider usage
remains unknown. This is a durable **call budget plus per-call output cap**, not
a claim of exact prepaid dollar or aggregate-token enforcement. Unsuccessful
requests conservatively keep their reserved call slot.

Each candidate's actual execution deadline is owned by the backend. The outer
experiment RPC timeout allows queue waiting until the remaining campaign wall
budget plus cleanup grace; it is not limited to the candidate's execution time.
Model requests and inactive streams have a separate timeout, controlled by
`--response-timeout-seconds`. A whole model call, including a continuously active
stream, is also bounded by `--model-call-timeout-seconds` (default 900 seconds).
The Python admission response further clamps that deadline to the remaining
search wall time, preserving the separately reserved final-evaluation window.
Cancelled calls retain their reservation; usage not reported before cancellation
stays unknown.

On a normal budget stop, new model calls stop and admitted tools drain. The host
also checks the durable queue and recovers any last orphaned admission whose
backend process exited before taking ownership, including with `--no-finalize`.
The CLI
then evaluates the best verified candidate once on the sealed holdout, if a
candidate exists and final evaluation has never started. Use `--no-finalize` to
leave that step for an explicit later host command. The model cannot request
holdout evaluations. A failed or interrupted final run is never retried
automatically.

Ctrl-C closes model admission and drains admitted tools; it does not trigger
automatic holdout evaluation. An explicit owner stop or infrastructure error
also does not trigger that evaluation. Graceful stopping can therefore take
as long as the remaining admitted work. Fatal provider and persistence errors
produce a nonzero exit, rather than a successful research report.

Only one native launcher may hold a campaign at a time. The launcher keeps an
operating-system lock on `.launcher.lock`; a crash releases the lock even if the
file remains. SQLite remains authoritative for admission across processes.

Restart with the same command and campaign directory. Before starting models,
the launcher calls `recover`: queued experiments can continue, existing running
experiments reach their terminal state or hard deadline, and completed work is
reused. `--recovery-timeout-seconds` bounds this recovery wait and defaults to
24 hours. The launcher then creates **fresh conversations** from the ledger's
candidate history, findings, and remaining budgets. It does not restore lost
in-memory conversations or claim full distributed durable execution.

This implementation is one machine and one shared campaign directory, with
bounded local model workers and a backend-controlled compute queue. Increasing
workers does not create GPU capacity or guarantee a better solution. It does
not upload a Kaggle submission automatically.

## Verify without an API key

```bash
cargo test -p infinity-research --all-targets
cargo clippy -p infinity-research --all-targets -- -D warnings
cargo run -p infinity-research -- --help
```

The tests exercise native concurrent workers, asynchronous result delivery,
automatic continuation, atomic model admission, fatal provider failures,
operating-system locks, subprocess protocol handling, queue-wait deadlines, and
total deadlines for continuously active model streams.
One test runs 100 native workers against a mock provider, with four concurrent
experiment slots and a 250-call cap, and checks that all 100 admitted experiments
drain without exceeding those limits.
A local HTTP fixture checks the actual rig Chat Completions path and serialized
output cap. The cross-language campaign test uses the real SQLite ledger,
candidate subprocesses, and independent evaluator: it recovers a queued
baseline, fits a line through a deterministic mock model, records evidence,
drains an orphan queued at budget stop, performs one holdout evaluation, and
verifies that a restart cannot reset the
model budget. These are architecture tests on a tiny regression fixture, not a
real LLM run or a Kaggle performance claim.

A separate generic integration uses 100 native threads and the real Python work
graph, pinned skills, exact n=4/n=5 checks, artifact reading and dependency joins.
The model dynamically creates two child work items and waits for them; the root
is then reclaimed without treating waiting as failure. Fewer than 60 model calls
are admitted for the entire scripted flow; a completed restart makes no calls.
Only the model decisions are scripted, not the tools or ledger.
