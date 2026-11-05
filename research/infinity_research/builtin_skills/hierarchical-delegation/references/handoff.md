# A useful delegation brief

This is an example shape for the `brief` object, not a new permission schema:

```json
{
  "context": "Compare the proposed search operator with the existing baseline.",
  "input_work_ids": ["actual-existing-work-id"],
  "input_effect_ids": ["actual-existing-effect-id"],
  "deliverable": "A comparison table, reproducible program and failure analysis.",
  "acceptance_criteria": [
    "Use the same fixed benchmark inputs for both versions.",
    "Record every attempted run, including failures.",
    "Separate measured improvement from a conjectured explanation."
  ],
  "scope": "Change the search operator; retain the supplied evaluator."
}
```

Replace illustrative IDs with IDs that actually exist. Read the referenced
records before relying on them. For a proof task, identify the exact statement,
assumptions and allowed dependencies. For an engineering document, identify the
source disclosures and distinguish measured, simulated and proposed effects.

On return, link to durable artifacts instead of repeating entire source files.
Report `unsupported`, `refuted`, `incomplete` or another justified status when
appropriate. These are descriptive agent claims; authoritative tool verdicts
remain in the original effect records.
