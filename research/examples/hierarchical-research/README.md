# Small hierarchical campaigns

These schema-2 examples use root → child → grandchild delegation. At most three
children may ever be created by one work item, for a maximum tree of 13 logical
agents. All of them share four work slots, two model calls and one tool call.
The host enforces these limits; prompt wording alone is not the limit.

- `math-task.json` asks for small exact square constructions and their evidence.
  Its checker runs without Docker. It is not a complete Kaggle competition adapter.
- `disclosure-task.json` uses an explicitly fictional cooling-controller brief to
  demonstrate technical disclosure and support review. It has no web-search,
  patent-search, experiment or filing tool. Missing evidence stays missing.

From the repository root:

```bash
python3 -m pip install -e ./research
python3 -m infinity_research.cli init \
  --task research/examples/hierarchical-research/math-task.json \
  --campaign research/runs/hierarchical-math-001

export RESEARCH_API_KEY='your-provider-key'
export RESEARCH_API_BASE='https://your-provider.example/v1'
export RESEARCH_MODEL='your-tool-calling-model'

cargo run --release -p infinity-research -- \
  --campaign research/runs/hierarchical-math-001 \
  --research-root research \
  --provider openai-compatible \
  --api-base "$RESEARCH_API_BASE" \
  --api-key-env RESEARCH_API_KEY \
  --model "$RESEARCH_MODEL" \
  --workers 4
```

Use a new campaign directory for the disclosure example. Set `--python` when the
Python package is installed in a virtual environment. Provider-specific thinking
and token-limit requirements are described in the [native launcher guide](../../../crates/infinity-research/README.md).

`--workers 1` is valid: a parent that calls `wait_work` parks and releases its work
slot so its children can run. The original conversation resumes with a new lease
in `resume_claim`. Depth 2 agents can keep working and iterating, but cannot spawn.
Use `all_settled` to join exploratory routes even if one fails; inspect their
actual states before deciding which findings to retain.

```bash
python3 -m infinity_research.cli status --campaign research/runs/hierarchical-math-001
python3 -m infinity_research.cli report \
  --campaign research/runs/hierarchical-math-001 \
  --output /tmp/hierarchical-math-report.json
```

Restart the same launcher command to recover from persisted work and effects.
In-process parked conversations are retained; a process restart reconstructs
context from the ledger rather than recovering every previous model message.
The immutable contract cannot be edited in place to raise limits or change tools.

The examples have been checked for initialization and catalog loading. Automated
integration uses a scripted model with real Infinity threads and Python RPC; no
paid model result, patent search, mathematical discovery or multi-day reliability
result is claimed. See [validation](../../VALIDATION.md) and the
[hierarchy design](../../docs/hierarchical-agents.md).
