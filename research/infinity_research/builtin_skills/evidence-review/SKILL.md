---
name: evidence-review
description: Audit what a result actually establishes and separate verified evidence from hypotheses. Use when integrating agent outputs, accepting a solution, reviewing a claimed improvement, or resolving conflicting results.
---

# Review evidence

Read the actual artifact and checker receipt, not just the producing agent's
summary. Match the receipt's subject hash, task contract, checker version, and
scope to the claim being made. If they do not match, leave the claim unresolved.

Use the distinctions in [result-types.md](references/result-types.md) when a
result could be mistaken for a stronger conclusion.

Check that dependencies support the final claim under the same assumptions.
Keep conditional statements conditional. Look for changed targets, excluded
failures, leaked labels, omitted cases, precision loss, and circular reasoning.
An independently reproduced artifact or checker run is stronger evidence than
another agent agreeing with the same explanation.

Return: the exact claim, evidence IDs and hashes, accepted scope, remaining
assumptions, contradictions, and the next check. Use the runtime's evidence
types; do not write a self-issued verified flag into a narrative artifact.
If a required checker is absent, preserve the useful candidate and state that
verification is pending. Do not treat tool unavailability as refutation.
