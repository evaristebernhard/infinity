---
name: strategy-evolution
description: Propose and evaluate improvements to reusable research strategies, tool implementations, and skill instructions. Use when repeated failures suggest changing how agents work, or when comparing a challenger procedure with an incumbent across tasks.
---

# Evolve a strategy

Identify a repeated failure or missed opportunity from actual run evidence.
Specify what behavior should change and why it should generalize. Keep a frozen
incumbent and write the challenger as a versioned artifact: a solver, a proposed
tool implementation, a task-selection procedure, or a skill package.

Use [the comparison plan](references/comparison-plan.md) to define evaluation
before running the challenger. Include varied tasks, negative cases, execution
failures, and regressions. A strategy winning on the one result that inspired it
has not yet demonstrated transferable improvement.

Run candidate code through the configured executor. Keep proposed instructions
as candidate artifacts until the host's promotion policy accepts them. Skills
can explain how to use tools; their prose and `allowed-tools` metadata do not
grant new capabilities or register executable code.

Retain the old version, provenance, evaluation receipts, and rollback reference.
If evidence is mixed, preserve both branches with their measured domains rather
than declaring a universal winner. Preserve the task definition, verifier, and
acceptance authority throughout this comparison; changing those would invalidate
the original experiment.

Return the challenger artifact, parent version, expected mechanism, evaluation
plan, actual results, and recommendation. If the runtime has no promotion tool,
leave a proposal for the host instead of claiming the strategy was activated.
