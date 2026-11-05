---
name: formal-proof
description: Develop machine-checkable proofs while preserving the intended theorem and assumptions. Use when a task exposes a formal proof toolchain, needs lemma decomposition, or requires auditing a claimed formal proof.
---

# Develop a formal proof

Read the frozen target, definitions, imports, permitted axioms, prover version,
and available checking tools. If no prover is available, produce a proof plan or
candidate source and label it unverified; never claim a tool ran from memory.

Separate alternative proof routes from the lemmas required within each route.
Retain useful checked lemmas after a route stalls. State each lemma's assumptions
explicitly and include the final derivation from lemmas to the original target.
A collection of true lemmas does not by itself prove the goal.

Use small checker iterations. Read the actual proof state and error output;
change one failed step or isolate a missing lemma. Search for counterexamples
when a conjectured lemma appears false instead of weakening the target silently.

Before accepting the result, compare the exported declaration with the fixed
target and inspect its full axiom dependencies. Reject a result that introduces
unapproved axioms, leaves proof holes, or changes definitions to make the goal
trivial. A successful build alone is not the complete acceptance check.

Return source artifacts, toolchain fingerprints, the target declaration,
dependencies, and checker receipts. Preserve the separate question of whether
the formal statement faithfully expresses the original natural-language goal.
